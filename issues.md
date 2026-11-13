# ChatInt — Final Verification Audit: All 33 Issues & Roadmap Items Resolved (`[fixed]`)

This document records the comprehensive second-pass verification audit across the entire ChatInt codebase (`src/chatint/`, `src/chatint/storage/`, `src/chatint/providers/`, `src/chatint/services/`, `src/chatint/ui/`, `src/chatint/ui/js/`, `tests/`, `ChatInt.spec`, `pyproject.toml`, and `README.md`).

> **Final Audit Verdict Summary:**
> - **Previously Verified Fixed (11 items):** `H2`, `H3`, `M1`, `M2`, `M5`, `M7`, `M11`, `M12`, `L1`, `L2`, `L4`, and `R3` — all re-verified intact with zero regressions.
> - **All 33 Remaining, Regressed & Newly Discovered Items: `[fixed]` and Re-Verified (0 Open Issues Remaining).**
> - **Automated Test Verification:** `uv run python -m unittest discover -s tests -v` runs **50 tests (`OK`, 1 opt-in live `.exe` GUI smoke test skipped by default)**, including **12 live headless Edge Chromium ES-module & strict-CSP E2E behavioral tests** in `tests/test_frontend_ui.py` (zero Node.js/npm).

---

## Summary Overview

| Severity / Category | Count | Status | Key Verified Fixes |
| :--- | :---: | :---: | :--- |
| **1. Critical (Security & Sanitizer)** | 3 | **3 / 3 `[fixed]`** | Sealed `PublicBridgeApi` (`__slots__ = ()` + `__getattribute__` allowlist), fail-closed `_require_trusted_origin`, WebView2 `NavigationStarting` guard, global `dragover`/`drop` & `auxclick`/`mailto:` handling (`C1`); host-bound API key exfiltration guard in `fetch_provider_models` & `save_settings` (`C2`); zero-base-URL `_isSafeUrl`, UI-redressing class/`position: fixed` block, click-to-load external images, and full KaTeX CSS/MathML preservation (`C3`) |
| **2. High (Races, Data Loss & Core UX)** | 6 | **6 / 6 `[fixed]`** | `is_auto_named` column + non-suffixed `"New Chat"` drafts + deleted-chat `chat_named` suppression + draft-close confirmation (`H5`); `.welcome-placeholder` removal without wiping user bubbles (`NEW-H1`); `draftInput`/`activeToolStatus` persistence + `_streamGen` race guard (`NEW-H2`); pre-delete provider validation + `abort_conversation` + post-RPC DOM cleanup (`NEW-H3`); post-`await` `clientStreamId` guard + buffer reset (`H1`); `clear=True` `.env` key wipe on simultaneous rename + lossless whitespace & case-insensitive `.env` keys (`H4`) |
| **3. Medium (SDKs, Tools, Logs & State)** | 11 | **11 / 11 `[fixed]`** | `gpt-5` `"max" -> "xhigh"` & OpenAI-compatible effort clamping + tool-retry before effort downgrade + DeepSeek-only `reasoning_content` replay (`M4`); daemon-thread `fetch_remote_models` timeout with worker-owned client close (`M6`); HTML-comment-delimited `<details>`, unclosed-fence lookahead, `### System`, & JSON token metadata preservation (`M8`); Win32 process-verified `_find_existing_chatint_hwnd` (`M10`); round-start-only `<think>` parser (`NEW-M1`); multi-round `\n\n` thinking separator, live accordion re-expansion, & aborted tool ledger accounting (`NEW-M2`); `_SecretScrubFormatter` traceback + custom-secret scrubbing (`NEW-M3`); **Default Model** selector (`NEW-M4`); dirty-guarded `settingsBuffer` (`NEW-M5`); validated `add_provider`/`save_app_settings` + 25 MB / UTF-8-BOM import (`NEW-M6`); URL/alt-safe KaTeX extraction, currency guard, & GFM task lists (`NEW-M7`) |
| **4. Low (Polish, Persistence, Docs & Tests)** | 6 | **6 / 6 `[fixed]`** | Dirty-tracked `flushTabsState` + Python `window.events.closing` flush (`L3`); `README.md` build, data/logs, & test docs (`L5`); opt-in `CHATINT_RUN_EXE_SMOKE=1` + PID-only cleanup + isolated mutex in `test_single_instance.py` (`L6`); `_origHtml`/`_copyTimer` Copy button & delayed `URL.revokeObjectURL` (`NEW-L1`); `<select>`-safe `Escape`, `.user-bubble.editing`, `!e.isComposing`, deduplicated `conversationId` tabs, & `.markdown-table-scroll` (`NEW-L2`); `PRAGMA busy_timeout=30000` (`NEW-L3`) |
| **5. Roadmap & Architecture** | 7 | **7 / 7 `[fixed]`** | Modularized `storage/`, `providers/`, `services/`, & 9 native ES modules (`R1`); unified `ProviderAdapter` layer (`R2`); Windows DPAPI `dpapi:` secret encryption at rest (`R4`); sequential `MIGRATIONS` runner + WAL-safe `.bak` backup (`R5`); **Open Logs Folder** button & narrowed exception logging (`R6`); uniform `{ok, data, error}` RPC DTOs (`R7`); automated Chromium ES-module behavioral frontend tests (`R8`) |

