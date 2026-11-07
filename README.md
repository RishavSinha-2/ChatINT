# ChatInt 🚀

A minimalist, multi-tab, multi-provider desktop chat interface built with **Python**, **pywebview** (Windows Edge WebView2), and the **OpenAI SDK**, styled like ChatGPT and Google Gemini.

---

## ✨ Features

- **Native Desktop Application**: Standalone native window with zero browser chrome, powered entirely by Python via `uv` (no Node.js or npm required).
- **Multi-Tab Workspace with Up to 5 Side-by-Side Split Panes**:
  - Open unlimited conversation tabs (`Ctrl+T` to open a tab, `Ctrl+W` to close).
  - Side-by-side **Split View (`Ctrl+\`)**: Open up to **5 tabs side-by-side simultaneously**, each getting equal screen area.
  - **Reorderable Split Panes**: Move and swap tab positions horizontally (`<` and `>` buttons in pane headers) at any time.
- **Smart Conversation Persistence & Auto-Naming**:
  - New tabs start as clean drafts and are **not** saved to history until there is at least one full question-and-answer exchange.
  - As soon as the first response begins streaming, a concise, human-readable title is automatically generated from your question and the chat immediately appears in the left sidebar.
  - Rename chats at any time via the sidebar or settings.
- **Streamlined Settings (General & Providers)**:
  - Settings navigation with two clean master tabs: **General** and **Providers**.
  - Under **Providers**: configure endpoints for OpenAI, OpenRouter, DeepSeek, Ollama, or click **`+ Add Provider`** to connect any OpenAI-compatible API.
- **Dynamic Endpoint Sync & Model Dropdown Mirroring**:
  - At startup, automatically checks accessible endpoints with configured keys, discovers available models, removes absent ones, and syncs the top 5 models into the config list.
  - The model selection dropdown directly mirrors the configured models list.
  - **Dynamic Thinking Selector**: The thinking dropdown automatically hides when a non-thinking model is selected and appears only for models that support reasoning.
- **ChatGPT / Gemini Style "Thinking Process"**:
  - Live collapsible thinking accordion displaying reasoning tokens or `<think>` tags with real-time status and animations.
- **Markdown & Indentation**:
  - Rich Markdown streaming with proper nested list indentation, tables, blockquotes, and LaTeX math.
  - Syntax-highlighted code blocks with language pills and one-click **Copy Code** buttons.
- **Token & Cache Analytics**:
  - Collapsible statistics bar at the bottom of each tab pane.
  - Input & output token counts broken down **per model** used in that conversation.
  - **Cache hit & cache miss statistics** (with cache hit rate percentage) whenever reported by the model.

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
Press `Ctrl+,` or click the **Settings** gear icon in the sidebar to enter your API keys and endpoints.

---

## ⌨️ Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Enter` | Send message |
| `Shift + Enter` | New line in message |
| `Ctrl + T` | Open new tab |
| `Ctrl + W` | Close active tab |
| `Ctrl + \` | Add / Toggle side-by-side split pane (up to 5) |
| `Ctrl + ,` | Open Settings |
| `Escape` | Close any open modal |
