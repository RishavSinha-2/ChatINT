# ChatInt Fix & Cleanup Walkthrough

Here is a plain-English guide to every bug that was fixed in ChatInt, what was causing each problem, and how the app was cleaned up to run faster and smoother.

---

## 1. Critical Fixes (Stop Data Loss & Crashes)

### 1.1 Background sync wiping out custom providers and API keys
- **What went wrong:** When ChatInt refreshed model lists in the background for one provider (for example, OpenAI), it accidentally saved *only* that single provider back to the database. Because the save function treated any missing providers as deleted, it could wipe out your other custom providers and erase their saved API keys from `.env`.
- **How we fixed it:**
  - Added a dedicated `update_provider_models()` helper in `src/chatint/db.py` that only updates that one provider's model list without touching anything else.
  - Added a `prune_missing` option on `save_providers()` and updated background sync (`BackgroundModelSyncer` in `src/chatint/client_manager.py`) so it never touches other providers or `.env` keys.

### 1.2 Anthropic streams crashing on mid-stream errors & miscounting tokens
- **What went wrong:**
  1. If an Anthropic (Claude) response dropped in the middle of streaming, the error handler returned the wrong number of values (6 instead of 7), crashing the background worker with an unpacking error before it could cleanly finish.
  2. When Claude sent its final token summary (`message_delta`) with `0` or empty input tokens, the app accidentally overwrote the real input/cache token counts received at the start of the message (`message_start`), making token usage and cost look smaller than they really were.
- **How we fixed it:** Updated `_stream_anthropic()` in `src/chatint/streamer.py` so mid-stream errors return the exact 7 values expected, and updated token tracking so non-zero counts from `message_start` are preserved and combined accurately with cache read/write tokens.

### 1.3 Web search crashing on older/different Gemini accounts or corrupting search status
- **What went wrong:** When running a web search with a missing fallback model (like a 404 Not Found), the search tool reported that error against your main Gemini provider—flipping Gemini's global status to `"invalid_model"` and blocking web search even if other search models worked. Also, Gemini grounding source collection didn't stop cleanly after reaching 6 sources.
- **How we fixed it:** In `src/chatint/web_search_tool.py`, only genuine API-key or internet errors update Gemini's global status; missing-model errors simply try the next search model cleanly, and the source collector stops cleanly at 6 sources.

---

## 2. High-Priority Fixes (UI Glitches, Stuck States & Reliability)

### 2.1 Database connections staying open & `.env` race conditions
- **What went wrong:** Many database functions opened a SQLite connection and forgot to close it in a `finally` block if an error occurred, which can cause `"database is locked"` errors on Windows. Meanwhile, two background threads writing `.env` at the same time could overwrite each other's changes.
- **How we fixed it:**
  - Added a safe `open_db()` context manager in `src/chatint/db.py` so every database connection is automatically closed whether an operation succeeds or fails.
  - Added column whitelists (`_ALLOWED_CONV_COLUMNS`, `_ALLOWED_MSG_COLUMNS`) so invalid column names can never slip into SQL queries.
  - Protected `.env` reads/writes with a thread lock (`_ENV_LOCK`) so simultaneous updates wait their turn safely.

### 2.2 AI replies appearing to drop characters & failing slowly on bad API keys
- **What went wrong:**
  1. While watching for `<think>` tags during streaming, any trailing characters held in the small look-ahead buffer (for example `<thi`) at the end of a stream were silently thrown away instead of being sent to the chat.
  2. When Gemini rejected a request for a bad API key (401/403) or missing model (404), the app retried the same request up to 6 times with different thinking budgets, wasting several seconds on an error that could never succeed.
  3. Clearing the "remembered thinking setting" for a model only cleared memory and skipped the database unless a specific level was named.
- **How we fixed it:**
  - `_flush_think_buffer()` in `src/chatint/streamer.py` now sends any leftover buffered text to the UI instead of discarding it.
  - Added `_is_gemini_retryable_config_error()` so Gemini only retries parameter mismatches (400/422) and immediately shows auth, quota, or network errors.
  - Updated `clear_resolved_thinking_cache()` and `remove_model_resolved_thinking()` so clearing a model's cache cleans both memory and the database.
  - Preserved `thought_signature` parts during Gemini multi-turn tool calls.

### 2.3 Imported chats without AI replies disappearing on restart
- **What went wrong:** ChatInt hides empty "New Chat" tabs from the sidebar until an AI reply arrives. Because imported chats weren't marked as imported, uploading a user-only note or transcript without assistant messages made it vanish from the sidebar after restarting the app.
- **How we fixed it:** Added an `is_imported` flag in the `conversations` table (`src/chatint/db.py`), preserved it when duplicating/branching a chat, and updated `get_conversations()` so imported chats stay visible while blank drafts stay hidden.

### 2.4 Send / Retry / Regenerate buttons getting stuck on errors
- **What went wrong:** In `src/chatint/ui/app.js`:
  - If sending, editing, regenerating, or retrying failed (for example missing message ID or backend error), `tab.isStreaming` stayed stuck as `true` and the Stop button stayed frozen instead of reverting back to Send.
  - Retrying a failed message could read edited text from the wrong element or silently fail if the message hadn't finished saving yet.
  - `bridge.py`'s `edit_message()` didn't double-check whether the target message actually existed in the database before starting a stream.
- **How we fixed it:** Added a shared `setTabStreamingUI()` helper, stored clean `_rawContent` on user message rows, wrapped API calls in `try/catch` to always restore buttons on failure, and made `edit_message()` return an error immediately if the target message isn't found.