---

## 1. Critical Issues (Security, Origin Guard Bypasses & Sanitizer Regressions)

### [fixed] C1. `DesktopBridge` Origin Guard Bypass via Public Helper Methods, Fail-Open URL Checks, & Drag-and-Drop Navigation
- **Where:** `src/chatint/bridge.py`, `src/chatint/main.py`, `src/chatint/ui/app.js`
- **How it was fixed & verified:**
  - `webview.create_window` now receives `js_api=bridge.as_public_api()`, a sealed `PublicBridgeApi` (`__slots__ = ()` + strict `__getattribute__` allowlist over `PUBLIC_RPC_METHODS`) that blocks access to `attach_window`, `set_origin_trusted`, `push_event`, `_window`, and `__dict__`.
  - `_require_trusted_origin()` fails **closed** when `allow_headless_rpc=False` and `_window is None`, when `get_current_url()` returns `""` or raises, on mismatched loopback ports, and on any `file://` path other than the exact resolved `index.html`.
  - `_attach_origin_guard()` in `src/chatint/main.py` attaches WebView2's native `NavigationStarting` event (`nav_args.Cancel = True`) in addition to `before_load`/`loaded`, and sets `ALLOW_FILE_URLS = False`.
  - `src/chatint/ui/app.js` intercepts global `dragover`/`drop` (`e.preventDefault()`, allowing safe `.json`/`.md` import drops only on `#sidebar`), intercepts both `click` and `auxclick` (middle-click) on links, and routes valid `http:`, `https:`, and `mailto:` links through `open_external_url`.
  - **Verified by:** `test_c1_sealed_public_bridge_api_and_fail_closed_origin_guard`, `test_c1_origin_guard_and_external_url_validation` (`tests/test_suite.py`), and `test_e2e_csp_bootstrap_dedup_click_to_load_drag_and_shortcuts` (`tests/test_frontend_ui.py`).

### [fixed] C2. Stored API Key Exfiltration via `base_url` Override in `fetch_provider_models` & `save_settings`
- **Where:** `src/chatint/client_manager.py`, `src/chatint/bridge.py`
- **How it was fixed & verified:**
  - Added `normalize_endpoint_origin()` and `is_loopback_endpoint()` in `src/chatint/client_manager.py`.
  - Whenever `base_url` normalized `(scheme, hostname, port)` differs from the persisted provider's `base_url`, both `ClientManager._resolve_provider_auth()` (used by `fetch_provider_models`) and `DesktopBridge.save_settings()` refuse to reuse a previously saved `.env` secret—requiring either an explicit newly entered `api_key`, `clear_api_key=True`, or a loopback local endpoint (`localhost` / `127.0.0.1` / `::1`, which uses placeholder `"ollama"`).
  - **Verified by:** `test_c2_blocks_stored_api_key_exfiltration_on_base_url_override` and `test_c2_get_provider_api_key_returns_masked_only` (`tests/test_suite.py`).

### [fixed] C3. `_isSafeUrl` Dummy-Base Flaw, UI Redressing (`position: fixed` + App Classes), Unfixed Tracking Pixels, & Broken KaTeX Rendering
- **Where:** `src/chatint/ui/js/markdown.js`, `src/chatint/ui/index.html`, `src/chatint/ui/style.css`
- **How it was fixed & verified:**
  - `_isSafeUrl()` in `src/chatint/ui/js/markdown.js` rejects `//` protocol-relative/UNC paths and calls `new URL(trimmed)` **without** any dummy base URL so relative paths (`../../../secret.png`, `other.html`) throw and fail closed.
  - `sanitizeMarkdownDom()` tracks KaTeX-generated nodes in a `WeakSet` (`trustedKatexNodes`), strips ChatInt app/modal classes (`FORBIDDEN_UI_CLASSES`) and `position: fixed | sticky` (`KATEX_POSITION_PROPS` allowed only inside KaTeX), and preserves `border-bottom-width`, `border-top-width`, `border-left-width`, `border-right-width`, `min-height`, `max-height`, `line-height`, plus MathML/SVG tags (`menclose`, `mlabeledtr`, `mglyph`, `circle`, `polygon`, `polyline`).
  - External images from trusted Wikimedia hosts (`upload.wikimedia.org`, `*.wikimedia.org`) auto-load with `referrerpolicy="no-referrer"` and `loading="lazy"`, whereas untrusted external image URLs render as `.blocked-external-image` click-to-load controls so tracking pixels never auto-fire.
  - Updated `index.html` CSP (`data:` & `blob:` image/connect support) and scoped `style.css` image rules to `.assistant-markdown-content img:not(.katex img)`.
  - **Verified by:** `test_c3_is_safe_url_blocks_relative_unc_and_dangerous_schemes`, `test_c3_katex_styles_mathml_preserved_and_ui_redressing_blocked`, `test_c3_external_tracking_pixel_gated_while_wikimedia_autoloads`, and `test_e2e_csp_bootstrap_dedup_click_to_load_drag_and_shortcuts` (`tests/test_frontend_ui.py`).

