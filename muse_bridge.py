#!/usr/bin/env python3
"""
muse_bridge.py -- Bridge between Meta's Muse AI web app (muse.ai) and local coding agents (Claude Code, etc.).

Features:
  - Automated login & session saving (storage_state.json)
  - Automatic selector and chat URL discovery (no manual DevTools inspection needed)
  - Fast background daemon (serve) maintaining a warm browser session
  - Instant CLI send / read commands (connects to daemon if running, or runs one-shot headless)
  - Native MCP Server mode (for Claude Code / Cursor / Windsurf tool integration)
  - OpenAI-compatible chat completion endpoint (/v1/chat/completions)
  - Diagnostic inspect tool with screenshot & DOM snapshot
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from playwright.sync_api import (
        sync_playwright,
        Browser,
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeoutError,
    )
except ImportError:
    sys.exit(
        "playwright is not installed. "
        "Run: pip install playwright && playwright install chromium"
    )

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "storage_state.json"
CONFIG_FILE = BASE_DIR / "bridge_config.json"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 600
POLL_INTERVAL_S = 0.5
SETTLE_POLLS = 4

DEFAULT_CONFIG: Dict[str, Any] = {
    "chat_url": "",
    "port": DEFAULT_PORT,
    "headless": True,
    "timeout": DEFAULT_TIMEOUT,
    "proxy": "",  # e.g. "http://127.0.0.1:1080" or "socks5://127.0.0.1:1080"
    "cdp_url": "",  # e.g. "http://127.0.0.1:9222" to attach to your real browser
    "geolocation": {
        "latitude": 40.7128,
        "longitude": -74.0060,
    },
    "locale": "en-US",
    "timezone_id": "America/New_York",
    "selectors": {
        "composer": "",
        "send_button": "",
        "message_list": "",
        "message": "",
    },
}


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    cfg.update(loaded)
                    if "selectors" in loaded and isinstance(loaded["selectors"], dict):
                        cfg["selectors"] = {**DEFAULT_CONFIG["selectors"], **loaded["selectors"]}
        except Exception as e:
            print(f"Warning: could not read {CONFIG_FILE.name}: {e}", file=sys.stderr)
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Warning: failed to save {CONFIG_FILE.name}: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# DOM Auto-Discovery Engine
# ---------------------------------------------------------------------------

AUTO_DETECT_JS = """
(() => {
    function isVisible(el) {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }

    function getCssPath(el) {
        if (!(el instanceof Element)) return '';
        if (el.id) return '#' + CSS.escape(el.id);
        if (el.getAttribute('data-testid')) return `[data-testid="${CSS.escape(el.getAttribute('data-testid'))}"]`;
        if (el.getAttribute('aria-label')) return `${el.tagName.toLowerCase()}[aria-label="${CSS.escape(el.getAttribute('aria-label'))}"]`;
        
        let path = [];
        while (el && el.nodeType === Node.ELEMENT_NODE && el.tagName.toLowerCase() !== 'body' && el.tagName.toLowerCase() !== 'html') {
            let selector = el.tagName.toLowerCase();
            if (el.className && typeof el.className === 'string') {
                const classes = el.className.trim().split(/\\s+/).filter(c => !c.startsWith('!') && !c.includes(':') && !c.includes('[') && c.length < 30);
                if (classes.length > 0) {
                    selector += '.' + CSS.escape(classes[0]);
                }
            }
            let sib = el, nth = 1;
            while (sib = sib.previousElementSibling) {
                if (sib.tagName.toLowerCase() === el.tagName.toLowerCase()) nth++;
            }
            if (nth > 1) selector += `:nth-of-type(${nth})`;
            path.unshift(selector);
            el = el.parentElement;
            if (path.length > 4) break;
        }
        return path.join(' > ');
    }

    // 1. Find Composer
    const composerCandidates = [
        'textarea',
        'div[contenteditable="true"]',
        '[role="textbox"]',
        'input[type="text"]'
    ];
    let composerEl = null;
    let composerSel = '';
    for (const sel of composerCandidates) {
        const els = Array.from(document.querySelectorAll(sel)).filter(isVisible);
        if (els.length > 0) {
            // Sort by lowest on the screen (chat composers are at bottom)
            els.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
            composerEl = els[0];
            if (composerEl.tagName.toLowerCase() === 'textarea') {
                composerSel = 'textarea';
            } else if (composerEl.getAttribute('contenteditable') === 'true') {
                composerSel = 'div[contenteditable="true"]';
            } else if (composerEl.getAttribute('role') === 'textbox') {
                composerSel = '[role="textbox"]';
            } else {
                composerSel = getCssPath(composerEl);
            }
            break;
        }
    }

    // 2. Find Send Button
    let sendBtnEl = null;
    let sendBtnSel = '';
    if (composerEl) {
        const compRect = composerEl.getBoundingClientRect();
        // Look in composer container or adjacent elements
        const container = composerEl.closest('form, div:has(button)') || document.body;
        const btns = Array.from(container.querySelectorAll('button, [role="button"]')).filter(isVisible);
        for (const b of btns) {
            const label = (b.getAttribute('aria-label') || b.innerText || '').toLowerCase();
            const rect = b.getBoundingClientRect();
            // button near the composer
            const isNear = Math.abs(rect.top - compRect.top) < 150;
            if (label.includes('send') || label.includes('submit') || b.type === 'submit' || b.querySelector('svg')) {
                if (isNear) {
                    sendBtnEl = b;
                    if (b.getAttribute('aria-label')) {
                        sendBtnSel = `button[aria-label="${b.getAttribute('aria-label')}"]`;
                    } else if (b.type === 'submit') {
                        sendBtnSel = 'button[type="submit"]';
                    } else {
                        sendBtnSel = getCssPath(b);
                    }
                    break;
                }
            }
        }
    }

    // 3. Find Messages & Message List
    let messageSel = '';
    let messageListSel = '';
    
    // Check common message selectors
    const msgPatterns = [
        '[data-role="assistant"], [data-role="user"]',
        '[data-testid*="message" i]',
        'div[class*="message-bubble" i]',
        'div[class*="ChatMessage" i]',
        'div[class*="message" i]',
        'article',
        '[role="article"]'
    ];
    for (const pat of msgPatterns) {
        const matches = Array.from(document.querySelectorAll(pat)).filter(isVisible);
        if (matches.length >= 1) {
            messageSel = pat;
            const parent = matches[0].parentElement;
            if (parent) {
                messageListSel = getCssPath(parent) || '[role="log"], main';
            }
            break;
        }
    }

    return {
        url: window.location.href,
        composer: composerSel || 'textarea, div[contenteditable="true"], [role="textbox"]',
        send_button: sendBtnSel || 'button[type="submit"], button[aria-label*="send" i]',
        message_list: messageListSel || '[role="log"], main, div[class*="chat" i]',
        message: messageSel || '[data-role="assistant"], [data-role="user"], div[class*="message" i], article',
        has_composer: !!composerEl,
        has_send_button: !!sendBtnEl
    };
})()
"""


def detect_selectors(page: Page) -> Dict[str, Any]:
    try:
        data = page.evaluate(AUTO_DETECT_JS)
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"Warning: DOM auto-detection encountered: {e}", file=sys.stderr)
    return {
        "url": page.url,
        "composer": "textarea, div[contenteditable=\"true\"], [role=\"textbox\"]",
        "send_button": "button[type=\"submit\"], button[aria-label*=\"send\" i]",
        "message_list": "[role=\"log\"], main, div[class*=\"chat\" i]",
        "message": "[data-role=\"assistant\"], [data-role=\"user\"], div[class*=\"message\" i], article",
    }


def _fill_composer(page: Page, composer_selector: str, text: str) -> None:
    """Robustly fill the composer element, triggering React/Next.js state updates."""
    sel = composer_selector or "textarea[placeholder='Message'], textarea"
    loc = page.locator(sel).last
    loc.wait_for(state="visible", timeout=15_000)
    loc.click()
    page.wait_for_timeout(200)

    try:
        loc.evaluate("el => { el.focus(); el.select(); }")
    except Exception:
        pass

    # For shorter text without newlines, keyboard typing directly triggers full React input state
    if len(text) < 1200 and "\n" not in text:
        try:
            page.keyboard.type(text)
        except Exception:
            page.keyboard.insert_text(text)
    else:
        try:
            page.keyboard.insert_text(text)
        except Exception:
            page.keyboard.type(text)

    # Wait for the Send button to become visible/enabled (confirming React state caught up)
    try:
        page.wait_for_selector('button[aria-label="Send"], button[aria-label*="Send" i]', timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(300)


def _click_send(page: Page, send_btn_selector: str, composer_selector: str) -> None:
    """Click send button and ensure prompt is submitted via Enter key."""
    clicked = False
    send_candidates = [
        'button[aria-label="Send"]',
        'button[aria-label*="Send" i]',
        send_btn_selector,
    ]
    for s in send_candidates:
        if not s:
            continue
        try:
            btn = page.locator(s).last
            if btn.is_visible() and btn.is_enabled():
                print(f"[*] Clicking send button: {s}", file=sys.stderr, flush=True)
                btn.click(timeout=2000)
                clicked = True
                break
        except Exception as ex:
            print(f"[*] Send button candidate {s} failed: {ex}", file=sys.stderr, flush=True)

    # If send button wasn't clicked, or if composer still holds text, press Enter to submit
    try:
        page.wait_for_timeout(400)
        composer_loc = page.locator(composer_selector or "textarea").last
        val = composer_loc.input_value() if composer_loc.count() > 0 else ""
        if val or not clicked:
            print("[*] Submitting via Enter key...", file=sys.stderr, flush=True)
            composer_loc.press("Enter")
    except Exception:
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass

    page.wait_for_timeout(600)


def _extract_messages(page: Page, message_selector: str) -> List[Dict[str, str]]:
    """Extract message items as list of {'author': str, 'text': str}."""
    sel = message_selector or ".hatch-chat-groupable-bubble"
    try:
        loc = page.locator(sel)
        count = loc.count()
    except Exception:
        return []

    out = []
    for i in range(count):
        el = loc.nth(i)
        try:
            # Note: Do NOT filter with is_visible() here because long chat containers
            # clip off-screen bubbles, but inner_text() remains accessible in the DOM.
            text = el.inner_text().strip()
            if not text:
                continue
            classes = el.get_attribute("class") or ""
            is_user = (
                "bg-chat-user-bubble" in classes
                or "user" in classes.lower()
                or text.lstrip().startswith("You:")
                or text.lstrip().startswith("You\n")
            )
            role = "user" if is_user else "assistant"
            clean_text = text
            if is_user:
                clean_text = re.sub(r"^You\s*:\s*", "", clean_text, flags=re.IGNORECASE).strip()
            out.append({"author": role, "text": clean_text})
        except Exception:
            continue
    return out


def _is_generating(page: Page) -> bool:
    """Check if Muse is currently generating, thinking, or streaming."""
    try:
        stop_btn = page.locator("button[aria-label*='Stop' i], button:has-text('Stop'), [data-testid*='stop' i]")
        for i in range(stop_btn.count()):
            if stop_btn.nth(i).is_visible():
                return True
    except Exception:
        pass

    try:
        active_indicators = page.locator(
            "[role='log'] [class*='typing'], [role='log'] [class*='thinking'], "
            "[role='log'] [aria-label*='thinking' i], [role='log'] [aria-label*='loading' i], "
            ".hatch-chat-groupable-bubble [class*='animate-pulse']"
        )
        for i in range(min(active_indicators.count(), 5)):
            if active_indicators.nth(i).is_visible():
                return True
    except Exception:
        pass

    return False


def _wait_for_reply(
    page: Page,
    message_selector: str,
    n_before: int = 0,
    timeout_s: int = DEFAULT_TIMEOUT,
    prompt_text: str = "",
) -> str:
    """Wait for assistant response to stream and settle."""
    deadline = time.time() + timeout_s
    last_text = None
    stable_count = 0
    captured_texts: List[str] = []
    seen_generating = False

    # Clean target snippet from prompt_text to match in user message bubble
    target_snippet = prompt_text.strip()[:40].strip().lower() if prompt_text else ""

    print(f"[*] Waiting for Muse AI response (timeout={timeout_s}s)...", file=sys.stderr, flush=True)
    poll_count = 0
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        poll_count += 1

        # 1. Scroll window and scrollable containers to bottom so new elements render
        try:
            page.evaluate("""() => {
                const scroller = document.getElementById('hatch-chat-scroll') || document.querySelector('[class*="overflow-y-auto"]');
                if (scroller) {
                    scroller.scrollTop = scroller.scrollHeight;
                }
                window.scrollTo(0, document.body.scrollHeight);
                const scrollables = document.querySelectorAll('[role="log"], main, div[class*="chat" i], div[class*="overflow" i], div[class*="scroll" i]');
                for (const el of scrollables) {
                    if (el.scrollHeight > el.clientHeight) {
                        el.scrollTop = el.scrollHeight;
                    }
                }
            }""")
        except Exception:
            pass

        # 2. Extract current messages in the DOM
        msgs = _extract_messages(page, message_selector)
        if not msgs:
            continue

        # 3. Locate the user message for this prompt
        matched_user_idx = None
        if target_snippet:
            for idx in range(len(msgs) - 1, -1, -1):
                if msgs[idx].get("author") == "user":
                    u_txt = msgs[idx].get("text", "").strip().lower()
                    if target_snippet in u_txt or u_txt[:40] in target_snippet:
                        matched_user_idx = idx
                        break

        if matched_user_idx is None:
            # Fallback to the latest user message found in msgs
            for idx in range(len(msgs) - 1, -1, -1):
                if msgs[idx].get("author") == "user":
                    matched_user_idx = idx
                    break

        # 4. Extract assistant messages that appear AFTER the matched user message
        assistant_msgs = []
        if matched_user_idx is not None and matched_user_idx < len(msgs) - 1:
            assistant_msgs = [
                m["text"] for m in msgs[matched_user_idx + 1 :]
                if m.get("author") == "assistant" or not m.get("text", "").lstrip().startswith("You:")
            ]
        elif not assistant_msgs and n_before and len(msgs) > n_before:
            new_msgs = msgs[n_before:]
            assistant_msgs = [m["text"] for m in new_msgs if m.get("author") == "assistant"]
            if not assistant_msgs and len(new_msgs) >= 2:
                assistant_msgs = [new_msgs[-1]["text"]]

        if not assistant_msgs:
            if poll_count % 8 == 0:
                print(f"[*] Waiting for assistant bubble... (DOM bubbles={len(msgs)}, n_before={n_before}, matched_user={matched_user_idx})", file=sys.stderr, flush=True)
                try:
                    conn_elem = page.locator("text='Still sending', text='Connecting...'")
                    if conn_elem.count() > 0 and conn_elem.first.is_visible():
                        print(
                            "\n[!] ALERT: Muse session expired or disconnected ('Connecting...' / 'Still sending' detected).\n"
                            "    Your login session in storage_state.json has expired.\n"
                            "    Please refresh it by running: python muse_bridge.py login\n",
                            file=sys.stderr,
                            flush=True,
                        )
                except Exception:
                    pass
            continue

        BUBBLE_SEP = "\n\n<!-- bubble -->\n\n"
        cur_text = BUBBLE_SEP.join(m.strip() for m in assistant_msgs if m.strip())
        captured_texts = [cur_text]

        is_gen = _is_generating(page)
        if is_gen:
            seen_generating = True

        if poll_count % 8 == 0:
            print(f"[*] Stream settling ({poll_count * POLL_INTERVAL_S:.1f}s): bubbles={len(msgs)}, assistant_msgs={len(assistant_msgs)}, chars={len(cur_text)}, is_gen={is_gen}, seen_gen={seen_generating}, stable={stable_count}/{SETTLE_POLLS}", file=sys.stderr, flush=True)

        # 5. Settle check:
        # If Muse was actively generating and has now stopped (Stop button disappeared), and text is stable:
        if seen_generating and not is_gen and cur_text and cur_text == last_text:
            print(f"[*] Muse AI generation completed ({len(assistant_msgs)} bubble(s), {len(cur_text)} chars).", file=sys.stderr, flush=True)
            return cur_text

        # If Muse is still marked as generating, require text to remain static for 20 polls (10s) as safety timeout
        if is_gen:
            if cur_text and cur_text == last_text:
                stable_count += 1
                if stable_count >= 20:
                    print(f"[*] Muse AI stream completed (10s static threshold; {len(assistant_msgs)} bubble(s), {len(cur_text)} chars).", file=sys.stderr, flush=True)
                    return cur_text
            else:
                stable_count = 0
        elif cur_text and cur_text == last_text:
            stable_count += 1
            if stable_count >= SETTLE_POLLS:
                print(f"[*] Muse AI response complete ({len(assistant_msgs)} bubble(s), {len(cur_text)} chars).", file=sys.stderr, flush=True)
                return cur_text
        else:
            stable_count = 0

        last_text = cur_text

    print(
        "Warning: generation timed out or took longer than expected; returning captured text.",
        file=sys.stderr,
    )
    return captured_texts[-1] if captured_texts else ""


def _launch_browser_and_context(
    p,
    cfg: Dict[str, Any],
    headless: bool = True,
    with_storage: bool = True,
) -> Tuple[Browser, BrowserContext]:
    """Launch browser with built-in location spoofing (mimicking Location Guard), proxy, and optional CDP."""
    cdp_url = cfg.get("cdp_url")
    if cdp_url:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            sys.exit(
                f"\n[Error] Could not connect to Chrome at {cdp_url} ({e}).\n\n"
                f"Why did this happen?\n"
                f"  Chrome was not running with remote debugging enabled.\n\n"
                f"How to fix this:\n"
                f"  Option 1: If you have a desktop VPN app running (Nord, Proton, etc.),\n"
                f"            you DO NOT need --cdp! Just run:\n"
                f"                python muse_bridge.py login\n\n"
                f"  Option 2: If your VPN is a Chrome extension, launch Chrome with debugging:\n"
                f"                python muse_bridge.py launch-chrome\n"
                f"            Then in another terminal run:\n"
                f"                python muse_bridge.py login --cdp http://127.0.0.1:9222\n"
            )
        contexts = browser.contexts
        ctx = contexts[0] if contexts else browser.new_context()
        return browser, ctx

    launch_kwargs: Dict[str, Any] = {"headless": headless}
    proxy = cfg.get("proxy")
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}

    browser = p.chromium.launch(**launch_kwargs)

    # Location spoofing matching Location Guard + US Locale/Timezone
    ctx_kwargs: Dict[str, Any] = {
        "geolocation": cfg.get("geolocation", {"latitude": 40.7128, "longitude": -74.0060}),
        "permissions": ["geolocation"],
        "locale": cfg.get("locale", "en-US"),
        "timezone_id": cfg.get("timezone_id", "America/New_York"),
        "accept_downloads": True,
    }
    if with_storage and STATE_FILE.exists():
        ctx_kwargs["storage_state"] = str(STATE_FILE)

    ctx = browser.new_context(**ctx_kwargs)
    return browser, ctx


# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------

def cmd_login(args: argparse.Namespace) -> None:
    """Headed login flow: opens browser, logs in, auto-discovers selectors."""
    cfg = load_config()
    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "cdp", None):
        cfg["cdp_url"] = args.cdp

    target_url = args.url or cfg.get("chat_url") or "https://muse.ai"

    print("=" * 60)
    print("MUSE AI LOGIN & SETUP")
    print("=" * 60)
    print("Launching Chromium browser window...")
    print("  [✓] Location spoofing: US (New York) + America/New_York timezone")
    if cfg.get("proxy"):
        print(f"  [✓] Proxy: {cfg['proxy']}")
    if cfg.get("cdp_url"):
        print(f"  [✓] Attaching via CDP to: {cfg['cdp_url']}")
    print("1. Log in to your Muse account.")
    print("2. Open the chat you want to use with Claude Code.")
    print("3. Return here and press ENTER.")
    print("-" * 60)

    with sync_playwright() as p:
        browser, ctx = _launch_browser_and_context(p, cfg, headless=False, with_storage=False)
        page = ctx.new_page()
        page.goto(target_url)

        try:
            input("\n--> Press ENTER here after logging in and opening your chat: ")
        except (KeyboardInterrupt, EOFError):
            print("\nLogin cancelled.")
            browser.close()
            return

        # Auto-detect selectors from current page
        print("\nAnalyzing page DOM and discovering selectors...")
        detected = detect_selectors(page)

        # Save session state
        ctx.storage_state(path=str(STATE_FILE))
        try:
            STATE_FILE.chmod(0o600)
        except OSError:
            pass

        # Update config
        final_url = page.url
        cfg["chat_url"] = final_url
        cfg["selectors"]["composer"] = detected.get("composer", "")
        cfg["selectors"]["send_button"] = detected.get("send_button", "")
        cfg["selectors"]["message_list"] = detected.get("message_list", "")
        cfg["selectors"]["message"] = detected.get("message", "")
        save_config(cfg)

        print(f"\n[OK] Session saved to {STATE_FILE.name}")
        print(f"[OK] Chat URL saved: {final_url}")
        print("[OK] Discovered selectors:")
        print(f"     Composer:    {cfg['selectors']['composer']}")
        print(f"     Send Button: {cfg['selectors']['send_button']}")
        print(f"     Messages:    {cfg['selectors']['message']}")
        print(f"[OK] Configuration written to {CONFIG_FILE.name}")
        print("\nYou can now test sending messages with:")
        print('    python muse_bridge.py send "hello"')
        browser.close()


def _ensure_configured(cfg: Dict[str, Any]) -> str:
    if not STATE_FILE.exists():
        sys.exit(
            "No saved login session found.\n"
            "Please run: python muse_bridge.py login"
        )
    chat_url = cfg.get("chat_url", "").strip()
    if not chat_url or chat_url.startswith("PASTE_"):
        sys.exit(
            "No chat_url configured.\n"
            "Please run: python muse_bridge.py login"
        )
    return chat_url


def _query_daemon(endpoint: str, data: Optional[dict] = None, port: int = DEFAULT_PORT) -> Optional[dict]:
    """Check if the local background daemon is running and respond."""
    url = f"http://127.0.0.1:{port}{endpoint}"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8") if data is not None else None,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def cmd_send(args: argparse.Namespace) -> None:
    cfg = load_config()
    port = args.port or cfg.get("port", DEFAULT_PORT)

    # 1. Try sending through running daemon (fast mode)
    daemon_health = _query_daemon("/health", port=port)
    if daemon_health and daemon_health.get("status") == "ok":
        resp = _query_daemon("/send", {"text": args.text, "timeout": args.timeout}, port=port)
        if resp and resp.get("status") == "ok":
            reply = resp.get("reply", "")
            if args.json:
                print(json.dumps({"reply": reply}, ensure_ascii=False))
            else:
                print(reply)
            return

    # 2. Fallback: launch one-shot headless browser
    chat_url = args.chat_url or _ensure_configured(cfg)
    headless = not args.headful if args.headful else cfg.get("headless", True)
    selectors = cfg.get("selectors", {})
    composer_sel = selectors.get("composer") or "textarea, div[contenteditable=\"true\"], [role=\"textbox\"]"
    send_sel = selectors.get("send_button") or "button[type=\"submit\"], button[aria-label*=\"send\" i]"
    msg_sel = selectors.get("message") or "[data-role=\"assistant\"], [data-role=\"user\"], div[class*=\"message\" i], article"

    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "cdp", None):
        cfg["cdp_url"] = args.cdp

    print(f"[*] Launching browser (headless={headless})...", file=sys.stderr, flush=True)
    with sync_playwright() as p:
        browser, ctx = _launch_browser_and_context(p, cfg, headless=headless, with_storage=True)
        page = ctx.new_page()
        print(f"[*] Navigating to {chat_url}...", file=sys.stderr, flush=True)
        page.goto(chat_url, timeout=45_000)

        print("[*] Waiting for composer...", file=sys.stderr, flush=True)
        try:
            page.wait_for_selector(composer_sel, timeout=30_000)
        except PlaywrightTimeoutError:
            detected = detect_selectors(page)
            composer_sel = detected.get("composer") or composer_sel
            send_sel = detected.get("send_button") or send_sel
            msg_sel = detected.get("message") or msg_sel

        # Ensure existing chat history is loaded before calculating n_before
        try:
            page.wait_for_selector(msg_sel, timeout=10_000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        n_before = len(_extract_messages(page, msg_sel))
        print(f"[*] Found {n_before} messages in thread. Submitting prompt...", file=sys.stderr, flush=True)
        _fill_composer(page, composer_sel, args.text)
        _click_send(page, send_sel, composer_sel)
        print("[*] Message submitted. Waiting for Muse AI response...", file=sys.stderr, flush=True)

        reply = _wait_for_reply(page, msg_sel, n_before, timeout_s=args.timeout, prompt_text=args.text)

        if args.json:
            print(json.dumps({"reply": reply}, ensure_ascii=False))
        else:
            print(reply)
        browser.close()


def cmd_read(args: argparse.Namespace) -> None:
    cfg = load_config()
    port = args.port or cfg.get("port", DEFAULT_PORT)
    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "cdp", None):
        cfg["cdp_url"] = args.cdp

    # 1. Try daemon
    resp = _query_daemon(f"/read?limit={args.limit}", port=port)
    if resp and resp.get("status") == "ok":
        print(json.dumps(resp.get("messages", []), indent=2, ensure_ascii=False))
        return

    # 2. One-shot browser
    chat_url = args.chat_url or _ensure_configured(cfg)
    headless = not args.headful if args.headful else cfg.get("headless", True)
    msg_sel = cfg.get("selectors", {}).get("message") or ".hatch-chat-groupable-bubble, [data-hatch-assistant-message-body='true']"

    with sync_playwright() as p:
        browser, ctx = _launch_browser_and_context(p, cfg, headless=headless, with_storage=True)
        page = ctx.new_page()
        page.goto(chat_url, timeout=45_000)
        try:
            page.wait_for_selector(msg_sel, timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        msgs = _extract_messages(page, msg_sel)
        print(json.dumps(msgs[-args.limit :], indent=2, ensure_ascii=False))
        browser.close()


def cmd_inspect(args: argparse.Namespace) -> None:
    """Inspect the chat DOM and take a diagnostic screenshot."""
    cfg = load_config()
    chat_url = args.chat_url or _ensure_configured(cfg)
    if getattr(args, "proxy", None):
        cfg["proxy"] = args.proxy
    if getattr(args, "cdp", None):
        cfg["cdp_url"] = args.cdp

    print("Launching browser to inspect DOM at:", chat_url)
    with sync_playwright() as p:
        browser, ctx = _launch_browser_and_context(p, cfg, headless=not args.headful, with_storage=True)
        page = ctx.new_page()
        page.goto(chat_url, timeout=45_000)
        try:
            page.wait_for_selector("textarea", timeout=20_000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        detected = detect_selectors(page)
        screenshot_path = BASE_DIR / "debug_screenshot.png"
        page.screenshot(path=str(screenshot_path))

        print("\n--- Detected Elements ---")
        print(f"Page Title:   {page.title()}")
        print(f"Page URL:     {page.url}")
        print(f"Composer:     {detected.get('composer')}")
        print(f"Send Button:  {detected.get('send_button')}")
        print(f"Message List: {detected.get('message_list')}")
        print(f"Message:      {detected.get('message')}")
        print(f"\nScreenshot saved to: {screenshot_path}")

        msg_sel = cfg.get("selectors", {}).get("message") or detected.get("message", "")
        msgs = _extract_messages(page, msg_sel)
        print(f"Extracted {len(msgs)} messages:")
        for m in msgs[-3:]:
            snippet = m["text"].replace("\n", " ")[:80]
            print(f"  - [{m.get('author') or 'unknown'}]: {snippet}...")

        browser.close()


def _extract_archive(archive_path: Path, cwd: str) -> List[str]:
    """Safely extract tar.gz or zip archive into target directory, flattening single top-level folder if present."""
    target_dir = Path(cwd)
    target_dir.mkdir(parents=True, exist_ok=True)
    extracted_names: List[str] = []

    # 1. TAR Archive (.tar.gz, .tgz, .tar)
    if tarfile.is_tarfile(str(archive_path)):
        with tarfile.open(str(archive_path), "r:*") as tar:
            members = tar.getmembers()
            prefixes = {m.name.split("/")[0] for m in members if "/" in m.name}
            single_root = list(prefixes)[0] if len(prefixes) == 1 else None

            for member in members:
                orig_name = member.name
                if single_root and (orig_name == single_root or orig_name.startswith(single_root + "/")):
                    parts = orig_name.split("/")
                    if len(parts) > 1:
                        member.name = "/".join(parts[1:])
                    else:
                        continue
                if member.name:
                    try:
                        tar.extract(member, path=str(target_dir), filter=getattr(tarfile, "data_filter", None))
                    except TypeError:
                        tar.extract(member, path=str(target_dir))
                    if not member.isdir():
                        extracted_names.append(member.name)

    # 2. ZIP Archive (.zip)
    elif zipfile.is_zipfile(str(archive_path)):
        with zipfile.ZipFile(str(archive_path), "r") as zf:
            infolist = zf.infolist()
            prefixes = {info.filename.split("/")[0] for info in infolist if "/" in info.filename}
            single_root = list(prefixes)[0] if len(prefixes) == 1 else None

            for info in infolist:
                orig_name = info.filename
                dest_name = orig_name
                if single_root and orig_name.startswith(single_root + "/"):
                    dest_name = orig_name[len(single_root) + 1 :]
                if dest_name and not info.is_dir():
                    dest_path = target_dir / dest_name
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(dest_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted_names.append(dest_name)

    return extracted_names


def _download_and_extract_recent_archives(page: Page, cwd: str) -> List[str]:
    """Detect downloadable archive cards attached by Muse, download and extract to cwd."""
    if not cwd or not os.path.isdir(cwd):
        return []

    extracted: List[str] = []
    try:
        # Check for sandbox file card options buttons
        options_btns = page.locator("[data-testid='hatch-sandbox-file-card-options']")
        if options_btns.count() == 0:
            return []

        options_btn = options_btns.last
        if not options_btn.is_visible():
            return []

        card_container = options_btn.locator("xpath=./ancestor::span[contains(@class, 'rounded') or contains(@class, 'hatch-chat-groupable-bubble')][1]")
        card_text = card_container.inner_text().lower() if card_container.count() > 0 else ""
        if not any(ext in card_text for ext in (".tar", ".zip", ".tgz", ".gz", "archive")):
            return []

        print("[*] Detected downloadable project archive in Muse chat; triggering download...", file=sys.stderr, flush=True)
        options_btn.click()
        page.wait_for_timeout(600)

        download_item = page.locator(
            "[role='menuitem']:has-text('Download'), [data-slot='dropdown-menu-item']:has-text('Download'), button:has-text('Download')"
        ).first
        if not download_item.is_visible():
            return []

        tmp_dir = Path(tempfile.gettempdir()) / "muse_downloads"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        archive_path = tmp_dir / f"project_archive_{int(time.time())}.tar.gz"

        with page.expect_download(timeout=30000) as dl_info:
            download_item.click()
        dl = dl_info.value
        suggested_name = dl.suggested_filename
        if suggested_name:
            archive_path = tmp_dir / suggested_name
        dl.save_as(str(archive_path))
        print(f"[*] Successfully downloaded archive: {archive_path.name} ({archive_path.stat().st_size} bytes)", file=sys.stderr, flush=True)

        extracted = _extract_archive(archive_path, cwd)
        print(f"[*] Extracted {len(extracted)} files directly into {cwd}", file=sys.stderr, flush=True)
    except Exception as ex:
        print(f"[!] Archive download/extraction encountered: {ex}", file=sys.stderr, flush=True)

    return extracted


# ---------------------------------------------------------------------------
# Background Daemon (HTTP / OpenAI-Compatible Server)
# ---------------------------------------------------------------------------

class BrowserManager:
    """Manages Playwright on a dedicated worker thread for thread-safe HTTP request handling."""

    def __init__(self, chat_url: str, headless: bool = True):
        self.chat_url = chat_url
        self.headless = headless
        self.task_queue: queue.Queue = queue.Queue()
        self.ready_event = threading.Event()
        self.lock = threading.Lock()
        self.url = ""
        self.error: Optional[Exception] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready_event.wait(timeout=60)
        if self.error:
            raise self.error

    def _run(self) -> None:
        cfg = load_config()
        selectors = cfg.get("selectors", {})
        composer_sel = selectors.get("composer") or "textarea[placeholder='Message'], textarea"
        send_sel = selectors.get("send_button") or 'button[aria-label="Send"], button[aria-label*="Send" i]'
        msg_sel = selectors.get("message") or ".hatch-chat-groupable-bubble"

        try:
            with sync_playwright() as p:
                browser, ctx = _launch_browser_and_context(
                    p, cfg, headless=self.headless, with_storage=True
                )
                page = ctx.new_page()
                print(f"[Daemon] Opening chat at {self.chat_url}...")
                page.goto(self.chat_url, timeout=60_000)
                try:
                    page.wait_for_selector(composer_sel, timeout=30_000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)

                self.url = page.url
                self.ready_event.set()
                print("[Daemon] Browser warm and ready for requests.")

                while True:
                    task = self.task_queue.get()
                    action = task[0]
                    if action == "stop":
                        break
                    elif action == "send":
                        _, text, timeout_s, res_q, req_cwd = task
                        try:
                            try:
                                page.wait_for_selector(msg_sel, timeout=5000)
                            except Exception:
                                pass
                            page.wait_for_timeout(1000)
                            n_before = len(_extract_messages(page, msg_sel))
                            _fill_composer(page, composer_sel, text)
                            _click_send(page, send_sel, composer_sel)
                            reply = _wait_for_reply(page, msg_sel, n_before, timeout_s=timeout_s, prompt_text=text)
                            extracted = _download_and_extract_recent_archives(page, req_cwd)
                            res_q.put(("ok", (reply, extracted)))
                        except Exception as ex:
                            res_q.put(("error", str(ex)))
                    elif action == "read":
                        _, limit, res_q = task
                        try:
                            msgs = _extract_messages(page, msg_sel)
                            res_q.put(("ok", msgs[-limit:]))
                        except Exception as ex:
                            res_q.put(("error", str(ex)))
                    elif action == "navigate":
                        _, new_url, res_q = task
                        try:
                            print(f"[Daemon] Navigating to: {new_url}", file=sys.stderr, flush=True)
                            page.goto(new_url, timeout=60_000)
                            try:
                                page.wait_for_selector(composer_sel, timeout=30_000)
                            except Exception:
                                pass
                            page.wait_for_timeout(2000)
                            self.url = page.url
                            self.chat_url = page.url
                            cfg["chat_url"] = self.url
                            save_config(cfg)
                            print(f"[Daemon] Successfully navigated to: {self.url}", file=sys.stderr, flush=True)
                            res_q.put(("ok", self.url))
                        except Exception as ex:
                            res_q.put(("error", str(ex)))

                browser.close()
        except Exception as e:
            self.error = e
            self.ready_event.set()

    def stop(self) -> None:
        self.task_queue.put(("stop",))

    def navigate(self, new_url: str) -> str:
        with self.lock:
            res_q = queue.Queue()
            self.task_queue.put(("navigate", new_url, res_q))
            try:
                status, val = res_q.get(timeout=60)
            except queue.Empty:
                raise RuntimeError("Muse navigation timed out.")
            if status == "error":
                raise RuntimeError(val)
            return val

    def send_prompt(self, text: str, timeout_s: int = DEFAULT_TIMEOUT, cwd: str = "") -> Tuple[str, List[str]]:
        with self.lock:
            res_q = queue.Queue()
            self.task_queue.put(("send", text, timeout_s, res_q, cwd))
            try:
                status, val = res_q.get(timeout=timeout_s + 30)
            except queue.Empty:
                raise RuntimeError(f"Muse prompt timed out after {timeout_s}s waiting for response.")
            if status == "error":
                raise RuntimeError(val)
            return val

    def get_messages(self, limit: int = 10) -> List[Dict[str, str]]:
        with self.lock:
            res_q = queue.Queue()
            self.task_queue.put(("read", limit, res_q))
            try:
                status, val = res_q.get(timeout=30)
            except queue.Empty:
                raise RuntimeError("Muse get_messages timed out.")
            if status == "error":
                raise RuntimeError(val)
            return val


def strip_system_tags(text: str) -> str:
    """Remove <system-reminder>...</system-reminder> and unwrap <pasted_content> tags injected by Claude Code."""
    cleaned = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"<pasted_content[^>]*>(.*?)</pasted_content[^>]*>", r"\1", cleaned, flags=re.DOTALL)
    return cleaned.strip()


def is_dump_all_contents_query(text: str) -> bool:
    """Check if user prompt asks to output/read the contents of existing local files in the current folder."""
    t = text.strip().lower()
    # If the user is asking to create, transfer, save, generate, or build, it is NOT a local dump!
    if re.search(r"\b(?:create|make|write|generate|build|transfer|save|disk|workspace|implement|develop|solve|coding|codeblocks|code\s*block)\b", t):
        return False
    if re.search(r"\b(?:output|print|show|display|dump|read|cat|give\s+me|get)\b.*\b(?:contents?|text|code|body)\b.*\b(?:all\s+)?(?:files?|folder|directory|dir|project|here)\b", t):
        return True
    if re.search(r"\b(?:contents?|what(?:'s|\s+is)\s+(?:in|inside))\b.*\b(?:all\s+)?(?:files?|folder|directory|here)\b", t):
        return True
    if re.search(r"\b(?:read|cat|dump|output|print|show)\s+(?:all\s+)?(?:the\s+)?files?\s+(?:in\s+)?(?:this|the|current)?\s*(?:folder|directory|dir|here)?\b", t):
        if any(w in t for w in ("content", "contents", "inside", "text", "cat", "dump", "read")):
            return True
    return False


def is_folder_analysis_query(text: str) -> bool:
    """Check if user prompt asks a question or analysis about the files in the project folder."""
    t = text.strip().lower()
    # If the user is asking to create, transfer, save, generate, or build, it is NOT an analysis of existing local files!
    if re.search(r"\b(?:create|make|write|generate|build|transfer|save|disk|workspace|implement|develop)\b", t):
        return False
    if re.search(r"\b(?:all\s+)?(?:the\s+)?files?\b.*\b(?:this|the|current)?\s*(?:folder|directory|dir|project|repo|codebase|workspace|here)\b", t):
        return True
    if re.search(r"\b(?:this|the|current)?\s*(?:folder|directory|dir|project|repo|codebase|workspace|here)\b.*\b(?:all\s+)?(?:the\s+)?files?\b", t):
        return True
    return False


def get_local_folder_contents(cwd: str, max_files: int = 50, max_size_bytes: int = 100_000) -> Dict[str, str]:
    """Read all text files in cwd, skipping VCS and dependencies."""
    files_content: Dict[str, str] = {}
    ignored_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode", "dist", "build"}
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith(".")]
        for f in sorted(files):
            if f.startswith("."):
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, cwd).replace("\\", "/")
            try:
                if os.path.isfile(full_path) and os.path.getsize(full_path) <= max_size_bytes:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as fp:
                        files_content[rel_path] = fp.read()
            except Exception:
                pass
            if len(files_content) >= max_files:
                break
        if len(files_content) >= max_files:
            break
    return files_content


def format_folder_contents(contents: Dict[str, str], cwd: str) -> str:
    """Format dictionary of local file contents clearly."""
    if not contents:
        return f"No files were found in your local folder (`{cwd}`)."
    parts = [f"Here are the contents of the files in your folder (`{cwd}`):\n"]
    for fname, fcontent in sorted(contents.items()):
        parts.append(f"{fname}:\n{fcontent.strip()}\n")
    return "\n".join(parts).strip()


def list_local_files(cwd: str, max_files: int = 100) -> List[str]:
    """List relative paths of files in cwd, skipping VCS/dependencies."""
    found = []
    ignored_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode", "dist", "build"}
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith(".")]
        for f in sorted(files):
            if f.startswith("."):
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, cwd).replace("\\", "/")
            found.append(rel_path)
            if len(found) >= max_files:
                break
        if len(found) >= max_files:
            break
    return sorted(found)


def is_delete_all_files_query(text: str) -> bool:
    """Check if the prompt requests deleting all files in the current folder."""
    t = text.strip().lower()
    # E.g. "clear the directory", "empty the folder", "vaciar la carpeta", "clean the folder"
    if re.search(r"\b(?:clear|clean|wipe|empty|vaciar|limpiar)\b\s+(?:out\s+)?(?:all\s+)?(?:the|this|la|el)?\s*(?:folder|directory|dir|carpeta|here)\b", t):
        return True
    # E.g. "delete/remove all/the files in the folder/directory"
    if re.search(r"\b(?:delete|remove|clear|clean|wipe|erase|empty|borra|borrar|elimina|eliminar|vaciar|limpiar)\b.*\b(?:all\s+)?(?:the\s+|los\s+)?(?:files|everything|todos|archivos)\b", t):
        return True
    if re.search(r"\b(?:delete|remove|borra|borrar|elimina|eliminar)\b\s+(?:all\s+)?(?:the\s+|los\s+)?(?:files|archivos)\b", t):
        return True
    if t in (
        "delete files", "delete all files", "clean folder", "clear folder", "empty folder",
        "delete the files in the folder", "delete the files in this folder",
        "borra los archivos de la carpeta", "borra los archivos", "borra todo", "elimina todo",
        "clear the directory", "empty the directory", "empty directory", "clear directory"
    ):
        return True
    return False


def detect_delete_intent(text: str, cwd: str, tool_names: set) -> Optional[Tuple[str, str, dict]]:
    """
    Detect if the user prompt requests deleting local files in cwd.
    Returns: (thought, tool_name, tool_input) or None.
    """
    t = text.strip().lower()
    cmd_tool = "PowerShell" if "PowerShell" in tool_names else ("Bash" if "Bash" in tool_names else None)
    if not cmd_tool:
        return None

    # 1. Delete all files in current folder / directory
    if is_delete_all_files_query(t):
        existing = []
        try:
            for item in os.listdir(cwd):
                full = os.path.join(cwd, item)
                if not item.startswith("."):
                    existing.append(item)
        except Exception:
            pass

        if existing:
            if cmd_tool == "PowerShell":
                paths_str = ", ".join(f"'{os.path.join(cwd, f)}'" for f in existing)
                cmd = f"Remove-Item -Path {paths_str} -Recurse -Force"
            else:
                paths_str = " ".join(f"'{os.path.join(cwd, f)}'" for f in existing)
                cmd = f"rm -rf {paths_str}"
            thought = f"Deleting files in `{cwd}` ({', '.join(existing)})."
            return thought, cmd_tool, {"command": cmd}
        else:
            return None

    # 2. Delete specific file(s) mentioned in prompt
    is_delete_verb = bool(re.search(r"\b(?:delete|remove|rm|erase|del|unlink|borra|borrar|elimina|eliminar|quitar)\b", t))
    if is_delete_verb:
        existing_names = []
        try:
            for item in os.listdir(cwd):
                if not item.startswith("."):
                    existing_names.append(item)
        except Exception:
            pass

        file_candidates = re.findall(r"\b([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)\b", text)
        for item in existing_names:
            if item not in file_candidates:
                if re.search(rf"\b{re.escape(item)}\b", text, re.IGNORECASE):
                    file_candidates.append(item)

        valid_files_to_delete = []
        for fc in file_candidates:
            full = os.path.normpath(os.path.join(cwd, fc))
            if (os.path.isfile(full) or os.path.isdir(full)) and (fc, full) not in valid_files_to_delete:
                valid_files_to_delete.append((fc, full))

        if valid_files_to_delete:
            if cmd_tool == "PowerShell":
                paths_str = ", ".join(f"'{p}'" for _, p in valid_files_to_delete)
                cmd = f"Remove-Item -Path {paths_str} -Recurse -Force"
            else:
                paths_str = " ".join(f"'{p}'" for _, p in valid_files_to_delete)
                cmd = f"rm -rf {paths_str}"
            names_str = ", ".join(f"`{f}`" for f, _ in valid_files_to_delete)
            thought = f"Deleting {names_str} on your computer."
            return thought, cmd_tool, {"command": cmd}

    return None


def is_file_list_query(p: str) -> bool:
    """Check if the user prompt is asking to inspect or list files in the current folder."""
    p_lower = p.strip().lower()
    if p_lower in ("ls", "dir", "get-childitem", "pwd"):
        return True
    # If the user is asking to create, make, write, generate, transfer, save, delete, etc.
    if re.search(r"\b(?:create|make|write|generate|add|edit|modify|update|delete|remove|clear|clean|wipe|erase|transfer|save|disk|workspace|build|develop|implement|borra|borrar|elimina|eliminar|vaciar)\b", p_lower):
        return False
    # If the user is asking for file contents/code, it's a content query, not a pure listing query
    if any(w in p_lower for w in ("content", "contents", "inside", "code", "text", "body")):
        return False
    # If the user is asking to explain, analyze, or understand what files do, it's an analysis query
    if re.search(r"\b(?:explain|analyze|summarize|describe|what\s+do|how\s+do|purpose|meaning|mean|do\s+the\s+files\s+do)\b", p_lower):
        return False
    if re.search(r"\b(?:what|show|list|see|tell me|display|check|find|view|output)\b.*\b(?:files|folders|directory contents|workspace contents)\b", p_lower):
        return True
    if re.search(r"\b(?:what files|which files|list files|show files)\b", p_lower):
        return True
    return False


def _detect_intent_tool_call(user_msg: str, tool_names: set, cwd: str) -> Optional[Tuple[str, str, dict]]:
    """
    Detect if the user prompt directly requests a local filesystem action that requires
    querying the user's computer rather than Muse's remote cloud container.
    """
    clean_msg = strip_system_tags(user_msg)

    # 0. Delete / remove files on local computer
    del_intent = detect_delete_intent(clean_msg, cwd, tool_names)
    if del_intent:
        return del_intent

    # 1. File listing / workspace inspection
    if is_file_list_query(clean_msg):
        if "Glob" in tool_names:
            thought = "Checking the files in your current working directory on your computer."
            return thought, "Glob", {"pattern": "*"}
        elif "PowerShell" in tool_names:
            return "Listing files in your current directory on your computer.", "PowerShell", {"command": "Get-ChildItem -Name"}
        elif "Bash" in tool_names:
            return "Listing files in your current directory on your computer.", "Bash", {"command": "ls"}

    # 2. Read / inspect of a specific file on the user's computer
    m_read = re.search(
        r"\b(?:read|cat|view|inspect|check|open|show(?:\s+me)?|what(?:'s|\s+is)\s+(?:in|inside))\s+(?:the\s+)?(?:contents?\s+of\s+)?(?:file\s+)?`?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)`?",
        clean_msg,
        flags=re.IGNORECASE,
    )
    if m_read and ("Read" in tool_names or "FileRead" in tool_names):
        fname = m_read.group(1).strip()
        abs_path = fname if os.path.isabs(fname) else os.path.normpath(os.path.join(cwd, fname))
        t_name = "Read" if "Read" in tool_names else "FileRead"
        return f"Reading `{fname}` on your computer.", t_name, {"file_path": abs_path}

    # 3. Direct execution of a command
    m_run = re.search(
        r"^(?:(?:run|execute|ejecuta|ejecutar|corre|correr)\s+(?:(?:the\s+)?command\s+|el\s+comando\s+)?)?(?:`([^`]+)`|\"([^\"]+)\"|((?:python|python3|py|git|pytest|npm|node|cargo|go|gcc|g\+\+|dotnet|make|pip)\s+[^\n\r]+|pytest))\s*$",
        clean_msg,
        flags=re.IGNORECASE,
    )
    if m_run and ("PowerShell" in tool_names or "Bash" in tool_names):
        cmd = (m_run.group(1) or m_run.group(2) or m_run.group(3)).strip()
        t_name = "PowerShell" if "PowerShell" in tool_names else "Bash"
        return f"Executing `{cmd}` on your computer.", t_name, {"command": cmd}

    return None


def _clean_file_list(raw_output: str) -> List[str]:
    """Parse and clean file listing output, removing internal .git and __pycache__ entries."""
    cleaned = raw_output.strip()
    files = []
    if cleaned.startswith("[") and cleaned.endswith("]"):
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                files = [str(f) for f in parsed]
        except Exception:
            pass
    if not files:
        lines = [line.strip().strip("'\"[],") for line in cleaned.splitlines()]
        files = [l for l in lines if l]

    filtered = []
    for f in files:
        norm = f.replace("/", "\\")
        if norm.startswith(".git\\") or norm == ".git" or "\\.git\\" in norm or norm.startswith(".git/"):
            continue
        if norm.startswith("__pycache__\\") or norm == "__pycache__" or "\\__pycache__\\" in norm or norm.startswith("__pycache__/"):
            continue
        filtered.append(f)
    return sorted(set(filtered))


def _format_file_list(files: List[str], cwd: str) -> str:
    """Format file listing output cleanly."""
    if not files:
        return f"No files were found in `{cwd}`."
    file_items = "\n".join(f"- `{f}`" for f in sorted(files))
    return f"Here are the files in your current folder (`{cwd}`):\n\n{file_items}"


def is_pure_listing_query(text: str) -> bool:
    """Check if the user literally only wants a directory listing (ls/dir), not analysis/answering a question."""
    t = text.strip().lower()
    if t in ("ls", "dir", "get-childitem", "pwd"):
        return True
    if re.search(r"\b(?:contain|related|login|credential|secret|token|personal|do|does|why|how|which|search|find|mean|explain|breakdown|responsible|handle|used for)\b", t):
        return False
    if re.search(r"\b(?:what|show|list|see|tell me|display)\b.*\b(?:files|folders|directory|folder)\b", t):
        return True
    return False


def find_referenced_local_files(text: str, cwd: str) -> List[Tuple[str, str, str]]:
    """
    Find files mentioned in text that exist on disk in cwd.
    Returns: list of (filename, abs_path, content)
    """
    candidates = re.findall(r"\b([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)\b", text)
    found = []
    seen = set()
    for c in candidates:
        rel = os.path.basename(c)
        if rel in seen:
            continue
        full_path = os.path.normpath(os.path.join(cwd, c))
        if os.path.isfile(full_path):
            seen.add(rel)
            try:
                if os.path.getsize(full_path) < 150_000:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        found.append((rel, full_path, f.read()))
            except Exception:
                pass

    if found:
        return found

    # Fallback: If no explicit filename was matched, but user refers to local files / website / project,
    # extract distinctive terms (e.g. capitalized brand names, quoted strings) and search cwd
    t_lower = text.lower()
    if any(k in t_lower for k in ("local file", "local files", "website", "site", "web", "html", "top left", "header", "navbar", "page", "browser")):
        keywords = []
        for m in re.finditer(r'["\']([^"\']+)["\']', text):
            keywords.append(m.group(1).strip())
        for m in re.finditer(r'\b([A-Z][a-zA-Z0-9_]{2,})\b', text):
            w = m.group(1).strip()
            if w.lower() not in ("from", "what", "where", "when", "here", "there", "please", "banana", "this", "that", "with", "have"):
                keywords.append(w)

        search_exts = {".html", ".htm", ".js", ".css", ".ts", ".jsx", ".tsx", ".json", ".py"}
        cand_matches = []
        for root, dirs, files in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", ".next", "__pycache__", "dist", "build")]
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext in search_exts:
                    fpath = os.path.join(root, fn)
                    try:
                        if os.path.getsize(fpath) < 150_000:
                            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                                content = f.read()
                                for kw in keywords:
                                    if kw and kw in content:
                                        rel_path = os.path.relpath(fpath, cwd)
                                        if rel_path not in seen:
                                            seen.add(rel_path)
                                            # Prioritize html files for UI / website prompts
                                            priority = 0 if ext in (".html", ".htm") else 1
                                            cand_matches.append((priority, rel_path, fpath, content))
                                        break
                    except Exception:
                        pass
            if len(cand_matches) >= 5:
                break

        cand_matches.sort(key=lambda x: (x[0], len(x[1])))
        for _, rel_path, fpath, content in cand_matches[:3]:
            found.append((rel_path, fpath, content))

    return found


LANG_EXT_MAP: Dict[str, set] = {
    "js": {"js", "javascript"},
    "ts": {"ts", "typescript"},
    "jsx": {"jsx", "javascript"},
    "tsx": {"tsx", "typescript"},
    "py": {"py", "python"},
    "txt": {"txt", "text"},
    "html": {"html"},
    "css": {"css"},
    "json": {"json"},
    "md": {"md", "markdown"},
    "sh": {"sh", "bash"},
    "bat": {"bat", "batch"},
    "ps1": {"ps1", "powershell"},
    "yaml": {"yaml", "yml"},
    "yml": {"yaml", "yml"},
    "sql": {"sql"},
    "c": {"c"},
    "cpp": {"cpp", "c++"},
    "cs": {"cs", "c#"},
    "rs": {"rs", "rust"},
    "go": {"go", "golang"},
    "java": {"java"},
}


def is_valid_card_ending(fname: str, tag: str) -> bool:
    """Check if the tag matches the filename extension."""
    if "." not in fname:
        return False
    ext = fname.split(".")[-1].lower()
    tag_l = tag.lower()
    return tag_l == ext or tag_l in LANG_EXT_MAP.get(ext, set())


def is_code_line(line: str) -> bool:
    """Check if a line looks like source code rather than prose."""
    s = line.strip()
    if not s:
        return False
    code_starts = (
        "//", "/*", "*", "#", "import ", "from ", "export ", "const ", "let ", "var ",
        "function ", "def ", "class ", "return ", "if ", "for ", "while ", "try ",
        "catch ", "{", "}", "[", "]", "<", "package ", "use ", "public ", "private "
    )
    return any(s.startswith(p) for p in code_starts)


def is_workspace_promise(text: str) -> bool:
    """Detect if Muse is hallucinating an internal ~/workspace container or background runner without emitting code."""
    t = text.lower()
    has_workspace = bool(
        "~/workspace" in t
        or "workspace/" in t
        or re.search(r"\b(?:in|into|inside|from|at)\s+(?:my\s+)?workspace\b", t)
        or re.search(r"\b(?:building it now|still building|i'll report back|will report back|built and verified)\b", t)
    )
    if not has_workspace:
        return False
    # If the response already contains code blocks or file cards, it's not an empty promise
    _, files = parse_cards_and_fences(text)
    return len(files) == 0


def parse_cards_and_fences(block: str) -> Tuple[str, List[Tuple[str, str]]]:
    """
    Extract generated files from a response block, supporting:
    1. Single or multiple Muse native file cards:
       - Top-headed: <filename>\n<TAG>\n<code>
       - Bottom-tagged: <code>\n<filename>\n<TAG>
    2. Markdown code fences with filenames in header, info string, or first line comment.
    Returns: (thought_commentary, [(filename, content), ...])
    """
    found_files: List[Tuple[str, str]] = []
    thoughts: List[str] = []
    lines = block.splitlines()

    # Find all (i, filename, tag) occurrences
    matches = []
    for i in range(len(lines) - 1):
        l1 = lines[i].strip()
        l2 = lines[i + 1].strip()
        m = re.match(r"^([a-zA-Z0-9_.\-\\/]+\.([a-zA-Z0-9_]+))$", l1)
        if m and is_valid_card_ending(l1, l2):
            matches.append((i, l1, l2))

    if matches:
        first_i, first_fname, first_tag = matches[0]
        after_code = False
        if first_i + 2 < len(lines):
            for j in range(first_i + 2, min(len(lines), first_i + 6)):
                if is_code_line(lines[j]):
                    after_code = True
                    break

        if after_code:
            # Case A: Filename and TAG are at the TOP of code
            intro_lines = lines[:first_i]
            clean_intro = []
            for l in intro_lines:
                if not re.match(r"^[a-zA-Z0-9_.\-\\/]+\.[a-zA-Z0-9_]+$", l.strip()):
                    clean_intro.append(l)
            if clean_intro:
                thoughts.append("\n".join(clean_intro).strip())

            raw_code = lines[first_i + 2:]
            idx = len(raw_code) - 1
            while idx >= 0 and not is_code_line(raw_code[idx]):
                idx -= 1
            final_code = raw_code[:idx + 1]
            trailing = raw_code[idx + 1:]

            if trailing:
                trailing_txt = "\n".join(trailing).strip()
                if trailing_txt:
                    thoughts.append(trailing_txt)

            found_files.append((first_fname, "\n".join(final_code).strip()))
        else:
            # Case B: Filename and TAG are at the BOTTOM of code
            prev_idx = 0
            for card_idx, fname, tag in matches:
                content_lines = lines[prev_idx:card_idx]
                content = "\n".join(content_lines).strip()
                parts = re.split(r"\n{2,}", content)
                if len(parts) > 1 and not is_code_line(parts[0]):
                    intro = parts[0].strip()
                    if intro:
                        thoughts.append(intro)
                    clean_content = "\n\n".join(parts[1:]).strip()
                else:
                    clean_content = content
                found_files.append((fname, clean_content))
                prev_idx = card_idx + 2

            if prev_idx < len(lines):
                trailing = "\n".join(lines[prev_idx:]).strip()
                if trailing:
                    thoughts.append(trailing)

    # 2. If no native cards, check markdown code fences
    if not found_files:
        raw_fences = list(re.finditer(r"```([a-zA-Z0-9_+\-]*)(?::([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+))?\n+(.*?)\n+```", block, re.DOTALL))
        for m in raw_fences:
            lang = m.group(1)
            fn_in_fence_tag = m.group(2)
            content = m.group(3)

            pre_text = block[:m.start()]
            pre_lines = [l.strip() for l in pre_text.splitlines() if l.strip()]
            last_pre_line = pre_lines[-1] if pre_lines else ""

            fname = fn_in_fence_tag
            if not fname and last_pre_line:
                m_fn = re.search(r"[`\"']?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)[`\"']?", last_pre_line)
                if m_fn:
                    cand = m_fn.group(1)
                    if "." in cand and len(cand.split(".")[-1]) <= 6:
                        fname = cand

            if not fname:
                first_line = content.splitlines()[0].strip() if content.splitlines() else ""
                m_first = re.match(r"^(?:#|//|/\*|<!--)\s*(?:filename:?\s*)?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)", first_line, re.IGNORECASE)
                if m_first:
                    fname = m_first.group(1)
                    content = "\n".join(content.splitlines()[1:]).strip()

            if fname and fname not in [f[0] for f in found_files]:
                found_files.append((fname, content))

        if found_files:
            cleaned = re.sub(r"```[a-zA-Z0-9_+\-]*.*?\n+.*?\n+```", "", block, flags=re.DOTALL).strip()
            if cleaned:
                thoughts.append(cleaned)

    thought_str = "\n\n".join(thoughts).strip()
    return thought_str, found_files


def parse_single_card(block: str) -> Optional[Tuple[str, str]]:
    """Legacy helper: returns the first card or None."""
    _, files = parse_cards_and_fences(block)
    return files[0] if files else None


def _detect_tool_calls(reply: str, tool_names: set, cwd: str) -> Tuple[str, List[Tuple[str, dict]]]:
    """
    Detect if Muse's reply represents actions to execute on the local computer
    (e.g., creating files, executing commands, reading files), or if Muse inspected
    its remote cloud container and needs to be redirected to the local PC.
    Returns: (thought_text, [(tool_name, tool_input_dict), ...])
    """
    clean_reply = reply.strip()
    tools: List[Tuple[str, dict]] = []
    thoughts: List[str] = []

    # 0a. Check if Muse reported that a specific file is not in its cloud workspace
    m_no_file = re.search(
        r"there'?s no\s+([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)\s+in (?:my )?workspace",
        clean_reply,
        flags=re.IGNORECASE,
    )
    if m_no_file:
        missing_name = m_no_file.group(1).strip()
        local_path = os.path.normpath(os.path.join(cwd, missing_name))
        if os.path.isfile(local_path) and ("Read" in tool_names or "FileRead" in tool_names):
            t_name = "Read" if "Read" in tool_names else "FileRead"
            return f"Reading `{missing_name}` from your computer.", [(t_name, {"file_path": local_path})]

    # 0b. Check if Muse reported its own remote server workspace directories
    cloud_workspace_indicators = [
        "banana.txt", "cherry.txt", "apple.txt",
        "top of my workspace", "my workspace", "in my workspace", "of my workspace",
        "my own workspace", "skipping internal system folders", "internal system folders",
        "cron.d", "onboarding_tour", "self_improvement", "spaces/", "your_files",
        "in workspace:", "not on your local pc"
    ]
    reply_lower = clean_reply.lower()
    if any(ind in reply_lower for ind in cloud_workspace_indicators):
        if "Glob" in tool_names:
            return "Checking the actual files in your local working directory on your computer.", [("Glob", {"pattern": "*"})]
        elif "PowerShell" in tool_names:
            return "Listing files in your local folder.", [("PowerShell", {"command": "Get-ChildItem -Name"})]
        elif "Bash" in tool_names:
            return "Listing files in your local folder.", [("Bash", {"command": "ls"})]
        else:
            return _format_file_list(list_local_files(cwd), cwd), []

    # 0c. Check if Muse states it cannot read a file or needs to read a local file
    m_cant_read = re.search(
        r"(?:can't|cannot|unable to)\s+(?:open|read|access|be sure about|tell)\s+.*?`?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)`?|`?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)`?\s+(?:I\s+)?(?:can't|cannot)\s+be\s+sure\s+about\s+without\s+reading|paste\s+(?:its|the)\s+contents?\s+(?:of\s+)?`?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)`?",
        clean_reply,
        flags=re.IGNORECASE,
    )
    if m_cant_read:
        fname = (m_cant_read.group(1) or m_cant_read.group(2) or m_cant_read.group(3) or "").strip()
        if fname:
            local_path = fname if os.path.isabs(fname) else os.path.normpath(os.path.join(cwd, fname))
            if os.path.isfile(local_path) and ("Read" in tool_names or "FileRead" in tool_names):
                t_name = "Read" if "Read" in tool_names else "FileRead"
                return f"Reading `{fname}` on your computer to inspect its contents.", [(t_name, {"file_path": local_path})]

    # Split into bubbles / blocks
    if "<!-- bubble -->" in clean_reply:
        blocks = [b.strip() for b in clean_reply.split("<!-- bubble -->") if b.strip()]
    else:
        blocks = [b.strip() for b in re.split(r"\n{2,}", clean_reply) if b.strip()]

    write_tool_name = "Write" if "Write" in tool_names else ("FileWrite" if "FileWrite" in tool_names else None)

    for block in blocks:
        # Check explicit tags: [TOOL: ToolName {...}]
        m_tags = list(re.finditer(r"\[(?:TOOL|TOOL_USE|ACTION):\s*([a-zA-Z0-9_]+)\s*(\{.*?\})\s*\]", block, flags=re.DOTALL))
        if m_tags:
            for m_tag in m_tags:
                t_name = m_tag.group(1).strip()
                try:
                    t_input = json.loads(m_tag.group(2).strip())
                    if t_name in tool_names:
                        if "file_path" in t_input and not os.path.isabs(t_input["file_path"]):
                            t_input["file_path"] = os.path.normpath(os.path.join(cwd, t_input["file_path"]))
                        tools.append((t_name, t_input))
                except Exception:
                    pass
            th = block[:m_tags[0].start()].strip()
            if th:
                thoughts.append(th)
            continue

        # Check tool markdown block: ```tool:ToolName ... ```
        m_blocks = list(re.finditer(r"```tool:([a-zA-Z0-9_]+)\n+(.*?)\n+```", block, flags=re.DOTALL))
        if m_blocks:
            for mb in m_blocks:
                t_name = mb.group(1).strip()
                try:
                    t_input = json.loads(mb.group(2).strip())
                    if t_name in tool_names:
                        if "file_path" in t_input and not os.path.isabs(t_input["file_path"]):
                            t_input["file_path"] = os.path.normpath(os.path.join(cwd, t_input["file_path"]))
                        tools.append((t_name, t_input))
                except Exception:
                    pass
            th = block[:m_blocks[0].start()].strip()
            if th:
                thoughts.append(th)
            continue

        # Check bash/powershell code fence
        m_cmd = re.search(r"```(?:bash|sh|powershell|cmd|shell)\n+(.*?)\n+```", block, flags=re.DOTALL)
        if m_cmd and ("Bash" in tool_names or "PowerShell" in tool_names):
            cmd = m_cmd.group(1).strip()
            t_name = "PowerShell" if "PowerShell" in tool_names else "Bash"
            tools.append((t_name, {"command": cmd}))
            th = block[:m_cmd.start()].strip()
            if th:
                thoughts.append(th)
            continue

        # Check Muse native file cards and markdown code fences
        block_thought, found_files = parse_cards_and_fences(block)
        if found_files and write_tool_name:
            if block_thought:
                thoughts.append(block_thought)
            for fname, content in found_files:
                abs_path = fname if os.path.isabs(fname) else os.path.normpath(os.path.join(cwd, fname))
                tools.append((write_tool_name, {"file_path": abs_path, "content": content}))
            continue

        # Check if Muse text states it deleted or wants to delete existing files in cwd
        m_del = re.findall(r"\b(?:delete|deleted|remove|removed|deleting|removing|borrar|borrado|eliminar|eliminado)\s+[`\"]?([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_]+)[`\"]?", block, flags=re.IGNORECASE)
        if m_del and ("PowerShell" in tool_names or "Bash" in tool_names):
            del_paths = []
            for fn in m_del:
                full = os.path.normpath(os.path.join(cwd, fn))
                if (os.path.isfile(full) or os.path.isdir(full)) and full not in del_paths:
                    del_paths.append(full)
            if del_paths:
                cmd_tool = "PowerShell" if "PowerShell" in tool_names else "Bash"
                if cmd_tool == "PowerShell":
                    paths_str = ", ".join(f"'{p}'" for p in del_paths)
                    del_cmd = f"Remove-Item -Path {paths_str} -Recurse -Force"
                else:
                    paths_str = " ".join(f"'{p}'" for p in del_paths)
                    del_cmd = f"rm -rf {paths_str}"
                tools.append((cmd_tool, {"command": del_cmd}))
                continue

        # Normal text block (thought / commentary)
        thoughts.append(block)

    thought_str = "\n\n".join(thoughts).strip()
    if not thought_str and tools:
        if len(tools) == 1:
            thought_str = f"Performing {tools[0][0]} on your computer."
        else:
            thought_str = f"Performing {len(tools)} actions on your computer."

    return thought_str, tools


def run_daemon_server(mgr: BrowserManager, port: int) -> None:
    class BridgeHTTPHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write(f"[HTTP] {self.address_string()} - {fmt % args}\n")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

        def _send_json(self, status: int, data: Any):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

        def _send_sse(self, event: str, data: dict):
            line = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
            try:
                self.wfile.write(line)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

        def do_GET(self):
            clean_path = self.path.split("?")[0].rstrip("/")
            if clean_path == "/health":
                self._send_json(200, {"status": "ok", "url": mgr.url or ""})
            elif clean_path == "/chat_url":
                self._send_json(200, {"url": mgr.url or mgr.chat_url})
            elif clean_path.startswith("/read"):
                limit = 10
                if "limit=" in self.path:
                    try:
                        limit = int(self.path.split("limit=")[1].split("&")[0])
                    except ValueError:
                        pass
                msgs = mgr.get_messages(limit=limit)
                self._send_json(200, {"status": "ok", "messages": msgs})
            elif clean_path in ("/v1/models", "/models", "/api/v1/models", "/api/models"):
                models_list = [
                    {"id": "muse", "type": "model", "object": "model", "display_name": "Muse AI"},
                    {"id": "stealth/union-alpha", "type": "model", "object": "model", "display_name": "Stealth Union Alpha"},
                    {"id": "claude-3-7-sonnet", "type": "model", "object": "model", "display_name": "Claude 3.7 Sonnet"},
                    {"id": "claude-3-5-sonnet-20241022", "type": "model", "object": "model", "display_name": "Claude 3.5 Sonnet"},
                    {"id": "claude-3-5-haiku-20241022", "type": "model", "object": "model", "display_name": "Claude 3.5 Haiku"},
                ]
                self._send_json(200, {"data": models_list})
            else:
                self._send_json(404, {"error": "Not found"})

        def do_POST(self):
            clean_path = self.path.split("?")[0].rstrip("/")
            content_len = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            try:
                payload = json.loads(raw_body)
            except Exception:
                payload = {}

            if clean_path == "/chat_url":
                new_url = payload.get("url") or payload.get("chat_url", "")
                if not new_url:
                    self._send_json(400, {"status": "error", "error": "Missing 'url' parameter"})
                    return
                try:
                    final_url = mgr.navigate(new_url)
                    self._send_json(200, {"status": "ok", "url": final_url})
                except Exception as e:
                    self._send_json(500, {"status": "error", "error": str(e)})
                return
            elif clean_path == "/reset":
                try:
                    final_url = mgr.navigate("https://muse.ai/")
                    self._send_json(200, {"status": "ok", "url": final_url})
                except Exception as e:
                    self._send_json(500, {"status": "error", "error": str(e)})
                return
            elif clean_path == "/send":
                text = payload.get("text") or payload.get("prompt", "")
                if not text:
                    self._send_json(400, {"status": "error", "error": "Missing 'text' parameter"})
                    return
                timeout_s = payload.get("timeout", DEFAULT_TIMEOUT)
                try:
                    reply, extracted = mgr.send_prompt(text, timeout_s=timeout_s, cwd=os.getcwd())
                    self._send_json(200, {"status": "ok", "reply": reply, "extracted_files": extracted})
                except Exception as e:
                    self._send_json(500, {"status": "error", "error": str(e)})

            elif clean_path in ("/v1/messages/count_tokens", "/messages/count_tokens", "/api/v1/messages/count_tokens"):
                self._send_json(200, {"input_tokens": 50})

            elif clean_path in ("/v1/messages", "/messages", "/api/v1/messages", "/api/messages"):
                # Anthropic Messages API (Claude Code native endpoint!)
                messages = payload.get("messages", [])
                tools = payload.get("tools", [])
                tool_names = {t.get("name") for t in tools if isinstance(t, dict)}
                model_name = payload.get("model", "claude-3-5-sonnet-20241022")
                is_stream = payload.get("stream", False)
                msg_id = f"msg_muse_{int(time.time())}"

                # Extract working directory from environment/system prompt
                cwd = os.getcwd()
                sys_val = payload.get("system") or ""
                if isinstance(sys_val, list):
                    sys_val = "\n".join(b.get("text", "") for b in sys_val if isinstance(b, dict))
                sys_txts = [str(sys_val)]
                for m in messages:
                    if m.get("role") == "system":
                        c = m.get("content", "")
                        if isinstance(c, list):
                            sys_txts.append("\n".join(b.get("text", "") for b in c if isinstance(b, dict)))
                        else:
                            sys_txts.append(str(c))
                combined_sys = "\n".join(sys_txts)
                m_cwd = re.search(r"Primary working directory:\s*([^\n\r]+)", combined_sys)
                if m_cwd:
                    cand_cwd = m_cwd.group(1).strip().strip("\"'")
                    if os.path.isdir(cand_cwd):
                        cwd = cand_cwd

                # Check if this is a follow-up with tool_result
                is_tool_result = False
                prev_tool_outputs: List[str] = []
                executed_tool_names: List[str] = []
                last_user_msg = ""

                last_user_msg_obj = None
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user_msg_obj = m
                        break  # CRITICAL: Only inspect the MOST RECENT user message!

                if last_user_msg_obj:
                    c = last_user_msg_obj.get("content", "")
                    if isinstance(c, list):
                        parts = []
                        for block in c:
                            if isinstance(block, dict):
                                if block.get("type") == "tool_result":
                                    is_tool_result = True
                                    rc = block.get("content", "")
                                    if isinstance(rc, list):
                                        rc = "\n".join(rb.get("text", "") for rb in rc if isinstance(rb, dict))
                                    prev_tool_outputs.append(str(rc))
                                elif block.get("type") == "text":
                                    parts.append(block.get("text", ""))
                        if not is_tool_result:
                            last_user_msg = "\n".join(parts)
                    else:
                        last_user_msg = str(c)

                if "[Request interrupted by user]" in last_user_msg:
                    last_user_msg = last_user_msg.split("[Request interrupted by user]")[-1].strip()

                prev_tool_output = "\n\n".join(prev_tool_outputs)

                if is_tool_result:
                    for m in reversed(messages):
                        if m.get("role") == "assistant":
                            c = m.get("content", [])
                            if isinstance(c, list):
                                for b in c:
                                    if isinstance(b, dict) and b.get("type") == "tool_use":
                                        executed_tool_names.append(b.get("name", ""))
                            if executed_tool_names:
                                break
                    last_tool_name = executed_tool_names[-1] if executed_tool_names else ""

                if not last_user_msg and not is_tool_result:
                    self._send_json(400, {"error": "No user message found"})
                    return

                # CRITICAL FIX: Claude Code sends background [SUGGESTION MODE: ...] requests
                # to predict the user's next CLI keystroke. If sent to Muse, Muse stays silent,
                # blocking the daemon for 240s and stalling Claude Code with 'Pontificating...'.
                if "[SUGGESTION MODE:" in last_user_msg:
                    print("[*] Intercepted Claude Code SUGGESTION MODE request; returning empty response immediately.", file=sys.stderr, flush=True)
                    if is_stream:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "close")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()

                        self._send_sse("message_start", {
                            "type": "message_start",
                            "message": {
                                "id": msg_id,
                                "type": "message",
                                "role": "assistant",
                                "content": [],
                                "model": model_name,
                                "stop_reason": None,
                                "stop_sequence": None,
                                "usage": {"input_tokens": 10, "output_tokens": 0},
                            },
                        })
                        self._send_sse("content_block_start", {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        })
                        self._send_sse("content_block_stop", {"type": "content_block_stop", "index": 0})
                        self._send_sse("message_delta", {
                            "type": "message_delta",
                            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                            "usage": {"output_tokens": 0},
                        })
                        self._send_sse("message_stop", {"type": "message_stop"})
                        self.close_connection = True
                    else:
                        self._send_json(200, {
                            "id": msg_id,
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "text", "text": ""}],
                            "model": model_name,
                            "stop_reason": "end_turn",
                            "stop_sequence": None,
                            "usage": {"input_tokens": 10, "output_tokens": 0},
                        })
                    return

                stream_headers_sent = [False]

                def send_with_keepalive(prompt: str, cwd: str) -> Tuple[str, List[str]]:
                    """Send prompt to Muse with immediate SSE headers and periodic keepalive comments to prevent client timeout."""
                    if not is_stream or stream_headers_sent[0]:
                        return mgr.send_prompt(prompt, cwd=cwd)

                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()

                    self._send_sse("message_start", {
                        "type": "message_start",
                        "message": {
                            "id": msg_id,
                            "type": "message",
                            "role": "assistant",
                            "content": [],
                            "model": model_name,
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": len(last_user_msg.split()), "output_tokens": 1},
                        },
                    })
                    stream_headers_sent[0] = True

                    res_box: Dict[str, Any] = {}
                    def _call():
                        try:
                            r, ext = mgr.send_prompt(prompt, cwd=cwd)
                            res_box["reply"] = r
                            res_box["extracted"] = ext
                        except Exception as ex:
                            res_box["error"] = ex

                    wt = threading.Thread(target=_call, daemon=True)
                    wt.start()

                    while wt.is_alive():
                        wt.join(timeout=2.0)
                        if wt.is_alive():
                            try:
                                self.wfile.write(b": ping\n\n")
                                self.wfile.flush()
                            except Exception:
                                break

                    if "error" in res_box:
                        raise res_box["error"]
                    return res_box.get("reply", ""), res_box.get("extracted", [])

                clean_last_user_msg = strip_system_tags(last_user_msg) if last_user_msg else ""
                tools_to_emit: List[Tuple[str, dict]] = []
                thought = ""
                if is_tool_result:
                    if executed_tool_names and all(t in ("Write", "FileWrite") for t in executed_tool_names):
                        reply = "I have successfully created and updated the requested file(s) on your computer."
                    elif "Glob" in executed_tool_names:
                        cleaned_files = _clean_file_list(prev_tool_output)
                        orig_user_msg = ""
                        for m in messages:
                            if m.get("role") == "user":
                                c = m.get("content", "")
                                if isinstance(c, str):
                                    orig_user_msg = c
                                elif isinstance(c, list):
                                    txts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
                                    if txts:
                                        orig_user_msg = "\n".join(txts)

                        orig_user_msg = strip_system_tags(orig_user_msg)
                        if is_dump_all_contents_query(orig_user_msg):
                            reply = format_folder_contents(get_local_folder_contents(cwd), cwd)
                        elif is_pure_listing_query(orig_user_msg):
                            reply = _format_file_list(cleaned_files, cwd)
                        else:
                            file_list_str = "\n".join(f"- {f}" for f in cleaned_files)
                            prompt_to_muse = (
                                f"Here are the files in the project:\n"
                                f"{file_list_str}\n\n"
                                f"The user asks:\n{orig_user_msg}\n\n"
                                f"Please inspect and analyze which files match the user's question and explain clearly:"
                            )
                            try:
                                reply, _ = send_with_keepalive(prompt_to_muse, cwd=cwd)
                            except Exception as e:
                                reply = f"Here are the files in the project:\n\n{file_list_str}\n\n(Error analyzing with model: {e})"
                    elif "Read" in executed_tool_names or "FileRead" in executed_tool_names:
                        orig_user_msg = ""
                        for m in messages:
                            if m.get("role") == "user":
                                c = m.get("content", "")
                                if isinstance(c, str):
                                    orig_user_msg = c
                                elif isinstance(c, list):
                                    txts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
                                    if txts:
                                        orig_user_msg = "\n".join(txts)

                        orig_user_msg = strip_system_tags(orig_user_msg)
                        is_pure_dump = bool(re.search(r"^(?:read|cat|view|show(?:\s+me)?)\s+[`\"]?[a-zA-Z0-9_\-\./\\]+[`\"]?\s*$", orig_user_msg, flags=re.IGNORECASE))
                        if is_pure_dump or not orig_user_msg:
                            reply = f"Contents of the file:\n\n```\n{prev_tool_output.strip()}\n```"
                        else:
                            prompt_to_muse = (
                                f"Here is the content of the file from the user's PC:\n\n"
                                f"```\n{prev_tool_output.strip()}\n```\n\n"
                                f"The user asked:\n{orig_user_msg}\n\n"
                                f"Please inspect the file content and provide a thorough, accurate answer:"
                            )
                            try:
                                reply, _ = send_with_keepalive(prompt_to_muse, cwd=cwd)
                            except Exception as e:
                                reply = f"Contents of the file:\n\n```\n{prev_tool_output.strip()}\n```\n\n(Error analyzing with model: {e})"
                    elif executed_tool_names and all(t in ("PowerShell", "Bash") for t in executed_tool_names):
                        prev_cmds = []
                        for m in reversed(messages):
                            if m.get("role") == "assistant":
                                c = m.get("content", [])
                                if isinstance(c, list):
                                    for b in c:
                                        if isinstance(b, dict) and b.get("type") == "tool_use":
                                            prev_cmds.append(str(b.get("input", {}).get("command", "")).lower())
                                if prev_cmds:
                                    break
                        if any("remove-item" in cmd or "rm " in cmd for cmd in prev_cmds):
                            reply = "I have successfully deleted the requested file(s) from your computer."
                        else:
                            reply = f"Command output:\n\n```\n{prev_tool_output.strip()}\n```"
                    else:
                        reply = f"Completed action(s) on your computer:\n\n{prev_tool_output}"
                else:
                    # 0. Direct check: Is this a request to delete all files in current folder?
                    if is_delete_all_files_query(clean_last_user_msg):
                        del_call = detect_delete_intent(clean_last_user_msg, cwd, tool_names)
                        if del_call:
                            thought, t_name, t_input = del_call
                            tools_to_emit = [(t_name, t_input)]
                        else:
                            reply = f"No files were found in your local folder (`{cwd}`) to delete."
                    # 1. Direct check: Is this a request to dump/output all file contents in current folder?
                    elif is_dump_all_contents_query(clean_last_user_msg):
                        print(f"[*] Detected dump all files query; reading local folder {cwd}", file=sys.stderr, flush=True)
                        local_contents = get_local_folder_contents(cwd)
                        reply = format_folder_contents(local_contents, cwd)
                    # 2. Direct check: Is this a request to list files in current folder?
                    elif is_file_list_query(clean_last_user_msg):
                        print(f"[*] Detected list files query; listing local folder {cwd}", file=sys.stderr, flush=True)
                        cleaned_files = list_local_files(cwd)
                        reply = _format_file_list(cleaned_files, cwd)
                    else:
                        # Check first if user prompt is directly asking for a local computer / filesystem action
                        intent_call = _detect_intent_tool_call(clean_last_user_msg, tool_names, cwd)
                        if intent_call:
                            thought, t_name, t_input = intent_call
                            tools_to_emit = [(t_name, t_input)]
                        else:
                            # Check if any local files on user's PC are referenced in the prompt!
                            ref_files = find_referenced_local_files(clean_last_user_msg, cwd)
                            is_edit_request = bool(
                                re.search(
                                    r"\b(?:add|insert|edit|modify|update|solve|change|replace|overwrite|fix|patch|write\s+(?:the\s+)?solution)\b",
                                    clean_last_user_msg,
                                    flags=re.IGNORECASE,
                                )
                            )

                            prompt_to_send = clean_last_user_msg
                            target_file_info = None
                            if ref_files:
                                file_context_parts = []
                                for fname, fpath, fcontent in ref_files:
                                    file_context_parts.append(f"[File: {fname} on user's PC]\n{fcontent.strip()}")
                                file_context_str = "\n\n".join(file_context_parts)

                                if is_edit_request and ("Write" in tool_names or "FileWrite" in tool_names or "Edit" in tool_names):
                                    target_file_info = ref_files[0]
                                    prompt_to_send = (
                                        f"{clean_last_user_msg}\n\n"
                                        f"Here is the current content of the file from the user's PC:\n"
                                        f"{file_context_str}\n\n"
                                        f"Output ONLY the full updated content for `{target_file_info[0]}` with the solution or edit applied (with no conversational filler, or wrap in a code block):"
                                    )
                                else:
                                    prompt_to_send = (
                                        f"{clean_last_user_msg}\n\n"
                                        f"Here is the content of the referenced file from the user's PC:\n"
                                        f"{file_context_str}"
                                    )
                            elif is_folder_analysis_query(clean_last_user_msg):
                                local_contents = get_local_folder_contents(cwd)
                                if local_contents:
                                    file_context_parts = [f"[File: {fname} on user's PC]\n{fcontent.strip()}" for fname, fcontent in local_contents.items()]
                                    file_context_str = "\n\n".join(file_context_parts)
                                    prompt_to_send = (
                                        f"{clean_last_user_msg}\n\n"
                                        f"Here are the files and their contents from the user's local PC (`{cwd}`):\n\n"
                                        f"{file_context_str}\n\n"
                                        f"Please analyze the user's local files above to answer their question:"
                                    )
                            elif re.search(r"\b(?:create|make|write|generate|build|implement|develop|platform|architecture|code|deliverable|deliverables|spec|specification|transfer|save|disk|export)\b", clean_last_user_msg, flags=re.IGNORECASE):
                                prompt_to_send = (
                                    f"{clean_last_user_msg}\n\n"
                                    f"[MANDATORY HARNESS INSTRUCTION: You are driving an automated developer harness on the user's local computer. "
                                    f"You DO NOT have an internal ~/workspace container, background runner, or bash terminal. "
                                    f"Do NOT say 'building it now', 'going into ~/workspace', or that you will report back later. "
                                    f"You MUST output the complete, functional code for the requested files directly in this response right now. "
                                    f"Format each file with a clear filename header and code block (e.g. ### `filename.ext` followed by ```language ... ```) "
                                    f"so the harness can immediately create the files on the user's PC.]"
                                )

                            try:
                                reply, extracted_files = send_with_keepalive(prompt_to_send, cwd=cwd)
                            except Exception as e:
                                self._send_json(500, {"error": str(e)})
                                return

                            # Check if Muse hallucinated putting files into ~/workspace or background execution
                            for retry_attempt in range(2):
                                if not is_workspace_promise(reply) or extracted_files:
                                    break
                                print(f"[*] Detected workspace promise from Muse (attempt {retry_attempt + 1}/2); automatically re-prompting for direct code output...", file=sys.stderr, flush=True)
                                retry_prompt = (
                                    "I need the project on my local computer. Please package all files from your workspace as a downloadable archive (tar.gz or zip), "
                                    "or output the files with their filenames and complete code in markdown code blocks, so the automated harness can extract or save them to disk immediately."
                                )
                                try:
                                    retry_reply, retry_extracted = send_with_keepalive(retry_prompt, cwd=cwd)
                                    if retry_reply:
                                        reply = retry_reply
                                    if retry_extracted:
                                        extracted_files = retry_extracted
                                except Exception as ex:
                                    print(f"[!] Error on workspace promise retry: {ex}", file=sys.stderr, flush=True)
                                    break

                            if extracted_files:
                                file_list_preview = "\n".join(f"- `{f}`" for f in sorted(extracted_files[:25]))
                                more_count = len(extracted_files) - 25
                                more_suffix = f"\n- ... and {more_count} more files" if more_count > 0 else ""
                                thought = f"Extracted {len(extracted_files)} files directly into your workspace (`{cwd}`)."
                                reply = (
                                    f"I have built the project and automatically downloaded and extracted all {len(extracted_files)} files directly into your local workspace (`{cwd}`):\n\n"
                                    f"{file_list_preview}{more_suffix}\n\n"
                                    f"All files are saved to disk and ready to use."
                                )

                            thought, tools_to_emit = _detect_tool_calls(reply, tool_names, cwd)
                            if not tools_to_emit and is_edit_request and target_file_info and ("Write" in tool_names or "FileWrite" in tool_names):
                                clean_sol = reply.strip()
                                m_fence = re.search(r"^```(?:[a-zA-Z0-9_\-]+)?\n+(.*?)\n+```$", clean_sol, flags=re.DOTALL)
                                if m_fence:
                                    clean_sol = m_fence.group(1).strip()
                                t_name = "Write" if "Write" in tool_names else "FileWrite"
                                thought = f"Updating `{target_file_info[0]}` with the solution on your computer."
                                tools_to_emit = [(t_name, {"file_path": target_file_info[1], "content": clean_sol})]

                if tools_to_emit:
                    clean_thought = re.sub(r"<!-- bubble -->", "", thought).strip()
                    print(f"[*] Emitting {len(tools_to_emit)} tool call(s) to Claude Code: {[t[0] for t in tools_to_emit]}", file=sys.stderr, flush=True)

                    if is_stream:
                        if not stream_headers_sent[0]:
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                            self.send_header("Cache-Control", "no-cache")
                            self.send_header("Connection", "close")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.end_headers()

                            self._send_sse("message_start", {
                                "type": "message_start",
                                "message": {
                                    "id": msg_id,
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [],
                                    "model": model_name,
                                    "stop_reason": None,
                                    "stop_sequence": None,
                                    "usage": {"input_tokens": len(last_user_msg.split()), "output_tokens": 1},
                                },
                            })
                            stream_headers_sent[0] = True

                        idx = 0
                        if clean_thought:
                            self._send_sse("content_block_start", {
                                "type": "content_block_start",
                                "index": idx,
                                "content_block": {"type": "text", "text": ""},
                            })
                            self._send_sse("content_block_delta", {
                                "type": "content_block_delta",
                                "index": idx,
                                "delta": {"type": "text_delta", "text": clean_thought + "\n\n"},
                            })
                            self._send_sse("content_block_stop", {"type": "content_block_stop", "index": idx})
                            idx += 1

                        total_out = 0
                        for i, (t_name, t_input) in enumerate(tools_to_emit):
                            tool_id = f"toolu_{int(time.time())}_{i}"
                            self._send_sse("content_block_start", {
                                "type": "content_block_start",
                                "index": idx,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": tool_id,
                                    "name": t_name,
                                    "input": {},
                                },
                            })
                            raw_in = json.dumps(t_input, ensure_ascii=False)
                            total_out += len(raw_in) // 4 + 10
                            self._send_sse("content_block_delta", {
                                "type": "content_block_delta",
                                "index": idx,
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": raw_in,
                                },
                            })
                            self._send_sse("content_block_stop", {"type": "content_block_stop", "index": idx})
                            idx += 1

                        self._send_sse("message_delta", {
                            "type": "message_delta",
                            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                            "usage": {"output_tokens": total_out},
                        })
                        self._send_sse("message_stop", {"type": "message_stop"})
                        self.close_connection = True
                    else:
                        content_blocks = []
                        if clean_thought:
                            content_blocks.append({"type": "text", "text": clean_thought})
                        total_out = 0
                        for i, (t_name, t_input) in enumerate(tools_to_emit):
                            tool_id = f"toolu_{int(time.time())}_{i}"
                            content_blocks.append({
                                "type": "tool_use",
                                "id": tool_id,
                                "name": t_name,
                                "input": t_input,
                            })
                            total_out += len(json.dumps(t_input)) // 4 + 10

                        resp = {
                            "id": msg_id,
                            "type": "message",
                            "role": "assistant",
                            "content": content_blocks,
                            "model": model_name,
                            "stop_reason": "tool_use",
                            "stop_sequence": None,
                            "usage": {
                                "input_tokens": len(last_user_msg.split()),
                                "output_tokens": total_out,
                            },
                        }
                        self._send_json(200, resp)
                else:
                    clean_reply = re.sub(r"<!-- bubble -->", "", reply).strip()
                    if is_stream:
                        if not stream_headers_sent[0]:
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                            self.send_header("Cache-Control", "no-cache")
                            self.send_header("Connection", "close")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.end_headers()

                            self._send_sse("message_start", {
                                "type": "message_start",
                                "message": {
                                    "id": msg_id,
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [],
                                    "model": model_name,
                                    "stop_reason": None,
                                    "stop_sequence": None,
                                    "usage": {"input_tokens": len(last_user_msg.split()), "output_tokens": 1},
                                },
                            })
                            stream_headers_sent[0] = True

                        self._send_sse("content_block_start", {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        })

                        chunk_size = 40
                        for i in range(0, len(clean_reply), chunk_size):
                            chunk = clean_reply[i:i + chunk_size]
                            self._send_sse("content_block_delta", {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {"type": "text_delta", "text": chunk},
                            })
                            time.sleep(0.01)

                        self._send_sse("content_block_stop", {
                            "type": "content_block_stop",
                            "index": 0,
                        })

                        self._send_sse("message_delta", {
                            "type": "message_delta",
                            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                            "usage": {"output_tokens": len(clean_reply.split())},
                        })

                        self._send_sse("message_stop", {
                            "type": "message_stop",
                        })
                        self.close_connection = True
                    else:
                        resp = {
                            "id": msg_id,
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": clean_reply,
                                }
                            ],
                            "model": model_name,
                            "stop_reason": "end_turn",
                            "stop_sequence": None,
                            "usage": {
                                "input_tokens": len(last_user_msg.split()),
                                "output_tokens": len(clean_reply.split()),
                            },
                        }
                        self._send_json(200, resp)

            elif self.path in ("/v1/chat/completions", "/chat/completions"):
                # OpenAI Compatible Chat Endpoint
                messages = payload.get("messages", [])
                last_user_msg = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user_msg = m.get("content", "")
                        break
                if not last_user_msg and messages:
                    last_user_msg = messages[-1].get("content", "")

                if not last_user_msg:
                    self._send_json(400, {"error": "No user message found"})
                    return

                try:
                    reply = mgr.send_prompt(last_user_msg)
                    resp = {
                        "id": f"chatcmpl-muse-{int(time.time())}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": payload.get("model", "muse"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": reply,
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": len(last_user_msg.split()),
                            "completion_tokens": len(reply.split()),
                            "total_tokens": len(last_user_msg.split()) + len(reply.split()),
                        },
                    }
                    self._send_json(200, resp)
                except Exception as e:
                    self._send_json(500, {"error": str(e)})
            else:
                self._send_json(404, {"error": "Endpoint not found"})

    server = ThreadingHTTPServer(("127.0.0.1", port), BridgeHTTPHandler)
    print(f"[Daemon] HTTP Server listening at http://127.0.0.1:{port}")
    print(f"  - Health check:            http://127.0.0.1:{port}/health")
    print(f"  - Anthropic API (Claude):  POST http://127.0.0.1:{port}/v1/messages")
    print(f"  - OpenAI API:              POST http://127.0.0.1:{port}/v1/chat/completions")
    print(f"  - Send endpoint:           POST http://127.0.0.1:{port}/send")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()


def cmd_serve(args: argparse.Namespace) -> None:
    cfg = load_config()
    chat_url = args.chat_url or _ensure_configured(cfg)
    port = args.port or cfg.get("port", DEFAULT_PORT)
    headless = not args.headful if args.headful else cfg.get("headless", True)

    mgr = BrowserManager(chat_url=chat_url, headless=headless)
    try:
        mgr.start()
        run_daemon_server(mgr, port)
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# MCP Server Mode (Model Context Protocol over stdio for Claude Code)
# ---------------------------------------------------------------------------

def cmd_mcp(args: argparse.Namespace) -> None:
    """Run an MCP server over stdio for Claude Code."""
    cfg = load_config()
    port = args.port or cfg.get("port", DEFAULT_PORT)

    def send_rpc(obj: dict):
        line = json.dumps(obj)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        req_id = req.get("id")
        method = req.get("method")

        if method == "initialize":
            send_rpc({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "muse-bridge", "version": "1.0.0"},
                },
            })
        elif method == "notifications/initialized":
            pass
        elif method == "ping":
            send_rpc({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "tools/list":
            send_rpc({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "ask_muse",
                            "description": "Send a question or task to Meta Muse AI (1 billion tokens) and receive its answer.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "prompt": {
                                        "type": "string",
                                        "description": "The message or prompt to send to Muse",
                                    }
                                },
                                "required": ["prompt"],
                            },
                        },
                        {
                            "name": "read_muse_chat",
                            "description": "Read recent chat messages from the Muse AI session.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "limit": {
                                        "type": "integer",
                                        "description": "Number of recent messages to return (default: 10)",
                                        "default": 10,
                                    }
                                },
                            },
                        },
                    ]
                },
            })
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            arguments = params.get("arguments", {})

            if name == "ask_muse":
                prompt = arguments.get("prompt", "")
                # Query daemon if running, else one-shot
                reply_text = ""
                daemon_res = _query_daemon("/send", {"text": prompt}, port=port)
                if daemon_res and daemon_res.get("status") == "ok":
                    reply_text = daemon_res.get("reply", "")
                else:
                    # One-shot fallback
                    try:
                        chat_url = _ensure_configured(cfg)
                        msg_sel = cfg.get("selectors", {}).get("message") or "[data-role=\"assistant\"], [data-role=\"user\"], div[class*=\"message\" i], article"
                        comp_sel = cfg.get("selectors", {}).get("composer") or "textarea, div[contenteditable=\"true\"], [role=\"textbox\"]"
                        send_sel = cfg.get("selectors", {}).get("send_button") or "button[type=\"submit\"], button[aria-label*=\"send\" i]"

                        with sync_playwright() as p:
                            browser, ctx = _launch_browser_and_context(p, cfg, headless=True, with_storage=True)
                            page = ctx.new_page()
                            page.goto(chat_url, timeout=45_000)
                            page.wait_for_selector(comp_sel, timeout=30_000)
                            n_before = len(_extract_messages(page, msg_sel))
                            _fill_composer(page, comp_sel, prompt)
                            _click_send(page, send_sel, comp_sel)
                            reply_text = _wait_for_reply(page, msg_sel, n_before)
                            browser.close()
                    except Exception as ex:
                        reply_text = f"Error communicating with Muse: {ex}"

                send_rpc({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": reply_text}],
                        "isError": False,
                    },
                })

            elif name == "read_muse_chat":
                limit = arguments.get("limit", 10)
                daemon_res = _query_daemon(f"/read?limit={limit}", port=port)
                if daemon_res and daemon_res.get("status") == "ok":
                    msgs = daemon_res.get("messages", [])
                else:
                    msgs = []
                send_rpc({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(msgs, indent=2)}],
                        "isError": False,
                    },
                })
            else:
                send_rpc({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {name}"},
                })


# ---------------------------------------------------------------------------
# CLI Argument Parser & Entry Point
# ---------------------------------------------------------------------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Unofficial Meta Muse AI bridge for Claude Code & local agents."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    # login
    p_login = sub.add_parser("login", help="Headed login & automatic selector discovery")
    p_login.add_argument("--url", default=None, help="Custom chat or login URL (default: https://muse.ai)")
    p_login.add_argument("--proxy", default=None, help="Proxy server e.g. http://127.0.0.1:1080 or socks5://...")
    p_login.add_argument("--cdp", default=None, help="Connect to running Chrome via CDP e.g. http://127.0.0.1:9222")
    p_login.set_defaults(fn=cmd_login)

    # send
    p_send = sub.add_parser("send", help="Send a prompt and return the assistant reply")
    p_send.add_argument("text", help="Message or prompt to send")
    p_send.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Seconds to wait for complete reply")
    p_send.add_argument("--json", action="store_true", help="Output reply as JSON")
    p_send.add_argument("--chat-url", default=None, help="Override chat URL")
    p_send.add_argument("--headful", action="store_true", help="Show browser window for debugging")
    p_send.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port of background daemon if active")
    p_send.add_argument("--proxy", default=None, help="Proxy server e.g. http://127.0.0.1:1080 or socks5://...")
    p_send.add_argument("--cdp", default=None, help="Connect to running Chrome via CDP e.g. http://127.0.0.1:9222")
    p_send.set_defaults(fn=cmd_send)

    # read
    p_read = sub.add_parser("read", help="Print recent chat messages as JSON")
    p_read.add_argument("--limit", type=int, default=10, help="Number of messages to fetch")
    p_read.add_argument("--chat-url", default=None, help="Override chat URL")
    p_read.add_argument("--headful", action="store_true", help="Show browser window")
    p_read.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port of background daemon if active")
    p_read.add_argument("--proxy", default=None, help="Proxy server")
    p_read.add_argument("--cdp", default=None, help="Connect via CDP")
    p_read.set_defaults(fn=cmd_read)

    # inspect
    p_inspect = sub.add_parser("inspect", help="Diagnose chat DOM selectors and capture a debug screenshot")
    p_inspect.add_argument("--chat-url", default=None, help="Override chat URL")
    p_inspect.add_argument("--headful", action="store_true", help="Show browser window")
    p_inspect.add_argument("--proxy", default=None, help="Proxy server")
    p_inspect.add_argument("--cdp", default=None, help="Connect via CDP")
    p_inspect.set_defaults(fn=cmd_inspect)

    # serve (background daemon)
    p_serve = sub.add_parser("serve", help="Run background daemon with fast API & OpenAI compatibility")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP port (default: {DEFAULT_PORT})")
    p_serve.add_argument("--chat-url", default=None, help="Override chat URL")
    p_serve.add_argument("--headful", action="store_true", help="Show browser window while running")
    p_serve.add_argument("--proxy", default=None, help="Proxy server")
    p_serve.add_argument("--cdp", default=None, help="Connect via CDP")
    p_serve.set_defaults(fn=cmd_serve)

    # launch-chrome
    p_chrome = sub.add_parser("launch-chrome", help="Launch Chrome with remote debugging enabled")
    p_chrome.add_argument("--port", type=int, default=9222, help="Debugging port (default: 9222)")
    p_chrome.add_argument("--url", default="https://muse.ai", help="Initial URL")
    p_chrome.set_defaults(fn=cmd_launch_chrome)

    # mcp (Model Context Protocol)
    p_mcp = sub.add_parser("mcp", help="Run as Model Context Protocol (MCP) server for Claude Code")
    p_mcp.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to query daemon from")
    p_mcp.set_defaults(fn=cmd_mcp)

    args = ap.parse_args(argv)
    args.fn(args)


def cmd_launch_chrome(args: argparse.Namespace) -> None:
    """Launch Google Chrome (or Edge) with remote debugging enabled on Windows."""
    import subprocess
    port = args.port or 9222
    url = args.url or "https://muse.ai"
    user_data = Path(os.environ.get("LOCALAPPDATA", ".")) / "Google" / "Chrome" / "User Data Debug"

    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    browser_exe = None
    for c in candidates:
        if os.path.exists(c):
            browser_exe = c
            break

    if not browser_exe:
        sys.exit("Error: Could not find Chrome or Edge executable on this machine.")

    cmd = [
        browser_exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        url,
    ]
    print(f"Launching browser with remote debugging on port {port}...")
    subprocess.Popen(cmd)
    print(f"\n[OK] Browser launched!")
    print(f"1. Log in to Muse and open your chat in the opened Chrome window.")
    print(f"2. Then run:")
    print(f"    python muse_bridge.py login --cdp http://127.0.0.1:{port}")


if __name__ == "__main__":
    main()
