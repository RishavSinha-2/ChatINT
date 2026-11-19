# ChatInt 🚀

A minimalist, multi-tab, multi-provider desktop chat interface built with **Python**, **pywebview** (Windows Edge WebView2), and native SDKs for **OpenAI**, **Anthropic**, and **Google Gemini**.

---

## ✨ Features

- **Native Desktop Application**: Standalone native window powered entirely by Python via `uv` (no Node.js or npm required).
- **Multi-Tab Workspace with Up to 5 Side-by-Side Split Panes**:
  - Open unlimited conversation tabs (`Ctrl+T` to open a tab, `Ctrl+W` to close).
  - Side-by-side **Split View (`Ctrl+\`)**: Open up to **5 tabs side-by-side simultaneously**, each getting equal screen area.
  - **Branch Active Tab to Split Pane (`Ctrl+Shift+=`)**: Duplicate the current conversation into a new side-by-side pane to compare prompts or models.
  - **Reorderable Split Panes**: Move and swap tab positions horizontally (`<` and `>` buttons in pane headers) at any time.
- **Smart Conversation Persistence, Import/Export & Auto-Naming**:
  - New tabs start as clean drafts and are **not** saved to history until there is at least one full question-and-answer exchange.
  - Once the response completes, a concise title is automatically generated from your prompt and the chat appears in the left sidebar.
  - **Import & Export**: Import conversations from `.json` or `.md` files and export any chat as Markdown or JSON.
  - Rename, duplicate/branch, or delete chats at any time via the sidebar or conversation settings.
- **Streamlined Settings (General, Providers & Web Search)**:
  - Three master tabs: **General**, **Providers**, and **Web Search**.
  - Under **Providers**: native SDK support for **Anthropic** (`anthropic` SDK) and **Gemini** (`google-genai` SDK) with automatic endpoint management, plus configurable endpoints for OpenAI, OpenRouter, DeepSeek, Ollama, or **`+ Add Provider`** for any OpenAI-compatible API.
  - Buffered Settings modal edits with explicit **Save**, write-only masked `.env` API key management, **`Fetch from Endpoint`** verification, and **`Auto add`** for up to 5 models per provider.
  - Under **Web Search**: configure Gemini-backed Google Search and Image Search fallback models with request tracking (`monthly / 5,000` & all-time totals).
- **Dynamic Startup Verification & Model Dropdown Mirroring**:
  - At startup, verifies configured providers and available models in the background while preserving your previously configured models and pricing.
  - The model selection dropdown directly mirrors the configured models list.
  - **Dynamic Thinking Selector**: The thinking dropdown automatically hides when a non-thinking model is selected and appears only for models that support reasoning.
- **ChatGPT / Gemini Style "Thinking Process"**:
  - Live collapsible thinking accordion displaying reasoning tokens or `<think>` tags with real-time status and animations.
- **Web Search & Image Search Integration**:
  - Per-tab Web Search toggle allowing models to invoke Gemini-grounded Google Search and Image Search with live status pills.
- **Offline Markdown, Code Highlighting & LaTeX**:
  - Rich Markdown streaming with nested lists, tables, blockquotes, and bundled local KaTeX math fonts (`file://` offline-safe).
  - Syntax-highlighted code blocks with language pills and one-click **Copy Code** buttons.
- **Token & Cache Analytics**:
  - Collapsible statistics bar at the bottom of each tab pane showing input/output tokens, cache hits/misses, estimated cost per model, and Google Search usage.

---

## 📥 Installation

### 1. One-Line PowerShell Web Installer (Fastest & Recommended)
Install ChatInt seamlessly with zero setup and no SmartScreen warnings:

```powershell
irm https://is.gd/chatint | iex
```
*(Or via direct GitHub CDN: `irm https://raw.githubusercontent.com/RishavSinha-2/ChatINT/main/install.ps1 | iex`)*

This downloads the latest release, installs silently to `%LOCALAPPDATA%\Programs\ChatInt`, creates Start Menu and Desktop shortcuts, and registers standard uninstallation in Windows Settings.

---

### 2. Direct Downloads (GitHub Releases)
Grab the standalone files directly from [GitHub Releases](https://github.com/RishavSinha-2/ChatINT/releases/latest):

* **[ChatInt-Setup-x64.exe](https://github.com/RishavSinha-2/ChatINT/releases/latest)** — Standard Windows installer.
* **[ChatInt-Portable-v0.5.1.zip](https://github.com/RishavSinha-2/ChatINT/releases/latest)** — Zero-install portable edition. Extract anywhere and run `ChatInt.exe`.

---

### 3. Windows Package Manager (WinGet)
*Submission under community review in [PR #480977](https://github.com/microsoft/winget-pkgs/pull/480977):*

```powershell
winget install chatint
```

---

## 🚀 Developer Quick Start

If running directly from the source code, ensure you have [uv](https://docs.astral.sh/uv/) installed:

### 1. Run ChatInt
```bash
uv run chatint
```
or:
```bash
uv run python -m chatint
```

### 2. Configure Providers & API Keys
Press `Ctrl+,` or click the **Settings** gear icon in the sidebar to enter your API keys, fetch endpoints, and configure models.

---

## ⌨️ Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Enter` | Send message |
| `Shift + Enter` | New line in message |
| `Ctrl + T` | Open new tab |
| `Ctrl + W` | Close active tab |
| `Ctrl + \` | Add side-by-side split pane (up to 5) |
| `Ctrl + Shift + =` | Branch active tab into a split pane |
| `Ctrl + ,` | Open Settings |
| `Escape` | Close any open modal or cancel inline message edit |

---

## 📦 Building Releases

To compile the standalone `.exe`, package the portable `.zip`, and build the Inno Setup installer:

```powershell
uv run python packaging/build_release.py
```

This automatically prepares the sanitized baseline template database, packages the portable archive, and prints SHA-256 checksums for all release artifacts.

---

## 🗂️ Data & Storage Architecture

ChatInt features deterministic multi-mode path resolution and automatic database self-healing:

* **Installed Mode (`%LOCALAPPDATA%\Programs\ChatInt`)**:
  - Chat history and databases persist across updates in **`%APPDATA%\ChatInt\data\chatint.db`**.
  - Provider API keys are encrypted at rest using Windows DPAPI in **`%APPDATA%\ChatInt\.env`**.
* **Portable Mode (Custom folder or alongside `data/`)**:
  - Completely self-contained next to `ChatInt.exe` in **`<exe_dir>\data\chatint.db`** and **`<exe_dir>\.env`**.
* **Database Self-Healing**:
  - Automatically checks SQLite integrity on launch. If corruption is detected, safely quarantines the corrupted database to `chatint.db.corrupted.<timestamp>`, restores from an intact `.bak` backup if available, or cleanly re-initializes from the bundled template without crashing.
* **Diagnostic Logs**:
  - Rotating logs with automatic secret scrubbing are stored in `data/logs/chatint.log` (accessible directly via **Settings → General → Open Logs Folder**).

---

## 🧪 Running Tests

Run the automated backend, frontend, path resolution, and self-healing test suites with `uv`:

```bash
uv run python -m unittest discover -s tests -v
```

To run the live GUI smoke test against a built standalone executable (ensure no live `ChatInt` session is open first):

```bash
CHATINT_RUN_EXE_SMOKE=1 CHATINT_EXE_PATH=dist/v0.5.1/ChatInt.exe uv run python -m unittest tests/test_dist_exe.py -v
```