---

## 2. High-Priority Bugs (Auto-Naming Breakage, Streaming Races & Data Loss)

### [fixed] H5. `"New Chat (1)"` Deduplication Permanently Breaks Auto-Naming, `chat_named` Runs Every Turn & Resurrects Deleted Chats, and `only_if_empty` Deletes User Prompts
- **Where:** `src/chatint/storage/conversations_repo.py`, `src/chatint/services/stream_coordinator.py`, `src/chatint/ui/js/panes.js`, `src/chatint/ui/js/streaming.js`, `src/chatint/ui/js/sidebar.js`
- **How it was fixed & verified:**
  - `create_conversation("New Chat")` keeps default placeholder titles as `"New Chat"` with `is_auto_named = 0` (instead of suffixing `"New Chat (1)"`), and `is_default_conversation_title()` also recognizes legacy `"New Chat (N)"` rows so `update_conversation(..., only_if_default=True)` deduplicates only when assigning the real title and marks `is_auto_named = 1`.
  - `stream_chat_completion_impl()` only triggers `run_auto_naming()` when `not is_auto_named` and `is_default_conversation_title(title)`, eliminating redundant daemon threads and `chat_named` events on Turn 2+.
  - Both `run_auto_naming()` (re-checking `get_conversation(conversation_id)` before dispatch) and `state.recentlyDeletedConvIds` in `streaming.js` prevent deleted chats from resurrecting in the sidebar.
  - `closeTab()` in `panes.js` checks `hasUnsentInput`, `hasUnansweredPrompt`, custom `systemPrompt`, and unmounted restored draft conversations via `get_conversation`—prompting a confirmation dialog before discarding any unsent or unanswered user draft.
  - **Verified by:** `test_h5_multiple_new_chats_auto_name_once_and_ignore_deleted_chats` (`tests/test_suite.py`) and `test_h5_chat_named_ignores_recently_deleted_conversations` (`tests/test_frontend_ui.py`).

### [fixed] NEW-H1. Welcome Placeholder (`"Start a new conversation"`) Never Clears on First Message
- **Where:** `src/chatint/ui/js/panes.js`, `src/chatint/ui/js/streaming.js`
- **How it was fixed & verified:**
  - Added `class="welcome-placeholder"` to `renderWelcomePlaceholder()` and replaced the broken `div[style*="Start a new conversation"]` / `messagesContainer.innerHTML = ""` wipe with `clearWelcomeAndStaleBanners(messagesContainer)` (removing `.welcome-placeholder` and stale `.stream-error-banner` nodes individually while preserving the newly appended user prompt bubble).
  - **Verified by:** `test_new_h1_welcome_placeholder_clears_on_start_without_wiping_user_bubble` (`tests/test_frontend_ui.py`).

### [fixed] NEW-H2. `renderActivePanes()` Wipes Unsaved `<textarea>` Drafts Across All Split Panes & Races `get_conversation` Against `done`
- **Where:** `src/chatint/ui/js/panes.js`, `src/chatint/ui/js/streaming.js`, `src/chatint/ui/js/state.js`
- **How it was fixed & verified:**
  - `renderActivePanes()` snapshots active `<textarea>` values into `tab.draftInput` before unmounting panes, `chatInput.oninput` updates `tab.draftInput` continuously, and `populatePaneContent()` restores `tab.draftInput`, `tab.pendingUserDraft`, and `tab.activeToolStatus`.
  - Added per-tab `_streamGen` counter incremented on `start`, `done`, and `error`; if `_streamGen` changes while `populatePaneContent`'s `get_conversation` RPC is in flight, `populatePaneContent` automatically re-fetches the fresh post-`done` conversation snapshot.
  - **Verified by:** `test_new_h2_render_active_panes_preserves_draft_input_and_tool_status` (`tests/test_frontend_ui.py`).

