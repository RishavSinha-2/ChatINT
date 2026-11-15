# Unresolved Codebase Issues

## Overview
This file tracks active, **unresolved** defects, edge cases, and architectural issues in ChatInt. Following the prompt directive, resolved issues have been pruned so that **only unresolved issues** are maintained here.

---

## Active Unresolved Issues
> **Status:** There are currently **0 unresolved issues** in the codebase.
> 
> All 28 previously discovered issues across Phase 1 (`REF-M1` through `REF-L6`), Phase 2 (`DEF-M1` through `DEF-L10`), Phase 3 (`DEF-L11` through `DEF-L13`), and Phase 4 (`DEF-L14`) have been fully remediated, verified, and closed.

---

## Audit & Verification Matrix

The codebase was subjected to an exhaustive static analysis, strict type-checking, behavioral testing, and standalone binary evaluation:

| Verification Suite | Target Scope | Command | Result |
|---|---|---|---|
| **Ruff Linter & Bugbear** | Codebase quality, closure safety, dead code | `uv run ruff check src tests --select F,B` | **Passed (0 issues)** |
| **Mypy Type Checker** | Strict static type definitions across modules | `uv run mypy src tests --ignore-missing-imports` | **Passed (0 errors in 34 files)** |
| **Automated Test Suite** | Backend APIs, storage, streaming, path resolution, and self-healing DB | `uv run python -m unittest discover tests` | **Passed (67/67 tests OK)** |
| **Windows Executable** | Optimized binary (32.65 MB, single-instance, 0s reopen handover) | `dist/v0.5.1/ChatInt.exe` | **Passed (embedded version info verified)** |
| **Portable Distribution** | Zero-install ZIP package (32.04 MB) with `.env` and `data/` | `dist/v0.5.1/ChatInt-Portable-v0.5.1.zip` | **Passed (packaged with portable config)** |
| **Dual Publishing** | Inno Setup WinGet installer script and documentation | `packaging/ChatInt.iss`, `packaging/WINGET.md` | **Verified (user-level installer, upgrade preservation)** |

---

## Summary of Completed Remediations (Pruned from Active Tracking)

For historical reference, the 28 resolved issues that were verified and pruned from active tracking comprise:
- **Refactoring & UI Audits (`REF-M1` – `REF-L6`):**
  - `REF-M1`: Null thumbnail & empty imageinfo guard in Wikimedia API (`web_search_tool.py`).
  - `REF-M2`: Balanced-parentheses URL extraction in Markdown images and links (`markdown.js`).
  - `REF-M3`: Scheme-less loopback endpoint parsing (`state.js`).
  - `REF-M4`: Disambiguated cumulative ledger spend vs active turn tokens (`ledger_repo.py`).
  - `REF-L1`: Preserved native drag-and-drop text editing in inputs/textareas (`app.js`).
  - `REF-L2`: Re-queried `#theme-icon` DOM reference after theme switch (`dialogs.js`).
  - `REF-L3`: Coalesced contiguous streamed Part objects in multi-round Gemini calls (`gemini_adapter.py`).
  - `REF-L4`: Added dedicated "Branch Chat" button in chat pane headers (`panes.js`).
  - `REF-L5`: Atomic tool-call budget limit (`MAX_TOOL_CALLS_PER_PROMPT = 10`) across parallel calls (`web_search_tool.py`, adapters).
  - `REF-L6`: Loopback subnet check expansion for `0.0.0.0` and `127.0.0.0/8` (`client_manager.py`).

- **Static, Type, and Concurrency Audits (`DEF-M1` – `DEF-L14`):**
  - `DEF-M1`: Guarded against null provider IDs in SDK type resolution (`client_manager.py`).
  - `DEF-M2`: Replaced foreign `httpx.Timeout` with SDK-native `openai.Timeout` / `anthropic.Timeout` (`client_manager.py`).
  - `DEF-M3`: Added null check on `webview.create_window` return value (`main.py`).
  - `DEF-M4`: Fixed broken import path for `is_gemini_3_or_newer` to `chatint.catalog` (`web_search_tool.py`, `test_suite.py`).
  - `DEF-L1`: Added `*.markdown` support to file picker and native open dialog filters (`bridge.py`, `index.html`).
  - `DEF-L2`: Corrected `import_conversation_data` return type annotation to `Optional[Dict[str, Any]]` (`conversations_repo.py`).
  - `DEF-L3`: Added null guards prior to stream iteration in OpenAI and Anthropic adapters (`openai_adapter.py`, `anthropic_adapter.py`).
  - `DEF-L4`: Cleaned up re-typed variable and renamed unused loop variable (`web_search_tool.py`).
  - `DEF-L5`: Inlined stream processing and text coalescing in Gemini adapter to resolve Bugbear B023 loop closures (`gemini_adapter.py`, `openai_adapter.py`).
  - `DEF-L6`: Fixed truthiness check warning on `sys.stderr` (`logging_setup.py`).
  - `DEF-L7`: Added type annotation to `_event_queue` in `DesktopBridge` (`bridge.py`).
  - `DEF-L8`: Explicit `str()` coercion during default provider seeding (`secrets.py`, `connection.py`).
  - `DEF-L9`: Removed unused imports and variables across codebase (`providers_repo.py`, `client_manager.py`, `tests`).
  - `DEF-L10`: Replaced `getattr(db_mod, ...)` with direct attribute access (`connection.py`, `secrets.py`).
  - `DEF-L11`: Empty title bypass guard in `update_conversation(conv_id, title="")` normalizing to unique non-empty title (`conversations_repo.py`).
  - `DEF-L12`: Number-only candidate title handling (e.g. `"(1)"`) in `get_unique_conversation_title` preventing leading whitespace base (`conversations_repo.py`).
  - `DEF-L13`: Committed SQLite table resets in test suite `setUp` avoiding cross-test row leakage (`test_suite.py`).
  - `DEF-L14`: Single-instance mutex held across 5-9s process teardown blocking immediate application reopening after closing window (`main.py`, `test_single_instance.py`).
