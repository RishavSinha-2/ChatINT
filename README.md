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
  - **Import & Export**: Import conversations from `.json` or `.md` files and export any chat as Markdown or JSON (preserving token ledgers, cost breakdowns, and agent tool traces).
  - Rename, duplicate/branch, or delete chats at any time via the sidebar or conversation settings.
- **Streamlined Settings (General, Providers & Web Search)**:
  - Three master tabs: **General**, **Providers**, and **Web Search**.
  - Under **General**: theme/appearance toggles, workspace path defaults, **Storage & Quotas** presets (`No limit` or configurable per-chat / total-chats size presets), live storage usage stats, **Manage Large Chats** (one-click Export & Delete), and diagnostic log access.
  - Under **Providers**: native SDK support for **Anthropic** (`anthropic` SDK) and **Gemini** (`google-genai` SDK) with automatic endpoint management, plus configurable endpoints for OpenAI, OpenRouter, DeepSeek, Ollama, or **`+ Add Provider`** for any OpenAI-compatible API (with custom provider display names across badges/stats and keyless support for local/loopback endpoints).
  - Buffered Settings modal edits with explicit **Save**, write-only masked `.env` API key management, **`Fetch from Endpoint`** verification, and **`Auto add`** for up to 5 models per provider.
  - Under **Web Search**: DuckDuckGo works out of the box with zero API key setup, plus optional Gemini-grounded Google Search fallback with request tracking (`monthly / 5,000` & all-time totals).
- **Dynamic Startup Verification & Model Dropdown Mirroring**:
  - At startup, verifies configured providers and available models in the background while preserving your previously configured models and pricing.
  - The model selection dropdown directly mirrors the configured models list.
  - **Dynamic Thinking Selector**: The thinking dropdown automatically hides when a non-thinking model is selected and appears only for models that support reasoning.
- **ChatGPT / Gemini Style "Thinking Process"**:
  - Live collapsible thinking accordion displaying reasoning tokens or `<think>` tags with real-time status and animations.
- **Web Search, Image Search & Agent Mode**:
  - Per-tab Web Search toggle (DuckDuckGo by default or Gemini Google Search fallback) with live status pills.
  - Per-tab **Agent Mode** toggle granting controlled access to sandboxed workspace file/command tools and safe public URL fetching, with selectable **Manual** or **Auto** approvals, inline expandable tool cards, and non-intrusive per-chat storage quota warning banners.
- **Offline Markdown, Code Highlighting & LaTeX**:
  - Rich Markdown streaming with nested lists, tables, blockquotes, and bundled local KaTeX math fonts (`file://` offline-safe).
  - Syntax-highlighted code blocks with language pills and one-click **Copy Code** buttons.
- **Per-Prompt Speed, Token & Cache Analytics**:
  - Displays per-prompt generation speed (`tok/s`) on standard responses and `Worked for Xs` duration metrics on Agent Mode replies directly alongside provider/model/token metadata.
  - Collapsible, horizontally scrollable statistics bar at the bottom of each tab pane showing input/output tokens, cache hits/misses, estimated cost per model, and Google Search usage.

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
> [!NOTE]
> **Active local source version:** `v0.5.3` (currently unreleased — not yet published on GitHub Releases). The PowerShell installer and links below point to the latest published stable release (`v0.5.2`) until `v0.5.3` is tagged and released.

Grab the latest published standalone files directly from [GitHub Releases](https://github.com/RishavSinha-2/ChatINT/releases/latest):

* **[ChatInt-Setup-x64.exe](https://github.com/RishavSinha-2/ChatINT/releases/latest)** — Standard Windows installer.
* **[ChatInt-Portable-v0.5.2.zip](https://github.com/RishavSinha-2/ChatINT/releases/latest)** — Zero-install portable edition for the current published release (`v0.5.2`). Building locally from source produces `dist/v0.5.3/ChatInt-Portable-v0.5.3.zip`.

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

This automatically prepares a clean schema-v6 baseline template database in `data/chatint.db` (preserving curated models, pricing, and default settings while scrubbing any runtime chats, per-chat databases, tab sessions, search quota counts, and database API keys), builds `dist/v0.5.3/ChatInt.exe`, packages `dist/v0.5.3/ChatInt-Portable-v0.5.3.zip`, and prints SHA-256 checksums for all release artifacts.

---

## 🗂️ Data & Storage Architecture

ChatInt uses a hybrid two-tier SQLite storage architecture with deterministic path resolution and automatic self-healing:

* **Two-Tier SQLite Layout (`chatint.db` + `conversations/<conv_id>.db`)**:
  - **Global index (`data/chatint.db`)**: Stores configured providers, conversation metadata/counts/sizes, open tab sessions, app settings (including storage quotas), and Google Search usage counters.
  - **Per-conversation databases (`data/conversations/<conv_id>.db`)**: Each conversation is isolated in its own SQLite database storing `conv_meta`, `messages` (including tool traces), and `token_ledger` with incremental auto-vacuum so individual large chats stay fast and deleting a chat reclaims disk space immediately.
* **Installed Mode (`%LOCALAPPDATA%\Programs\ChatInt`)**:
  - Chat index and per-conversation databases persist across updates under **`%APPDATA%\ChatInt\data\`** (`chatint.db` and `conversations/`).
  - Provider API keys are encrypted at rest using Windows DPAPI in **`%APPDATA%\ChatInt\.env`**.
* **Portable Mode (Custom folder or alongside `data/`)**:
  - Completely self-contained next to `ChatInt.exe` in **`<exe_dir>\data\chatint.db`**, **`<exe_dir>\data\conversations\`**, and **`<exe_dir>\.env`**.
* **Database Self-Healing, Backwards Compatibility & Storage Quotas**:
  - Automatically checks SQLite integrity and runs sequential schema migrations (`v0`–`v5` → `v6`) with pre-migration `.bak` snapshots, seamlessly migrating older monolithic `chatint.db` databases into per-conversation `.db` files on first launch.
  - If corruption is detected in either `chatint.db` or a single conversation database, ChatInt quarantines only the affected file (`.corrupted.<timestamp>`), restores from an intact `.bak` backup if available, or cleanly recovers without affecting sibling chats.
  - Configurable **Storage & Quotas** presets (`No limit` or size presets per chat / across all chats) show per-pane warnings at 80% / 100% capacity and let you inspect, export, or delete large chats from **Settings → General**.
* **Diagnostic Logs**:
  - Rotating logs with automatic secret scrubbing are stored in `data/logs/chatint.log` (accessible directly via **Settings → General → Open Logs Folder**).

---

## 🧪 Running Tests

Run the automated backend, frontend, path resolution, and self-healing test suites with `uv`:

```bash
uv run python -m unittest discover -s tests -v
```

To run the live GUI smoke test against the local `v0.5.3` standalone executable (ensure no live `ChatInt` session is open first):

```bash
CHATINT_RUN_EXE_SMOKE=1 CHATINT_EXE_PATH=dist/v0.5.3/ChatInt.exe uv run python -m unittest tests/test_dist_exe.py -v
```