### [fixed] NEW-H3. Destructive `Regenerate` / `Inline Edit` / `Retry` Deletes DOM & SQLite History Before Stream Validation, Skips `abort_conversation`, & Leaves Orphaned Partial Rows
- **Where:** `src/chatint/bridge.py`, `src/chatint/ui/js/panes.js`
- **How it was fixed & verified:**
  - `DesktopBridge.regenerate_message()` and `DesktopBridge.edit_message()` now validate target message existence and `_check_provider_stream_ready(provider_id, model_id)` **before** mutating SQLite, and call `StreamManager.abort_conversation(conversation_id)` before `delete_messages_from(...)`.
  - In `panes.js`, `Regenerate`, `Inline Edit`, and `Retry` remove all downstream sibling rows (`removeDownstreamSiblings`, including partial assistant rows before an error banner) **only after** the RPC succeeds.
  - **Verified by:** `test_new_h3_regenerate_and_edit_validate_provider_before_deleting_messages` (`tests/test_suite.py`).

### [fixed] H1. Post-`await` `activeStreamId` / `conversationId` Overwrite Race & Stale `streamingContent` Leaking Across Turns
- **Where:** `src/chatint/ui/js/panes.js`, `src/chatint/ui/js/streaming.js`, `src/chatint/ui/js/state.js`
- **How it was fixed & verified:**
  - All stream-initiating flows (`sendMessageFromPane`, `regenerate`, `inlineEditUserMessage`, `Retry`) call `resetTabStreamBuffers(tab)` upfront, guard post-`await` continuations with `if (tab.activeStreamId !== clientStreamId && tab.activeStreamId !== res.stream_id) return;`, and call `persistTabsState()` immediately when `tab.conversationId` is first assigned.
  - `resetTabStreamBuffers(tab)` also clears `streamingContent`, `streamingThinking`, `hasAutoCollapsedThinking`, `isActivelyThinking`, and `activeToolStatus` on `done` and `error`.
  - **Verified by:** `test_h1_stream_manager_isolation_and_event_coalescing` (`tests/test_suite.py`).

### [fixed] H4. `.env` Key Clearing Fails on Simultaneous Provider Rename + Key Whitespace Stripping & Case Mismatch
- **Where:** `src/chatint/storage/secrets.py`, `src/chatint/storage/providers_repo.py`
- **How it was fixed & verified:**
  - Added explicit `clear: bool = False` support to `set_api_key_in_env()` and `batch_update_provider_env_keys()` so clearing an API key while renaming a provider in the same Save writes `""` instead of falling back to `old_val`.
  - Preserved intentional whitespace in secret values (no `.strip()` on API keys, lossless double-quoted `.env` escaping), normalized `*_API_KEY` lookups case-insensitively, and batched `.env` updates into a single atomic read+write per `save_providers()` call.
  - **Verified by:** `test_h4_r4_dpapi_encryption_clear_on_rename_whitespace_and_case_insensitivity` and `test_h4_env_atomic_write_preserves_comments_and_quotes_special_values` (`tests/test_suite.py`).

---

## 3. Medium-Priority Bugs (SDKs, Tool Calling, Import/Export, Logging & Settings)

### [fixed] M4. Incomplete OpenAI `reasoning_effort` Clamping, Tool-Error Cache Poisoning, & Strict `reasoning_content` Rejection on Tool Replay
- **Where:** `src/chatint/providers/openai_adapter.py`
- **How it was fixed & verified:**
  - `_clamp_openai_effort()` clamps `"max" -> "xhigh"` for `gpt-5*` models and clamps `"minimal" -> "low"` / `"xhigh"|"max" -> "high"` for `o1`/`o3`/`o4` and non-`"openai"` OpenAI-compatible providers.
  - `_stream_openai()` peeks the first SSE chunk (`_peek_and_wrap`) before caching `resolved_thinking`, retries `(effort, use_tools=False)` **before** downgrading `effort` so tool-unsupported `400`s never poison `resolved_thinking` to `"none"`, and replays `reasoning_content` on tool-call assistant turns only for DeepSeek endpoints (`_uses_deepseek_reasoning_replay`).
  - **Verified by:** `test_m4_gpt5_clamping_tool_retry_without_cache_poisoning_and_deepseek_replay` (`tests/test_suite.py`).

### [fixed] M6. `ThreadPoolExecutor` Timeout Race Closes `client` Under Running Worker Thread & Blocks Process Exit
- **Where:** `src/chatint/client_manager.py`
- **How it was fixed & verified:**
  - Replaced `ThreadPoolExecutor` in `ClientManager.fetch_remote_models()` with an explicit daemon `threading.Thread(..., daemon=True)` using bounded per-request SDK timeouts (`min(timeout, 15.0)`) and closing `client` inside the worker thread's own `finally:` block so hung socket reads neither race `httpx` close nor block `atexit` shutdown.
  - **Verified by:** `test_m6_hung_fetch_remote_models_times_out_on_daemon_thread` (`tests/test_suite.py`).

