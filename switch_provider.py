#!/usr/bin/env python3
"""
switch_provider.py -- Quick utility to toggle Claude Code between Muse AI Bridge and OpenRouter.

Usage:
  python switch_provider.py muse         # Switch Claude Code to use local Muse AI Bridge
  python switch_provider.py openrouter   # Switch Claude Code back to OpenRouter
  python switch_provider.py status       # Show current Claude Code backend
"""

import sys
import json
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CLAUDE_DIR = Path.home() / ".claude"
SETTINGS_FILE = CLAUDE_DIR / "settings.json"
BACKUP_FILE = CLAUDE_DIR / "settings.json.backup"

MUSE_BASE_URL = "http://127.0.0.1:8765"
OPENROUTER_BASE_URL = "https://openrouter.ai/api"


def load_settings():
    if not SETTINGS_FILE.exists():
        sys.exit(f"Error: {SETTINGS_FILE} does not exist.")
    return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))


def save_settings(data):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_current():
    settings = load_settings()
    env = settings.get("env", {})
    url = env.get("ANTHROPIC_BASE_URL", "")
    if "8765" in url or "127.0.0.1" in url or "localhost" in url:
        return "muse", url
    elif "openrouter" in url:
        return "openrouter", url
    else:
        return "custom", url


def switch_to_muse():
    settings = load_settings()
    env = settings.setdefault("env", {})

    # Save backup of current settings if not already backed up
    if not BACKUP_FILE.exists():
        BACKUP_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        print(f"[OK] Created backup at {BACKUP_FILE}")

    env["ANTHROPIC_BASE_URL"] = MUSE_BASE_URL
    env["ANTHROPIC_AUTH_TOKEN"] = "muse-free-tokens"
    env["ANTHROPIC_API_KEY"] = "muse-free-tokens"
    
    save_settings(settings)
    print("\n" + "=" * 55)
    print("  CLAUDE CODE PROVIDER SWITCHED TO: MUSE AI")
    print("=" * 55)
    print(f"  • Base URL: {MUSE_BASE_URL}")
    print("  • Token balance: 1 Billion Muse Free Tokens")
    print("  • Make sure the daemon is running in a terminal:")
    print("      python muse_bridge.py serve")
    print("\n  Now you can simply run:")
    print("      claude\n")


def switch_to_openrouter():
    settings = load_settings()
    
    # Try restoring from backup if available
    if BACKUP_FILE.exists():
        backup_data = json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
        save_settings(backup_data)
        print("\n" + "=" * 55)
        print("  CLAUDE CODE PROVIDER RESTORED TO: OPENROUTER")
        print("=" * 55)
        print(f"  • Base URL: {backup_data.get('env', {}).get('ANTHROPIC_BASE_URL')}")
        print(f"  • Model:    {backup_data.get('model')}")
        print("\n  Now you can run Claude Code with your OpenRouter account:\n      claude\n")
        return

    env = settings.setdefault("env", {})
    env["ANTHROPIC_BASE_URL"] = OPENROUTER_BASE_URL
    openrouter_key = env.get("OPENROUTER_API_KEY", "")
    if openrouter_key:
        env["ANTHROPIC_AUTH_TOKEN"] = openrouter_key
    save_settings(settings)
    print("\n" + "=" * 55)
    print("  CLAUDE CODE PROVIDER SWITCHED TO: OPENROUTER")
    print("=" * 55)
    print(f"  • Base URL: {OPENROUTER_BASE_URL}\n")


def print_status():
    provider, url = get_current()
    print("\n" + "=" * 50)
    print("  CLAUDE CODE BACKEND STATUS")
    print("=" * 50)
    if provider == "muse":
        print("  ACTIVE: [OK] Muse AI Bridge (1 Billion Free Tokens)")
        print(f"  URL:    {url}")
    elif provider == "openrouter":
        print("  ACTIVE: [OK] OpenRouter")
        print(f"  URL:    {url}")
    else:
        print(f"  ACTIVE: Custom ({url})")
    print("=" * 50 + "\n")


def main():
    if len(sys.argv) < 2:
        print_status()
        print("To switch, run:")
        print("  python switch_provider.py muse")
        print("  python switch_provider.py openrouter\n")
        return

    cmd = sys.argv[1].lower()
    if cmd in ("muse", "museai", "local"):
        switch_to_muse()
    elif cmd in ("openrouter", "or"):
        switch_to_openrouter()
    elif cmd in ("status", "current"):
        print_status()
    else:
        print(f"Unknown option '{cmd}'. Use 'muse', 'openrouter', or 'status'.")


if __name__ == "__main__":
    main()