### 2.5 Settings "Fetch Models" test corrupting saved status & ignoring toggles
- **What went wrong:**
  - Clicking **Fetch** or **Auto add** inside Settings overwrote the live app's global sync status even before you clicked **Save Changes**, didn't pass whether the provider was enabled or had its key cleared, and stayed stuck in `"Fetching..."` if an error occurred.
  - Canceling Settings after previewing Light/Dark mode or clearing the password box didn't always restore the saved state cleanly.
  - Saving General settings or toggling the header Light/Dark button forced all providers to re-sync or re-rendered every open chat pane—wiping out unsaved message drafts and scroll positions.
- **How we fixed it:**
  - Added `update_sync_state=False` for Settings previews in `client_manager.py` and `bridge.py`, and passed `is_enabled` and `clear_api_key` cleanly.
  - Added a dedicated lightweight `set_theme()` endpoint so the header theme button doesn't touch providers or sync models.
  - Made `save_settings()` sync *only* providers whose key, URL, name, or enabled status actually changed.
  - Updated `saveSettingsFromModal()` in `app.js` to refresh dropdowns, tabs, and token bars **without** destroying open panes—so unsaved message drafts and scroll positions stay intact.

---

## 3. Medium & Low-Priority Fixes

- **External website bar broken on strict websites (`src/chatint/main.py`):** Rewrote the floating "Return to ChatInt" bar without `innerHTML` using safe `document.createElement()` calls so strict security rules (Trusted Types CSP on sites like GitHub and Google) no longer block it.
- **XSS safety inside Markdown responses (`src/chatint/ui/app.js`):** Added a DOM sanitizer (`sanitizeMarkdownDom`) to strip dangerous HTML tags (`<script>`, `<iframe>`, `<style>`, etc.), `on*` event attributes, and `javascript:` links before displaying rendered Markdown, plus protected unclosed streaming code fences and randomized math/code placeholders so literal placeholder text in a chat can't collide.
- **Clicking a link in chat stopped real-time streaming updates (`src/chatint/ui/app.js`):** Clicking a markdown link used to set `isPageUnloading = true`, which permanently froze background polling for all tabs. Removed that flag from link clicks while keeping tab state saving.
- **Keyboard shortcuts & Escape key issues (`src/chatint/ui/app.js`):**
  - Fixed `Escape` inside alert dialogs (`showNoticeDialog` / `showConfirmDialog`) where an old `Enter` key listener or missing Cancel handler stopped `Escape` from closing the popup.
  - Stopped `Ctrl+T`, `Ctrl+W`, `Ctrl+\`, `Ctrl+,`, and `Ctrl+Shift+=` from firing behind open Settings/Export dialogs.
- **Split-pane loading race & highlight styling (`src/chatint/ui/app.js`):**
  - If you switched chats quickly before an older chat finished loading, the slower response could overwrite the newer chat's messages. Added an ID and connection check (`pane.isConnected && tab.conversationId === expectedConvId`) to discard stale loads.
  - Fixed `.all-tabs-popover-item.split-active` CSS class name mismatch (`"split"` → `" split-active"`), preserved model/thinking preferences when branching or importing chats, and matched model IDs consistently regardless of `models/` prefix or uppercase/lowercase differences.
- **Cleaner title generation & safer JSON import (`src/chatint/naming.py`, `src/chatint/bridge.py`):**
  - Stopped `naming.py` from sending invalid requests first to newer OpenAI (`gpt-5` / `o1` / `o3`) models by preferring `max_completion_tokens` with automatic fallback to `max_tokens`.
  - Hardened `parse_chat_file_content()` so malformed non-dictionary entries inside imported JSON lists are safely skipped instead of crashing.

---

## 4. Cleanups & Performance Improvements

1. **Scoped icon updates (`renderIcons` in `src/chatint/ui/app.js`):**
   - Previously, every small UI update—including every 45ms streaming chunk—ran `lucide.createIcons()` across the *entire* page.
   - Replaced all 20+ full-page calls with `renderIcons(container)`, which only scans the small piece of HTML that actually changed.
2. **Removed duplicate & dead code:**
   - Consolidated 4 nearly identical model-fetching handlers in `app.js` into shared `applyFetchedModelsToProvider()`, `applyFetchedModelsToWebSearch()`, and `runProviderFetchAction()` helpers (~200 duplicate lines removed).
   - Removed unused helper functions (`getModelRecencyScore`, `sortModelsNewerFirst`, `syncSearchModelsWithGeminiFetch`) from `app.js`.
   - Extracted shared `is_gemini_3_or_newer()` in `src/chatint/client_manager.py` (used by `streamer.py`, `naming.py`, and `web_search_tool.py`) and `_emit_api_error()` in `src/chatint/streamer.py`.

---

## 5. Verification & Automated Tests

- Updated `tests/test_suite.py` so tests run cleanly in an isolated temporary database and temporary `.env` file (never touching your real `.env` or chat database) and mock network requests cleanly.
- Added automated regression tests covering background sync preservation, Anthropic mid-stream error tuples & token accounting, `<think>` buffer flushing, Gemini auth fast-fail, `open_db()` & column whitelists, imported user-only chats, theme/fetch/selective-sync isolation, missing-message editing, and title generation.
- **Result:** All **22 unit tests** pass (`Ran 22 tests in 4.671s — OK`), all Python modules compile cleanly with `py_compile`, and JavaScript brace/bracket/template balance in `src/chatint/ui/app.js` verified cleanly.
