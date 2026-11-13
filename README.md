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

## 🚀 Quick Start

Ensure you have [uv](https://docs.astral.sh/uv/) installed.

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
| `Escape` | Close any open modal |
