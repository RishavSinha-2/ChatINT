# ChatInt — Comprehensive Audit: Bugs, Issues & Foolproofing Roadmap

This document catalogs **every bug, security vulnerability, edge case, performance bottleneck, and maintainability issue** identified across the entire ChatInt codebase (`src/chatint/`, `src/chatint/ui/`, `tests/`, `ChatInt.spec`, `pyproject.toml`, and `README.md`), followed by **concrete architectural improvements and refactorings** to make the application resilient and foolproof.

---

## Summary Overview

| Severity / Category | Count | Key Focus Areas |
| :--- | :---: | :--- |
| **Critical (Security & Secret Exposure)** | 3 | `pywebview.api` exposed to external websites, plaintext API key RPC, KaTeX/Markdown sanitizer bypass |
| **High (Concurrency, Races & Crashes)** | 5 | `StreamManager` tab race, `FOREIGN KEY` worker crashes on delete, $O(N^2)$ streaming DOM re-renders, non-atomic `.env` writes, auto-naming races |
| **Medium (Logic, SDKs, Data & UI State)** | 12 | Dropped stats/errors on background tabs, missing KaTeX font files, modal promise leaks, OpenAI thinking/tool fallbacks, offline local-LLM false negatives, client leaks, Markdown import/export corruption, Win32 64-bit `ctypes` bugs, PyInstaller `test` path filter |
| **Low (Polish, Leaks, Docs & Tests)** | 6 | Leaked `ResizeObserver`s, Light Mode error contrast, unthrottled IPC saves, dead state/duplicate icons, outdated `README.md`, non-discoverable test scripts |
| **Foolproofing & Refactorings** | 8 | Modularizing 3,800-line `app.js` / `db.py` / `streamer.py`, unified provider adapter, `stream_id` event bus, CSP + secret isolation, `PRAGMA user_version` migrations, rotating file logs, typed bridge validation, automated test suite |

---

## 1. Critical Issues (Security & Secret Exposure)

### C1. Privileged `window.pywebview.api` Exposed to External Websites & In-Window Navigation Destroys SPA State
- **Where:** `src/chatint/main.py:62-208, 230-241`, `src/chatint/bridge.py:293-305`, `src/chatint/ui/app.js:3738-3741`
- **What happens:**
  1. The main `pywebview` window is created with `js_api=bridge` (`main.py:233`). Clicking any link inside an assistant Markdown response navigates **that same privileged window** directly to the external website (`http://` / `https://`).
  2. As confirmed by `_EXTERNAL_NAV_BAR_JS` (`main.py:152-163`), `window.pywebview.api` remains injected and callable on the external webpage! Any untrusted webpage (or adversarial link surfaced via Web Search / prompt injection / imported chat) can execute `window.pywebview.api.get_provider_api_key("OpenAI")`, `get_initial_data()`, `get_conversations()`, `get_conversation(id)`, `delete_conversation(id)`, or `save_settings(...)`—exfiltrating **every plaintext API key in `.env` and the user's entire chat history**, or wiping local data.
  3. Navigating the main window unloads `index.html` and `app.js` completely: all in-flight streaming DOM rows, unsaved input box drafts, and client-side JS state are destroyed, while Python background streaming threads keep pushing events into `_pending_events` for tabs that no longer exist until `return_to_chat()` reloads the app from scratch.
  4. `bridge.attach_window(window, index_html.resolve().as_uri())` (`main.py:239`) initially stores a `file://` URI, whereas `pywebview` often serves local files over `http://127.0.0.1:<port>/`. If `_on_loaded` fails to capture `app_origin[0]` on first load or a user clicks a localhost link matching `127.0.0.1`, origin detection can misbehave.
- **How to make it foolproof:**
  - Intercept all `<a href="http(s)://...">` clicks in `app.js` (`e.preventDefault()`) and open external URLs either in the **OS default browser** (`open_external_url`, validating `scheme in ("http", "https")`) or in a **separate child `webview.create_window(..., js_api=None)`** with zero Python bridge privileges.
  - On the Python side, guard every `DesktopBridge` method by verifying `window.get_current_url()` matches the exact initial local app origin/token before executing any RPC.

### C2. Plaintext API Key Retrieval RPC (`get_provider_api_key`) Exposed Over Bridge
- **Where:** `src/chatint/bridge.py:390-393`, `src/chatint/ui/app.js:2859-2874`
- **What happens:** `DesktopBridge.get_provider_api_key(provider_name)` reads the raw secret from `.env` and returns `{"api_key": key}` in plaintext to JavaScript so the "Eye" toggle in Settings can reveal an already-saved key. Combined with C1 or C3 (any XSS in rendered Markdown), every stored provider secret (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, custom provider keys) can be read programmatically in one call.
- **How to make it foolproof:**
  - Never send saved secrets back to the WebView renderer. Once saved, treat API keys as write-only from the UI's perspective (show a masked placeholder like `sk-••••••1234` generated in Python if desired, and only reveal the newly typed value while it is still unsaved in the input box).
  - Restrict `open_external_url(url)` (`bridge.py:298-305`) so it strictly allows `https://` and `http://` schemes—right now `webbrowser.open(target)` accepts `file://` or custom OS protocol handlers if passed a non-HTTP URI.

### C3. Markdown / KaTeX Sanitizer Bypass & Weak URL Protocol Blocklist
- **Where:** `src/chatint/ui/app.js:1540-1664`, `src/chatint/ui/index.html:1-11`
- **What happens:**
  1. **Post-sanitizer HTML injection:** In `renderMarkdownWithEnhancements` (`app.js:1547-1661`), inline and block math expressions are rendered via `katex.renderToString(...)` into `mathTokens` **before** `sanitizeMarkdownDom(rootContainer)` runs (`line 1651`), and then spliced back into `rootContainer.innerHTML` via regex **after** sanitization (`lines 1653-1658`). Any HTML emitted by KaTeX (or any crafted token collision like `@@KATEX_TOKEN_0@@` typed in message text) bypasses `sanitizeMarkdownDom` completely.
  2. **Blocklist instead of allowlist on URLs:** `sanitizeMarkdownDom` (`app.js:1633-1636`) checks `lowerVal.startsWith("javascript:") || lowerVal.startsWith("vbscript:") || lowerVal.startsWith("data:text/html")`. This misses `data:image/svg+xml;base64,...` (which can embed `<script>` inside `<object>`/`<iframe>` contexts or unusual WebView handlers), `file://` local path probing, and whitespace/control-character-prefixed schemes.
  3. **Unsanitized `<img>` auto-loading & tracking pixels:** Markdown `![alt](https://attacker.tld/track)` renders `<img>` tags that immediately fire outbound requests from the user's IP whenever an AI response (with Web Search / prompt injection) or an imported `.md`/`.json` chat is opened. Furthermore, `index.html` defines **no Content-Security-Policy (`<meta http-equiv="Content-Security-Policy">`)**.