### [fixed] M8. Remaining Markdown / JSON Export & Import Data Loss
- **Where:** `src/chatint/services/import_export.py`, `src/chatint/storage/conversations_repo.py`
- **How it was fixed & verified:**
  - Markdown exports delimit thought blocks with `<!-- chatint-thought-start -->...<!-- chatint-thought-end -->` and escape literal `</details>`, include `<!-- chatint-conv-meta: ... -->` / `<!-- chatint-msg-meta: ... -->` comments (`provider_id`, `thinking_level`, `max_context_messages`, timestamps, token counts), export `role == "system"` turns as `### System`, and only enter `in_fence` during Markdown parsing when a matching closing fence exists ahead in the file (`_has_closing_fence`).
  - JSON/Markdown import (`import_conversation_data`) preserves per-message `prompt_tokens`, `completion_tokens`, and `cached_tokens` while keeping `token_ledger` spend at `0`.
  - **Verified by:** `test_m8_markdown_roundtrip_details_unclosed_fence_system_role_and_json_tokens` (`tests/test_suite.py`).

### [fixed] M10. `_find_existing_chatint_hwnd` Matches Unrelated WinForms Windows When Process Verification Fails
- **Where:** `src/chatint/main.py`
- **How it was fixed & verified:**
  - `_find_existing_chatint_hwnd()` tracks `proc_check_unavailable` separately from `proc_ok` and falls back to `cls_name.startswith("WindowsForms10")` **only** when `OpenProcess` / `QueryFullProcessImageNameW` could not inspect the process—never when it succeeded and proved the executable was not `ChatInt.exe` / `python.exe` / `pythonw.exe`.
  - **Verified by:** `test_m10_find_existing_chatint_hwnd_rejects_unrelated_winforms_exe` (`tests/test_suite.py`).

### [fixed] NEW-M1. `<think>` Tag Parser Swallows Normal Prose & Code Blocks Mentioning Literal `<think>`
- **Where:** `src/chatint/providers/base.py`
- **How it was fixed & verified:**
  - `_process_text_with_think_tags()` tracks per-round `[has_visible_text, has_closed_initial_think]` state and only enters `is_in_think_tag = True` once at the very start of an assistant round (after optional leading whitespace). Mid-prose or code-block `<think>` mentions—and even literal `<think>` tags emitted after `</think>` closes—remain visible content.
  - **Verified by:** `test_new_m1_think_tag_parser_only_opens_at_round_start` (`tests/test_suite.py`).

### [fixed] NEW-M2. Multi-Round Tool Calling Collapses Thinking Accordion Prematurely, Concatenates Thoughts Without Separator, & Loses Tokens on Aborted Tool Turns
- **Where:** `src/chatint/providers/base.py`, `src/chatint/services/stream_coordinator.py`, `src/chatint/ui/js/streaming.js`, `src/chatint/ui/js/panes.js`
- **How it was fixed & verified:**
  - Added `_ensure_post_tool_thinking_separator()` inserting `\n\n` between multi-round reasoning blocks, guarded `_run_web_search_tool_call()` so `status: "done"` is suppressed when `abort_event.is_set()`, and recorded `token_ledger` usage in `stream_chat_completion_impl()` whenever `prompt_tokens > 0 or completion_tokens > 0` even if no assistant message row is saved.
  - On the frontend, `thinking_chunk` sets `tab.isActivelyThinking = true` and `tab.hasAutoCollapsedThinking = false`, causing `applyLiveStreamRender()` to re-expand `#live-thinking-${tab.id}` with live `"Thinking..."` status during Round 1+ reasoning.
  - **Verified by:** `test_new_m2_multi_round_thinking_separator_abort_suppression_and_aborted_tool_ledger` (`tests/test_suite.py`) and `test_new_m2_multi_round_thinking_reexpands_live_accordion` (`tests/test_frontend_ui.py`).

### [fixed] NEW-M3. Log Secret Scrubber (`_SecretScrubFilter`) Misses Exception Tracebacks (`exc_info`) & Non-`sk-`/`AIza` API Keys
- **Where:** `src/chatint/logging_setup.py`, `src/chatint/storage/secrets.py`
- **How it was fixed & verified:**
  - Added `_SecretScrubFormatter(logging.Formatter)` which scrubs the **entire** formatted log record (including `exc_info` tracebacks), expanded `_SECRET_PATTERNS` to cover `sk-`, `AIza`, `gsk_`, `xai-`, `pplx-`, `hf_`, `csk-`, `dpapi:`, `Bearer`, `x-api-key`, `x-goog-api-key`, `?key=`, and `*API_KEY=`, and automatically synced all non-placeholder secrets from `.env` via `replace_known_secrets()`.
  - **Verified by:** `test_new_m3_r6_log_formatter_scrubs_tracebacks_provider_keys_and_custom_secrets` (`tests/test_suite.py`).

