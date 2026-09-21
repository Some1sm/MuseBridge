# Muse ↔ Claude Code Bridge

> Turn Meta's **Muse AI** (`muse.ai`, 1B token referral tier) into a complete, free, production-grade **Coding Agent Harness** for **Claude Code**, Cursor, and local coding tools via automated Playwright Chromium sessions.

---

## Table of Contents
- [Overview](#overview)
- [The 5 Harness Pillars](#the-5-harness-pillars)
- [Complete Step-by-Step Setup](#complete-step-by-step-setup)
  - [Step 1: Prerequisites & Installation](#step-1-prerequisites--installation)
  - [Step 2: One-Time Login & Selector Discovery](#step-2-one-time-login--selector-discovery)
  - [Step 3: Crucial! Configure Muse SOUL.md & MEMORY.md](#step-3-crucial-configure-muse-soulmd--memorymd)
  - [Step 4: Configure Claude Code to Use the Bridge](#step-4-configure-claude-code-to-use-the-bridge)
  - [Step 5: Start the Bridge Daemon](#step-5-start-the-bridge-daemon)
  - [Step 6: Launch Claude Code](#step-6-launch-claude-code)
- [How to Start a Fresh / New Chat Thread](#how-to-start-a-fresh--new-chat-thread)
- [CLI & Alternative Usage](#cli--alternative-usage)
  - [One-Shot CLI Prompting](#one-shot-cli-prompting)
  - [OpenAI-Compatible Endpoint (Cursor, Aider, Continue)](#openai-compatible-endpoint-cursor-aider-continue)
- [VPN & Location Guard (Spain / EU Access)](#vpn--location-guard-spain--eu-access)
- [Diagnostics & Troubleshooting](#diagnostics--troubleshooting)
- [Security](#security)

---

## Overview

Claude Code normally requires a paid Anthropic API key or subscription. This bridge acts as a local drop-in Anthropic Messages API server (`http://127.0.0.1:8765/v1/messages`). 

Whenever Claude Code sends a prompt, the bridge:
1. Translates the prompt and injects your real local workspace context.
2. Automates a headless Chromium session to query Muse AI on `muse.ai`.
3. Monitors Muse's active generation indicator (`...`) and handles streaming.
4. Intercepts file creation cards, code fences, deletions, reads, and commands, translating them into native Anthropic `tool_use` events for Claude Code.

---

## The 5 Harness Pillars

The bridge is designed as a complete coding harness supporting the entire software development lifecycle:

1. **GENERATE**:
   - Multi-file card & markdown fence extraction (`parse_cards_and_fences`).
   - Extracts all files emitted in a single turn without dropping secondary files or polluting source code with conversational chatter.
2. **DELETE**:
   - Supports direct deletion intents in English and Spanish (`delete the files in the folder`, `clear folder`, `borra temp.txt`, `elimina los archivos`, etc.).
   - Converts delete requests to PowerShell `Remove-Item -Path ... -Recurse -Force` on Windows or `rm -rf` on POSIX.
   - Automatically handles empty folders immediately to prevent empty tool call hangs.
3. **READ**:
   - Direct workspace inspection (`list files`, `dump all file contents`).
   - Detects referenced local files on your PC and injects their real contents into Muse's prompt for analysis.
4. **WRITE**:
   - Targeted file modification: reads existing disk files, prompts Muse for the solution, and emits a clean `Write` tool call with the updated code.
5. **EXECUTE**:
   - Shell command detection: recognizes execution prompts (`pytest`, `python script.py`, `npm test`, `git status`, `run ...`, `ejecuta ...`) and emits `PowerShell` / `Bash` tool calls.

---

## Complete Step-by-Step Setup

### Step 1: Prerequisites & Installation

Ensure you have **Python 3.10+** and **Git** installed.

1. Clone this repository:
   ```bash
   git clone https://github.com/Some1sm/MuseBridge.git
   cd MuseBridge
   ```

2. Install dependencies:
   ```bash
   pip install playwright
   playwright install chromium
   ```

3. Ensure Claude Code CLI is installed:
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

---

### Step 2: One-Time Login & Selector Discovery

Run the automated login setup:
```bash
python muse_bridge.py login
```

1. A Chromium browser window will open at `https://muse.ai`.
2. Log into your Muse / Meta account.
3. Open or start the chat thread you want to use.
4. Return to your terminal and press **ENTER**.

The bridge **automatically detects all DOM selectors** (chat composer, send button, message bubbles) and saves your authenticated session to `storage_state.json` and `bridge_config.json`. You do not need to inspect HTML manually.

---

### Step 3: Crucial! Configure Muse `SOUL.md` & `MEMORY.md`

> [!IMPORTANT]
> **Why this step is critical:**
> Muse AI has built-in persona files (`SOUL.md` and `MEMORY.md`) that it reads before every conversation. By default, Muse believes it is an autonomous agent operating inside a private cloud container (`~/workspace/`). If left unedited, Muse may hallucinate that it is "building in ~/workspace in the background" and fail to output code in the chat.
> 
> Editing these two files in Muse permanently aligns it as an AI coding engine paired with Claude Code.

In Muse's web interface, open your settings/files and update:

#### 1. `SOUL.md`
Copy and paste this into Muse's **`SOUL.md`**:

```markdown
# SOUL.md

You are the Lead Autonomous AI Coding Engine paired with Claude Code on the user's computer via MuseBridge.

You are not a chatbot; you are an elite systems architect and senior software engineer. Your responses directly drive an automated execution harness on the user's local machine.

### Operating Principles

1. **You Have No Private Cloud Sandbox**
   You do NOT have a private `~/workspace/` directory or a background terminal runner of your own. Never state "building it now in ~/workspace", "stored in workspace", or "reporting back later". Any file or command you intend to create must be delivered directly in your response so Claude Code can write or execute it on the user's machine immediately.

2. **Direct Code Delivery Protocol**
   When requested to create, edit, or generate code, deliver complete, unabridged, production-grade files. Never provide stubs, placeholders, `// TODO`, or truncated code.
   Always format each file using standard markdown code blocks with the file's path clearly indicated in the heading:
   ### `src/server.js`
   ```javascript
   // Full production implementation
   ```
   If multiple files are requested, output all files in logical order or structured batches without conversational filler.

3. **Tool & Command Execution Protocol**
   When you need to execute commands on the user's machine (tests, builds, installs, scripts), output clean, executable shell blocks:
   ```powershell
   npm test
   ```
   or
   ```bash
   npm run build
   ```
   Claude Code will execute them directly in the project directory on Windows.

4. **Resourcefulness & Context Alignment**
   Always inspect the provided project files, dependencies, and architecture before proposing changes. Match existing naming conventions, code style, and error handling idioms.

5. **No Filler, Pure Signal**
   Skip conversational pleasantries ("Certainly!", "I'd be happy to help!"). Begin immediately with the analysis, command, or file deliverables.
```

#### 2. `MEMORY.md`
Copy and paste this into Muse's **`MEMORY.md`**:

```markdown
<!-- Your curated long-term memory: durable facts, preferences, and commitments. -->

## Facts
- **Primary Interface**: Connected to the user's local terminal via Claude Code and the `MuseBridge` daemon (`http://127.0.0.1:8765`).
- **Target Environment**: Windows / macOS / Linux host with terminal execution; project directories located at your local workspace root (e.g. `C:\projects\...` or `~/projects/...`).
- **Execution Model**: Claude Code executes all file writes (`Write`), file reads (`Read`), file deletions (`PowerShell Remove-Item`), and commands (`PowerShell`/`Bash`) locally on the machine. Muse acts as the brain and code generator.
- **Delivery Mechanism**: Muse outputs files directly in chat responses using markdown blocks (`### filepath` + ```lang ... ```); MuseBridge parses these and emits `tool_use` actions to Claude Code.

## Preferences
- Bilingual: fully fluent in English and Spanish; responds in the language prompted.
- Complete implementations: zero placeholders, no omitted functions, no `// TODO` stubs.
- Direct code outputs: never defer to a remote cloud `~/workspace/` or background runner; always provide immediate code deliverables in the turn.
```

---

### Step 4: Configure Claude Code to Use the Bridge

You can configure Claude Code in two ways:

#### Option A: Using the Provider Switcher (Recommended)
Use `switch_provider.py` to point Claude Code to the bridge with one command:
```bash
python switch_provider.py muse
```
*(You can switch back to OpenRouter or native Anthropic anytime with `python switch_provider.py openrouter` or check status with `python switch_provider.py status`)*.

#### Option B: Environment Variables
Alternatively, set the standard Anthropic base URL in your terminal:

**PowerShell (Windows)**:
```powershell
$env:ANTHROPIC_BASE_URL="http://127.0.0.1:8765"
$env:ANTHROPIC_API_KEY="muse-bridge-local"
```

**Bash / Zsh (macOS/Linux)**:
```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8765"
export ANTHROPIC_API_KEY="muse-bridge-local"
```

---

### Step 5: Start the Bridge Daemon

Start the bridge server in your terminal:
```bash
python muse_bridge.py serve
```
*(On Windows, you can also double-click `start_muse_bridge.bat`)*.

The server will initialize headless Playwright, navigate to your chat thread, and begin listening for requests on:
```
http://127.0.0.1:8765
```

---

### Step 6: Launch Claude Code

In a new terminal window, navigate to your target project folder (e.g. `C:\projects\MyProject` or `~/my-project`) and run:
```bash
claude
```

You can now use Claude Code completely free!
- Ask it to generate full projects or multiple files: *"Create a full Express server in server.js and a package.json"*.
- Ask it to read local files: *"What does index.html do?"*.
- Ask it to edit or fix files: *"Add JWT authentication to auth.js"*.
- Ask it to delete files: *"Delete the temporary files in this folder"*.
- Ask it to run tests or commands: *"Run npm test"*.

---

---

## How to Start a Fresh / New Chat Thread

When your chat session in Muse gets long or cluttered, you can switch threads instantly without even restarting the daemon:

### Option 1: Live Switch via HTTP Endpoint (Instant, Zero Restart)
1. In Muse's web interface, click **New side chat** (or the **`+`** button).
2. Copy the new URL from your browser's address bar (e.g., `https://muse.ai/thread/YOUR-NEW-ID`).
3. Send a quick POST request to switch the daemon's active page:
   ```bash
   # Windows PowerShell
   Invoke-RestMethod -Uri "http://127.0.0.1:8765/chat_url" -Method Post -ContentType "application/json" -Body '{"url": "https://muse.ai/thread/YOUR-NEW-ID"}'

   # Bash / Curl
   curl -X POST http://127.0.0.1:8765/chat_url -H "Content-Type: application/json" -d '{"url": "https://muse.ai/thread/YOUR-NEW-ID"}'
   ```
   Or to reset to a brand new conversation at `https://muse.ai/`:
   ```bash
   curl -X POST http://127.0.0.1:8765/reset
   ```
   The browser navigates immediately and persists the new thread in `bridge_config.json`.

### Option 2: Update `bridge_config.json`
1. Open `bridge_config.json` in the `MuseAIBridge` folder:
   ```json
   {
     "chat_url": "https://muse.ai/thread/YOUR-NEW-ID",
     "port": 8765,
     "headless": true,
     "timeout": 600
   }
   ```
2. Restart the daemon (`python muse_bridge.py serve`).

---

## Automatic Project Dumping to Your Local Workspace

> **"If Muse creates a project, will it automatically dump it onto our workspace on the device?"**
> 
> **YES!** Here is how the automated harness works:

1. **Anti-Hallucination Guard**:
   By default, Muse may try to say *"I've built the project in ~/workspace/ and will report back."*
   MuseBridge intercepts this pattern immediately and automatically sends a high-priority harness re-prompt:
   *"I need the project on my local computer. Please package all files from your workspace as a downloadable archive (tar.gz or zip), or output the files with their filenames and complete code in markdown code blocks..."*
2. **Direct Disk Extraction**:
   - If Muse attaches a `.tar.gz` or `.zip` archive card, MuseBridge automatically downloads it in Chromium, uncompresses all files, and writes them directly into your current working directory (`cwd`).
   - If Muse outputs files using markdown fences or Muse native file cards (`### filename.ext` + ` ```lang ... ``` `), MuseBridge extracts each file and writes it to disk or emits Claude Code native `Write` tool calls.
3. **Keyword-Aware File Edits**:
   When you ask Claude Code to edit a component (e.g. *"from my local files can you please add a status indicator banner to index.html"*), the bridge automatically searches your local workspace files for matching terms, attaches the file context to Muse, and emits a targeted `Write` tool call to update the file on your disk.

---

## CLI & Alternative Usage

### One-Shot CLI Prompting
You can interact with Muse directly from your command line without Claude Code:
```bash
# Send a prompt and print the response
python muse_bridge.py send "Write a Python script to calculate Fibonacci numbers"

# Read the last 5 messages from the thread
python muse_bridge.py read --limit 5
```

### OpenAI-Compatible Endpoint (Cursor, Aider, Continue)
The bridge also exposes an OpenAI-compatible endpoint:
- **Base URL**: `http://127.0.0.1:8765/v1`
- **Model**: `muse`
- **Chat Endpoint**: `POST http://127.0.0.1:8765/v1/chat/completions`

Configure Cursor, Continue, or Aider to use `http://127.0.0.1:8765/v1` as the OpenAI Base URL with API Key `anything`.

---

## VPN & Location Guard (Spain / EU Access)

Muse restricts access in certain countries (including Spain). The bridge includes native circumvention:

1. **Built-in Location Spoofing**:
   The bridge automatically spoofs browser geolocation to New York (`40.7128, -74.0060`), sets timezone to `America/New_York`, and configures `en-US` locale. You do not need the Location Guard browser extension in Playwright.

2. **System-wide VPN**:
   If using a desktop VPN (e.g. ProtonVPN, Mullvad, NordVPN, WireGuard), simply connect to a US or supported server before running `login` or starting the daemon.

3. **Chrome DevTools Protocol (CDP)**:
   If you prefer using your existing desktop Chrome browser with active VPN extensions:
   ```bash
   # Launch Chrome with remote debugging
   chrome.exe --remote-debugging-port=9222
   ```
   Then attach the bridge:
   ```bash
   python muse_bridge.py login --cdp http://127.0.0.1:9222
   ```

---

## Diagnostics & Troubleshooting

- **Inspect DOM & Element Selectors**:
  ```bash
  python muse_bridge.py inspect
  ```
  Navigates to your configured chat, takes `debug_screenshot.png`, and prints detected selector details.

- **Session Expired / Logged Out**:
  If your cookies expire, simply re-run:
  ```bash
  python muse_bridge.py login
  ```

- **Viewing What Muse is Doing Live**:
  Set `"headless": false` in `bridge_config.json` or pass `--headful` to see the real Chromium browser window while it works:
  ```bash
  python muse_bridge.py serve --headful
  ```

- **Large Generation Timeouts**:
  The bridge defaults to a generous **600-second (10-minute) timeout** and automatically pauses completion checks while Muse's active thinking/loading dots (`...`) are animating.

---

## Security

- `storage_state.json` contains your active session cookies and is gitignored.
- `bridge_config.json` contains your private thread ID and local port settings.
- Do not commit or share these files.