- **How to make it foolproof:**
  - Inject KaTeX output *before* running a strict DOM allowlist sanitizer (e.g., bundled `DOMPurify` or an explicit tag/attribute allowlist), or replace placeholder `<span data-math-idx="i">` nodes via DOM APIs (`el.innerHTML = katexHtml` followed by sanitizing `el`).
  - Enforce a strict URL protocol **allowlist** (`https:`, `http:`, `mailto:` for `<a href>`, and `https:`, `http:` for `<img src>`), stripping everything else.
  - Add a strict `<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https: http: data:; font-src 'self' data:; connect-src 'none'; object-src 'none'; base-uri 'none';">` in `index.html`.

---

## 2. High-Priority Bugs (Concurrency, Streaming Races & Crashes)

### H1. `StreamManager` Keyed Only by `tab_id` Causes Stream Cancellation & State Corruption Races
- **Where:** `src/chatint/streamer.py:32-54, 1291-1358`, `src/chatint/ui/app.js:2011-2158, 2367-2375`
- **What happens:**
  1. `StreamManager._abort_events` is a `Dict[str, threading.Event]` keyed solely by `tab_id`—there is no per-request `stream_id`.
  2. When a user clicks **Stop** (or switches conversations in the active tab at `app.js:2368`, or clicks **Regenerate** / **Edit & Resend**) and immediately starts **Stream B** on the same `tab_id` while **Stream A** is still unwinding its network call or tool loop:
     - `register_stream(tab_id)` (`streamer.py:37`) overwrites `_abort_events[tab_id]` with Stream B's new `Event` (without calling `.set()` on any still-registered event).
     - Seconds later, when Stream A enters its `finally` block (`streamer.py:1357`), `StreamManager.unregister_stream(tab_id)` executes `_abort_events.pop(tab_id, None)`—**deleting Stream B's active `abort_event`!** Clicking Stop on Stream B now does nothing.
     - Worse, when Stream A was aborted after generating partial text, Stream A still executes `add_message(...)` and dispatches `{"type": "done", "tab_id": tab_id, ...}` (`streamer.py:1331-1355`). On the frontend, Stream A's `done` event arrives while Stream B is actively streaming, flipping `tab.isStreaming = false`, replacing Stream B's `#live-msg-${tab.id}` DOM element with Stream A's message (even if the user switched `tab.conversationId` to a completely different chat!), and hiding the Stop button while Stream B's chunks keep arriving!
- **How to make it foolproof:**
  - Generate a unique `stream_id = str(uuid.uuid4())` for every `_start_stream` call and return it from `send_message` / `regenerate_message` / `edit_message`.
  - In `StreamManager`, store `(stream_id, abort_event)` per `tab_id`: `register_stream(tab_id, stream_id)` must `.set()` any previous `abort_event` for `tab_id`, and `unregister_stream(tab_id, stream_id)` must only pop `tab_id` if the stored `stream_id` still matches.
  - Include `stream_id` and `conversation_id` in every emitted event (`start`, `thinking_chunk`, `content_chunk`, `tool_status`, `done`, `error`). In `app.js`, ignore any event whose `stream_id !== tab.activeStreamId` or `conversation_id !== tab.conversationId`.

### H2. Deleting a Conversation During Active Streaming / Web Search Crashes Worker Threads (`sqlite3.IntegrityError`)
- **Where:** `src/chatint/db.py:126, 189, 219`, `src/chatint/streamer.py:416, 1332, 1341`, `src/chatint/web_search_tool.py:456`, `src/chatint/ui/app.js:563-565, 2333-2340`
- **What happens:**
  1. Every SQLite connection enables `PRAGMA foreign_keys=ON;` (`db.py:126`), and both `messages.conversation_id` (`db.py:189`) and `token_ledger.conversation_id` (`db.py:219`) enforce `FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE`.
  2. In `app.js:2333-2340` (sidebar delete) and `app.js:563-565` (tab close), `callApi("delete_conversation", conv.id)` runs concurrently with `callApi("stop_streaming", t.id)`.
  3. Back in `stream_chat_completion` (`streamer.py:1331-1346`), `add_message(...)` and `record_token_usage(...)` sit **outside** the provider `try ... except Exception` block (`lines 1298-1329`)! If the conversation row was deleted while the stream was in flight (or unwinding after `stop_streaming`), `add_message` raises an unhandled `sqlite3.IntegrityError: FOREIGN KEY constraint failed` that crashes the background thread. The same `IntegrityError` occurs inside `execute_web_search` (`web_search_tool.py:456`) when recording search token usage.
- **How to make it foolproof:**
  - Have `DesktopBridge.delete_conversation(conv_id)` first abort and wait briefly for—or mark cancelled—any active stream associated with `conv_id`.
  - Guard `add_message` and `record_token_usage` against missing parent conversations (either check `SELECT 1 FROM conversations WHERE id = ?` inside the same write transaction or catch `sqlite3.IntegrityError` cleanly and return `None` when the conversation was deleted).