### [fixed] NEW-M4. `default_model` Setting Has No UI Control in Settings → General & Stays Stuck at `"gpt-4o"`
- **Where:** `src/chatint/ui/js/settings.js`, `src/chatint/ui/js/panes.js`
- **How it was fixed & verified:**
  - Added `<select id="setting-default-model">` in **Settings → General**, dynamically populated from the selected Default Provider's configured models (with `"Top Priority Model (Automatic)"` option when empty) and wired into `saveGeneralTabInputs()` and `updatePaneModelDropdown()`.
  - **Verified by:** `test_new_m4_new_m5_r6_settings_default_model_logs_button_and_dirty_buffer_guard` (`tests/test_frontend_ui.py`).

### [fixed] NEW-M5. Settings Buffer Clobbered by Background Sync & Edge-Case Bugs on Blank Name / Blank Base URL / New Custom Provider Fetch
- **Where:** `src/chatint/ui/js/settings.js`, `src/chatint/ui/js/streaming.js`, `src/chatint/storage/providers_repo.py`
- **How it was fixed & verified:**
  - Tracked `_keyEdited` and `_urlEdited` per provider in `state.settingsBuffer` so `provider_models_updated` events never overwrite uncommitted edits, and guarded modal re-renders when a `<select>` is focused or `#new-model-id-input` / `#ws-new-model-id-input` has draft text.
  - Assigned `activeProv.name = cleaned` unconditionally so blank names fail validation cleanly, passed `""` (not `null`) when `base_url` was cleared, passed `prov.name` as the 6th argument to `fetch_provider_models`, and enabled keyless loopback endpoints (`localhost` / `127.0.0.1`) on newly added custom providers.
  - **Verified by:** `test_new_m4_new_m5_r6_settings_default_model_logs_button_and_dirty_buffer_guard` (`tests/test_frontend_ui.py`) and `test_c2_blocks_stored_api_key_exfiltration_on_base_url_override` (`tests/test_suite.py`).

### [fixed] NEW-M6. Unvalidated Bridge Endpoints (`add_provider`, `save_app_settings`, `import_conversation_content`) & Non-UTF-8 / Unbounded File Import
- **Where:** `src/chatint/bridge.py`, `src/chatint/storage/settings_repo.py`, `src/chatint/services/import_export.py`, `src/chatint/ui/js/sidebar.js`
- **How it was fixed & verified:**
  - Validated `DesktopBridge.add_provider()` with `is_valid_provider_name` and case-insensitive duplicate checks, enforced `ALLOWED_APP_SETTING_KEYS` in `save_app_settings()`, capped imported chat files/payloads at `25 MB` (`MAX_IMPORT_BYTES`), and decoded imported files with `utf-8-sig` (stripping UTF-8 BOM) and `cp1252` fallback.
  - **Verified by:** `test_new_m6_new_l3_r2_r5_r7_validation_bom_import_busy_timeout_and_migrations` (`tests/test_suite.py`).

### [fixed] NEW-M7. KaTeX Placeholder Leaks into Markdown URLs/Image Alt Text, Inline `$` Currency False Positives, & Stripped Task List Checkboxes
- **Where:** `src/chatint/ui/js/markdown.js`, `src/chatint/ui/style.css`
- **How it was fixed & verified:**
  - Protected Markdown images (`![alt](url)`), link destinations (`](url)`), HTML attributes, and bare URLs before `$` math extraction (plus attribute fallback restoration), required closing `$` to match `\$(?![\$0-9])` with currency range filtering (`$100–$200`, `$5-$10`, `$5/$10`), rendered display math with `<span class="katex-display-block">`, and preserved `<input type="checkbox" disabled>` inside GFM task-list items.
  - **Verified by:** `test_new_m7_currency_urls_image_alt_task_lists_and_table_scroll` (`tests/test_frontend_ui.py`).

---

## 4. Low-Priority / Minor Issues (Persistence, UI Glitches, Docs & Tests)

### [fixed] L3. Unconditional `beforeunload` IPC Call & Unreliable WebView2 Shutdown Flush
- **Where:** `src/chatint/ui/js/api.js`, `src/chatint/bridge.py`, `src/chatint/main.py`
- **How it was fixed & verified:**
  - `flushTabsState()` in `src/chatint/ui/js/api.js` tracks `tabsStateDirty` so `beforeunload` is a zero-IPC no-op when clean, and `DesktopBridge` caches `_latest_tabs_state` in memory and flushes deterministically via `window.events.closing += bridge.flush_tabs_state` on Python shutdown.
  - **Verified by:** `test_l3_r7_dirty_tabs_flush_and_uniform_rpc_envelope` (`tests/test_frontend_ui.py`).

### [fixed] L5. `README.md` Omits Standalone `.exe` Build Instructions, Data/Log Locations, & Test Commands
- **Where:** `README.md`
- **How it was fixed & verified:**
  - Added **Building Standalone `.exe`**, **Data & Logs** (`.env` DPAPI encryption, `data/chatint.db` + `data/chatint.db.bak`, `data/logs/chatint.log`), and **Running Tests** sections to `README.md`.

### [fixed] L6. `test_dist_exe.py` Hardcodes `dist/v0.4.0`, Skips Fresh Builds Without `chatint.db`, & Kills Live `ChatInt.exe` Sessions (`test_single_instance.py` Also Collides with Open App)
- **Where:** `tests/test_dist_exe.py`, `tests/test_single_instance.py`, `src/chatint/main.py`
- **How it was fixed & verified:**
  - `tests/test_dist_exe.py` is gated behind `CHATINT_RUN_EXE_SMOKE=1` / `CHATINT_EXE_PATH`, skips if a live `"ChatInt"` window is already open, launches `ChatInt.exe` *before* asserting `data/chatint.db` creation, declares 64-bit `ctypes` `argtypes`/`restype`, and terminates only `proc1.pid` (never `/IM ChatInt.exe`).
  - `_ensure_single_instance(mutex_name=...)` accepts an isolated mutex name used by `tests/test_single_instance.py`.
  - **Verified by:** `test_single_instance.py` and `test_dist_exe.py`.

### [fixed] NEW-L1. Double-Clicking Any Copy Button Permanently Locks Its Label to `"Copied!"` + Synchronous `URL.revokeObjectURL` Race
- **Where:** `src/chatint/ui/js/dialogs.js`, `src/chatint/ui/js/sidebar.js`
- **How it was fixed & verified:**
  - `copyText()` stores `btnElement._origHtml` once and clears `btnElement._copyTimer` before resetting the timer, and `downloadExport()` delays `URL.revokeObjectURL(url)` via `setTimeout(..., 10000)`.
  - **Verified by:** `test_new_l1_copy_button_double_click_preserves_original_html` (`tests/test_frontend_ui.py`).

### [fixed] NEW-L2. Modal `Escape` Handling, Inline Edit Bubble Layout Glitch, Missing IME `isComposing` Check, & Duplicate `conversationId` Tabs / Stale Split-Pane Headers
- **Where:** `src/chatint/ui/app.js`, `src/chatint/ui/js/panes.js`, `src/chatint/ui/js/dialogs.js`, `src/chatint/ui/js/settings.js`, `src/chatint/ui/js/markdown.js`, `src/chatint/ui/style.css`
- **How it was fixed & verified:**
  - Global `keydown` ignores `e.isComposing`, ignores `Escape` when `e.target.tagName === "SELECT"`, cancels `.user-bubble.editing` on `Escape`, and normalizes `e.key.toLowerCase()` so `Ctrl+T` / `Ctrl+W` work under `CapsLock`.
  - Added `.user-bubble.editing` (`white-space: normal; width: 100%`), guarded all `Enter` handlers with `!e.isComposing`, deduplicated `conversationId` across restored tabs in `bootstrapData()`, updated split-pane header labels via `refreshPaneHeaderLabels()` on rename/`chat_named`, and wrapped Markdown tables in `.markdown-table-scroll`.
  - **Verified by:** `test_e2e_csp_bootstrap_dedup_click_to_load_drag_and_shortcuts` and `test_new_m7_currency_urls_image_alt_task_lists_and_table_scroll` (`tests/test_frontend_ui.py`).

### [fixed] NEW-L3. `get_connection()` Overrides 30s Connection Timeout Down to 10s
- **Where:** `src/chatint/storage/connection.py`
- **How it was fixed & verified:**
  - Aligned `conn.execute("PRAGMA busy_timeout=30000;")` with `sqlite3.connect(..., timeout=30.0)`.
  - **Verified by:** `test_new_m6_new_l3_r2_r5_r7_validation_bom_import_busy_timeout_and_migrations` (`tests/test_suite.py`).

---

## 5. Roadmap & Architectural Items

### [fixed] R1. Modularize the Three "God Files" (`app.js`, `db.py`, `streamer.py`)
- **How it was fixed & verified:**
  - Split `src/chatint/db.py` (**1,686 → 130 lines**) into `src/chatint/catalog.py` and `src/chatint/storage/{connection,conversations_repo,dpapi,ledger_repo,providers_repo,secrets,settings_repo}.py`.
  - Split `src/chatint/streamer.py` (**1,503 → 123 lines**) into `src/chatint/providers/{base,openai_adapter,anthropic_adapter,gemini_adapter}.py` and `src/chatint/services/{stream_coordinator,import_export}.py`.
  - Split `src/chatint/ui/app.js` (**4,236 → 511 lines**) into native ES modules (`src/chatint/ui/js/{state,api,dialogs,markdown,panes,sidebar,settings,streaming}.js`) loaded via `<script type="module" src="app.js"></script>` with zero Node.js/npm/bundlers, and registered explicit `.js`/`.mjs` MIME types in `src/chatint/main.py` plus PyInstaller `hiddenimports` in `ChatInt.spec`.
  - **Verified by:** `test_r1_modular_codebase_structure` (`tests/test_frontend_ui.py`).