### H3. Quadratic $O(N^2)$ Full-Document Markdown + KaTeX + Syntax Highlighting on Every Streaming Chunk
- **Where:** `src/chatint/ui/app.js:302-322, 2094-2116`
- **What happens:**
  1. `startEventPolling()` polls `get_pending_events` every `45ms` and loops synchronously through every queued event (`for (const ev of events)` at `app.js:310-314`).
  2. On **every single `content_chunk` event** (`app.js:2095-2110`), `app.js` appends `event.chunk` to `tab.streamingContent` and runs:
     ```js
     contentDiv.innerHTML = renderMarkdownWithEnhancements(tab.streamingContent);
     renderIcons(contentDiv);
     ```
  3. `renderMarkdownWithEnhancements` runs regex math extraction across the entire string, calls `katex.renderToString` on every equation, parses the entire Markdown document with `marked.parse`, runs `hljs.highlightElement` / `hljs.highlightAuto` (very CPU-heavy when language is unspecified!) on every `<pre><code>` block, traverses the full DOM in `sanitizeMarkdownDom`, and re-scans Lucide icons.
  4. If 15 chunks arrive in one 45ms poll batch during fast streaming (Groq, Cerebras, Gemini Flash) on a 4,000-word code-heavy answer, the entire document is parsed, syntax-highlighted, and rewritten to `innerHTML` **15 times synchronously in a single frame**—causing $O(N^2)$ CPU work, dropped frames, unresponsive Stop buttons, and broken text selection while streaming.
- **How to make it foolproof:**
  - Coalesce all `thinking_chunk` and `content_chunk` events per `tab_id` inside `get_pending_events` batch processing so DOM updates run **at most once per animation frame (`requestAnimationFrame`)** (or throttled to ~50–80ms).
  - Avoid `hljs.highlightAuto(codeText)` (which tests dozens of grammars on every render) on unclosed/unlabeled code fences during live streaming; only run `highlightAuto` once when `done` arrives, or incrementally render completed top-level Markdown blocks while only re-parsing the trailing active block.

### H4. Non-Atomic `.env` Writes Risk Credential Loss & Corrupt Special-Character API Keys
- **Where:** `src/chatint/db.py:55-88`
- **What happens:**
  1. `write_env_dict` (`db.py:83-88`) overwrites `.env` in place via `ENV_PATH.write_text(...)` with no thread lock and no atomic temp-file swap (`os.replace`). A process crash, power loss, or concurrent write truncates `.env` and loses **all** saved API keys.
  2. `write_env_dict` strips all user comments (`# ...`), blank lines, and unrelated formatting every time any provider key is updated, and writes raw `f"{k}={v}"` lines without quoting or escaping.
  3. Meanwhile, `read_env_dict` (`db.py:76-78`) strips surrounding `'` and `"` quotes. If a custom/proxy API key or bearer token contains `#`, leading/trailing whitespace, quotes, or newlines, writing and reading `.env` alters or truncates the key, and `.env` is created with default permissions rather than owner-only (`0o600` on POSIX / restricted ACL where applicable).
- **How to make it foolproof:**
  - Protect `.env` reads/writes with a module-level `threading.RLock`, write to a temporary file in the same directory (`ENV_PATH.with_suffix(".env.tmp")`), `flush()` + `os.fsync()`, and atomically `os.replace()` onto `ENV_PATH`.
  - Properly quote/escape values containing whitespace, quotes, or `#` (or use `python-dotenv`'s `set_key` / lossless line preservation so user comments and unrelated variables in `.env` are never destroyed).

### H5. Auto-Naming Race Conditions: Overwrites Manual Renames, Deletes Fast-Closed Chats, & Ghosts Failed Streams
- **Where:** `src/chatint/streamer.py:390-445, 1309-1325`, `src/chatint/ui/app.js:562-565, 1977-2010`
- **What happens:**
  1. **Manual rename overwritten:** `_trigger_auto_naming` (`streamer.py:400-426`) reads `conv = get_conversation(conversation_id)` *before* calling `generate_chat_title(...)` (which can take up to 10 seconds over the network), then unconditionally executes `update_conversation(conversation_id, title=final_title)` (`line 424`). If the user manually renamed the chat during those 10 seconds, auto-naming overwrites their custom title!
  2. **Closing a tab early deletes an active/saved conversation:** In `app.js:563-565`, `closeTab(tabId)` checks `!state.conversations.some((c) => c.id === closedTab.conversationId)` and immediately calls `delete_conversation`. Because a new chat is only added to `state.conversations` when `chat_named` arrives (`app.js:1994`), closing a tab while `generate_chat_title` is still in flight **permanently deletes the conversation from SQLite** even if the assistant already replied! Conversely, if `chat_named` arrives *after* `closeTab` spliced the tab out of `state.tabs`, `onStreamEvent` hits `if (!tab) return;` (`app.js:1977-1978`) and drops the `chat_named` event completely.
  3. **Ghost sidebar entries on failed first turns:** `_trigger_auto_naming` fires as soon as the **first chunk** arrives (even a `thinking_chunk`), pushing the chat into `state.conversations` (`app.js:1994`). If the stream subsequently fails with an error before saving an assistant message, the chat appears in the sidebar for the rest of the session, then vanishes on restart because `get_conversations()` (`db.py:789-793`) requires at least one `role = 'assistant'` message.
- **How to make it foolproof:**
  - Trigger auto-naming **after** the first assistant message is committed to SQLite (or only commit/broadcast `chat_named` once an assistant message exists).
  - Re-check in SQLite inside `update_conversation` (`WHERE id = ? AND LOWER(TRIM(title)) IN ('new chat', '')`) so auto-naming can **never** clobber a user-supplied title.
  - Handle `chat_named` in `app.js` **before** `const tab = state.tabs.find(...)` so sidebar state updates even if the originating tab was closed, and never `delete_conversation` from `closeTab` without checking in Python whether the conversation has messages (`DELETE FROM conversations WHERE id = ? AND NOT EXISTS (SELECT 1 FROM messages WHERE conversation_id = ?)`).

---

## 3. Medium-Priority Bugs (Logic, SDKs, Data Integrity & UI State)

### M1. Background Tab Stream Completion Drops `token_stats` & Silently Loses Stream Errors
- **Where:** `src/chatint/ui/app.js:2117-2234`
- **What happens:**
  - When a user streams in Tab A and switches the split pane to Tab B (so Tab A's `pane` is not currently mounted in `#panes-container`, making `pane === null`):
    1. When `event.type === "done"` arrives (`app.js:2117-2156`), `if (event.token_stats)` (`line 2139`) is nested **inside** `if (pane && messagesContainer)` (`line 2119`)! `tab.tokenStats` and `state.googleSearchTotals` are never updated for background tabs.
    2. When `event.type === "error"` arrives (`app.js:2157-2234`), `tab.isStreaming` is set to `false` and the entire error-rendering block is skipped because `pane` is `null`. When the user switches back to Tab A, `loadConversationMessages` loads history from SQLite—leaving **zero trace** that the stream failed or why!
- **How to make it foolproof:**
  - Always update `tab.tokenStats` and `state.googleSearchTotals` regardless of whether `pane` is mounted.
  - Store `tab.lastStreamError = event.error` on `tab` when `pane` is unmounted (clearing it on the next `start`/`send`), and render the error banner + Retry button inside `loadConversationMessages` when switching back to that tab (plus show a subtle error dot on the tab header).

### M2. Missing KaTeX Webfont Files (`fonts/KaTeX_*.woff2`) & Stripped `@font-face` Rules Break Math Typography
- **Where:** `src/chatint/ui/assets/katex.min.css:1`, `src/chatint/ui/assets/`
- **What happens:** `src/chatint/ui/assets/katex.min.css` has had all `@font-face` declarations removed and `src/chatint/ui/assets/fonts/` does not exist. However, the CSS rules throughout `katex.min.css` still set `font-family: KaTeX_Math`, `KaTeX_Size1`–`KaTeX_Size4`, `KaTeX_AMS`, `KaTeX_Caligraphic`, `KaTeX_Fraktur`, `KaTeX_Script`, and `KaTeX_Typewriter` **with no fallback font family**. Because KaTeX computes exact vertical/horizontal `em` positioning assuming its own font metrics, multi-line matrices, tall integrals/summations (`KaTeX_Size1`–`Size4`), blackboard bold (`\mathbb{R}`), and square roots render with misaligned fallback system glyphs.
- **How to make it foolproof:** Bundle the official `fonts/KaTeX_*.woff2` files under `src/chatint/ui/assets/fonts/` alongside the unmodified `katex.min.css`.

### M3. Single Global `activeDialogResolve` Leaks Promises When App Modals Overlap
- **Where:** `src/chatint/ui/app.js:150-224`
- **What happens:** `showNoticeDialog`, `showConfirmDialog`, and `showPromptDialog` share a single module-global `activeDialogResolve` variable and single `#app-dialog-modal` element. If a user has a Rename (`showPromptDialog`) or Delete (`showConfirmDialog`) modal open and an async callback (e.g., failed background fetch/send/import) invokes `showNoticeDialog(...)`, `activeDialogResolve = resolve` **overwrites** the first dialog's resolver without ever calling it—leaving the awaiting `async` function permanently hung and mutating the open dialog's buttons/inputs mid-interaction.
- **How to make it foolproof:** Maintain a FIFO dialog queue (each `show*Dialog` pushes `{ config, resolve }` onto a queue and displays the next dialog only after `closeAppDialog` resolves the current one).

### M4. OpenAI Reasoning Fallback Issues: Invalid `reasoning_effort` Values, Missing `(effort, no_tools)` Retry, & Swallowed Auth/Rate-Limit Errors
- **Where:** `src/chatint/streamer.py:237-245, 1096-1146`
- **What happens:**
  1. `_map_openai_reasoning_effort` (`streamer.py:237-245`) passes `"minimal"`, `"xhigh"`, and `"max"` directly as `reasoning_effort` to OpenAI-compatible endpoints. Standard OpenAI `o1` / `o3` / `gpt-5` chat completions only accept `"low"`, `"medium"`, or `"high"` (some newer endpoints accept `"minimal"`), causing guaranteed `400 Bad Request` roundtrips before falling back.
  2. In `_stream_openai`'s fallback loop (`streamer.py:1111-1145`), every effort level is tried **with** `tools` first (`[(eff, True) for eff in efforts]`), and only the single final fallback `(None, False)` strips both `reasoning_effort` **and** `tools` simultaneously! If a reasoning model supports `reasoning_effort="high"` but rejects `tools` (or vice versa), it wastes multiple failed API calls and drops thinking completely instead of trying `(effort, False)`.
  3. Both inner `except Exception:` blocks (`lines 1128` and `1144`) blindly catch **all** exceptions—including `401 Unauthorized`, `403 Forbidden`, `429 RateLimitError`, and connection timeouts—retrying 3–5 times on invalid keys or rate limits before re-raising on the final attempt!
- **How to make it foolproof:**
  - Clamp/map unsupported OpenAI effort tiers cleanly (`minimal -> low`, `xhigh/max -> high` unless the specific endpoint/model explicitly supports them).
  - Only trigger parameter-downgrade retries on `400` / `422` `BadRequestError` / unsupported-parameter errors—immediately raise `401`, `403`, `429`, `5xx`, and network/abort errors!
  - If `tools` is implicated in the `400` error message, retry `(primary_effort, use_tools=False)` immediately without dropping `reasoning_effort`.

### M5. `_is_network_available()` Fails Offline Local Providers (`Ollama` / `LM Studio`) & Corporate Proxies + Leaks Socket on Failure
- **Where:** `src/chatint/client_manager.py:97-115, 366`
- **What happens:**
  1. When `fetch_remote_models` catches an exception, `_is_network_available()` (`client_manager.py:97-115`) checks whether to classify the failure as transient (`is_network_error=True`, preserving configured models) by opening a raw TCP connection to Cloudflare DNS `1.1.1.1:53` and resolving `api.openai.com`.
  2. Look at lines 101–105:
     ```python
     s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
     s.settimeout(1.5)
     s.connect(("1.1.1.1", 53))
     s.close()
     ```
     If `s.connect(...)` raises `OSError` / `socket.timeout`, `s.close()` is **never called** (leaking the socket descriptor until GC).
  3. On corporate networks that block outbound port `53` / direct IP connections (requiring `HTTPS_PROXY`), `_is_network_available()` returns `False` even when HTTPS APIs work—or conversely, if a user runs **local Ollama / vLLM / LM Studio** on `http://localhost:11434/v1` while on an airplane (no internet), `_is_network_available()` returns `True`/`False` based on internet reachability rather than local loopback reachability!
- **How to make it foolproof:**
  - Use `with socket.create_connection(..., timeout=1.5):` so sockets are always closed.
  - Classify `is_network_error` primarily from the **SDK exception type** (`httpx.TransportError`, `httpx.ConnectError`, `httpx.TimeoutException`, `APIConnectionError`, `APITimeoutError`) rather than probing hardcoded external IPs (`1.1.1.1:53`). Only prune configured models when the server actually returned an HTTP `200 OK` model list! (Note: `fetch_remote_models` already only returns `models` on HTTP success, so relying on exception classes avoids external DNS/IP probes altogether.)

### M6. HTTP Client / Connection Pool Leaks & Orphaned Timeout Threads in `ClientManager` and `naming.py`
- **Where:** `src/chatint/client_manager.py:118-174, 349-406`, `src/chatint/naming.py:67-189`, `src/chatint/streamer.py:1077, 1179, 1219`
- **What happens:**
  1. Every single chat stream, title-generation call, web search, and model sync instantiates brand-new `openai.OpenAI` / `anthropic.Anthropic` / `genai.Client` objects (each spinning up a fresh `httpx.Client` SSL connection pool) and **never calls `.close()`** when finished. Over long desktop sessions, idle sockets and file descriptors accumulate.
  2. In `ClientManager.fetch_remote_models` (`client_manager.py:397-405`), `worker.join(timeout=timeout)` abandons the worker thread on timeout without closing the underlying `httpx` client, leaving hung sockets open in the background.
- **How to make it foolproof:**
  - Wrap temporary clients in `try ... finally: client.close()` (or context managers), or cache reusable `(provider_id, base_url, api_key_hash) -> client` instances inside `ClientManager` and close evicted clients when settings change.
  - Pass an explicit `timeout=httpx.Timeout(timeout, connect=10.0)` into the SDK client constructor in `fetch_remote_models` so the underlying HTTP request actually aborts when `timeout` expires.

### M7. `_build_context_messages` Drops Assistant Turns That Have `thinking_content` Only
- **Where:** `src/chatint/bridge.py:224-250`
- **What happens:** In `_build_context_messages` (`bridge.py:239`), historical messages are skipped whenever `not content` (`content == ""`). However, `stream_chat_completion` (`streamer.py:1331`) saves assistant messages whenever `full_content or full_thinking` is non-empty (e.g., user pressed **Stop** while a reasoning model was still thinking, so `thinking_content` is saved while `content == ""`). On the next user prompt, `_build_context_messages` silently drops that assistant turn, placing two `role: "user"` messages back-to-back even though the UI shows an assistant response between them.
- **How to make it foolproof:** Either do not persist empty-content assistant turns when stopped during thinking, or represent interrupted assistant turns with a minimal placeholder (e.g., `"[Response stopped]"`) when building context so turn alternation stays consistent with what the user sees on screen.

### M8. Markdown Export / Import Round-Trip Data Corruption
- **Where:** `src/chatint/bridge.py:98-145, 513-538`
- **What happens:**
  1. **Multi-line `system_prompt` breaks Markdown export/import:** `export_conversation` (`bridge.py:521`) writes `f"> **System Prompt:** {conv['system_prompt']}\n\n"` without prefixing subsequent lines of a multi-line system prompt with `> `. Upon re-importing that `.md` file (`bridge.py:114-116`), `re.search(r"^>\s*\*\*System Prompt:\*\*\s*(.+)$", text, re.MULTILINE)` only captures the **first line** of the system prompt!
  2. **Fenced code blocks containing `### User` or `### Assistant` split messages:** `parse_chat_file_content` (`bridge.py:119`) splits Markdown on `r"^###\s+(User|Assistant(?:\s*\([^)]*\))?)\s*$"` across the raw file without ignoring fenced code blocks (` ```markdown ... ``` `). Any exported coding chat discussing `### User` headings gets chopped into fake conversation turns on import!
  3. **Model IDs containing parentheses fail header regex:** `(?:\s*\([^)]*\))?` fails to match `### Assistant (model (preview))` if a model ID/name contains parentheses.
  4. **Export drops `thinking_content` in Markdown** and JSON import (`bridge.py:54-95`) drops `provider_id`, `thinking_level`, and `created_at` metadata that JSON export wrote out!
- **How to make it foolproof:**
  - Prefix all lines of multi-line system prompts in Markdown blockquotes (or store metadata in a YAML/HTML comment frontmatter header on exported `.md` files) and skip fenced code blocks (` ``` ` / `~~~`) when scanning for `### User` / `### Assistant` turn dividers.
  - Preserve `provider_id`, `model_id`, `thinking_level`, and `thinking_content` faithfully on JSON round-trip imports.

### M9. Error "Retry" Button Uses Fragile DOM Sibling Lookup & Breaks on Regenerate / Unsaved IDs
- **Where:** `src/chatint/ui/app.js:2186-2231`
- **What happens:**
  - When a stream errors, the inline **Retry** button (`app.js:2194-2205`) looks up `const prevRow = targetRow.previousElementSibling; const prevMsgId = prevRow ? prevRow.dataset.msgId : null;` and calls `edit_message(..., prevMsgId, prevText, ...)`.
  - This breaks in three scenarios:
    1. If `send_message` hadn't finished assigning the DB UUID or failed early, `prevRow.dataset.msgId` is `"user_temp_173..."` (truthy!), which passes `if (!prevMsgId)` and fails in SQLite with `"Target message not found"`.
    2. If two errors happen in a row or a system/banner element sits between rows, `previousElementSibling` is not the user bubble row (`prevText` becomes `""` or the error text itself!).
    3. Clicking Retry calls `edit_message`, which deletes any subsequent messages without updating `tab.tokenStats` from `res.token_stats` (`lines 2211-2225` ignore `res.token_stats`).
- **How to make it foolproof:** Walk backwards (`prevRow = prevRow.previousElementSibling`) until finding `.user-message-row`, verify `prevMsgId && !prevMsgId.startsWith("user_temp_")`, and if `prevMsgId` is still temporary, re-invoke `send_message` instead of `edit_message`.

### M10. Win32 Single-Instance Mutex Missing 64-Bit `ctypes` Signatures & Un-Maximizes Maximized Windows
- **Where:** `src/chatint/main.py:14-39`
- **What happens:**
  1. `kernel32.CreateMutexW`, `user32.FindWindowW`, `user32.ShowWindow`, and `user32.SetForegroundWindow` are called without setting `argtypes` or `restype`. On 64-bit Python (`win_amd64`), `ctypes` defaults `restype` and `None` arguments to 32-bit `c_int`, which can truncate 64-bit `HANDLE` / `HWND` pointers above `0x7FFFFFFF` and cause `OverflowError` or invalid handle calls.
  2. `FindWindowW(None, "ChatInt")` (`line 32`) matches **any** window on the user's desktop titled `"ChatInt"` (such as File Explorer open to a `ChatInt` folder!), rather than scoping by window class or local IPC.
  3. `user32.ShowWindow(hwnd, 9)` (`SW_RESTORE` at `line 34`) is called unconditionally—if the existing ChatInt window is currently **maximized**, `SW_RESTORE` shrinks/un-maximizes it instead of leaving it maximized! (`ShowWindow(hwnd, SW_RESTORE)` should only be called when `user32.IsIconic(hwnd)` is true.)
- **How to make it foolproof:** Declare explicit `ctypes.wintypes` signatures (`LPVOID`, `BOOL`, `LPCWSTR`, `HWND`, `HANDLE`), check `if user32.IsIconic(hwnd): user32.ShowWindow(hwnd, 9)`, and use a dedicated named pipe / loopback wakeup event or verify the window belongs to a `ChatInt` process before focusing it.

### M11. SQLite / `.env` Performance Overhead, Missing Explicit Rollback, & Title Uniqueness TOCTOU Race
- **Where:** `src/chatint/db.py:55-88, 120-137, 459, 699-773`
- **What happens:**
  1. **Repeated `.env` disk reads:** `get_providers()` (`db.py:459`) calls `get_api_key_from_env(p["name"])` inside its `for r in rows:` loop, and `read_env_dict()` calls `ensure_env_file()` (`write_text` check + `read_text`) every time—reading `.env` from disk **6–12+ times** on a single `get_providers()` / `save_settings()` call instead of reading `env_dict = read_env_dict()` once before the loop.
  2. **`PRAGMA journal_mode=WAL;` on every connection:** `get_connection()` (`db.py:125`) runs `PRAGMA journal_mode=WAL;` on every single DB call (WAL mode is persistent in the database header and only needs to be set once in `init_db()`).
  3. **Missing explicit rollback on exception:** `open_db()` (`db.py:131-137`) closes the connection in `finally: conn.close()` without calling `conn.rollback()` in an `except:` block if a caller started mutations before raising an exception.
  4. **Title uniqueness TOCTOU race:** `get_unique_conversation_title` (`db.py:699-725`) checks existing titles in Python outside an `IMMEDIATE` transaction; two simultaneous background auto-naming threads finishing for `"Python Script"` can both pick `"Python Script"` at the same instant.
- **How to make it foolproof:** Read `env_dict` once per `get_providers()` call, move `PRAGMA journal_mode=WAL;` into `init_db()`, add `except Exception: conn.rollback(); raise` inside `open_db()`, and run title deduplication + `UPDATE`/`INSERT` inside a single `BEGIN IMMEDIATE` SQLite transaction.

### M12. PyInstaller `ChatInt.spec` Breaks When Project Path Contains `"test"` + Dead `'httpx2'` Entry
- **Where:** `ChatInt.spec:8-28`
- **What happens:**
  1. Line 16 filters collected package data files using `'test' not in item[0].lower()`, where `item[0]` is the **absolute filesystem path** of the source file in `.venv`! If the repository or virtual environment lives anywhere under a folder containing `"test"` (e.g., `C:\Users\...\Desktop\test\chatint` or CI `/workspace/test-build/chatint`), **every single data file** from `webview` (including `WebView2Loader.dll` and JS bridge scripts!), `openai`, `anthropic`, and `google.genai` is silently excluded from the `.exe`!
  2. Lines 8 and 24 iterate over `'httpx2'`, which is not an installed package in `pyproject.toml`, throwing and catching a `PackageNotFoundError` on every build, while `certifi` is not in the `collect_all` data collection list (relying solely on default hooks).
- **How to make it foolproof:** Check `'test' not in item[1].lower().replace('\\', '/').split('/')` on the **relative destination bundle path** (`item[1]`), never the host machine's absolute path `item[0]`; remove `'httpx2'`; and explicitly include `certifi`'s `cacert.pem`.

---

## 4. Low-Priority / Minor Issues (UI Polish, Memory Leaks, Docs & Test Hygiene)

### L1. Undisconnected `ResizeObserver` Instances Leaked on Every Pane Render
- **Where:** `src/chatint/ui/app.js:904, 1009-1018`
- **What happens:** Every call to `renderActivePanes()` clears `el.panesContainer.innerHTML = ""` and calls `createPaneElement()`, which creates a `new ResizeObserver(...)` and attaches `ro.observe(pane)` (`line 1017`) without ever calling `ro.disconnect()` when panes are replaced. `renderActivePanes()` runs on every tab switch, split toggle, conversation click, settings save, and chat delete.
- **Fix:** Store `pane._resizeObserver = ro` and disconnect existing observers (`el.panesContainer.querySelectorAll(".chat-pane").forEach(p => p._resizeObserver?.disconnect())`) before clearing `el.panesContainer.innerHTML`.

### L2. Poor Error Contrast in Light Theme & Inconsistent Native `<select>` Option Colors
- **Where:** `src/chatint/ui/app.js:2178-2179`, `src/chatint/ui/style.css:1212, 763-766`
- **What happens:**
  - Stream error banners (`app.js:2179`) hardcode `color: #ff8a80` (light pastel pink) on `rgba(229, 57, 53, 0.1)`. In Light Theme (`#ffffff` background), `#ff8a80` has a ~2.1:1 contrast ratio (well below WCAG 4.5:1), making error messages hard to read. The same hardcoded `#ff8a80` appears on `.input-model-hint[data-tooltip]:hover::after` (`style.css:1212`).
  - `.thinking-dropdown option` (`style.css:763-766`) uses CSS variables (`var(--bg-secondary)`), whereas `.model-dropdown option` and `.pane-tab-dropdown option` (`lines 686-707`) use explicit `[data-theme="dark"]` / `[data-theme="light"]` hex colors with `!important` to work reliably in Windows WebView2 native popups.
- **Fix:** Introduce a `--danger-text` theme token (`#ff8a80` in dark mode, `#c62828` in light mode) and apply the same `[data-theme]` `<option>` rules to `.thinking-dropdown option`.

### L3. Unthrottled `persistTabsState()` IPC Calls & Unawaited Mutating Bridge Calls
- **Where:** `src/chatint/ui/app.js:449-476, 559, 564, 2333, 2338, 3738-3741`
- **What happens:**
  - `persistTabsState()` serializes all open tabs and writes to SQLite (`app_settings.open_tabs_state`) synchronously via `save_tabs_state` with zero debouncing—rapidly switching tabs, changing models, renaming, or clicking links fires back-to-back SQLite writes.
  - Mutating calls like `callApi("delete_conversation", ...)` (`lines 564, 2333`) and `callApi("stop_streaming", ...)` (`lines 559, 2338, 2368`) are fired without `await` or `.catch(...)`, so rejected promises or race ordering with subsequent calls go unhandled.
- **Fix:** Debounce `persistTabsState()` by `150–250ms` (with an immediate flush on `beforeunload`) and `await` mutating bridge calls inside `try/catch`.

### L4. Dead State Properties, Stale Comments, Duplicate Binary Icons, & Placeholder `pyproject.toml`
- **Where:** `src/chatint/ui/app.js:71, 73, 3457`, `src/chatint/ui/style.css:1498`, `pyproject.toml:4`, `./logo.ico` vs `src/chatint/ui/assets/logo.ico`
- **What happens:**
  - `state.activeConvSettingsId` (`app.js:73`) is dead/unused; `openConvSettingsModal` and `saveConvSettings` (`line 3457`) dynamically mutate `state.activeConvSettingsTabId` instead.
  - Comments at `app.js:71` and `style.css:1498` still state *"Only 2 options (General, Providers)"* even though `Web Search` is the third master tab.
  - Identical `90,313`-byte `logo.ico` files exist in both project root (`./logo.ico`, referenced by `ChatInt.spec:65`) and `src/chatint/ui/assets/logo.ico` (referenced by `main.py:243`).
  - `pyproject.toml` still contains `description = "Add your description here"`.
- **Fix:** Replace `activeConvSettingsId` with `activeConvSettingsTabId` in `state`, update comments and `pyproject.toml`, and point `ChatInt.spec` to `src/chatint/ui/assets/logo.ico` so root `logo.ico` can be removed.

### L5. Outdated `README.md`
- **Where:** `README.md:1-86`
- **What happens:** `README.md` does not document the **Web Search** & **Image Search** capabilities (and `Web Search` settings tab / 5,000 monthly request counter), **Chat Upload/Import** (`.json` / `.md`), **Branch to Split Pane** (`Ctrl+Shift+=`), or the buffered **Settings** workflow + **Auto-Add** button, and line 45 still claims ChatInt *"automatically syncs with configured provider endpoints at startup to discover and verify the top 5 available models"* (auto-adding on startup sync was intentionally removed so models are only added manually or via the Auto-Add button).
- **Fix:** Update `README.md` features, keyboard shortcuts table, build output path, and settings documentation to match current behavior.

### L6. Non-Discoverable Side-Effect Test Scripts (`test_single_instance.py`, `test_dist_exe.py`)
- **Where:** `tests/test_single_instance.py:1-51`, `tests/test_dist_exe.py:1-58`
- **What happens:**
  - Both files match `test*.py`, so running standard `python -m unittest discover -s tests` imports them and **immediately executes top-level process spawns, `time.sleep(...)`, window kills, and `sys.exit(1)`** at import time!
  - `test_single_instance.py:11` hardcodes `["uv", "run", "chatint"]` (failing when `uv` is not on `PATH` or on Windows `uv.exe`), and `test_dist_exe.py:8` hardcodes `dist/v0.4.0/ChatInt.exe` (failing whenever the version changes or `dist/` hasn't been built).
- **Fix:** Wrap standalone integration checks in `unittest.TestCase` classes guarded by `@unittest.skipUnless(...)` (e.g., skipping `.exe` smoke tests when `CHATINT_EXE_PATH` is absent, and launching `[sys.executable, "-m", "chatint"]` instead of hardcoded `"uv"`).

---

## 5. Improvements & Refactoring Roadmap (Making ChatInt Foolproof)

Beyond fixing individual bugs, the following structural improvements will eliminate entire classes of regressions and make ChatInt clean, fast, and foolproof as features grow:

### R1. Modularize the Three "God Files" (`app.js`, `db.py`, `streamer.py`)
Currently, almost the entire application lives in three massive files:
- `src/chatint/ui/app.js` (**3,836 lines** — state store, IPC bridge, Markdown/KaTeX/sanitizer, tabs/split panes, live streaming DOM, sidebar, settings buffer, model configurator, import/export, dialogs, shortcuts)
- `src/chatint/db.py` (**1,483 lines** — `.env` parser, SQLite connection helpers, schema seeding, provider CRUD, model heuristics/pricing catalog, conversation/message CRUD, branching/duplicating/importing, token ledger, Google search counters)
- `src/chatint/streamer.py` (**1,359 lines** — `StreamManager`, `BackgroundModelSyncer`, thinking parameter mappers, tool loop orchestration, OpenAI/Anthropic/Gemini streaming parsers, auto-naming trigger)

**Recommended Split (Pure Python + Vanilla ES Modules—Zero Node/Bundler Required!):**
- **Backend (`src/chatint/`):**
  - `storage/connection.py` — SQLite connection pool/context manager, WAL setup, versioned migrations
  - `storage/secrets.py` — Thread-safe, atomic `.env` (or OS keyring) secret store
  - `storage/repositories/` — `providers_repo.py`, `conversations_repo.py`, `ledger_repo.py`, `settings_repo.py`
  - `catalog/model_Capabilities.py` — Model pricing, thinking heuristics, and web-search model normalization (currently duplicated between `db.py` and `app.js`!)
  - `providers/` — `base.py` (`ProviderAdapter` protocol), `openai_adapter.py`, `anthropic_adapter.py`, `gemini_adapter.py`, `sync_service.py`
  - `services/` — `stream_coordinator.py`, `import_export.py`, `naming_service.py`
- **Frontend (`src/chatint/ui/js/` loaded via native `<script type="module" src="js/main.js">`):**
  - `state.js` (reactive state + debounced persistence), `api.js` (typed `pywebview` RPC client + event bus), `markdown.js` (sanitized Markdown + KaTeX + incremental code highlighting), `panes.js`, `sidebar.js`, `settings-modal.js`, `dialogs.js`. WebView2 natively supports ES modules (`import` / `export`) with zero build step or bundler!

### R2. Unified `ProviderAdapter` Layer (Eliminate Triplicated SDK Calling Logic)
Right now, SDK initialization, model-prefix stripping, thinking-level mapping, fallback retries, and token extraction (`prompt_tokens`, `completion_tokens`, `cached_tokens`) are implemented separately across `streamer.py`, `naming.py`, `web_search_tool.py`, and `client_manager.py`.
- Introduce a single `ProviderAdapter` interface with implementations `OpenAIAdapter`, `AnthropicAdapter`, and `GeminiAdapter` exposing:
  - `list_models(timeout) -> list[str]`
  - `stream_turn(messages, model, thinking_level, tools, abort_event) -> Iterator[StreamDelta]`
  - `complete_once(messages, model, max_tokens, timeout) -> CompletionResult`
- Every feature (chat streaming, tool follow-up turns, auto-naming, web search) then reuses one battle-tested error/retry/token-accounting implementation per SDK.

### R3. Generation-Scoped Streaming Event Bus (`stream_id` + Sequence Numbers + `requestAnimationFrame`)
- Tag every stream lifecycle with an immutable `stream_id` and monotonic `seq` counter.
- Coalesce consecutive `content_chunk` and `thinking_chunk` deltas **both in Python (`DesktopBridge._pending_events`)** when multiple chunks queue up between polls **and in JS via `requestAnimationFrame`**—cutting IPC payload count and DOM work by 80–95% during high-speed token streaming.
- Switch event polling to an **adaptive cadence** (`30ms` while any stream or model sync is active; `500ms` or event-driven push when idle) so ChatInt consumes **0% idle CPU/battery** when sitting open in the background.

### R4. Defense-in-Depth Security Boundary
- Never navigate the `js_api`-enabled main window to external URLs; open external links via validated `https://`/`http://` system browser launch (or an unprivileged child preview window with `js_api=None`).
- Add a strict `Content-Security-Policy` meta tag to `index.html`, ship `DOMPurify` (or a strict allowlist DOM walker applied *after* KaTeX injection), and optionally support Windows Credential Manager (`ctypes` `CredWriteW`/`CredReadW` or `keyring`) alongside atomic `.env` storage so secrets are encrypted at rest under the user's Windows account.

### R5. Versioned Database Schema Migrations (`PRAGMA user_version`) & Foreign-Key-Safe Writes
- Currently `init_db()` (`db.py:242-259`) relies on ad-hoc `PRAGMA table_info(...)` column checks. Adopting a simple sequential migration table / `PRAGMA user_version` list (`MIGRATIONS = [v1_sql, v2_sql, ...]`) makes future schema upgrades deterministic, testable, and reversible, with automatic pre-migration backup (`shutil.copy2("chatint.db", "chatint.db.bak")`).
- Add composite index `CREATE INDEX IF NOT EXISTS idx_messages_conv_role_created ON messages(conversation_id, role, created_at);` so `get_conversations()`'s `EXISTS (... role = 'assistant')` subquery and context-window tail queries (`ORDER BY created_at DESC LIMIT ?`) stay instantaneous even with thousands of long conversations.

### R6. Structured Rotating File Logging (`data/logs/chatint.log`) + Global Crash Handler
- Because `ChatInt.spec` builds with `console=False`, all current `print(...)` diagnostics in `db.py`, `streamer.py`, and `main.py` disappear silently in production `.exe` builds.
- Configure Python's `logging` module with a `RotatingFileHandler` (`data/logs/chatint.log`, e.g., `2 MB × 3` backups, scrubbing any API key substrings) plus `sys.excepthook` and `threading.excepthook` so any unexpected crash in the field leaves a clean, actionable stack trace—and add an *"Open Logs Folder"* button in Settings → General.

### R7. Validated Bridge DTOs & Consistent `{ok, data, error}` RPC Envelope
- Today, `DesktopBridge` methods return inconsistent shapes (`{"success": True, ...}` vs `{"error": ...}` vs raw `List` vs `Optional[Dict]`) and trust raw JS arguments (e.g., `int(max_context_messages)` without clamping ranges or checking types).
- Validate all incoming bridge parameters at the boundary (clamping `max_context_messages` to `1..500`, validating `thinking_level` against `VALID_THINKING_LEVELS`, checking non-empty `provider_id`/`model_id`) and return a uniform `{"ok": bool, "data": ..., "error": Optional[str]}` envelope handled centrally by `callApi()` in JS.

### R8. Foolproof Automated Test Suite
Expand `tests/` so `uv run python -m unittest discover -s tests` runs cleanly in <5 seconds and covers:
1. **Concurrency & Cancellation:** Starting Stream B immediately after stopping Stream A on the same `tab_id`; deleting a conversation while a mocked stream/web-search is mid-flight.
2. **Import / Export Fuzzing:** Multi-line system prompts, Markdown containing fenced code blocks with `### User` / `### Assistant`, Unicode/emoji titles, and malformed JSON inputs.
3. **`.env` & Secret Round-Tripping:** Keys with special characters, comments preservation, concurrent writes, and write-only secret masking on the bridge.
4. **Provider Fallback Matrix:** Unit tests verifying OpenAI/Anthropic/Gemini parameter downgrade behavior (`400` unsupported parameter retries vs immediate fail on `401`/`429`).
5. **Security Invariants:** Sanitizer tests against XSS payloads inside Markdown links, images, HTML blocks, and KaTeX expressions.