### [fixed] R2. Unified `ProviderAdapter` Layer
- **How it was fixed & verified:**
  - Implemented `ProviderAdapter` base contract (`src/chatint/providers/base.py`) with concrete `OpenAIAdapter`, `AnthropicAdapter`, and `GeminiAdapter` classes (`get_provider_adapter`), unifying model listing (`list_raw_models`), chat title completion (`complete_title`), and multi-round tool/thinking streaming (`stream_chat`) across `client_manager.py`, `naming.py`, and `streamer.py`.
  - **Verified by:** `test_new_m6_new_l3_r2_r5_r7_validation_bom_import_busy_timeout_and_migrations` (`tests/test_suite.py`).

### [fixed] R4. Encrypted Secret Storage at Rest (Windows DPAPI)
- **How it was fixed & verified:**
  - Implemented user-scoped Windows DPAPI encryption (`CryptProtectData` / `CryptUnprotectData` via `ctypes` in `src/chatint/storage/dpapi.py`). Secrets written to `.env` are automatically encrypted with a `dpapi:` prefix on Windows while transparently reading and upgrading legacy plaintext entries.
  - **Verified by:** `test_h4_r4_dpapi_encryption_clear_on_rename_whitespace_and_case_insensitivity` (`tests/test_suite.py`).

### [fixed] R5. Sequential Schema Migration Runner & Pre-Migration `.bak` Backup
- **How it was fixed & verified:**
  - Implemented `SCHEMA_VERSION = 2` and ordered `MIGRATIONS` (`_migration_v1`, `_migration_v2`) in `src/chatint/storage/connection.py`, with automatic online SQLite `conn.backup(...)` creation of `chatint.db.bak` (`contextlib.closing` safe on Windows) before upgrading lower-version populated databases.
  - **Verified by:** `test_new_m6_new_l3_r2_r5_r7_validation_bom_import_busy_timeout_and_migrations` (`tests/test_suite.py`).

### [fixed] R6. *"Open Logs Folder"* Button in Settings & Structured Exception Logging
- **How it was fixed & verified:**
  - Added `DesktopBridge.open_logs_folder()` (`src/chatint/bridge.py`) and `#open-logs-folder-btn` (**Open Logs Folder**) in **Settings → General** (`src/chatint/ui/js/settings.js`), and replaced silent broad `except Exception: pass` blocks across `storage/`, `client_manager.py`, `web_search_tool.py`, `services/import_export.py`, and `main.py` with narrow exception types or structured `_logger.debug`/`warning` diagnostics.
  - **Verified by:** `test_new_m3_r6_log_formatter_scrubs_tracebacks_provider_keys_and_custom_secrets` (`tests/test_suite.py`) and `test_new_m4_new_m5_r6_settings_default_model_logs_button_and_dirty_buffer_guard` (`tests/test_frontend_ui.py`).

### [fixed] R7. Uniform `{ok, data, error}` Bridge DTO Envelope
- **How it was fixed & verified:**
  - `wrap_rpc_envelope()` and `make_public_bridge_api()` (`src/chatint/bridge.py`) wrap every JS-exposed RPC response in a uniform `{"ok": bool, "data": ..., "error": Optional[str]}` envelope, paired with `callApiEnvelope()` and `callApi()` in `src/chatint/ui/js/api.js`.
  - **Verified by:** `test_c1_sealed_public_bridge_api_and_fail_closed_origin_guard` (`tests/test_suite.py`) and `test_l3_r7_dirty_tabs_flush_and_uniform_rpc_envelope` (`tests/test_frontend_ui.py`).

### [fixed] R8. Automated Frontend Sanitizer, KaTeX & Streaming State Behavioral Tests
- **How it was fixed & verified:**
  - Created `tests/test_frontend_ui.py`, which boots a local Python HTTP server and runs 12 behavioral tests inside headless Microsoft Edge Chromium (WebView2's engine) against the real production ES modules, `DOMParser`, `marked.min.js`, `katex.min.js`, `highlight.min.js`, and strict `Content-Security-Policy`—requiring zero Node.js or npm.
  - **Verified by:** `test_r8_browser_es_modules_sanitizer_katex_and_streaming_state` (`tests/test_frontend_ui.py`).
