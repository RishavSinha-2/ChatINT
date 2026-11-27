import os
import sys
import tempfile
import sqlite3
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add src to sys.path
sys.path.insert(0, os.path.abspath("src"))

import chatint.config as cfg_mod
import chatint.db as db_mod
from chatint.db import (
    init_db,
    open_db,
    create_conversation,
    get_conversation,
    get_conversations,
    update_conversation,
    update_message,
    delete_conversation,
    duplicate_conversation,
    import_conversation_data,
    get_branch_title,
    record_token_usage,
    get_token_stats,
    add_message,
    get_providers,
    save_providers,
    update_model_resolved_thinking,
    get_model_resolved_thinking,
    get_api_key_from_env,
    set_api_key_in_env,
    mask_api_key,
    get_app_settings,
)
from chatint.bridge import parse_chat_file_content, DesktopBridge
from chatint.catalog import is_gemini_3_or_newer
from chatint.client_manager import (
    BackgroundModelSyncer,
    ClientManager,
    _is_network_available,
    _is_transport_exception,
)
from chatint.naming import generate_chat_title
from chatint.web_search_tool import (
    IMAGE_SEARCH_TOOL_NAME,
    MAX_TOOL_ROUNDS,
    get_openai_image_tool_spec,
    get_anthropic_image_tool_spec,
    get_web_search_system_hint,
    execute_image_search,
    execute_web_search,
)
from chatint.streamer import (
    StreamManager,
    stream_chat_completion,
    _run_web_search_tool_call,
    _flush_think_buffer,
    _stream_anthropic,
    _stream_gemini,
    _is_openai_retryable_param_error,
    _clamp_openai_effort,
    clear_resolved_thinking_cache,
)


class TestChatIntFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create a temporary directory for test DB & isolated .env
        cls.test_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.test_dir.name) / "test_chatint.db"
        cls.env_path = Path(cls.test_dir.name) / "test.env"
        cls._orig_cfg_db = cfg_mod.DB_PATH
        cls._orig_db_db = db_mod.DB_PATH
        cls._orig_cfg_env = cfg_mod.ENV_PATH
        cls._orig_db_env = db_mod.ENV_PATH

        db_mod.DB_PATH = cls.db_path
        cfg_mod.DB_PATH = cls.db_path
        db_mod.ENV_PATH = cls.env_path
        cfg_mod.ENV_PATH = cls.env_path
        init_db()

    @classmethod
    def tearDownClass(cls):
        db_mod.DB_PATH = cls._orig_db_db
        cfg_mod.DB_PATH = cls._orig_cfg_db
        db_mod.ENV_PATH = cls._orig_db_env
        cfg_mod.ENV_PATH = cls._orig_cfg_env
        cls.test_dir.cleanup()

    def setUp(self):
        import shutil
        # Reset global tables, per-conv DB folder, and isolate .env between tests
        shutil.rmtree(Path(self.test_dir.name) / "conversations", ignore_errors=True)
        with open_db() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM conversations")
            c.execute("DELETE FROM providers")
            c.execute("DELETE FROM app_settings")
            c.execute("DELETE FROM tab_sessions")
            c.execute("DELETE FROM google_search_usage")
            conn.commit()
        if self.env_path.exists():
            self.env_path.unlink()
        BackgroundModelSyncer._verified_providers.clear()
        BackgroundModelSyncer._verified_model_ids.clear()
        BackgroundModelSyncer._sync_status.clear()
        init_db()

    # --- 1. DEDUPLICATION OF TITLES ---
    def test_unique_conversation_title_basic(self):
        conv1 = create_conversation("Project Apollo")
        self.assertEqual(conv1["title"], "Project Apollo")

        # Duplicate title should get (1)
        conv2 = create_conversation("Project Apollo")
        self.assertEqual(conv2["title"], "Project Apollo (1)")

        # Third duplicate should get (2)
        conv3 = create_conversation("Project Apollo")
        self.assertEqual(conv3["title"], "Project Apollo (2)")

        # Case-insensitivity check
        conv4 = create_conversation("project apollo")
        self.assertEqual(conv4["title"], "project apollo (3)")

    def test_unique_conversation_title_increment(self):
        # When user creates "Report (1)" directly and "Report (1)" already exists
        create_conversation("Report (1)")
        conv2 = create_conversation("Report (1)")
        self.assertEqual(conv2["title"], "Report (2)")

        conv3 = create_conversation("Report (2)")
        self.assertEqual(conv3["title"], "Report (3)")

    def test_update_conversation_preserves_own_title(self):
        conv = create_conversation("Alpha")
        # Updating settings on the same conversation should NOT increment title
        update_conversation(conv["id"], system_prompt="You are helpful.")
        updated = get_conversation(conv["id"])
        self.assertEqual(updated["title"], "Alpha")
        self.assertEqual(updated["system_prompt"], "You are helpful.")

    def test_update_conversation_rename_collision(self):
        create_conversation("Original")
        c2 = create_conversation("Target")

        # Try renaming c2 to "Original"
        resolved = update_conversation(c2["id"], title="Original")
        self.assertEqual(resolved, "Original (1)")
        c2_updated = get_conversation(c2["id"])
        self.assertEqual(c2_updated["title"], "Original (1)")

    # --- 2. BRANCHING / DUPLICATING ---
    def test_branch_title_generation(self):
        title1 = get_branch_title("Design Document")
        self.assertEqual(title1, "Design Document (Branch)")

        create_conversation("Design Document (Branch)")
        title2 = get_branch_title("Design Document")
        self.assertEqual(title2, "Design Document (Branch 1)")

        create_conversation("Design Document (Branch 1)")
        title3 = get_branch_title("Design Document")
        self.assertEqual(title3, "Design Document (Branch 2)")

        # Branching from an already branched conversation
        title4 = get_branch_title("Design Document (Branch 1)")
        self.assertEqual(title4, "Design Document (Branch 2)")

    def test_duplicate_conversation(self):
        c1 = create_conversation("Research Topic", system_prompt="Expert Analyst", max_context=20)
        # Add messages and token usage to c1
        add_message(c1["id"], "user", "What is quantum computing?", prompt_tokens=25)
        add_message(
            c1["id"],
            "assistant",
            "Quantum computing uses qubits...",
            thinking_content="Thinking about superposition...",
            provider_id="openai",
            model_id="gpt-4o",
            thinking_level="high",
            prompt_tokens=25,
            completion_tokens=150,
            cached_tokens=10,
        )
        record_token_usage(c1["id"], "openai", "gpt-4o", 25, 150, 10)

        # Check c1 token stats are non-zero
        stats1 = get_token_stats(c1["id"])
        self.assertGreater(stats1["grand_total"]["total_tokens"], 0)

        # Now duplicate c1
        branch = duplicate_conversation(c1["id"])
        self.assertIsNotNone(branch)
        self.assertEqual(branch["title"], "Research Topic (Branch)")
        self.assertEqual(branch["system_prompt"], "Expert Analyst")
        self.assertEqual(branch["max_context_messages"], 20)
        self.assertNotEqual(branch["id"], c1["id"])

        # Check messages are duplicated with exact content and thinking
        self.assertEqual(len(branch["messages"]), 2)
        bm1 = branch["messages"][0]
        bm2 = branch["messages"][1]
        self.assertEqual(bm1["role"], "user")
        self.assertEqual(bm1["content"], "What is quantum computing?")
        self.assertEqual(bm2["role"], "assistant")
        self.assertEqual(bm2["content"], "Quantum computing uses qubits...")
        self.assertEqual(bm2["thinking_content"], "Thinking about superposition...")
        self.assertEqual(bm2["model_id"], "gpt-4o")
        self.assertEqual(bm2["thinking_level"], "high")

        # Crucial requirement: Token usage on the branch starts strictly from 0
        self.assertEqual(bm1["prompt_tokens"], 0)
        self.assertEqual(bm2["prompt_tokens"], 0)
        self.assertEqual(bm2["completion_tokens"], 0)
        self.assertEqual(bm2["cached_tokens"], 0)

        branch_stats = get_token_stats(branch["id"])
        self.assertEqual(branch_stats["grand_total"]["total_tokens"], 0)
        self.assertEqual(branch_stats["grand_total"]["total_cost"], 0.0)

        # Ensure original c1 is untouched
        c1_after = get_conversation(c1["id"])
        self.assertEqual(len(c1_after["messages"]), 2)
        self.assertEqual(c1_after["title"], "Research Topic")

    # --- 3. CHAT FILE PARSING & IMPORT ---
    def test_parse_json_export(self):
        json_content = json.dumps({
            "title": "My Exported Chat",
            "system_prompt": "You are a code reviewer.",
            "max_context_messages": 40,
            "messages": [
                {"role": "user", "content": "Review this snippet"},
                {
                    "role": "assistant",
                    "content": "The code looks solid.",
                    "thinking_content": "Inspecting bounds and memory safety...",
                    "provider_id": "anthropic",
                    "model_id": "claude-3-7-sonnet",
                    "thinking_level": "medium",
                }
            ]
        })

        parsed = parse_chat_file_content(json_content, filename="export.json")
        self.assertEqual(parsed["title"], "My Exported Chat")
        self.assertEqual(parsed["system_prompt"], "You are a code reviewer.")
        self.assertEqual(parsed["max_context"], 40)
        self.assertEqual(len(parsed["messages"]), 2)
        self.assertEqual(parsed["messages"][1]["thinking_content"], "Inspecting bounds and memory safety...")

    def test_parse_markdown_export(self):
        md_content = """# Machine Learning Basics
> **System Prompt:** Act as an ML Professor.

### User
Explain gradient descent.

### Assistant (claude-3-7-sonnet)
<details><summary>Thought Process</summary>
Gradient descent is an optimization algorithm that iteratively follows the negative gradient.
</details>

Gradient descent minimizes the loss function step by step.
"""
        parsed = parse_chat_file_content(md_content, filename="ml.md")
        self.assertEqual(parsed["title"], "Machine Learning Basics")
        self.assertEqual(parsed["system_prompt"], "Act as an ML Professor.")
        self.assertEqual(len(parsed["messages"]), 2)
        self.assertEqual(parsed["messages"][0]["role"], "user")
        self.assertEqual(parsed["messages"][0]["content"], "Explain gradient descent.")
        self.assertEqual(parsed["messages"][1]["role"], "assistant")
        self.assertIn("minimizes the loss function", parsed["messages"][1]["content"])
        self.assertIn("optimization algorithm", parsed["messages"][1]["thinking_content"])
        self.assertEqual(parsed["messages"][1]["model_id"], "claude-3-7-sonnet")

    def test_import_conversation_data(self):
        create_conversation("Imported Chat")
        # Now import with the same title -> should become "Imported Chat (1)"
        imported = import_conversation_data(
            title="Imported Chat",
            system_prompt="System instructions",
            max_context=25,
            messages=[
                {"role": "user", "content": "Hello!"},
                {"role": "assistant", "content": "Hi there!", "thinking_content": "Greeting..."}
            ]
        )
        self.assertEqual(imported["title"], "Imported Chat (1)")
        self.assertEqual(imported["system_prompt"], "System instructions")
        self.assertEqual(len(imported["messages"]), 2)

        # Starts with 0 token ledger
        stats = get_token_stats(imported["id"])
        self.assertEqual(stats["grand_total"]["total_tokens"], 0)

    # --- 4. IMAGE SEARCH TOOL SPECIFICATIONS & LIVE EXECUTION ---
    def test_image_search_specs(self):
        op = get_openai_image_tool_spec()
        self.assertEqual(op["function"]["name"], IMAGE_SEARCH_TOOL_NAME)
        self.assertIn("illustrate and explain", op["function"]["description"])

        ant = get_anthropic_image_tool_spec()
        self.assertEqual(ant["name"], IMAGE_SEARCH_TOOL_NAME)
        self.assertIn("illustrate and explain", ant["description"])

        hint = get_web_search_system_hint()
        self.assertIn(IMAGE_SEARCH_TOOL_NAME, hint)
        self.assertIn("illustrate and explain", hint)
        self.assertEqual(MAX_TOOL_ROUNDS, 10)

    def test_image_search_execution(self):
        wiki_resp = MagicMock()
        wiki_resp.status_code = 200
        wiki_resp.json.return_value = {
            "query": {
                "pages": {
                    "1": {
                        "title": "Golden Gate Bridge",
                        "original": {"source": "https://upload.wikimedia.org/bridge.jpg"},
                        "pageimage": "bridge.jpg",
                        "extract": "Suspension bridge in San Francisco.",
                    }
                }
            }
        }
        with patch("httpx.get", return_value=wiki_resp):
            res = execute_image_search("Golden Gate Bridge", max_results=3)
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(len(res["images"]), 1)
        self.assertLessEqual(len(res["images"]), 3)
        img = res["images"][0]
        self.assertTrue(img["url"].startswith("http"))
        self.assertTrue(img["markdown"].startswith("!["))

    # --- 5. STREAMER EVENT DISPATCH FOR IMAGE SEARCH ---
    def test_streamer_image_tool_dispatch(self):
        events = []
        class MockBridge:
            def push_event(self, ev):
                events.append(ev)

        abort = threading.Event()
        wiki_resp = MagicMock()
        wiki_resp.status_code = 200
        wiki_resp.json.return_value = {
            "query": {
                "pages": {
                    "1": {
                        "title": "Mars Rover",
                        "original": {"source": "https://upload.wikimedia.org/mars.jpg"},
                        "pageimage": "mars.jpg",
                        "extract": "NASA rover on Mars.",
                    }
                }
            }
        }
        with patch("httpx.get", return_value=wiki_resp):
            res = _run_web_search_tool_call(
                bridge=MockBridge(),
                tab_id="tab_test",
                conversation_id="conv_test",
                tool_name=IMAGE_SEARCH_TOOL_NAME,
                raw_args={"query": "Mars rover"},
                abort_event=abort,
            )
        self.assertTrue(res["ok"])
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["type"], "tool_status")
        self.assertEqual(events[0]["status"], "searching")
        self.assertEqual(events[0]["tool"], "image_search")
        self.assertEqual(events[1]["status"], "done")
        self.assertEqual(events[1]["tool"], "image_search")

    # --- 6. CRITICAL & HIGH PRIORITY BUG REGRESSION TESTS ---
    def test_background_sync_preserves_other_providers_and_env_keys(self):
        set_api_key_in_env("OpenAI", "sk-openai-keep")
        set_api_key_in_env("CustomLab", "sk-custom-keep")
        custom_prov = {
            "id": "custom_lab",
            "name": "CustomLab",
            "base_url": "https://api.custom.example/v1",
            "is_enabled": True,
            "models": [{"id": "custom-1", "name": "custom-1"}],
        }
        provs = get_providers()
        provs.append(custom_prov)
        save_providers(provs)

        mock_models = [{"id": "gpt-4o", "name": "gpt-4o", "input_price": 0.0, "cached_input_price": 0.0, "output_price": 0.0}]
        with patch.object(ClientManager, "fetch_remote_models", return_value=(mock_models, None, False)):
            BackgroundModelSyncer._sync_single_provider("openai")

        after = {p["id"]: p for p in get_providers()}
        self.assertIn("custom_lab", after)
        self.assertIn("anthropic", after)
        self.assertEqual(get_api_key_from_env("CustomLab"), "sk-custom-keep")
        self.assertEqual(get_api_key_from_env("OpenAI"), "sk-openai-keep")
        self.assertIn("gpt-4o", after["openai"]["fetched_model_ids"])

    def test_anthropic_stream_error_tuple_and_token_accounting(self):
        class FakeEvent:
            def __init__(self, typ, **kwargs):
                self.type = typ
                for k, v in kwargs.items():
                    setattr(self, k, v)

        class FakeUsage:
            def __init__(self, input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0):
                self.input_tokens = input_tokens
                self.output_tokens = output_tokens
                self.cache_read_input_tokens = cache_read_input_tokens
                self.cache_creation_input_tokens = cache_creation_input_tokens

        def make_stream(raise_after_first=False):
            msg_start_usage = FakeUsage(input_tokens=100, cache_read_input_tokens=20, cache_creation_input_tokens=10)
            yield FakeEvent("message_start", message=MagicMock(usage=msg_start_usage))
            if raise_after_first:
                raise RuntimeError("midstream network drop")
            yield FakeEvent("content_block_start", index=0, content_block=MagicMock(type="text", text=""))
            yield FakeEvent("content_block_delta", index=0, delta=MagicMock(type="text_delta", text="Hello world"))
            yield FakeEvent("content_block_stop", index=0)
            # message_delta carries cumulative output_tokens and input_tokens=0
            yield FakeEvent("message_delta", delta=MagicMock(stop_reason="end_turn"), usage=FakeUsage(input_tokens=0, output_tokens=42))

        client_err = MagicMock()
        client_err.messages.create.return_value = make_stream(raise_after_first=True)
        events = []
        class MockBridge:
            def push_event(self, ev):
                events.append(ev)

        with patch.object(ClientManager, "get_anthropic_client", return_value=(client_err, None)), \
             patch("chatint.streamer._trigger_auto_naming"):
            res_err = _stream_anthropic(
                bridge=MockBridge(),
                tab_id="t1",
                conversation_id="c1",
                messages=[{"role": "user", "content": "hi"}],
                provider_id="anthropic",
                model_id="claude-3-7-sonnet",
                thinking_level="none",
                assistant_msg_id=None,
                abort_event=threading.Event(),
                user_question="hi",
                web_search_enabled=False,
            )
        self.assertEqual(len(res_err), 7)
        self.assertTrue(res_err[5])  # stream_had_error is True
        self.assertTrue(any(ev.get("type") == "error" and "midstream network drop" in ev.get("error", "") for ev in events))

        client_ok = MagicMock()
        client_ok.messages.create.return_value = make_stream(raise_after_first=False)
        events.clear()
        with patch.object(ClientManager, "get_anthropic_client", return_value=(client_ok, None)), \
             patch("chatint.streamer._trigger_auto_naming"):
            res_ok = _stream_anthropic(
                bridge=MockBridge(),
                tab_id="t1",
                conversation_id="c1",
                messages=[{"role": "user", "content": "hi"}],
                provider_id="anthropic",
                model_id="claude-3-7-sonnet",
                thinking_level="none",
                assistant_msg_id=None,
                abort_event=threading.Event(),
                user_question="hi",
                web_search_enabled=False,
            )
        text, think, p_in, p_out, p_cached, had_err, _think_lvl = res_ok
        self.assertFalse(had_err)
        self.assertEqual("".join(text), "Hello world")
        self.assertEqual(p_in, 130)  # 100 base + 20 cache_read + 10 cache_create
        self.assertEqual(p_cached, 20)
        self.assertEqual(p_out, 42)

    def test_flush_think_buffer_emits_trailing_text(self):
        events = []
        class MockBridge:
            def push_event(self, ev):
                events.append(ev)

        full_content = []
        full_thinking = []
        _flush_think_buffer("<thi", False, full_content, full_thinking, MockBridge(), "t1", "c1")
        self.assertEqual(full_content, ["<thi"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "content_chunk")
        self.assertEqual(events[0]["chunk"], "<thi")

    def test_clear_resolved_thinking_cache_clears_db_without_level(self):
        provs = get_providers()
        for p in provs:
            if p["id"] == "openai":
                p["models"] = [{"id": "gpt-5", "name": "gpt-5"}]
        save_providers(provs)
        update_model_resolved_thinking("openai", "gpt-5", "high", "reasoning_effort:high")
        self.assertEqual(get_model_resolved_thinking("openai", "gpt-5", "high"), "reasoning_effort:high")
        clear_resolved_thinking_cache("openai", "gpt-5")
        self.assertIsNone(get_model_resolved_thinking("openai", "gpt-5", "high"))

    def test_gemini_fast_fails_on_auth_errors_without_retrying(self):
        client = MagicMock()
        client.models.generate_content_stream.side_effect = RuntimeError("401 UNAUTHORIZED: API key not valid")
        bridge = MagicMock()
        with patch.object(ClientManager, "get_gemini_client", return_value=(client, None)), \
             patch("chatint.streamer._trigger_auto_naming"):
            res = _stream_gemini(
                bridge=bridge,
                tab_id="t1",
                conversation_id="c1",
                messages=[{"role": "user", "content": "hi"}],
                provider_id="gemini",
                model_id="gemini-2.5-pro",
                thinking_level="medium",
                assistant_msg_id=None,
                abort_event=threading.Event(),
                user_question="hi",
                web_search_enabled=False,
            )
        self.assertEqual(client.models.generate_content_stream.call_count, 1)
        self.assertTrue(res[5])  # stream_had_error is True

    def test_imported_user_only_conversation_persists_in_list(self):
        draft = create_conversation("Unsent Draft")
        add_message(draft["id"], "user", "Never got an AI reply")
        imported = import_conversation_data(
            title="User Notes Import",
            system_prompt="",
            max_context=30,
            messages=[{"role": "user", "content": "Imported note without assistant reply"}],
        )
        convs = {c["id"]: c for c in get_conversations()}
        self.assertNotIn(draft["id"], convs)
        self.assertIn(imported["id"], convs)

    def test_db_column_whitelists_and_json_list_import(self):
        conv = create_conversation("Whitelist Check")
        msg = add_message(conv["id"], "user", "hello")
        with self.assertRaises(ValueError):
            update_conversation(conv["id"], bad_column="oops")
        with self.assertRaises(ValueError):
            update_message(msg["id"], bad_column="oops")

        raw_list = json.dumps(["not a dict", {"role": "user", "content": "Valid question"}])
        parsed = parse_chat_file_content(raw_list, filename="list.json")
        self.assertEqual(len(parsed["messages"]), 1)
        self.assertEqual(parsed["messages"][0]["content"], "Valid question")

    def test_bridge_theme_selective_sync_and_fetch_isolation(self):
        bridge = DesktopBridge()
        res_theme = bridge.set_theme("light")
        self.assertTrue(res_theme["success"])
        self.assertEqual(get_app_settings()["theme"], "light")

        # Mark gemini ready in DB/.env/syncer first
        set_api_key_in_env("Gemini", "AIza-test")
        BackgroundModelSyncer.set_sync_status("gemini", "ready", "")

        with patch.object(ClientManager, "fetch_remote_models", return_value=([], "401 Unauthorized: invalid api key", False)) as mock_fetch:
            res_fetch = bridge.fetch_provider_models("gemini", api_key=None, base_url=None, is_enabled=True, clear_api_key=True)
        self.assertEqual(res_fetch["models"], [])
        self.assertIn("401", res_fetch["error"])
        self.assertEqual(res_fetch["sync_status"], "invalid_key")
        self.assertFalse(mock_fetch.call_args.kwargs.get("update_sync_state", True))
        # DB/global sync status remained 'ready' despite Settings preview failure
        after_gemini = next(p for p in get_providers() if p["id"] == "gemini")
        self.assertEqual(after_gemini["sync_status"], "ready")

        # Mark existing keyed providers verified so unchanged ones do not re-sync
        BackgroundModelSyncer._verified_providers.update({"gemini", "ollama"})
        # save_settings should only sync changed providers
        current_provs = get_providers()
        current_settings = get_app_settings()
        current_provs[0]["base_url"] = "https://custom-changed.example/v1"
        with patch.object(BackgroundModelSyncer, "start_background_sync") as mock_sync:
            bridge.save_settings(current_provs, current_settings)
        mock_sync.assert_called_once()
        self.assertEqual(mock_sync.call_args.kwargs.get("provider_ids"), [current_provs[0]["id"]])

    def test_edit_message_missing_id_and_web_search_status_isolation(self):
        bridge = DesktopBridge()
        conv = create_conversation("Missing Msg Check")
        res = bridge.edit_message("tab1", conv["id"], "nonexistent_id", "hi", "openai", "gpt-4o", "none")
        self.assertIn("error", res)

        # Web search model 404 must not overwrite gemini global sync_status when Gemini search is opted in
        from chatint.db import save_app_settings

        save_app_settings({"use_gemini_web_search": "true"})
        set_api_key_in_env("Gemini", "AIza-test")
        BackgroundModelSyncer.set_sync_status("gemini", "ready", "")

        fake_client = MagicMock()
        fake_client.models.generate_content.side_effect = RuntimeError("404 NOT_FOUND model not found")
        with patch.object(ClientManager, "get_gemini_client", return_value=(fake_client, None)):
            ws_res = execute_web_search("latest python release")
        self.assertFalse(ws_res["ok"])
        after_gemini = next(p for p in get_providers() if p["id"] == "gemini")
        self.assertEqual(after_gemini["sync_status"], "ready")

    def test_naming_and_gemini_version_helper(self):
        self.assertTrue(is_gemini_3_or_newer("gemini-3.1-flash-lite"))
        self.assertTrue(is_gemini_3_or_newer("models/gemini-3-pro"))
        self.assertFalse(is_gemini_3_or_newer("gemini-2.5-flash"))

        fake_openai = MagicMock()
        fake_msg = MagicMock(content='"Quantum Entanglement Basics"')
        fake_usage = MagicMock(prompt_tokens=15, completion_tokens=5, prompt_tokens_details=None)
        fake_openai.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=fake_msg)],
            usage=fake_usage,
        )
        with patch.object(ClientManager, "get_client", return_value=(fake_openai, None)):
            title, tin, tout, _tc = generate_chat_title("Explain entanglement", provider_id="openai", model_id="gpt-5")
        self.assertEqual(title, "Quantum Entanglement Basics")
        self.assertEqual(tin, 15)
        self.assertEqual(tout, 5)
        self.assertIn("max_completion_tokens", fake_openai.chat.completions.create.call_args.kwargs)

    # ------------------------------------------------------------------
    # Regression tests for security, concurrency, integrity & edge-case fixes
    # ------------------------------------------------------------------

    def test_c1_origin_guard_and_external_url_validation(self):
        bridge = DesktopBridge()
        dummy_window = MagicMock()
        dummy_window.get_current_url.return_value = "file:///C:/app/ui/index.html"
        bridge.attach_window(dummy_window, index_url="file:///C:/app/ui/index.html")

        # Exact allowed local URL works cleanly
        self.assertTrue(bridge.set_theme("dark")["success"])

        # Navigating to untrusted remote origin blocks RPC even before URL changes via set_origin_trusted
        dummy_window.get_current_url.return_value = "https://evil.example/payload.html"
        with self.assertRaises(PermissionError):
            bridge.set_theme("light")

        # Restoring local URL allows RPC again
        dummy_window.get_current_url.return_value = "file:///C:/app/ui/index.html"
        self.assertTrue(bridge.set_theme("dark")["success"])

        # External URL validation rejects dangerous schemes / whitespace
        with patch("chatint.bridge.webbrowser.open") as mock_open:
            self.assertFalse(bridge.open_external_url("javascript:alert(1)")["success"])
            self.assertFalse(bridge.open_external_url("file:///C:/Windows/System32/calc.exe")["success"])
            self.assertFalse(bridge.open_external_url("https://example.com\nhttps://evil.com")["success"])
            mock_open.assert_not_called()

            res_ok = bridge.open_external_url("https://example.com/docs?q=1")
            self.assertTrue(res_ok["success"])
            mock_open.assert_called_once_with("https://example.com/docs?q=1")

    def test_c2_get_provider_api_key_returns_masked_only(self):
        set_api_key_in_env("OpenAI", "sk-super-secret-value-9876")
        bridge = DesktopBridge()
        res = bridge.get_provider_api_key("OpenAI")
        self.assertTrue(res["has_api_key"])
        self.assertNotIn("api_key", res)
        self.assertNotIn("super-secret", res["masked_hint"])
        self.assertTrue(res["masked_hint"].startswith("sk-s"))
        self.assertTrue(res["masked_hint"].endswith("9876"))

        # Short keys are masked completely without leaking secret bytes
        self.assertEqual(mask_api_key("tiny"), "••••")

    def test_h1_stream_manager_isolation_and_event_coalescing(self):
        bridge = DesktopBridge()
        s1_ev = StreamManager.register_stream("tab_a", stream_id="s1", conversation_id="conv_1")
        self.assertEqual(StreamManager._streams_by_tab["tab_a"][0], "s1")

        # Registering newer stream on same tab aborts prior stream; unregistering s1 later must NOT remove s2
        s2_ev = StreamManager.register_stream("tab_a", stream_id="s2", conversation_id="conv_2")
        self.assertTrue(s1_ev.is_set())
        StreamManager.unregister_stream("tab_a", stream_id="s1")
        self.assertEqual(StreamManager._streams_by_tab["tab_a"][0], "s2")
        self.assertFalse(s2_ev.is_set())

        # Aborting by conversation_id aborts only matching streams
        StreamManager.abort_conversation("conv_2")
        self.assertTrue(s2_ev.is_set())
        StreamManager.unregister_stream("tab_a", stream_id="s2")

        # Event coalescing must NOT merge chunks across different stream_ids or conversation_ids
        bridge.push_event({"type": "content_chunk", "tab_id": "t1", "conversation_id": "c1", "stream_id": "s1", "chunk": "hello "})
        bridge.push_event({"type": "content_chunk", "tab_id": "t1", "conversation_id": "c1", "stream_id": "s1", "chunk": "world"})
        bridge.push_event({"type": "content_chunk", "tab_id": "t1", "conversation_id": "c1", "stream_id": "s2", "chunk": "next"})
        events = bridge.get_pending_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["chunk"], "hello world")
        self.assertEqual(events[0]["stream_id"], "s1")
        self.assertEqual(events[1]["chunk"], "next")
        self.assertEqual(events[1]["stream_id"], "s2")

    def test_h2_fk_safety_on_deleted_chat_and_delete_conversation_only_if_empty(self):
        # Draft with only a user prompt (no assistant reply yet) IS deleted by only_if_empty=True
        draft = create_conversation("Failed Draft")
        add_message(draft["id"], "user", "hi")
        self.assertTrue(delete_conversation(draft["id"], only_if_empty=True))
        self.assertIsNone(get_conversation(draft["id"]))

        # Conversation WITH an assistant reply must NOT be deleted by only_if_empty=True
        conv = create_conversation("Ephemeral")
        add_message(conv["id"], "user", "hi")
        add_message(conv["id"], "assistant", "hello there")
        self.assertFalse(delete_conversation(conv["id"], only_if_empty=True))
        self.assertIsNotNone(get_conversation(conv["id"]))

        # Actual delete removes conversation cleanly
        self.assertTrue(delete_conversation(conv["id"], only_if_empty=False))
        # Subsequent FK writes must return None / False rather than crashing with IntegrityError
        self.assertIsNone(add_message(conv["id"], "assistant", "orphan reply"))
        self.assertFalse(record_token_usage(conv["id"], "openai", "gpt-4o", 10, 5))

        # Completely empty conversation is deleted by only_if_empty=True
        empty_conv = create_conversation("Empty Draft")
        self.assertTrue(delete_conversation(empty_conv["id"], only_if_empty=True))
        self.assertIsNone(get_conversation(empty_conv["id"]))

    def test_h4_env_atomic_write_preserves_comments_and_quotes_special_values(self):
        env_path = cfg_mod.ENV_PATH
        env_path.write_text("# Header comment\nOTHER_VAR=keep_me\n\n# Key comment\nOPENAI_API_KEY=old\n", encoding="utf-8")

        secret_with_specials = 'sk-#hash "quoted"\nline2'
        set_api_key_in_env("OpenAI", secret_with_specials)
        raw_text = env_path.read_text(encoding="utf-8")
        self.assertIn("# Header comment", raw_text)
        self.assertIn("# Key comment", raw_text)
        self.assertIn("OTHER_VAR=keep_me", raw_text)
        self.assertEqual(get_api_key_from_env("OpenAI"), secret_with_specials)

        # Updating a second key leaves the special-character key & comments intact
        set_api_key_in_env("Gemini", "AIza-123")
        self.assertEqual(get_api_key_from_env("OpenAI"), secret_with_specials)
        self.assertEqual(get_api_key_from_env("Gemini"), "AIza-123")
        self.assertIn("# Header comment", env_path.read_text(encoding="utf-8"))

    def test_h5_only_if_default_title_and_post_save_auto_naming(self):
        conv = create_conversation("New Chat")
        # Manual rename
        update_conversation(conv["id"], title="User Custom Title")
        # Auto-naming passing only_if_default=True must NOT clobber user's manual title
        res_skipped = update_conversation(conv["id"], title="Auto Generated Title", only_if_default=True)
        self.assertIsNone(res_skipped)
        self.assertEqual(get_conversation(conv["id"])["title"], "User Custom Title")

        # Default title IS updated when only_if_default=True
        conv2 = create_conversation("New Chat")
        res_applied = update_conversation(conv2["id"], title="Auto Generated Title", only_if_default=True)
        self.assertEqual(res_applied, "Auto Generated Title")
        self.assertEqual(get_conversation(conv2["id"])["title"], "Auto Generated Title")

        # Verify stream_chat_completion triggers auto-naming AFTER add_message succeeds (on still-unnamed New Chat)
        conv3 = create_conversation("New Chat")
        add_message(conv3["id"], "user", "Explain gravity")
        with (
            patch.dict("chatint.streamer._SDK_STREAMERS", {"anthropic": MagicMock(return_value=(["Gravity bends spacetime."], [], 10, 5, 0, False, "none"))}),
            patch("chatint.streamer._trigger_auto_naming") as mock_auto_name,
        ):
            events = []
            bridge = DesktopBridge()
            bridge.push_event = events.append
            stream_chat_completion(
                bridge=bridge,
                tab_id="tab1",
                conversation_id=conv3["id"],
                messages=[{"role": "user", "content": "Explain gravity"}],
                provider_id="anthropic",
                model_id="claude-sonnet-4-5",
                thinking_level="none",
                stream_id="s-name",
            )
            mock_auto_name.assert_called_once()
            self.assertEqual(events[-1]["type"], "done")
            self.assertEqual(events[-1]["stream_id"], "s-name")

    def test_m4_openai_param_retry_guard_and_effort_clamping(self):
        self.assertEqual(_clamp_openai_effort("openai", "o3-mini", "minimal"), "low")
        self.assertEqual(_clamp_openai_effort("openai", "o1", "xhigh"), "high")
        self.assertEqual(_clamp_openai_effort("openai", "o4-mini", "max"), "high")
        self.assertEqual(_clamp_openai_effort("openai", "gpt-5", "xhigh"), "xhigh")
        self.assertEqual(_clamp_openai_effort("openai", "o3", "medium"), "medium")

        class _AuthErr(Exception):
            status_code = 401

        class _BadParamErr(Exception):
            status_code = 400

        self.assertFalse(_is_openai_retryable_param_error(_AuthErr("invalid_api_key")))
        self.assertTrue(_is_openai_retryable_param_error(_BadParamErr("Unsupported parameter: reasoning_effort")))

    def test_m5_m6_network_check_socket_cleanup_and_client_close(self):
        # Mocked socket ensures context manager closes cleanly
        fake_sock = MagicMock()
        fake_cm = MagicMock()
        fake_cm.__enter__.return_value = fake_sock
        fake_cm.__exit__.return_value = False
        with patch("chatint.client_manager.socket.create_connection", return_value=fake_cm):
            self.assertTrue(_is_network_available())
            fake_cm.__exit__.assert_called_once()

        # Localhost base_url bypasses external DNS/TCP reachability check
        self.assertTrue(_is_network_available(base_urls=["http://127.0.0.1:11434/v1"]))

        # Client close happens cleanly in fetch_remote_models
        set_api_key_in_env("OpenAI", "sk-test-close")
        fake_openai = MagicMock()
        fake_openai.models.list.return_value = MagicMock(data=[MagicMock(id="gpt-4o")])
        with patch.object(ClientManager, "get_client", return_value=(fake_openai, None)):
            models, err, is_auth_err = ClientManager.fetch_remote_models("openai")
        self.assertIsNone(err)
        self.assertFalse(is_auth_err)
        self.assertEqual([m["id"] for m in models], ["gpt-4o"])
        fake_openai.close.assert_called_once()

        # Transport exception detection works cleanly
        class _TransportErr(Exception):
            pass

        self.assertTrue(_is_transport_exception(_TransportErr("Connection reset by peer")))

    def test_m7_context_retains_thinking_only_assistant_turns(self):
        bridge = DesktopBridge()
        conv = create_conversation("Thinking Only Turn")
        add_message(conv["id"], "user", "First question")
        add_message(conv["id"], "assistant", "", thinking_content="I reasoned halfway before stopping")
        add_message(conv["id"], "user", "Follow up")

        ctx = bridge._build_context_messages(get_conversation(conv["id"]))
        # Default system prompt may be first; check the 3 conversation turns
        non_sys = [m for m in ctx if m["role"] != "system"]
        self.assertEqual(len(non_sys), 3)
        self.assertEqual(non_sys[0], {"role": "user", "content": "First question"})
        self.assertEqual(non_sys[1]["role"], "assistant")
        self.assertEqual(non_sys[1]["content"], "[Response stopped]")
        self.assertEqual(non_sys[2], {"role": "user", "content": "Follow up"})

    def test_m8_markdown_import_preserves_multiline_system_prompt_and_code_headings(self):
        conv = create_conversation("Complex Export", system_prompt="System Line 1\nSystem Line 2")
        add_message(
            conv["id"],
            "user",
            "Look at this template:\n```markdown\n### User\nFake heading inside code block\n### Assistant (gpt-4o)\nFake\n```",
        )
        add_message(
            conv["id"],
            "assistant",
            "Here is the answer.",
            thinking_content="Line A\n\nLine B",
            model_id="gpt-4o (preview)",
        )
        exported = DesktopBridge().export_conversation(conv["id"], "markdown")
        parsed = parse_chat_file_content(exported["content"], "complex.md")
        self.assertEqual(parsed["title"], "Complex Export")
        self.assertEqual(parsed["system_prompt"], "System Line 1\nSystem Line 2")
        self.assertEqual(len(parsed["messages"]), 2)
        self.assertIn("### Assistant (gpt-4o)", parsed["messages"][0]["content"])
        self.assertEqual(parsed["messages"][1]["model_id"], "gpt-4o (preview)")
        self.assertEqual(parsed["messages"][1]["thinking_content"], "Line A\n\nLine B")
        self.assertEqual(parsed["messages"][1]["content"], "Here is the answer.")

    def test_m11_schema_user_version_and_chatint_spec_filter(self):
        with open_db() as conn:
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertGreaterEqual(ver, 1)

        # Verify ChatInt.spec filters datas by destination path (item[1]), not host path (item[0])
        spec_text = Path("ChatInt.spec").read_text(encoding="utf-8")
        self.assertIn("item[1]", spec_text)
        self.assertIn("'certifi'", spec_text)
        self.assertIn("'httpx2'", spec_text)
        self.assertIn("src/chatint/ui/assets/logo.ico", spec_text)

        # Verify CSP & local assets exist cleanly (C3 / M2 / L1 / L4)
        index_html = Path("src/chatint/ui/index.html").read_text(encoding="utf-8")
        self.assertIn("Content-Security-Policy", index_html)
        katex_css = Path("src/chatint/ui/assets/katex.min.css").read_text(encoding="utf-8")
        self.assertNotIn("https://cdn.jsdelivr.net", katex_css)
        app_js = Path("src/chatint/ui/app.js").read_text(encoding="utf-8")
        panes_js = Path("src/chatint/ui/js/panes.js").read_text(encoding="utf-8")
        self.assertNotIn("get_provider_api_key", app_js)
        self.assertIn("open_external_url", app_js)
        self.assertIn("disconnectPaneResizeObservers", app_js)
        self.assertIn("_resizeObserver", panes_js)

    # ------------------------------------------------------------------
    # Second-pass regression tests for all 33 audit & roadmap items
    # ------------------------------------------------------------------

    def test_c1_sealed_public_bridge_api_and_fail_closed_origin_guard(self):
        from chatint.bridge import PUBLIC_RPC_METHODS

        bridge = DesktopBridge(allow_headless_rpc=False)
        public_api = bridge.as_public_api()

        # Unattached headless calls are blocked when allow_headless_rpc=False
        with self.assertRaises(PermissionError):
            public_api.get_conversations()

        # PublicBridgeApi blocks helper & internal attributes (attach_window, set_origin_trusted, push_event, _window, __dict__)
        for forbidden in ("attach_window", "set_origin_trusted", "push_event", "_window", "_origin_trusted", "__dict__"):
            with self.assertRaises(AttributeError):
                getattr(public_api, forbidden)
        self.assertEqual(set(dir(public_api)), set(PUBLIC_RPC_METHODS))

        # Attach loopback HTTP window on specific port
        dummy_window = MagicMock()
        dummy_window.get_current_url.return_value = "http://127.0.0.1:43125/index.html"
        bridge.attach_window(
            dummy_window,
            index_url="http://127.0.0.1:43125/index.html",
            allowed_origin=("http", "127.0.0.1", 43125),
        )
        res_ok = public_api.set_theme("dark")
        self.assertEqual(res_ok, {"ok": True, "data": {"success": True, "theme": "dark"}, "error": None})

        # Empty URL fails CLOSED
        dummy_window.get_current_url.return_value = ""
        with self.assertRaises(PermissionError):
            public_api.set_theme("light")

        # Exception in get_current_url fails CLOSED
        dummy_window.get_current_url.side_effect = RuntimeError("webview transitioning")
        with self.assertRaises(PermissionError):
            public_api.set_theme("light")
        dummy_window.get_current_url.side_effect = None

        # Different localhost port fails CLOSED
        dummy_window.get_current_url.return_value = "http://127.0.0.1:9999/index.html"
        with self.assertRaises(PermissionError):
            public_api.set_theme("light")

        # Exact file:// path matching rejects sibling/other local HTML files
        bridge.attach_window(
            dummy_window,
            index_url="file:///C:/ChatInt/ui/index.html",
            allowed_origin=("file", "", None),
        )
        dummy_window.get_current_url.return_value = "file:///C:/Users/Alice/Downloads/ui/index.html"
        with self.assertRaises(PermissionError):
            public_api.set_theme("light")

        dummy_window.get_current_url.return_value = "file:///C:/ChatInt/ui/index.html"
        self.assertTrue(public_api.set_theme("dark")["ok"])

        # mailto: link validation
        with patch("chatint.bridge.webbrowser.open") as mock_open:
            self.assertTrue(public_api.open_external_url("mailto:support@example.com")["ok"])
            self.assertFalse(public_api.open_external_url("mailto:not-an-email")["ok"])
            mock_open.assert_called_once_with("mailto:support@example.com")

    def test_c2_blocks_stored_api_key_exfiltration_on_base_url_override(self):
        set_api_key_in_env("OpenAI", "sk-real-secret-key-9999")
        bridge = DesktopBridge()

        # 1. Previewing models against a different host WITHOUT entering a new key must NOT send saved .env secret
        with patch("openai.OpenAI") as mock_openai_cls:
            res_blocked = bridge.fetch_provider_models(
                "openai",
                api_key=None,
                base_url="https://attacker.example/v1",
                is_enabled=True,
            )
            mock_openai_cls.assert_not_called()
        self.assertEqual(res_blocked["models"], [])
        self.assertIn("enter the API key", res_blocked["error"])

        # Explicit new key IS allowed for preview on the new host (and uses only the newly entered key)
        fake_client = MagicMock()
        fake_client.models.list.return_value = MagicMock(data=[MagicMock(id="m1", created=1)])
        with patch("openai.OpenAI", return_value=fake_client) as mock_openai_cls:
            res_explicit = bridge.fetch_provider_models(
                "openai",
                api_key="sk-temp-test-key-1111",
                base_url="https://alt-host.example/v1",
                is_enabled=True,
            )
            mock_openai_cls.assert_called_once()
            self.assertEqual(mock_openai_cls.call_args.kwargs.get("api_key"), "sk-temp-test-key-1111")
        self.assertIsNone(res_explicit["error"])

        # Loopback localhost preview works keylessly without transmitting saved cloud secret
        with patch("openai.OpenAI", return_value=fake_client) as mock_openai_cls:
            res_local = bridge.fetch_provider_models(
                "provider_new_local",
                api_key=None,
                base_url="http://127.0.0.1:1234/v1",
                is_enabled=True,
                provider_name="LMStudio",
            )
            mock_openai_cls.assert_called_once()
            self.assertEqual(mock_openai_cls.call_args.kwargs.get("api_key"), "ollama")
        self.assertIsNone(res_local["error"])

        # 2. save_settings rejects changing base_url host when keeping existing saved secret
        provs = get_providers()
        settings = get_app_settings()
        for p in provs:
            if p["id"] == "openai":
                p["base_url"] = "https://attacker.example/v1"
        res_save_blocked = bridge.save_settings(provs, settings)
        self.assertFalse(res_save_blocked.get("success", False))
        self.assertIn("Clear Key", res_save_blocked.get("error", ""))

        # Clearing key or supplying new key permits base_url change
        for p in provs:
            if p["id"] == "openai":
                p["clear_api_key"] = True
        with patch.object(BackgroundModelSyncer, "start_background_sync"):
            res_save_ok = bridge.save_settings(provs, settings)
        self.assertTrue(res_save_ok["success"])
        self.assertEqual(get_api_key_from_env("OpenAI"), "")

    def test_h4_r4_dpapi_encryption_clear_on_rename_whitespace_and_case_insensitivity(self):
        from chatint.storage.dpapi import decrypt_secret_from_storage, encrypt_secret_for_storage, is_dpapi_enabled

        # Whitespace preservation + DPAPI encryption at rest on Windows
        padded_secret = "  sk-padded-secret-value  "
        set_api_key_in_env("OpenAI", padded_secret)
        self.assertEqual(get_api_key_from_env("OpenAI"), padded_secret)
        raw_env_disk = self.env_path.read_text(encoding="utf-8")
        if is_dpapi_enabled():
            self.assertIn("dpapi:", raw_env_disk)
            self.assertNotIn("sk-padded-secret-value", raw_env_disk)
        enc = encrypt_secret_for_storage("my-secret-token")
        self.assertEqual(decrypt_secret_from_storage(enc), "my-secret-token")

        # Clear key while renaming provider in the same operation must NOT restore old_val
        set_api_key_in_env("OldVendor", "sk-must-be-deleted")
        set_api_key_in_env("NewVendor", "", old_provider_name="OldVendor", clear=True)
        self.assertEqual(get_api_key_from_env("NewVendor"), "")
        self.assertEqual(get_api_key_from_env("OldVendor"), "")

        # Case-insensitive .env key lookup (lowercase openai_api_key=... in .env)
        self.env_path.write_text("openai_api_key=sk-lowercase-entry\n", encoding="utf-8")
        self.assertEqual(get_api_key_from_env("OpenAI"), "sk-lowercase-entry")

    def test_h5_multiple_new_chats_auto_name_once_and_ignore_deleted_chats(self):
        from chatint.services.stream_coordinator import run_auto_naming

        # Multiple default "New Chat" drafts do NOT become "New Chat (1)" with broken auto-naming
        c1 = create_conversation("New Chat")
        c2 = create_conversation("New Chat")
        self.assertEqual(c1["title"], "New Chat")
        self.assertEqual(c2["title"], "New Chat")
        self.assertFalse(c1["is_auto_named"])
        self.assertFalse(c2["is_auto_named"])

        # Auto-naming both produces unique titles and sets is_auto_named = True
        self.assertEqual(update_conversation(c1["id"], title="Quantum Physics", only_if_default=True), "Quantum Physics")
        self.assertEqual(update_conversation(c2["id"], title="Quantum Physics", only_if_default=True), "Quantum Physics (1)")
        self.assertTrue(get_conversation(c1["id"])["is_auto_named"])
        self.assertTrue(get_conversation(c2["id"])["is_auto_named"])

        # Turn 2 on an already named conversation does NOT call _trigger_auto_naming
        add_message(c1["id"], "user", "Follow-up question")
        with (
            patch.dict("chatint.streamer._SDK_STREAMERS", {"anthropic": MagicMock(return_value=(["Second reply."], [], 8, 4, 0, False, "none"))}),
            patch("chatint.streamer._trigger_auto_naming") as mock_auto_name,
        ):
            stream_chat_completion(
                bridge=DesktopBridge(),
                tab_id="tab1",
                conversation_id=c1["id"],
                messages=[{"role": "user", "content": "Follow-up question"}],
                provider_id="anthropic",
                model_id="claude-sonnet-4-5",
                thinking_level="none",
                stream_id="s-turn2",
            )
            mock_auto_name.assert_not_called()

        # Legacy "New Chat (1)" row with is_auto_named=0 still auto-names cleanly
        with open_db() as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, system_prompt, max_context_messages, is_imported, is_auto_named, created_at, updated_at) VALUES (?, ?, '', 30, 0, 0, 1.0, 1.0)",
                ("legacy_nc1", "New Chat (1)"),
            )
            conn.commit()
        self.assertEqual(update_conversation("legacy_nc1", title="Fixed Legacy Title", only_if_default=True), "Fixed Legacy Title")

        # Deleted chat before run_auto_naming finishes does NOT emit chat_named
        c_del = create_conversation("New Chat")
        delete_conversation(c_del["id"])
        emitted = []
        mock_bridge = MagicMock()
        mock_bridge.push_event.side_effect = emitted.append
        with patch("chatint.services.stream_coordinator.generate_chat_title", return_value=("Ghost Title", 5, 2, 0)):
            run_auto_naming(mock_bridge, "tab1", c_del["id"], "hello", "openai", "gpt-4o", stream_id="s1")
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.daemon and t.is_alive():
                t.join(timeout=0.5)
        self.assertFalse(any(ev.get("type") == "chat_named" and ev.get("conversation_id") == c_del["id"] for ev in emitted))

    def test_new_h3_regenerate_and_edit_validate_provider_before_deleting_messages(self):
        bridge = DesktopBridge()
        conv = create_conversation("Preserve History On Error")
        u_msg = add_message(conv["id"], "user", "Important prompt")
        a_msg = add_message(conv["id"], "assistant", "Important assistant answer")

        # OpenAI has NO API key in .env right now -> provider readiness check must fail BEFORE deleting SQLite rows!
        with patch.object(StreamManager, "abort_conversation") as mock_abort:
            res_regen = bridge.regenerate_message("tab1", conv["id"], a_msg["id"], "openai", "gpt-4o", "none")
            self.assertIn("error", res_regen)
            mock_abort.assert_not_called()

        still_there = get_conversation(conv["id"])
        self.assertEqual(len(still_there["messages"]), 2)
        self.assertEqual(still_there["messages"][1]["content"], "Important assistant answer")

        res_edit = bridge.edit_message("tab1", conv["id"], u_msg["id"], "Edited prompt", "openai", "gpt-4o", "none")
        self.assertIn("error", res_edit)
        still_there_after_edit = get_conversation(conv["id"])
        self.assertEqual(still_there_after_edit["messages"][0]["content"], "Important prompt")
        self.assertEqual(len(still_there_after_edit["messages"]), 2)

    def test_m4_gpt5_clamping_tool_retry_without_cache_poisoning_and_deepseek_replay(self):
        from chatint.providers.openai_adapter import _stream_openai, _uses_deepseek_reasoning_replay

        # 1. Effort clamping across gpt-5 and OpenAI-compatible providers
        self.assertEqual(_clamp_openai_effort("openai", "gpt-5", "max"), "xhigh")
        self.assertEqual(_clamp_openai_effort("openai", "gpt-5-mini", "minimal"), "minimal")
        self.assertEqual(_clamp_openai_effort("openrouter", "deepseek/deepseek-r1", "max"), "high")
        self.assertEqual(_clamp_openai_effort("deepseek", "deepseek-reasoner", "minimal"), "low")

        # 2. reasoning_content replay only enabled for DeepSeek
        self.assertTrue(_uses_deepseek_reasoning_replay("deepseek", "deepseek-reasoner"))
        self.assertTrue(_uses_deepseek_reasoning_replay("openrouter", "deepseek/deepseek-r1"))
        self.assertFalse(_uses_deepseek_reasoning_replay("openai", "gpt-5", "https://api.openai.com/v1"))

        # 3. Tool-rejection 400 retries (effort, use_tools=False) BEFORE downgrading effort, preserving thinking cache
        import httpx2
        import openai

        provs = get_providers()
        for p in provs:
            if p["id"] == "openai":
                p["models"] = [{"id": "o3", "name": "o3"}]
        save_providers(provs)

        call_log = []

        def fake_create(**kwargs):
            has_tools = "tools" in kwargs
            eff = kwargs.get("reasoning_effort")
            call_log.append((eff, has_tools))
            if has_tools:
                req = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
                resp = httpx2.Response(400, request=req)
                raise openai.BadRequestError("Unsupported parameter combination for this model", response=resp, body=None)
            chunk = MagicMock()
            chunk.usage = MagicMock(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None)
            delta = MagicMock(content="Solved with high effort!", reasoning_content=None, reasoning=None, tool_calls=None)
            chunk.choices = [MagicMock(delta=delta)]
            return iter([chunk])

        fake_client = MagicMock()
        fake_client.chat.completions.create.side_effect = fake_create

        with (
            patch.object(ClientManager, "get_client", return_value=(fake_client, None)),
            patch("chatint.providers.openai_adapter._resolve_search_enabled", return_value=True),
        ):
            text, _think, _in, _out, _cached, had_err, actual_lvl = _stream_openai(
                bridge=DesktopBridge(),
                tab_id="t1",
                conversation_id="c1",
                messages=[{"role": "user", "content": "test"}],
                provider_id="openai",
                model_id="o3",
                thinking_level="high",
                assistant_msg_id="m1",
                abort_event=threading.Event(),
                user_question="test",
                web_search_enabled=True,
            )
        self.assertFalse(had_err)
        self.assertEqual("".join(text), "Solved with high effort!")
        self.assertEqual(actual_lvl, "high")
        self.assertEqual(call_log[:2], [("high", True), ("high", False)])
        self.assertEqual(get_model_resolved_thinking("openai", "o3", "high"), "high")

    def test_m6_hung_fetch_remote_models_times_out_on_daemon_thread(self):
        set_api_key_in_env("OpenAI", "sk-timeout-test")
        block_ev = threading.Event()
        fake_client = MagicMock()

        def slow_list():
            block_ev.wait(timeout=2.0)
            return MagicMock(data=[])

        fake_client.models.list.side_effect = slow_list
        try:
            with (
                patch.object(ClientManager, "get_client", return_value=(fake_client, None)),
                patch("chatint.client_manager._is_network_available", return_value=True),
            ):
                models, err, is_net_err = ClientManager.fetch_remote_models("openai", timeout=0.12)
            self.assertEqual(models, [])
            self.assertIn("timed out", (err or "").lower())
            self.assertTrue(is_net_err)
        finally:
            block_ev.set()

    def test_m8_markdown_roundtrip_details_unclosed_fence_system_role_and_json_tokens(self):
        conv = create_conversation("Edge Case Export", system_prompt="Be concise.", max_context=42)
        add_message(
            conv["id"],
            "user",
            "Here is an unclosed fence:\n```python\ndef broken():\n    pass",
        )
        add_message(
            conv["id"],
            "assistant",
            "And here is the real reply after an unclosed fence.",
            thinking_content="Checking literal </details> inside thought block!\nStill thinking...",
            provider_id="anthropic",
            model_id="claude-sonnet-4-5",
            thinking_level="high",
        )
        add_message(conv["id"], "system", "Mid-conversation system note")

        bridge = DesktopBridge()
        md_exported = bridge.export_conversation(conv["id"], "markdown")
        parsed_md = parse_chat_file_content(md_exported["content"], "edge.md")
        self.assertEqual(parsed_md["title"], "Edge Case Export")
        self.assertEqual(parsed_md["max_context"], 42)
        self.assertEqual(len(parsed_md["messages"]), 3)
        self.assertEqual(parsed_md["messages"][0]["role"], "user")
        self.assertEqual(parsed_md["messages"][1]["role"], "assistant")
        self.assertIn("</details>", parsed_md["messages"][1]["thinking_content"])
        self.assertIn("Still thinking...", parsed_md["messages"][1]["thinking_content"])
        self.assertEqual(parsed_md["messages"][1]["provider_id"], "anthropic")
        self.assertEqual(parsed_md["messages"][1]["thinking_level"], "high")
        self.assertEqual(parsed_md["messages"][2]["role"], "system")
        self.assertEqual(parsed_md["messages"][2]["content"], "Mid-conversation system note")

        # JSON export/import preserves per-message token counts while keeping token_ledger spend at 0
        json_payload = json.dumps({
            "title": "Token Metadata Import",
            "messages": [
                {"role": "user", "content": "Hi"},
                {
                    "role": "assistant",
                    "content": "Hello!",
                    "prompt_tokens": 120,
                    "completion_tokens": 45,
                    "cached_tokens": 30,
                },
            ],
        })
        parsed_json = parse_chat_file_content(json_payload, "tokens.json")
        imp = import_conversation_data(
            title=parsed_json["title"],
            messages=parsed_json["messages"],
        )
        self.assertEqual(imp["messages"][1]["prompt_tokens"], 120)
        self.assertEqual(imp["messages"][1]["completion_tokens"], 45)
        self.assertEqual(imp["messages"][1]["cached_tokens"], 30)
        self.assertEqual(get_token_stats(imp["id"])["grand_total"]["total_tokens"], 0)

    def test_m10_find_existing_chatint_hwnd_rejects_unrelated_winforms_exe(self):
        from chatint.main import _find_existing_chatint_hwnd

        def _make_mocks(exe_path: str, open_proc_handle: int = 99, query_ok: bool = True):
            user32 = MagicMock()
            kernel32 = MagicMock()

            def fake_enum(cb, lparam):
                cb(12345, lparam)
                return True

            user32.EnumWindows.side_effect = fake_enum
            user32.IsWindowVisible.return_value = True
            user32.GetWindowTextLengthW.return_value = len("ChatInt")

            def fill_title(_hwnd, buf, _max_len):
                buf.value = "ChatInt"
                return len("ChatInt")

            def fill_class(_hwnd, buf, _max_len):
                buf.value = "WindowsForms10.Window.8.app.0.141b42a"
                return len(buf.value)

            def fill_pid(_hwnd, pid_ptr):
                pid_ptr._obj.value = 4242
                return 1

            user32.GetWindowTextW.side_effect = fill_title
            user32.GetClassNameW.side_effect = fill_class
            user32.GetWindowThreadProcessId.side_effect = fill_pid
            kernel32.OpenProcess.return_value = open_proc_handle

            def fill_exe(_h, _flags, buf, _len_ptr):
                if not query_ok:
                    return False
                buf.value = exe_path
                return True

            kernel32.QueryFullProcessImageNameW.side_effect = fill_exe
            return user32, kernel32

        # Unrelated WinForms app titled "ChatInt" -> MUST be rejected (returns 0)
        u_bad, k_bad = _make_mocks(r"C:\Program Files\OtherTool\OtherTool.exe")
        self.assertEqual(_find_existing_chatint_hwnd(u_bad, k_bad), 0)

        # Genuine ChatInt.exe -> matched
        u_good, k_good = _make_mocks(r"C:\ChatInt\dist\ChatInt.exe")
        self.assertEqual(_find_existing_chatint_hwnd(u_good, k_good), 12345)

        # OpenProcess blocked (returns 0) -> safe fallback to WindowsForms10 class
        u_fb, k_fb = _make_mocks("", open_proc_handle=0)
        self.assertEqual(_find_existing_chatint_hwnd(u_fb, k_fb), 12345)

    def test_new_m1_think_tag_parser_only_opens_at_round_start(self):
        from chatint.streamer import _process_text_with_think_tags

        bridge = DesktopBridge()

        # Case 1: Mid-prose or code-block <think> (even unclosed!) stays in visible content
        c_vis, c_think = [], []
        round_state = [False]
        buf, in_think = _process_text_with_think_tags(
            "Models like DeepSeek emit `<think>` at the start without closing:",
            "",
            False,
            c_vis,
            c_think,
            bridge,
            "t1",
            "c1",
            has_round_visible_text=round_state,
        )
        self.assertFalse(in_think)
        self.assertEqual(buf, "")
        self.assertEqual(c_think, [])
        self.assertIn("<think>", "".join(c_vis))

        # Case 2: Leading <think>...</think> followed immediately by literal <think>example</think> in answer
        c_vis2, c_think2 = [], []
        round_state2 = [False]
        buf2, in_think2 = _process_text_with_think_tags(
            "<think>User asked for literal tag.</think>\n\n<think>example</think>",
            "",
            False,
            c_vis2,
            c_think2,
            bridge,
            "t1",
            "c1",
            has_round_visible_text=round_state2,
        )
        self.assertFalse(in_think2)
        self.assertEqual("".join(c_think2), "User asked for literal tag.")
        self.assertEqual("".join(c_vis2), "<think>example</think>")

    def test_new_m2_multi_round_thinking_separator_abort_suppression_and_aborted_tool_ledger(self):
        from chatint.streamer import _ensure_post_tool_thinking_separator

        bridge = DesktopBridge()
        full_thinking = ["Round 0 thought."]
        _ensure_post_tool_thinking_separator(full_thinking, bridge, "t1", "c1")
        self.assertEqual("".join(full_thinking), "Round 0 thought.\n\n")

        # Aborted tool call does NOT dispatch status="done"
        events = []
        abort_ev = threading.Event()

        def aborting_search(*_args, **_kwargs):
            abort_ev.set()
            return {"ok": True, "images": []}

        mock_b = MagicMock()
        mock_b.push_event.side_effect = events.append
        with patch("chatint.providers.base.execute_image_search", side_effect=aborting_search):
            _run_web_search_tool_call(mock_b, "t1", "c1", IMAGE_SEARCH_TOOL_NAME, {"query": "mars"}, abort_ev)
        self.assertFalse(any(ev.get("status") == "done" for ev in events))

        # Aborted/empty Round 1 after billed Round 0 tool call still records tokens in token_ledger
        conv = create_conversation("Aborted Tool Turn")
        add_message(conv["id"], "user", "Search news")
        with patch.dict(
            "chatint.streamer._SDK_STREAMERS",
            {"openai": MagicMock(return_value=([], [], 250, 40, 0, True, "none"))},
        ):
            stream_chat_completion(
                bridge=DesktopBridge(),
                tab_id="t1",
                conversation_id=conv["id"],
                messages=[{"role": "user", "content": "Search news"}],
                provider_id="openai",
                model_id="gpt-4o",
                thinking_level="none",
                stream_id="s-tool-abort",
            )
        stats = get_token_stats(conv["id"])
        self.assertEqual(stats["grand_total"]["prompt_tokens"], 250)
        self.assertEqual(stats["grand_total"]["completion_tokens"], 40)

    def test_new_m3_r6_log_formatter_scrubs_tracebacks_provider_keys_and_custom_secrets(self):
        import logging
        from chatint.logging_setup import _SecretScrubFormatter, register_known_secrets

        register_known_secrets(["custom-proprietary-secret-token-999"])
        formatter = _SecretScrubFormatter("%(levelname)s: %(message)s")

        try:
            raise RuntimeError(
                "Header leak: gsk_abcdef1234567890 xai-abcdef1234567890 pplx-abcdef1234567890 "
                "hf_abcdef1234567890 csk-abcdef1234567890 custom-proprietary-secret-token-999"
            )
        except RuntimeError:
            record = logging.LogRecord(
                name="chatint.test",
                level=logging.ERROR,
                pathname=__file__,
                lineno=1,
                msg="Request failed",
                args=(),
                exc_info=sys.exc_info(),
            )
        formatted = formatter.format(record)
        for leaked in (
            "1234567890",
            "custom-proprietary-secret-token-999",
        ):
            self.assertNotIn(leaked, formatted)
        self.assertIn("gsk_abcdef***", formatted)
        self.assertIn("cust***", formatted)

        # open_logs_folder via PublicBridgeApi returns uniform {ok, data, error}
        bridge = DesktopBridge()
        with (
            patch("os.startfile", create=True),
            patch("chatint.bridge.subprocess.Popen"),
        ):
            env_res = bridge.as_public_api().open_logs_folder()
        self.assertTrue(env_res["ok"])
        self.assertIn("path", env_res["data"])

    def test_new_m6_new_l3_r2_r5_r7_validation_bom_import_busy_timeout_and_migrations(self):
        from chatint.db import save_app_settings
        from chatint.providers import get_provider_adapter
        from chatint.services.import_export import MAX_IMPORT_BYTES, read_chat_file_safely
        from chatint.storage.connection import SCHEMA_VERSION

        # 1. NEW-L3: SQLite busy_timeout == 30000 ms
        with open_db() as conn:
            bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertEqual(bt, 30000)

        # 2. NEW-M6: add_provider & save_app_settings validation + UTF-8 BOM & size cap
        bridge = DesktopBridge()
        self.assertIn("error", bridge.add_provider({"name": "Bad Name 123!", "base_url": "https://example.com/v1"}))
        self.assertIn("error", bridge.add_provider({"name": "OpenAI", "base_url": "https://example.com/v1"}))
        with self.assertRaises(ValueError):
            save_app_settings({"evil_injected_setting": "1"})

        bom_path = Path(self.test_dir.name) / "bom_chat.json"
        bom_payload = json.dumps({"title": "BOM Chat", "messages": [{"role": "user", "content": "Hello BOM"}]})
        bom_path.write_bytes(b"\xef\xbb\xbf" + bom_payload.encode("utf-8"))
        decoded = read_chat_file_safely(bom_path)
        self.assertFalse(decoded.startswith("\ufeff"))
        imp_res = bridge.import_conversation_content("\ufeff" + bom_payload, "bom_chat.json")
        self.assertEqual(imp_res["conversation"]["title"], "BOM Chat")

        oversized_res = bridge.import_conversation_content("x" * (MAX_IMPORT_BYTES + 10), "huge.md")
        self.assertIn("error", oversized_res)

        # 3. R2: Unified ProviderAdapter registry
        self.assertEqual(get_provider_adapter("anthropic").sdk_type, "anthropic")
        self.assertEqual(get_provider_adapter("gemini").sdk_type, "gemini")
        self.assertEqual(get_provider_adapter("openai").sdk_type, "openai")
        self.assertEqual(get_provider_adapter("custom_vllm").sdk_type, "openai")

        # 4. R5: Sequential migration from v1 DB creates .bak backup and upgrades to SCHEMA_VERSION (2)
        import contextlib

        v1_db = Path(self.test_dir.name) / "legacy_v1.db"
        with contextlib.closing(sqlite3.connect(v1_db)) as c_v1:
            c_v1.execute(
                "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, system_prompt TEXT NOT NULL DEFAULT '', max_context_messages INTEGER NOT NULL DEFAULT 30, is_imported INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            c_v1.execute(
                "INSERT INTO conversations (id, title, created_at, updated_at) VALUES ('c_old', 'Existing Named Topic', 1.0, 1.0), ('c_nc', 'New Chat (1)', 1.0, 1.0)"
            )
            c_v1.execute("PRAGMA user_version = 1")
            c_v1.commit()

        prev_db = db_mod.DB_PATH
        try:
            db_mod.DB_PATH = v1_db
            cfg_mod.DB_PATH = v1_db
            init_db()
            bak_file = v1_db.with_name("legacy_v1.db.bak")
            self.assertTrue(bak_file.exists())
            with open_db() as c_up:
                self.assertEqual(c_up.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                rows = {
                    r["id"]: r["is_auto_named"]
                    for r in c_up.execute("SELECT id, is_auto_named FROM conversations").fetchall()
                }
                self.assertEqual(rows["c_old"], 1)
                self.assertEqual(rows["c_nc"], 0)
        finally:
            db_mod.DB_PATH = prev_db
            cfg_mod.DB_PATH = prev_db

    def test_ref_m1_image_search_null_thumbnail_and_empty_imageinfo(self):
        """REF-M1: MediaWiki responses with null thumbnail and empty imageinfo do not raise AttributeError/IndexError."""
        wiki_resp = MagicMock()
        wiki_resp.status_code = 200
        wiki_resp.json.return_value = {
            "query": {
                "pages": {
                    "101": {
                        "title": "Eiffel Tower",
                        "thumbnail": None,  # Explicit None thumbnail
                        "imageinfo": [],    # Explicit empty imageinfo
                        "original": None,
                        "extract": "Iron lattice tower in Paris.",
                    },
                    "102": {
                        "title": "Eiffel Tower Base",
                        "thumbnail": {"source": "https://upload.wikimedia.org/thumb.jpg"},
                        "imageinfo": [{"thumburl": "https://upload.wikimedia.org/info.jpg"}],
                        "original": {"source": "https://upload.wikimedia.org/orig.jpg"},
                        "extract": "Base of tower.",
                    },
                }
            }
        }
        with patch("httpx.get", return_value=wiki_resp):
            res = execute_image_search("Eiffel Tower", max_results=3)
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(len(res["images"]), 1)
        self.assertEqual(res["images"][0]["title"], "Eiffel Tower Base")

    def test_ref_l6_and_m3_loopback_endpoints(self):
        """REF-L6 & REF-M3: is_loopback_endpoint supports scheme-less, 0.0.0.0, 127.*, and ipv6 loopback."""
        from chatint.client_manager import is_loopback_endpoint
        self.assertTrue(is_loopback_endpoint("localhost:11434"))
        self.assertTrue(is_loopback_endpoint("http://localhost:11434"))
        self.assertTrue(is_loopback_endpoint("127.0.0.1:8000"))
        self.assertTrue(is_loopback_endpoint("http://127.0.0.2:8000"))
        self.assertTrue(is_loopback_endpoint("http://127.12.34.56:5000"))
        self.assertTrue(is_loopback_endpoint("0.0.0.0:11434"))
        self.assertTrue(is_loopback_endpoint("http://0.0.0.0:8000"))
        self.assertTrue(is_loopback_endpoint("http://[::1]:11434"))
        self.assertTrue(is_loopback_endpoint("::1"))

        self.assertFalse(is_loopback_endpoint("https://api.openai.com/v1"))
        self.assertFalse(is_loopback_endpoint("http://192.168.1.1:8080"))
        self.assertFalse(is_loopback_endpoint("http://10.0.0.1:8000"))
        self.assertFalse(is_loopback_endpoint(""))
        self.assertFalse(is_loopback_endpoint(None))

    def test_ref_l5_and_ref_m4_tool_limits_and_token_stats(self):
        """REF-L5 & REF-M4: MAX_TOOL_CALLS_PER_PROMPT is 10 and get_token_stats returns active_turn_tokens."""
        from chatint.web_search_tool import MAX_TOOL_CALLS_PER_PROMPT
        self.assertEqual(MAX_TOOL_CALLS_PER_PROMPT, 10)

        conv = create_conversation("Token Stats Active Test")
        stats_empty = get_token_stats(conv["id"])
        self.assertIn("active_turn_tokens", stats_empty)
        self.assertEqual(stats_empty["active_turn_tokens"]["total_tokens"], 0)

        # Add message with tokens and verify active_turn_tokens reflects it
        add_message(conv["id"], "user", "Hello")
        add_message(conv["id"], "assistant", "Hi there", prompt_tokens=15, completion_tokens=8, cached_tokens=5)
        stats_turn = get_token_stats(conv["id"])
        self.assertEqual(stats_turn["active_turn_tokens"]["prompt_tokens"], 15)
        self.assertEqual(stats_turn["active_turn_tokens"]["completion_tokens"], 8)
        self.assertEqual(stats_turn["active_turn_tokens"]["cached_tokens"], 5)
        self.assertEqual(stats_turn["active_turn_tokens"]["total_tokens"], 23)

    def test_unique_conversation_title_number_only_base_and_empty_update(self):
        """Verify number-only candidate like '(1)' increments cleanly without leading whitespace, and empty update defaults to 'New Chat'."""
        from chatint.storage.conversations_repo import get_unique_conversation_title

        self.assertEqual(get_unique_conversation_title(""), "New Chat")
        self.assertEqual(get_unique_conversation_title("   "), "New Chat")

        c1 = create_conversation("(1)")
        self.assertEqual(c1["title"], "(1)")

        # Candidate "(1)" collision should increment to "(1) (1)", NEVER leading space " (2)"
        c2 = create_conversation("(1)")
        self.assertEqual(c2["title"], "(1) (1)")

        # Candidate "(1) (1)" collision increments to "(1) (2)"
        c3 = create_conversation("(1) (1)")
        self.assertEqual(c3["title"], "(1) (2)")

        # update_conversation with empty string, whitespace, or None should never store empty title
        c4 = create_conversation("Valid Name")
        res_empty = update_conversation(c4["id"], title="")
        self.assertTrue(res_empty and res_empty.startswith("New Chat"))
        c4_data = get_conversation(c4["id"])
        self.assertTrue(c4_data["title"] and not c4_data["title"].startswith(" "))

        res_ws = update_conversation(c4["id"], title="   ")
        self.assertTrue(res_ws and res_ws.startswith("New Chat"))

        res_none = update_conversation(c4["id"], title=None)
        self.assertTrue(res_none and res_none.startswith("New Chat"))

    def test_duration_ms_and_custom_provider_name_persistence_and_roundtrip(self):
        import contextlib
        import time
        from chatint.providers.base import _emit_api_error
        from chatint.storage.connection import SCHEMA_VERSION

        # 1. Custom provider name persisted on streamed message, token_stats, branch, and API errors
        provs = get_providers()
        provs.append({
            "id": "provider_1795606486438",
            "name": "Bonsai",
            "base_url": "https://api.bonsai.example/v1",
            "is_enabled": True,
            "models": [{"id": "bonsai-2.5-27b", "name": "bonsai-2.5-27b"}],
        })
        save_providers(provs)

        conv = create_conversation("Bonsai Speed Test")
        add_message(conv["id"], "user", "Explain photons")

        def slow_custom_stream(**_kwargs):
            time.sleep(0.05)
            return (["Photons are quanta of light."], [], 171, 467, 0, False, "none")

        with patch.dict("chatint.streamer._SDK_STREAMERS", {"openai": slow_custom_stream}):
            stream_chat_completion(
                bridge=DesktopBridge(),
                tab_id="tab1",
                conversation_id=conv["id"],
                messages=[{"role": "user", "content": "Explain photons"}],
                provider_id="provider_1795606486438",
                model_id="bonsai-2.5-27b",
                thinking_level="none",
                stream_id="s-bonsai",
            )

        loaded = get_conversation(conv["id"])
        asst = loaded["messages"][1]
        self.assertEqual(asst["provider_id"], "provider_1795606486438")
        self.assertEqual(asst["provider_name"], "Bonsai")
        self.assertGreaterEqual(asst["duration_ms"], 30)

        stats = get_token_stats(conv["id"])
        self.assertEqual(stats["per_model"][0]["provider_name"], "Bonsai")

        err_events = []
        mock_bridge = MagicMock()
        mock_bridge.push_event.side_effect = err_events.append
        _emit_api_error(mock_bridge, "t1", conv["id"], "provider_1795606486438", "bonsai-2.5-27b", RuntimeError("boom"), [], [])
        self.assertIn("Bonsai / bonsai-2.5-27b", err_events[0]["error"])

        # Branching preserves provider_name and resets duration_ms to 0 alongside 0 tokens
        branch = duplicate_conversation(conv["id"])
        self.assertEqual(branch["messages"][1]["provider_name"], "Bonsai")
        self.assertEqual(branch["messages"][1]["duration_ms"], 0)

        # 2. Markdown & JSON export/import roundtrip preserve provider_name and duration_ms
        bridge = DesktopBridge()
        md_exp = bridge.export_conversation(conv["id"], "markdown")
        parsed_md = parse_chat_file_content(md_exp["content"], "bonsai.md")
        self.assertEqual(parsed_md["messages"][1]["provider_name"], "Bonsai")
        self.assertEqual(parsed_md["messages"][1]["duration_ms"], asst["duration_ms"])

        json_exp = bridge.export_conversation(conv["id"], "json")
        parsed_json = parse_chat_file_content(json_exp["content"], "bonsai.json")
        imp = import_conversation_data(title="Imported Bonsai", messages=parsed_json["messages"])
        self.assertEqual(imp["messages"][1]["provider_name"], "Bonsai")
        self.assertEqual(imp["messages"][1]["duration_ms"], asst["duration_ms"])

        # 3. Migration from v2 DB upgrades cleanly to SCHEMA_VERSION (6) & splits per-conv DB
        v2_db = Path(self.test_dir.name) / "legacy_v2.db"
        with contextlib.closing(sqlite3.connect(v2_db)) as c_v2:
            c_v2.execute("CREATE TABLE providers (id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', is_enabled INTEGER NOT NULL DEFAULT 1, models_json TEXT NOT NULL DEFAULT '[]')")
            c_v2.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, system_prompt TEXT NOT NULL DEFAULT '', max_context_messages INTEGER NOT NULL DEFAULT 30, is_imported INTEGER NOT NULL DEFAULT 0, is_auto_named INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
            c_v2.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, thinking_content TEXT NOT NULL DEFAULT '', provider_id TEXT NOT NULL DEFAULT '', model_id TEXT NOT NULL DEFAULT '', thinking_level TEXT NOT NULL DEFAULT 'none', prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0, cached_tokens INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)")
            c_v2.execute("CREATE TABLE tab_sessions (id INTEGER PRIMARY KEY CHECK (id = 1), state_json TEXT NOT NULL, updated_at REAL NOT NULL)")
            c_v2.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            c_v2.execute("CREATE TABLE token_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, provider_id TEXT NOT NULL DEFAULT '', model_id TEXT NOT NULL DEFAULT '', prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0, cached_tokens INTEGER NOT NULL DEFAULT 0, google_search_requests INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)")
            c_v2.execute("CREATE TABLE google_search_usage (month_key TEXT PRIMARY KEY, request_count INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL)")
            c_v2.execute("INSERT INTO conversations (id, title, is_imported, is_auto_named, created_at, updated_at) VALUES ('c_v2', 'V2 Topic', 0, 1, 1.0, 2.0)")
            c_v2.execute("INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES ('m_v2', 'c_v2', 'assistant', 'Hi from v2', 1.5)")
            c_v2.execute("PRAGMA user_version = 2")
            c_v2.commit()

        from chatint.db import open_conv_db
        prev_db = db_mod.DB_PATH
        try:
            db_mod.DB_PATH = v2_db
            cfg_mod.DB_PATH = v2_db
            init_db()
            self.assertTrue(v2_db.with_name("legacy_v2.db.bak").exists())
            with open_db() as c_up:
                self.assertEqual(c_up.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                main_tbls = {r[0] for r in c_up.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                self.assertNotIn("messages", main_tbls)
                self.assertNotIn("token_ledger", main_tbls)
            with open_conv_db("c_v2") as c_conv:
                cols = {r["name"] for r in c_conv.execute("PRAGMA table_info(messages)").fetchall()}
                self.assertIn("duration_ms", cols)
                self.assertIn("provider_name", cols)
                self.assertEqual(c_conv.execute("SELECT content FROM messages WHERE id='m_v2'").fetchone()[0], "Hi from v2")
        finally:
            db_mod.DB_PATH = prev_db
            cfg_mod.DB_PATH = prev_db

    def test_aud_h1_tool_budget_exceeded_closes_tool_turns_across_adapters(self):
        from chatint.providers.openai_adapter import _stream_openai
        from chatint.providers.anthropic_adapter import _stream_anthropic
        from chatint.providers.gemini_adapter import _stream_gemini

        # 1. OpenAI: 12 tool_calls in one round -> 10 executed, 2 receive synthetic budget error tool responses
        oai_round_msgs = []
        oai_call_num = 0

        def fake_oai_create(**kwargs):
            nonlocal oai_call_num
            oai_call_num += 1
            oai_round_msgs.append(kwargs.get("messages", []))
            if oai_call_num == 1:
                deltas = []
                for idx in range(12):
                    tc = MagicMock()
                    tc.index = idx
                    tc.id = f"call_{idx}"
                    tc.function = MagicMock(name="web_search", arguments='{"query": "q"}')
                    deltas.append(
                        MagicMock(
                            usage=None,
                            choices=[MagicMock(delta=MagicMock(content=None, reasoning_content=None, reasoning=None, tool_calls=[tc]))],
                        )
                    )
                return iter(deltas)
            final_delta = MagicMock(
                usage=MagicMock(prompt_tokens=20, completion_tokens=5, prompt_tokens_details=None),
                choices=[MagicMock(delta=MagicMock(content="Done after budget", reasoning_content=None, reasoning=None, tool_calls=None))],
            )
            return iter([final_delta])

        oai_client = MagicMock()
        oai_client.chat.completions.create.side_effect = fake_oai_create
        with (
            patch.object(ClientManager, "get_client", return_value=(oai_client, None)),
            patch("chatint.providers.openai_adapter._resolve_search_enabled", return_value=True),
            patch(
                "chatint.providers.openai_adapter._run_web_search_tool_call",
                return_value={"ok": True},
            ) as mock_oai_tool,
        ):
            oai_text, *_oai_rest = _stream_openai(
                bridge=DesktopBridge(),
                tab_id="t1",
                conversation_id="c1",
                messages=[{"role": "user", "content": "hi"}],
                provider_id="openai",
                model_id="gpt-4o",
                thinking_level="none",
                assistant_msg_id="m_oai",
                abort_event=threading.Event(),
                user_question="hi",
                web_search_enabled=True,
            )
        self.assertEqual("".join(oai_text), "Done after budget")
        self.assertEqual(mock_oai_tool.call_count, 10)
        oai_tool_msgs = [m for m in oai_round_msgs[1] if m.get("role") == "tool"]
        self.assertEqual([m["tool_call_id"] for m in oai_tool_msgs], [f"call_{i}" for i in range(12)])
        self.assertIn("Tool call budget limit", oai_tool_msgs[10]["content"])

        # 2. Anthropic: 11 tool_use blocks -> all 11 receive tool_result blocks (11th marked is_error=True)
        ant_round_msgs = []
        ant_call_num = 0

        class FakeAntEv:
            def __init__(self, ev_type, **kw):
                self.type = ev_type
                for k, v in kw.items():
                    setattr(self, k, v)

        def fake_ant_create(**kwargs):
            nonlocal ant_call_num
            ant_call_num += 1
            ant_round_msgs.append(kwargs.get("messages", []))
            if ant_call_num == 1:
                evs = []
                for idx in range(11):
                    blk = MagicMock(type="tool_use", id=f"ant_{idx}", input={"query": "q"})
                    blk.name = "web_search"
                    evs.append(FakeAntEv("content_block_start", index=idx, content_block=blk))
                    evs.append(FakeAntEv("content_block_stop", index=idx))
                return iter(evs)
            return iter([
                FakeAntEv("content_block_start", index=0, content_block=MagicMock(type="text", text="")),
                FakeAntEv("content_block_delta", index=0, delta=MagicMock(type="text_delta", text="Ant done")),
                FakeAntEv("content_block_stop", index=0),
            ])

        ant_client = MagicMock()
        ant_client.messages.create.side_effect = fake_ant_create
        with (
            patch.object(ClientManager, "get_anthropic_client", return_value=(ant_client, None)),
            patch("chatint.providers.anthropic_adapter._resolve_search_enabled", return_value=True),
            patch(
                "chatint.providers.anthropic_adapter._run_web_search_tool_call",
                return_value={"ok": True},
            ) as mock_ant_tool,
        ):
            ant_text, *_ant_rest = _stream_anthropic(
                bridge=DesktopBridge(),
                tab_id="t1",
                conversation_id="c1",
                messages=[{"role": "user", "content": "hi"}],
                provider_id="anthropic",
                model_id="claude-3-7-sonnet",
                thinking_level="none",
                assistant_msg_id="m_ant",
                abort_event=threading.Event(),
                user_question="hi",
                web_search_enabled=True,
            )
        self.assertEqual("".join(ant_text), "Ant done")
        self.assertEqual(mock_ant_tool.call_count, 10)
        tool_result_blocks = ant_round_msgs[1][-1]["content"]
        self.assertEqual(len(tool_result_blocks), 11)
        self.assertIn("Tool call budget limit", tool_result_blocks[10]["content"])

        # 3. Gemini: 11 function_call parts -> all 11 receive function_response parts
        from google.genai import types as genai_types

        gem_round_contents = []
        gem_call_num = 0

        def fake_gem_stream(**kwargs):
            nonlocal gem_call_num
            gem_call_num += 1
            gem_round_contents.append(kwargs.get("contents", []))
            if gem_call_num == 1:
                fn_parts = [
                    genai_types.Part.from_function_call(name="web_search", args={"query": f"q{idx}"})
                    for idx in range(11)
                ]
                cand = MagicMock(content=MagicMock(parts=fn_parts))
                return iter([MagicMock(usage_metadata=None, candidates=[cand])])
            final_part = MagicMock(thought=False, text="Gem done", function_call=None)
            final_cand = MagicMock(content=MagicMock(parts=[final_part]))
            return iter([MagicMock(usage_metadata=None, candidates=[final_cand])])

        gem_client = MagicMock()
        gem_client.models.generate_content_stream.side_effect = fake_gem_stream
        with (
            patch.object(ClientManager, "get_gemini_client", return_value=(gem_client, None)),
            patch("chatint.providers.gemini_adapter._resolve_search_enabled", return_value=True),
            patch(
                "chatint.providers.gemini_adapter._run_web_search_tool_call",
                return_value={"ok": True},
            ) as mock_gem_tool,
        ):
            gem_text, *_gem_rest = _stream_gemini(
                bridge=DesktopBridge(),
                tab_id="t1",
                conversation_id="c1",
                messages=[{"role": "user", "content": "hi"}],
                provider_id="gemini",
                model_id="gemini-2.5-flash",
                thinking_level="none",
                assistant_msg_id="m_gem",
                abort_event=threading.Event(),
                user_question="hi",
                web_search_enabled=True,
            )
        self.assertEqual("".join(gem_text), "Gem done")
        self.assertEqual(mock_gem_tool.call_count, 10)
        tool_resp_content = gem_round_contents[1][-1]
        self.assertEqual(len(tool_resp_content.parts), 11)
        self.assertIn("Tool call budget limit", str(tool_resp_content.parts[10].function_response.response))

    def test_aud_m1_loopback_provider_keyless_status_and_sync(self):
        provs = get_providers()
        provs.append({
            "id": "custom_loopback",
            "name": "LocalLoop",
            "base_url": "http://127.0.0.1:11434/v1",
            "is_enabled": True,
            "models": [],
        })
        provs.append({
            "id": "ollama",
            "name": "Ollama",
            "base_url": "http://localhost:11434/v1",
            "is_enabled": True,
            "clear_api_key": True,
            "models": [],
        })
        save_providers(provs)

        reloaded = {p["id"]: p for p in get_providers()}
        self.assertTrue(reloaded["custom_loopback"]["has_api_key"])
        self.assertTrue(reloaded["ollama"]["has_api_key"])
        self.assertNotEqual(reloaded["ollama"]["sync_status"], "no_key")

        emitted = []
        with (
            patch.object(
                ClientManager,
                "fetch_remote_models",
                return_value=([{"id": "llama3:latest", "name": "llama3:latest"}], None, False),
            ),
            patch.object(BackgroundModelSyncer, "_emit_provider_update", side_effect=emitted.append),
        ):
            BackgroundModelSyncer._sync_single_provider("custom_loopback")
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["sync_status"], "ready")
        self.assertEqual(emitted[0]["fetched_model_ids"], ["llama3:latest"])

    def test_aud_m2_openai_stream_options_400_retries_preserving_effort(self):
        import httpx2
        import openai
        from chatint.providers.openai_adapter import _stream_openai

        provs = get_providers()
        for p in provs:
            if p["id"] == "openai":
                p["models"] = [{"id": "o3-compat", "name": "o3-compat"}]
        save_providers(provs)

        calls = []

        def fake_create(**kwargs):
            calls.append(("stream_options" in kwargs, kwargs.get("reasoning_effort")))
            if "stream_options" in kwargs:
                req = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
                resp = httpx2.Response(400, request=req)
                raise openai.BadRequestError("Extra inputs are not permitted", response=resp, body=None)
            chunk = MagicMock()
            chunk.usage = MagicMock(prompt_tokens=11, completion_tokens=7, prompt_tokens_details=None)
            chunk.choices = [MagicMock(delta=MagicMock(content="Compat reply", reasoning_content=None, reasoning=None, tool_calls=None))]
            return iter([chunk])

        client = MagicMock()
        client.chat.completions.create.side_effect = fake_create
        with (
            patch.object(ClientManager, "get_client", return_value=(client, None)),
            patch("chatint.providers.openai_adapter._resolve_search_enabled", return_value=False),
        ):
            text, _think, _in, _out, _cached, had_err, actual_lvl = _stream_openai(
                bridge=DesktopBridge(),
                tab_id="t1",
                conversation_id="c1",
                messages=[{"role": "user", "content": "hi"}],
                provider_id="openai",
                model_id="o3-compat",
                thinking_level="high",
                assistant_msg_id="m_compat",
                abort_event=threading.Event(),
                user_question="hi",
                web_search_enabled=False,
            )
        self.assertFalse(had_err)
        self.assertEqual("".join(text), "Compat reply")
        self.assertEqual(actual_lvl, "high")
        self.assertEqual(calls, [(True, "high"), (False, "high")])

    def test_aud_m3_error_emits_token_stats_updated_and_turn2_auto_names(self):
        from chatint.services.stream_coordinator import stream_chat_completion_impl

        conv = create_conversation("New Chat")
        add_message(conv["id"], "user", "First")
        add_message(conv["id"], "assistant", "Earlier reply")
        add_message(conv["id"], "user", "Second")

        events = []

        class CaptureBridge:
            def push_event(self, ev):
                events.append(ev)

        bridge = CaptureBridge()
        # 1. Stream fails after receiving token usage -> emits token_stats_updated
        err_streamer = MagicMock(return_value=(["Partial"], [], 33, 12, 0, True, "none"))
        auto_name_mock = MagicMock()
        stream_chat_completion_impl(
            bridge=bridge,
            tab_id="t1",
            conversation_id=conv["id"],
            messages=[{"role": "user", "content": "Second"}],
            provider_id="openai",
            model_id="gpt-4o",
            thinking_level="none",
            stream_id="s-err",
            sdk_streamers={"openai": err_streamer},
            auto_naming_fn=auto_name_mock,
        )
        stats_evs = [e for e in events if e.get("type") == "token_stats_updated"]
        self.assertEqual(len(stats_evs), 1)
        self.assertGreaterEqual(stats_evs[0]["token_stats"]["grand_total"]["total_tokens"], 45)
        auto_name_mock.assert_not_called()

        # 2. Successful Turn 2 on still-unnamed conversation triggers auto-naming
        ok_streamer = MagicMock(return_value=(["Full reply"], [], 10, 5, 0, False, "none"))
        stream_chat_completion_impl(
            bridge=bridge,
            tab_id="t1",
            conversation_id=conv["id"],
            messages=[{"role": "user", "content": "Second"}],
            provider_id="openai",
            model_id="gpt-4o",
            thinking_level="none",
            stream_id="s-ok",
            sdk_streamers={"openai": ok_streamer},
            auto_naming_fn=auto_name_mock,
        )
        auto_name_mock.assert_called_once()

    def test_aud_m4_prepare_clean_default_db_excludes_personal_state(self):
        import contextlib
        import importlib.util

        spec = importlib.util.spec_from_file_location("chatint_build_release", Path("packaging/build_release.py"))
        br = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(br)

        fake_root = Path(self.test_dir.name) / "pkg_root"
        with patch.object(br, "PROJECT_ROOT", fake_root):
            # 1. Fallback path when no source data/chatint.db exists
            clean_db = br.prepare_clean_default_db()
            self.assertTrue(clean_db and clean_db.is_file())
            self.assertFalse((clean_db.parent / ".env.tmp").exists())

            # 2. Curated baseline DB path: preserves models/settings, strips runtime/personal tables & api_key
            src_data = fake_root / "data"
            src_data.mkdir(parents=True, exist_ok=True)
            curated_src = src_data / "chatint.db"
            orig_db, orig_cfg_db = db_mod.DB_PATH, cfg_mod.DB_PATH
            orig_env, orig_cfg_env = db_mod.ENV_PATH, cfg_mod.ENV_PATH
            try:
                tmp_env = fake_root / ".env.src"
                db_mod.DB_PATH = cfg_mod.DB_PATH = curated_src
                db_mod.ENV_PATH = cfg_mod.ENV_PATH = tmp_env
                init_db()
                c_tmp = create_conversation("Private chat")
                add_message(c_tmp["id"], "user", "Secret prompt")
                record_token_usage(c_tmp["id"], "anthropic", "claude-fable-5-2", 10, 5, 0)
                with contextlib.closing(sqlite3.connect(curated_src)) as conn:
                    conn.execute(
                        "UPDATE providers SET api_key = 'raw-leak', models_json = ? WHERE id = 'anthropic'",
                        (json.dumps([{"id": "claude-fable-5-2", "name": "claude-fable-5-2", "input_price": 8.0}]),),
                    )
                    conn.execute("INSERT OR REPLACE INTO tab_sessions (id, state_json, updated_at) VALUES (1, '{}', 1.0)")
                    conn.execute("INSERT OR REPLACE INTO google_search_usage (month_key, request_count, updated_at) VALUES ('2026-11', 9, 1.0)")
                    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('default_model', 'claude-fable-5-2')")
                    conn.commit()
            finally:
                db_mod.DB_PATH, cfg_mod.DB_PATH = orig_db, orig_cfg_db
                db_mod.ENV_PATH, cfg_mod.ENV_PATH = orig_env, orig_cfg_env

            curated_clean = br.prepare_clean_default_db()

        with contextlib.closing(sqlite3.connect(curated_clean)) as c_clean:
            clean_tbls = {r[0] for r in c_clean.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertNotIn("messages", clean_tbls)
            self.assertNotIn("token_ledger", clean_tbls)
            for tbl in ("conversations", "tab_sessions", "google_search_usage"):
                self.assertEqual(c_clean.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0], 0, tbl)
            api_keys = [r[0] for r in c_clean.execute("SELECT api_key FROM providers").fetchall()]
            self.assertTrue(all(k == "" for k in api_keys))
            ant_models = json.loads(
                c_clean.execute("SELECT models_json FROM providers WHERE id = 'anthropic'").fetchone()[0]
            )
            self.assertEqual(ant_models[0]["id"], "claude-fable-5-2")
            def_model = c_clean.execute("SELECT value FROM app_settings WHERE key = 'default_model'").fetchone()[0]
            self.assertEqual(def_model, "claude-fable-5-2")

    def test_aud_l1_l2_l3_import_regen_cas_and_env_preservation(self):
        from chatint.storage.conversations_repo import delete_messages_from
        from chatint.storage.providers_repo import update_provider_models

        # AUD-L1: Unclosed thought delimiter does not swallow subsequent turns
        md_unclosed = (
            "### User\nFirst question\n\n"
            "### Assistant\n<!-- chatint-thought-start -->\nTrailing thought\n\n"
            "### User\nSecond question\n\n"
            "### Assistant\nSecond answer\n"
        )
        parsed = parse_chat_file_content(md_unclosed, "unclosed.md")
        self.assertEqual(len(parsed["messages"]), 4)
        self.assertEqual(parsed["messages"][2]["content"], "Second question")

        # AUD-L2: Empty message_id rejected by regenerate_message and delete_messages_from
        conv = create_conversation("Regen Guard")
        m_u = add_message(conv["id"], "user", "Keep me")
        res_empty = DesktopBridge().regenerate_message("t1", conv["id"], "", "openai", "gpt-5")
        self.assertIn("error", res_empty)
        delete_messages_from(conv["id"], "", include_self=True)
        self.assertEqual([m["id"] for m in get_conversation(conv["id"])["messages"]], [m_u["id"]])

        # AUD-L3: CAS guard rejects stale expected_models_json; ensure_env_file does not re-seed pruned keys
        self.assertFalse(update_provider_models("openai", [{"id": "x", "name": "x"}], expected_models_json='["stale"]'))
        env_p = db_mod.ENV_PATH
        orig_env = env_p.read_text(encoding="utf-8") if env_p.exists() else ""
        try:
            env_p.write_text('WORKOPENAI_API_KEY=""\n', encoding="utf-8")
            db_mod.ensure_env_file()
            self.assertNotRegex(env_p.read_text(encoding="utf-8"), r"(?m)^OPENAI_API_KEY=")
        finally:
            env_p.write_text(orig_env, encoding="utf-8")



    def test_stage1_ddg_default_web_search_and_gemini_opt_in(self):
        from chatint.db import get_app_settings, save_app_settings
        from chatint.web_search_tool import execute_ddg_web_search, is_web_search_available

        # 1. Default setting is off -> web search works out of the box without Gemini key
        self.assertEqual(get_app_settings().get("use_gemini_web_search"), "false")
        self.assertEqual(is_web_search_available(), (True, ""))

        fake_ddgs_instance = MagicMock()
        fake_ddgs_instance.text.return_value = [
            {"title": "Python 3.14 Docs", "href": "https://docs.python.org/3.14/", "body": "Official documentation."},
            {"title": "PEPs", "href": "https://peps.python.org/", "body": "Python Enhancement Proposals."},
        ]
        with patch("ddgs.DDGS", return_value=fake_ddgs_instance):
            res = execute_web_search("python 3.14 docs")

        self.assertTrue(res["ok"])
        self.assertEqual(res["provider_used"], "duckduckgo")
        self.assertEqual(res["search_requests_used"], 0)
        self.assertEqual(len(res["results"]), 2)
        self.assertEqual(res["sources"][0]["url"], "https://docs.python.org/3.14/")

        # Fallback to backend='auto' when restricted engine group yields no results
        fake_ddgs_fallback = MagicMock()
        fake_ddgs_fallback.text.side_effect = [
            [],
            [{"title": "Fallback Hit", "href": "https://example.org/", "body": "Recovered via auto."}],
        ]
        with patch("ddgs.DDGS", return_value=fake_ddgs_fallback):
            fb_res = execute_ddg_web_search("fallback query")
        self.assertTrue(fb_res["ok"])
        self.assertEqual(fb_res["results"][0]["title"], "Fallback Hit")
        self.assertEqual(fake_ddgs_fallback.text.call_count, 2)

        # Empty query & exception fallback stay non-throwing
        self.assertFalse(execute_ddg_web_search("   ")["ok"])
        fake_ddgs_err = MagicMock()
        fake_ddgs_err.text.side_effect = RuntimeError("rate limited")
        with patch("ddgs.DDGS", return_value=fake_ddgs_err):
            err_res = execute_ddg_web_search("rate limit test")
        self.assertFalse(err_res["ok"])
        self.assertIn("rate limited", err_res["error"])

        # 2. Opting into Gemini Google Search restores Gemini key gating
        save_app_settings({"use_gemini_web_search": "true"})
        self.assertEqual(get_app_settings().get("use_gemini_web_search"), "true")
        set_api_key_in_env("Gemini", "")
        avail, reason = is_web_search_available()
        self.assertFalse(avail)
        self.assertEqual(reason, "NO API KEY")

    def test_stage2_schema_v4_migration_and_clean_template_hygiene(self):
        import contextlib
        import importlib.util
        import sqlite3
        from chatint.db import SCHEMA_VERSION, get_app_settings, get_conversation, init_db, save_app_settings

        build_rel_path = Path(__file__).resolve().parent.parent / "packaging" / "build_release.py"
        spec = importlib.util.spec_from_file_location("build_release_mod", build_rel_path)
        build_release_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build_release_mod)
        prepare_clean_default_db = build_release_mod.prepare_clean_default_db

        # 1. Verify v3 -> v4 upgrade creates .bak backup, new columns, and app_settings defaults
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            fake_data = tmp_root / "data"
            fake_data.mkdir(parents=True, exist_ok=True)
            v3_db = fake_data / "chatint.db"
            with contextlib.closing(sqlite3.connect(v3_db)) as conn:
                conn.executescript(
                """
                PRAGMA user_version = 3;
                CREATE TABLE providers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
                    api_key TEXT NOT NULL DEFAULT '', is_enabled INTEGER NOT NULL DEFAULT 1,
                    models_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT 'New Chat',
                    system_prompt TEXT NOT NULL DEFAULT '', max_context_messages INTEGER NOT NULL DEFAULT 30,
                    is_imported INTEGER NOT NULL DEFAULT 0, is_auto_named INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, thinking_content TEXT NOT NULL DEFAULT '',
                    provider_id TEXT NOT NULL DEFAULT '', provider_name TEXT NOT NULL DEFAULT '',
                    model_id TEXT NOT NULL DEFAULT '', thinking_level TEXT NOT NULL DEFAULT 'none',
                    prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE TABLE token_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL DEFAULT '', model_id TEXT NOT NULL DEFAULT '',
                    prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0, google_search_requests INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE TABLE google_search_usage (
                    month_key TEXT PRIMARY KEY, request_count INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL
                );
                CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE tab_sessions (id INTEGER PRIMARY KEY CHECK (id = 1), state_json TEXT NOT NULL, updated_at REAL NOT NULL);
                INSERT INTO conversations (id, title, system_prompt, max_context_messages, is_imported, is_auto_named, created_at, updated_at)
                VALUES ('c_v3', 'Legacy Chat', '', 30, 0, 1, 1.0, 2.0);
                INSERT INTO messages (id, conversation_id, role, content, created_at)
                VALUES ('m_v3', 'c_v3', 'assistant', 'Legacy answer', 1.5);
                """
                )
                conn.commit()

            orig_db_path = db_mod.DB_PATH
            orig_proj_root = build_release_mod.PROJECT_ROOT
            try:
                db_mod.DB_PATH = v3_db
                init_db()
                bak_path = Path(str(v3_db) + ".bak")
                self.assertTrue(bak_path.is_file(), "Migration v3 -> v4 should create pre-migration .bak backup")

                with open_db() as migrated:
                    self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                    self.assertEqual(SCHEMA_VERSION, 6)
                    migrated_tbls = {r[0] for r in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                    self.assertNotIn("messages", migrated_tbls)
                    self.assertNotIn("token_ledger", migrated_tbls)

                from chatint.db import open_conv_db
                with open_conv_db("c_v3") as conv_conn:
                    msg_cols = {r["name"] for r in conv_conn.execute("PRAGMA table_info(messages)").fetchall()}
                    self.assertIn("worked_ms", msg_cols)
                    self.assertIn("tool_trace_json", msg_cols)
                    ledger_cols = {r["name"] for r in conv_conn.execute("PRAGMA table_info(token_ledger)").fetchall()}
                    self.assertIn("ddg_search_requests", ledger_cols)
                    self.assertIn("provider_name", ledger_cols)
                    self.assertIn("cost_usd", ledger_cols)

                conv = get_conversation("c_v3")
                self.assertEqual(conv["workspace_dir"], "")
                self.assertEqual(conv["agent_mode"], "manual")
                self.assertEqual(conv["messages"][0]["tool_trace"], [])
                app_cfg = get_app_settings()
                self.assertIn("default_workspace_dir", app_cfg)
                self.assertEqual(app_cfg["default_agent_mode"], "manual")
                self.assertEqual(app_cfg["max_chat_size_mb"], "50")
                self.assertEqual(app_cfg["max_total_chats_size_mb"], "2048")

                # 2. prepare_clean_default_db scrubs personal default_workspace_dir & resets default_agent_mode
                save_app_settings({"default_workspace_dir": "C:/Users/private/secret_repo", "default_agent_mode": "auto"})
                build_release_mod.PROJECT_ROOT = tmp_root
                clean_template = prepare_clean_default_db()
                self.assertIsNotNone(clean_template)
                with contextlib.closing(sqlite3.connect(clean_template)) as clean_conn:
                    clean_conn.row_factory = sqlite3.Row
                    cfg_rows = {r["key"]: r["value"] for r in clean_conn.execute("SELECT key, value FROM app_settings")}
                    self.assertEqual(cfg_rows["default_workspace_dir"], "")
                    self.assertEqual(cfg_rows["default_agent_mode"], "manual")
                    self.assertEqual(clean_conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0)
            finally:
                build_release_mod.PROJECT_ROOT = orig_proj_root
                db_mod.DB_PATH = orig_db_path

    def test_stage2_workspace_sandbox_tools_and_fetch_url_ssrf(self):
        from chatint.tools import (
            fetch_url,
            resolve_workspace_path,
            tool_edit_file,
            tool_list_dir,
            tool_read_file,
            tool_search_files,
            tool_write_file,
        )
        from chatint.tools.workspace_tools import preview_edit_file, preview_write_file

        with tempfile.TemporaryDirectory() as ws_tmp, tempfile.TemporaryDirectory() as outside_tmp:
            ws = Path(ws_tmp)
            outside = Path(outside_tmp)
            outside_secret = outside / "outside_secret.txt"
            outside_secret.write_text("TOP_SECRET", encoding="utf-8")

            # Sandbox blocks traversal, UNC, and protected filenames
            with self.assertRaises(PermissionError):
                resolve_workspace_path(ws, "../escape.txt")
            with self.assertRaises(PermissionError):
                resolve_workspace_path(ws, r"\\server\share\secret.txt")
            with self.assertRaises(PermissionError):
                resolve_workspace_path(ws, "//host/share/file.txt")
            with self.assertRaises(PermissionError):
                resolve_workspace_path(ws, ".env")
            with self.assertRaises(PermissionError):
                resolve_workspace_path(ws, "data/logs/chatint.log")
            with self.assertRaises(PermissionError):
                resolve_workspace_path(ws, "chatint.db")

            # Symlink escape check (works natively when OS allows symlinks, and via resolve check)
            link_path = ws / "escape_link.txt"
            try:
                link_path.symlink_to(outside_secret)
            except OSError:
                with patch.object(Path, "resolve", side_effect=[ws, outside_secret]):
                    with self.assertRaises(PermissionError):
                        resolve_workspace_path(ws, "escape_link.txt")
            else:
                with self.assertRaises(PermissionError):
                    resolve_workspace_path(ws, "escape_link.txt")

            # Read/Write tool lifecycle
            w_res = tool_write_file(ws, "src/app.py", "def greet():\n    return 'hi'\n", overwrite=True)
            self.assertTrue(w_res["ok"])
            self.assertTrue(w_res["created"])
            self.assertIn("return 'hi'", w_res["diff"])

            w_no_over = tool_write_file(ws, "src/app.py", "other", overwrite=False)
            self.assertFalse(w_no_over["ok"])

            (ws / "src" / "bin.dat").write_bytes(b"\x00\x01\x02\x03")
            (ws / ".git").mkdir()
            (ws / ".git" / "config").write_text("ignored", encoding="utf-8")
            (ws / ".env").write_text("OPENAI_API_KEY=secret", encoding="utf-8")

            l_res = tool_list_dir(ws, ".", max_depth=3)
            self.assertTrue(l_res["ok"])
            listed_paths = {e["path"] for e in l_res["entries"]}
            self.assertIn("src/app.py", listed_paths)
            self.assertNotIn(".git/", listed_paths)
            self.assertNotIn(".env", listed_paths)

            r_res = tool_read_file(ws, "src/app.py", start_line=1, end_line=10)
            self.assertTrue(r_res["ok"])
            self.assertIn("2:     return 'hi'", r_res["content"])
            self.assertFalse(tool_read_file(ws, "src/bin.dat")["ok"])

            tool_write_file(ws, "src/dup.txt", "alpha\nalpha\n", overwrite=True)
            s_res = tool_search_files(ws, r"return\s+'hi'", path="src", glob_pattern="*.py", is_regex=True)
            self.assertTrue(s_res["ok"])
            self.assertEqual(s_res["count"], 1)
            self.assertEqual(s_res["matches"][0]["path"], "src/app.py")

            # Preview & edit_file ambiguity / success
            prev_bad = preview_edit_file({"path": "src/dup.txt", "target": "alpha", "replacement": "beta"}, ws)
            self.assertFalse(prev_bad["ok"])
            self.assertIn("2 times", prev_bad["error"])

            e_res = tool_edit_file(ws, "src/app.py", target="return 'hi'", replacement="return 'hello'")
            self.assertTrue(e_res["ok"])
            self.assertIn("-    return 'hi'", e_res["diff"])
            self.assertIn("+    return 'hello'", e_res["diff"])
            self.assertEqual((ws / "src" / "app.py").read_text(encoding="utf-8"), "def greet():\n    return 'hello'\n")
            prev_write = preview_write_file({"path": "src/new.txt", "content": "abc\n"}, ws)
            self.assertTrue(prev_write["ok"])

            # Windows CRLF multi-line edit, null/string arg coercion, and unknown extra kwargs resilience
            from chatint.tools import get_tool_spec
            (ws / "src" / "win.txt").write_bytes(b"line_a\r\nline_b\r\nline_c\r\n")
            crlf_res = tool_edit_file(ws, "src/win.txt", target="line_a\nline_b", replacement="line_a\nline_b_edited")
            self.assertTrue(crlf_res["ok"])
            self.assertEqual((ws / "src" / "win.txt").read_bytes(), b"line_a\r\nline_b_edited\r\nline_c\r\n")
            extra_kw_res = get_tool_spec("read_file").handler(
                workspace_dir=str(ws), path="src/win.txt", start_line="1", end_line="2", unexpected_llm_arg=True
            )
            self.assertTrue(extra_kw_res["ok"])
            self.assertTrue(get_tool_spec("write_file").handler(workspace_dir=str(ws), path="src/null.txt", content=None)["ok"])

            # fetch_url SSRF guard checks
            self.assertFalse(fetch_url("file:///etc/passwd")["ok"])
            self.assertFalse(fetch_url("http://127.0.0.1:8080/")["ok"])
            self.assertFalse(fetch_url("http://localhost:5000/admin")["ok"])
            self.assertFalse(fetch_url("http://169.254.169.254/latest/meta-data")["ok"])
            with patch("chatint.tools.web_tools.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 80))]):
                self.assertFalse(fetch_url("https://internal.corp.example/secret")["ok"])

            # fetch_url public HTML extraction + redirect SSRF blocking
            class _FakeStreamResp:
                def __init__(self, status_code, headers, body=b""):
                    self.status_code = status_code
                    self.headers = headers
                    self._body = body
                    self.encoding = "utf-8"
                def iter_bytes(self):
                    yield self._body
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False

            with patch("chatint.tools.web_tools.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
                fake_client = MagicMock()
                fake_client.__enter__.return_value = fake_client
                fake_client.stream.return_value = _FakeStreamResp(
                    200,
                    {"content-type": "text/html; charset=utf-8"},
                    b"<html><head><title>Example Doc</title><style>body{}</style></head><body><h1>Hello World</h1><script>alert(1)</script><p>Readable body.</p></body></html>",
                )
                with patch("chatint.tools.web_tools.httpx.Client", return_value=fake_client):
                    ok_fetch = fetch_url("https://example.com/page")
                self.assertTrue(ok_fetch["ok"])
                self.assertEqual(ok_fetch["title"], "Example Doc")
                self.assertIn("Readable body.", ok_fetch["content"])
                self.assertNotIn("alert(1)", ok_fetch["content"])

                fake_redir = MagicMock()
                fake_redir.__enter__.return_value = fake_redir
                fake_redir.stream.return_value = _FakeStreamResp(302, {"location": "http://127.0.0.1:8000/"})
                with patch("chatint.tools.web_tools.httpx.Client", return_value=fake_redir):
                    bad_redir = fetch_url("https://example.com/redir")
                self.assertFalse(bad_redir["ok"])
                self.assertIn("127.0.0.1", bad_redir["error"])

                fake_c = MagicMock()
                fake_c.__enter__.return_value = fake_c
                fake_c.stream.return_value = _FakeStreamResp(200, {"content-type": "text/plain"}, b"ok text")
                with patch("chatint.tools.web_tools.httpx.Client", return_value=fake_c):
                    self.assertTrue(fetch_url("https://example.com/txt", max_chars="not_an_int")["ok"])

    def test_stage2_manual_approval_gate_context_summary_and_adapters(self):
        import time
        from chatint.db import add_message, create_conversation, duplicate_conversation, get_conversation
        from chatint.providers.anthropic_adapter import AnthropicAdapter
        from chatint.providers.base import _run_registered_tool_call
        from chatint.providers.gemini_adapter import GeminiAdapter
        from chatint.providers.openai_adapter import OpenAIAdapter
        from chatint.services.stream_coordinator import StreamManager

        with tempfile.TemporaryDirectory() as ws_tmp:
            ws = Path(ws_tmp)
            target_file = ws / "notes.txt"
            target_file.write_text("before", encoding="utf-8")

            conv = create_conversation("Agent Chat", workspace_dir=str(ws), agent_mode="manual")
            conv_id = conv["id"]
            bridge = MagicMock()
            dispatched = []
            bridge.push_event.side_effect = lambda ev: dispatched.append(ev)

            # 1. Manual mode Deny leaves file untouched and records denied trace
            abort_ev = threading.Event()
            trace_deny = []
            runtime_state = {"agent_mode": "manual"}
            StreamManager.register_stream("tab_a", stream_id="sid_deny", conversation_id=conv_id)
            try:
                def _deny_worker():
                    deadline = time.time() + 2.0
                    while time.time() < deadline:
                        ev = next((e for e in dispatched if e.get("type") == "tool_approval_required"), None)
                        if ev:
                            DesktopBridge().resolve_tool_approval("tab_a", "sid_deny", ev["approval_id"], "deny")
                            return
                        time.sleep(0.01)

                t = threading.Thread(target=_deny_worker, daemon=True)
                t.start()
                res_deny = _run_registered_tool_call(
                    bridge, "tab_a", conv_id, "write_file",
                    {"path": "notes.txt", "content": "overwritten"}, abort_ev, stream_id="sid_deny",
                    workspace_dir=str(ws), agent_mode="manual", runtime_state=runtime_state,
                    tool_trace=trace_deny, web_search_enabled=False, agent_enabled=True,
                )
                t.join(timeout=2.0)
                self.assertFalse(res_deny["ok"])
                self.assertEqual(target_file.read_text(encoding="utf-8"), "before")
                self.assertEqual(trace_deny[0]["status"], "denied")
                apr_ev = next(e for e in dispatched if e.get("type") == "tool_approval_required")
                self.assertIn("-before", apr_ev["diff"])
                self.assertIn("+overwritten", apr_ev["diff"])
            finally:
                StreamManager.unregister_stream("tab_a", stream_id="sid_deny")

            # 2. Manual mode allow_once writes file and keeps manual mode; allow_all_tab promotes to auto
            dispatched.clear()
            trace_allow = []
            StreamManager.register_stream("tab_a", stream_id="sid_allow", conversation_id=conv_id)
            try:
                def _allow_worker():
                    seen_ids = set()
                    deadline = time.time() + 3.0
                    while time.time() < deadline:
                        for e in list(dispatched):
                            if e.get("type") == "tool_approval_required" and e["approval_id"] not in seen_ids:
                                seen_ids.add(e["approval_id"])
                                dec = "allow_once" if len(seen_ids) == 1 else "allow_all_tab"
                                StreamManager.resolve_approval("sid_allow", e["approval_id"], dec)
                        if len(seen_ids) >= 2:
                            return
                        time.sleep(0.01)

                t2 = threading.Thread(target=_allow_worker, daemon=True)
                t2.start()
                res_1 = _run_registered_tool_call(
                    bridge, "tab_a", conv_id, "write_file",
                    {"path": "notes.txt", "content": "after_once"}, abort_ev, stream_id="sid_allow",
                    workspace_dir=str(ws), agent_mode="manual", runtime_state=runtime_state,
                    tool_trace=trace_allow, web_search_enabled=False, agent_enabled=True,
                )
                self.assertTrue(res_1["ok"])
                self.assertEqual(runtime_state["agent_mode"], "manual")
                res_2 = _run_registered_tool_call(
                    bridge, "tab_a", conv_id, "edit_file",
                    {"path": "notes.txt", "target": "after_once", "replacement": "after_auto"}, abort_ev,
                    stream_id="sid_allow", workspace_dir=str(ws), agent_mode="manual",
                    runtime_state=runtime_state, tool_trace=trace_allow, web_search_enabled=False, agent_enabled=True,
                )
                t2.join(timeout=2.0)
                self.assertTrue(res_2["ok"])
                self.assertEqual(runtime_state["agent_mode"], "auto")
                self.assertTrue(any(e.get("type") == "agent_mode_changed" and e.get("agent_mode") == "auto" for e in dispatched))
                self.assertEqual(get_conversation(conv_id)["agent_mode"], "auto")

                # Subsequent write skips approval automatically now that runtime_state is auto
                dispatched.clear()
                res_3 = _run_registered_tool_call(
                    bridge, "tab_a", conv_id, "write_file",
                    {"path": "second.txt", "content": "auto_ok"}, abort_ev,
                    stream_id="sid_allow", workspace_dir=str(ws), agent_mode="manual",
                    runtime_state=runtime_state, tool_trace=trace_allow, web_search_enabled=False, agent_enabled=True,
                )
                self.assertTrue(res_3["ok"])
                self.assertFalse(any(e.get("type") == "tool_approval_required" for e in dispatched))
            finally:
                StreamManager.unregister_stream("tab_a", stream_id="sid_allow")

            # 3. Abort unblocks waiting approval thread immediately
            abort_gate = threading.Event()
            StreamManager.register_stream("tab_a", stream_id="sid_abort", conversation_id=conv_id)
            gate_out = []
            gt = threading.Thread(
                target=lambda: gate_out.append(StreamManager.request_approval("tab_a", "sid_abort", "apr_wait", abort_gate, timeout=10.0)),
                daemon=True,
            )
            gt.start()
            time.sleep(0.05)
            StreamManager.abort_stream("tab_a", stream_id="sid_abort")
            gt.join(timeout=1.5)
            self.assertEqual(gate_out, ["deny"])
            StreamManager.unregister_stream("tab_a", stream_id="sid_abort")

            # 4. Persisted tool_trace -> compact context summary -> branch duplication
            add_message(conv_id, "user", "Update notes")
            add_message(conv_id, "assistant", "Updated notes file.", tool_trace=trace_allow)
            conv_loaded = get_conversation(conv_id)
            ctx_msgs = DesktopBridge._build_context_messages(conv_loaded)
            self.assertIn("[Tools used:", ctx_msgs[-1]["content"])
            self.assertIn("write_file(notes.txt)", ctx_msgs[-1]["content"])
            branched = duplicate_conversation(conv_id)
            self.assertEqual(branched["workspace_dir"], str(ws))
            self.assertEqual(branched["agent_mode"], "auto")
            self.assertEqual(len(branched["messages"][-1]["tool_trace"]), 3)

            # 5. Multi-round agent tool dispatch across OpenAI, Anthropic, and Gemini adapters
            from types import SimpleNamespace
            (ws / "readme.txt").write_text("line1\nhello agent\n", encoding="utf-8")

            # OpenAI
            oai_r1 = [
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(index=0, id="oai_tc_1", function=SimpleNamespace(name="read_file", arguments='{"path": "readme.txt"}'))],
                    reasoning_content=None,
                ))], usage=None),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=None, reasoning_content=None))], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None)),
            ]
            oai_r2 = [
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Read done.", tool_calls=None, reasoning_content=None))], usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4, prompt_tokens_details=None)),
            ]
            oai_client = MagicMock()
            oai_client.chat.completions.create.side_effect = [iter(oai_r1), iter(oai_r2)]
            oai_trace = []
            with patch("chatint.client_manager.ClientManager.get_client", return_value=(oai_client, None)), \
                 patch("chatint.client_manager.ClientManager.get_provider", return_value={"id": "openai", "base_url": ""}):
                c, _th, *_rest = OpenAIAdapter().stream_chat(
                    bridge, "t1", conv_id, [{"role": "user", "content": "read readme"}],
                    "openai", "gpt-5", "none", None, threading.Event(), "read readme",
                    web_search_enabled=False, stream_id=None, agent_enabled=True,
                    workspace_dir=str(ws), agent_mode="manual", tool_trace=oai_trace,
                )
            self.assertEqual("".join(c), "Read done.")
            self.assertEqual(oai_trace[0]["tool"], "read_file")
            self.assertIn("hello agent", oai_trace[0]["result_preview"])

            # Anthropic
            ant_r1 = [
                SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace(input_tokens=10, cache_read_input_tokens=0))),
                SimpleNamespace(type="content_block_start", index=0, content_block=SimpleNamespace(type="tool_use", id="ant_tc_1", name="list_dir", input={"path": "."})),
                SimpleNamespace(type="content_block_stop", index=0),
                SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=5)),
                SimpleNamespace(type="message_stop"),
            ]
            ant_r2 = [
                SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace(input_tokens=14, cache_read_input_tokens=0))),
                SimpleNamespace(type="content_block_start", index=0, content_block=SimpleNamespace(type="text")),
                SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="Listed.")),
                SimpleNamespace(type="content_block_stop", index=0),
                SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=3)),
                SimpleNamespace(type="message_stop"),
            ]
            ant_client = MagicMock()
            ant_client.messages.create.side_effect = [iter(ant_r1), iter(ant_r2)]
            ant_trace = []
            with patch("chatint.client_manager.ClientManager.get_anthropic_client", return_value=(ant_client, None)):
                c2, *_r2 = AnthropicAdapter().stream_chat(
                    bridge, "t1", conv_id, [{"role": "user", "content": "list dir"}],
                    "anthropic", "claude-sonnet-4-5", "none", None, threading.Event(), "list dir",
                    web_search_enabled=False, stream_id=None, agent_enabled=True,
                    workspace_dir=str(ws), agent_mode="manual", tool_trace=ant_trace,
                )
            self.assertEqual("".join(c2), "Listed.")
            self.assertEqual(ant_trace[0]["tool"], "list_dir")

            # Gemini
            gem_part_call = SimpleNamespace(
                thought=False,
                thought_signature=None,
                text=None,
                function_call=SimpleNamespace(id="gem_tc_1", name="search_files", args={"query": "hello agent"}),
            )
            gem_r1 = [
                SimpleNamespace(
                    candidates=[SimpleNamespace(content=SimpleNamespace(parts=[gem_part_call]))],
                    usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5, cached_content_token_count=0),
                )
            ]
            gem_r2 = [
                SimpleNamespace(
                    candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(thought=False, thought_signature=None, text="Searched.", function_call=None)]))],
                    usage_metadata=SimpleNamespace(prompt_token_count=12, candidates_token_count=4, cached_content_token_count=0),
                )
            ]
            gem_client = MagicMock()
            gem_client.models.generate_content_stream.side_effect = [iter(gem_r1), iter(gem_r2)]
            gem_trace = []
            with patch("chatint.client_manager.ClientManager.get_gemini_client", return_value=(gem_client, None)):
                c3, *_r3 = GeminiAdapter().stream_chat(
                    bridge, "t1", conv_id, [{"role": "user", "content": "search"}],
                    "gemini", "gemini-2.5-flash", "none", None, threading.Event(), "search",
                    web_search_enabled=False, stream_id=None, agent_enabled=True,
                    workspace_dir=str(ws), agent_mode="manual", tool_trace=gem_trace,
                )
            self.assertEqual("".join(c3), "Searched.")
            self.assertEqual(gem_trace[0]["tool"], "search_files")
            self.assertIn("readme.txt:2", gem_trace[0]["result_preview"])

    def test_v052c_model_ms_worked_ms_and_ddg_request_counting(self):
        import time
        from types import SimpleNamespace
        from chatint.providers.anthropic_adapter import _stream_anthropic
        from chatint.providers.gemini_adapter import _stream_gemini
        from chatint.providers.openai_adapter import _stream_openai
        from chatint.services.stream_coordinator import StreamManager
        from chatint.web_search_tool import execute_ddg_web_search

        with tempfile.TemporaryDirectory() as ws_tmp:
            ws = Path(ws_tmp)
            (ws / "note.txt").write_text("before", encoding="utf-8")

            # 1. OpenAI multi-round manual approval: model_ms excludes 150ms approval wait & retry error, worked_ms includes all
            conv = create_conversation("Timing Test", workspace_dir=str(ws), agent_mode="manual")
            add_message(conv["id"], "user", "Update note")

            class _SlowChunks:
                def __init__(self, items, delay_s=0.0):
                    self._items = list(items)
                    self._delay_s = delay_s
                def __iter__(self):
                    for idx, item in enumerate(self._items):
                        if idx > 0 and self._delay_s > 0:
                            time.sleep(self._delay_s)
                        yield item

            oai_r1 = _SlowChunks([
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(index=0, id="tc_w1", function=SimpleNamespace(name="write_file", arguments='{"path":"note.txt","content":"after"}'))],
                    reasoning_content=None,
                ))], usage=None),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=None, reasoning_content=None))], usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10, prompt_tokens_details=None)),
            ], delay_s=0.025)
            oai_r2 = _SlowChunks([
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Done.", tool_calls=None, reasoning_content=None))], usage=SimpleNamespace(prompt_tokens=30, completion_tokens=15, prompt_tokens_details=None)),
            ], delay_s=0.025)

            import openai as oai_mod
            req_mock = MagicMock()
            bad_err = oai_mod.APIStatusError("unsupported param", response=MagicMock(status_code=400, headers={}), body=None)
            oai_client = MagicMock()
            oai_client.chat.completions.create.side_effect = [bad_err, oai_r1, oai_r2]

            bridge = DesktopBridge()
            orig_push = bridge.push_event
            def _approve_slow(ev):
                orig_push(ev)
                if ev.get("type") == "tool_approval_required":
                    def _delayed():
                        time.sleep(0.18)
                        StreamManager.resolve_approval(ev["stream_id"], ev["approval_id"], "allow_once")
                    threading.Thread(target=_delayed, daemon=True).start()

            with patch("chatint.client_manager.ClientManager.get_client", return_value=(oai_client, None)), \
                 patch("chatint.client_manager.ClientManager.get_provider", return_value={"id": "openai", "name": "OpenAI", "base_url": ""}), \
                 patch.object(bridge, "push_event", side_effect=_approve_slow):
                stream_chat_completion(
                    bridge=bridge,
                    tab_id="t_time",
                    conversation_id=conv["id"],
                    messages=[{"role": "user", "content": "Update note"}],
                    provider_id="openai",
                    model_id="gpt-5",
                    thinking_level="none",
                    stream_id="sid_timing",
                    agent_enabled=True,
                    workspace_dir=str(ws),
                    agent_mode="manual",
                )

            asst = get_conversation(conv["id"])["messages"][-1]
            self.assertEqual(asst["content"], "Done.")
            self.assertEqual(asst["completion_tokens"], 25)
            self.assertGreater(asst["duration_ms"], 15)
            self.assertLess(asst["duration_ms"], 130)
            self.assertGreaterEqual(asst["worked_ms"], asst["duration_ms"] + 150)

            # 2. Anthropic & Gemini direct timing_stats exclude tool execution time
            def _slow_fetch_tool(*_a, **_kw):
                time.sleep(0.14)
                return {"ok": True, "url": "https://example.com", "title": "Ex", "content_type": "text/plain", "content": "ok", "truncated": False}

            ant_r1 = [
                SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace(input_tokens=10, cache_read_input_tokens=0))),
                SimpleNamespace(type="content_block_start", index=0, content_block=SimpleNamespace(type="tool_use", id="a1", name="fetch_url", input={"url": "https://example.com"})),
                SimpleNamespace(type="content_block_stop", index=0),
                SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=6)),
            ]
            ant_r2 = [
                SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace(input_tokens=12, cache_read_input_tokens=0))),
                SimpleNamespace(type="content_block_start", index=0, content_block=SimpleNamespace(type="text")),
                SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="Fetched.")),
                SimpleNamespace(type="message_delta", usage=SimpleNamespace(output_tokens=4)),
            ]
            ant_client = MagicMock()
            ant_client.messages.create.side_effect = [iter(ant_r1), iter(ant_r2)]
            ant_timing = {"model_ms": 0}
            with patch("chatint.client_manager.ClientManager.get_anthropic_client", return_value=(ant_client, None)), \
                 patch("chatint.tools.web_tools.fetch_url", side_effect=_slow_fetch_tool):
                _stream_anthropic(
                    bridge, "t1", conv["id"], [{"role": "user", "content": "fetch"}],
                    "anthropic", "claude-sonnet-4-5", "none", None, threading.Event(), "fetch",
                    agent_enabled=True, workspace_dir=str(ws), timing_stats=ant_timing,
                )
            self.assertLess(ant_timing["model_ms"], 100)

            gem_part = SimpleNamespace(thought=False, thought_signature=None, text=None, function_call=SimpleNamespace(id="g1", name="fetch_url", args={"url": "https://example.com"}))
            gem_r1 = [SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[gem_part]))], usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5, cached_content_token_count=0))]
            gem_r2 = [SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(thought=False, thought_signature=None, text="Gem ok.", function_call=None)]))], usage_metadata=SimpleNamespace(prompt_token_count=12, candidates_token_count=4, cached_content_token_count=0))]
            gem_client = MagicMock()
            gem_client.models.generate_content_stream.side_effect = [iter(gem_r1), iter(gem_r2)]
            gem_timing = {"model_ms": 0}
            with patch("chatint.client_manager.ClientManager.get_gemini_client", return_value=(gem_client, None)), \
                 patch("chatint.tools.web_tools.fetch_url", side_effect=_slow_fetch_tool):
                _stream_gemini(
                    bridge, "t1", conv["id"], [{"role": "user", "content": "fetch"}],
                    "gemini", "gemini-2.5-flash", "none", None, threading.Event(), "fetch",
                    agent_enabled=True, workspace_dir=str(ws), timing_stats=gem_timing,
                )
            self.assertLess(gem_timing["model_ms"], 100)

            # 3. DDG HTTP request counting tracks multi-engine requests inside DDGS without polluting per_model
            conv_ddg = create_conversation("DDG Count Test")
            from ddgs.ddgs import DDGS as RealDDGS
            from ddgs.results import TextResult

            class _FakeEngine:
                def __init__(self, idx):
                    self.provider = f"p{idx}"
                    self.name = f"eng{idx}"
                    self.priority = 1
                    self.http_client = SimpleNamespace(request=lambda *a, **kw: idx)
                def search(self, query, **_kw):
                    self.http_client.request("GET", f"https://search.example/{self.name}")
                    r = TextResult()
                    r.title = f"Hit {self.name}"
                    r.href = f"https://example.com/{self.name}"
                    r.body = "Body"
                    return [r]

            class _FakeDDGS(RealDDGS):
                def _get_engines(self, category, backend):
                    return [_FakeEngine(1), _FakeEngine(2), _FakeEngine(3)]

            with patch("ddgs.DDGS", _FakeDDGS):
                ddg_res = execute_ddg_web_search("python 3.14", max_results=2, conversation_id=conv_ddg["id"])
            self.assertTrue(ddg_res["ok"])
            self.assertGreaterEqual(ddg_res["ddg_requests_used"], 2)
            record_token_usage(conv_ddg["id"], "websearch", "gemini-3.1-flash-lite", 10, 5, 0, google_search_requests=2)
            ddg_stats = get_token_stats(conv_ddg["id"])
            self.assertEqual(ddg_stats["grand_total"]["ddg_search_requests"], ddg_res["ddg_requests_used"])
            self.assertEqual(ddg_stats["grand_total"]["google_search_requests"], 2)
            self.assertEqual(ddg_stats["grand_total"]["web_search_requests"], 2 + ddg_res["ddg_requests_used"])
            self.assertNotIn("duckduckgo", [m["model_id"] for m in ddg_stats["per_model"]])

    def test_v052c_full_export_import_roundtrip_settings_messages_and_ledger(self):
        provs = get_providers()
        provs.append({
            "id": "provider_custom_exp",
            "name": "AcmeCloud",
            "base_url": "https://api.acme.example/v1",
            "is_enabled": True,
            "models": [{"id": "acme-pro", "name": "acme-pro", "input_price": 2.0, "cached_input_price": 0.5, "output_price": 10.0}],
        })
        save_providers(provs)

        conv = create_conversation(
            "Full Fidelity Chat",
            system_prompt="Be exact.",
            max_context=44,
            workspace_dir="C:/Work/project_alpha",
            agent_mode="auto",
        )
        add_message(conv["id"], "user", "Analyze repo")
        trace = [{"call_id": "c1", "tool": "read_file", "status": "done", "summary": "Read app.py", "args_preview": "app.py", "result_preview": "1: print(1)", "diff": "", "duration_ms": 18}]
        add_message(
            conv["id"],
            "assistant",
            "Done analyzing.",
            thinking_content="Checked files",
            provider_id="provider_custom_exp",
            provider_name="AcmeCloud",
            model_id="acme-pro",
            thinking_level="high",
            prompt_tokens=1000,
            completion_tokens=500,
            cached_tokens=200,
            duration_ms=4000,
            worked_ms=12500,
            tool_trace=trace,
        )
        # Extra regenerated spend + search requests recorded in token_ledger
        record_token_usage(conv["id"], "provider_custom_exp", "acme-pro", 500, 100, 0, provider_name="AcmeCloud")
        record_token_usage(conv["id"], "websearch", "", 0, 0, 0, google_search_requests=2, ddg_search_requests=3)

        orig_stats = get_token_stats(conv["id"])
        self.assertGreater(orig_stats["grand_total"]["total_cost"], 0.0)

        bridge = DesktopBridge()
        md_exp = bridge.export_conversation(conv["id"], "markdown")
        json_exp = bridge.export_conversation(conv["id"], "json")

        # Remove custom provider before importing to prove exact exported cost & provider_name survive without local pricing
        save_providers([p for p in get_providers() if p["id"] != "provider_custom_exp"])

        from chatint.db import get_google_search_request_totals
        g_before = get_google_search_request_totals()["all_time"]

        imp_json_res = bridge.import_conversation_content(json_exp["content"], "full.json")
        imp_md_res = bridge.import_conversation_content(md_exp["content"], "full.md")
        self.assertEqual(get_google_search_request_totals()["all_time"], g_before)

        for imp_conv in (imp_json_res["conversation"], imp_md_res["conversation"]):
            self.assertEqual(imp_conv["system_prompt"], "Be exact.")
            self.assertEqual(imp_conv["max_context_messages"], 44)
            self.assertEqual(imp_conv["workspace_dir"], "C:/Work/project_alpha")
            self.assertEqual(imp_conv["agent_mode"], "auto")
            asst_m = imp_conv["messages"][1]
            self.assertEqual(asst_m["provider_name"], "AcmeCloud")
            self.assertEqual(asst_m["duration_ms"], 4000)
            self.assertEqual(asst_m["worked_ms"], 12500)
            self.assertEqual(asst_m["tool_trace"], trace)
            st = imp_conv["token_stats"]
            self.assertEqual(st["per_model"][0]["provider_name"], "AcmeCloud")
            self.assertAlmostEqual(st["grand_total"]["total_cost"], orig_stats["grand_total"]["total_cost"], places=8)
            self.assertEqual(st["grand_total"]["total_tokens"], orig_stats["grand_total"]["total_tokens"])
            self.assertEqual(st["grand_total"]["google_search_requests"], 2)
            self.assertEqual(st["grand_total"]["ddg_search_requests"], 3)
            self.assertEqual(st["grand_total"]["web_search_requests"], 5)
            self.assertEqual(st["active_turn_tokens"]["total_tokens"], 1500)

    def test_v052c_workspace_and_ssrf_loophole_fixes(self):
        import subprocess
        from types import SimpleNamespace
        from chatint.tools import fetch_url, resolve_workspace_path, tool_list_dir, tool_search_files

        with tempfile.TemporaryDirectory() as tmp:
            root_tmp = Path(tmp)
            outside = root_tmp / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("TOP_SECRET", encoding="utf-8")

            ws = root_tmp / "ws"
            ws.mkdir()
            (ws / ".env").write_text("OPENAI_API_KEY=sk-hidden\n", encoding="utf-8")
            (ws / ".env.prod").write_text("PROD_KEY=hidden-prod\n", encoding="utf-8")
            (ws / "chatint.db").write_text("DB_DATA\n", encoding="utf-8")
            (ws / "data" / "logs").mkdir(parents=True)
            (ws / "data" / "logs" / "chatint.log").write_text("LOG_SECRET\n", encoding="utf-8")
            (ws / "ok.txt").write_text("visible line\n", encoding="utf-8")

            if sys.platform == "win32":
                subprocess.run(["cmd", "/c", "mklink", "/J", str(ws / "junc_out"), str(outside)], check=True, capture_output=True)

            # 1. search_files blocks direct & recursive protected files and junction traversal
            self.assertFalse(tool_search_files(ws, ".*", path=".env", is_regex=True)["ok"])
            self.assertFalse(tool_search_files(ws, ".*", path=".env.prod", is_regex=True)["ok"])
            self.assertFalse(tool_search_files(ws, ".*", path="chatint.db", is_regex=True)["ok"])
            self.assertFalse(tool_search_files(ws, ".*", path="data/logs/chatint.log", is_regex=True)["ok"])
            rec_search = tool_search_files(ws, "hidden", path=".")
            self.assertTrue(rec_search["ok"])
            self.assertEqual(rec_search["count"], 0)
            log_search = tool_search_files(ws, "LOG_SECRET", path=".")
            self.assertEqual(log_search["count"], 0)

            # 2. list_dir hides .env*, chatint.db*, data/logs, and NTFS junctions
            listed = tool_list_dir(ws, ".", max_depth=4)
            self.assertTrue(listed["ok"])
            listed_paths = {e["path"] for e in listed["entries"]}
            self.assertEqual(listed_paths, {"data/", "ok.txt"})
            self.assertFalse(tool_list_dir(ws, "data/logs")["ok"])

            # 3. resolve_workspace_path blocks ADS (:), DOS device names, trailing dots/spaces, drive-relative paths
            for bad_rel in (
                "note.txt:hidden",
                ".env:my_stream",
                "CON",
                "con.txt",
                "sub/NUL.md",
                "chatint.db.",
                ".env ",
                "C:outside.txt",
            ):
                with self.assertRaises(PermissionError, msg=f"Expected block for {bad_rel!r}"):
                    resolve_workspace_path(ws, bad_rel)

            # 4. fetch_url blocks CGNAT and DNS-rebind peer IP
            self.assertFalse(fetch_url("http://100.64.0.1/internal")["ok"])
            self.assertFalse(fetch_url("http://[::ffff:100.64.0.1]/internal")["ok"])

            class _FakeRebindResp:
                status_code = 200
                headers = {"content-type": "text/plain"}
                encoding = "utf-8"
                extensions = {"network_stream": SimpleNamespace(get_extra_info=lambda k: ("127.0.0.1", 80) if k == "peername" else None)}
                def iter_bytes(self):
                    yield b"local secret"
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False

            with patch("chatint.tools.web_tools.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 80))]):
                fake_c = MagicMock()
                fake_c.__enter__.return_value = fake_c
                fake_c.stream.return_value = _FakeRebindResp()
                with patch("chatint.tools.web_tools.httpx.Client", return_value=fake_c):
                    rebind_res = fetch_url("https://rebind.example/test")
                self.assertFalse(rebind_res["ok"])
                self.assertIn("127.0.0.1", rebind_res["error"])

    def test_v053_per_conv_db_migration_quotas_races_and_sandbox(self):
        import concurrent.futures
        import contextlib
        from chatint.db import (
            SCHEMA_VERSION,
            delete_messages_from,
            get_conv_db_path,
            get_conv_db_size_bytes,
            get_conversations_dir,
            get_storage_summary,
            get_total_conv_dbs_size_bytes,
            open_conv_db,
            record_token_usage,
            save_app_settings,
        )
        from chatint.tools import resolve_workspace_path, tool_list_dir

        # 1. Legacy v5 -> v6 migration preserving worked_ms, tool_trace, ddg_search_requests, conflict archive, & partial v6 init
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            v5_db = tmp_dir / "chatint.db"
            stale_conv = tmp_dir / "conversations" / "c_v5_1.db"
            stale_conv.parent.mkdir(parents=True, exist_ok=True)
            stale_conv.write_bytes(b"OLD_CONFLICT_DB_DATA")

            with contextlib.closing(sqlite3.connect(v5_db)) as conn:
                conn.executescript(
                    """
                    PRAGMA user_version = 5;
                    CREATE TABLE providers (id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', is_enabled INTEGER NOT NULL DEFAULT 1, models_json TEXT NOT NULL DEFAULT '[]');
                    CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, system_prompt TEXT NOT NULL DEFAULT '', max_context_messages INTEGER NOT NULL DEFAULT 30, is_imported INTEGER NOT NULL DEFAULT 0, is_auto_named INTEGER NOT NULL DEFAULT 0, workspace_dir TEXT NOT NULL DEFAULT '', agent_mode TEXT NOT NULL DEFAULT 'manual', created_at REAL NOT NULL, updated_at REAL NOT NULL);
                    CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, thinking_content TEXT NOT NULL DEFAULT '', provider_id TEXT NOT NULL DEFAULT '', provider_name TEXT NOT NULL DEFAULT '', model_id TEXT NOT NULL DEFAULT '', thinking_level TEXT NOT NULL DEFAULT 'none', prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0, cached_tokens INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0, worked_ms INTEGER NOT NULL DEFAULT 0, tool_trace_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL);
                    CREATE TABLE token_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, provider_id TEXT NOT NULL DEFAULT '', provider_name TEXT NOT NULL DEFAULT '', model_id TEXT NOT NULL DEFAULT '', prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0, cached_tokens INTEGER NOT NULL DEFAULT 0, google_search_requests INTEGER NOT NULL DEFAULT 0, ddg_search_requests INTEGER NOT NULL DEFAULT 0, cost_usd REAL DEFAULT NULL, created_at REAL NOT NULL);
                    CREATE TABLE google_search_usage (month_key TEXT PRIMARY KEY, request_count INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
                    CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE tab_sessions (id INTEGER PRIMARY KEY CHECK (id = 1), state_json TEXT NOT NULL, updated_at REAL NOT NULL);
                    INSERT INTO conversations VALUES ('c_v5_1', 'Agent Run', 'sys', 40, 0, 1, 'C:/proj', 'auto', 100.0, 200.0);
                    INSERT INTO conversations VALUES ('c_v5_missing', 'No Disk Copy', '', 30, 1, 1, '', 'manual', 100.0, 150.0);
                    INSERT INTO messages VALUES ('m1', 'c_v5_1', 'user', 'Check repo', '', 'openai', 'OpenAI', 'gpt-4o', 'none', 10, 0, 0, 0, 0, '[]', 101.0);
                    INSERT INTO messages VALUES ('m2', 'c_v5_1', 'assistant', 'Done.', 'thought', 'openai', 'OpenAI', 'gpt-4o', 'medium', 200, 50, 25, 450, 1230, '[{"tool":"read_file","status":"done"}]', 102.0);
                    INSERT INTO token_ledger (conversation_id, provider_id, provider_name, model_id, prompt_tokens, completion_tokens, cached_tokens, google_search_requests, ddg_search_requests, cost_usd, created_at)
                    VALUES ('c_v5_1', 'openai', 'OpenAI', 'gpt-4o', 200, 50, 25, 1, 3, 0.42, 102.0);
                    """
                )
                conn.commit()

            orig_db, orig_cfg = db_mod.DB_PATH, cfg_mod.DB_PATH
            try:
                db_mod.DB_PATH = cfg_mod.DB_PATH = v5_db
                init_db()
                self.assertEqual(SCHEMA_VERSION, 6)
                self.assertTrue(v5_db.with_name("chatint.db.bak").is_file())
                self.assertTrue(list((tmp_dir / "conversations").glob("c_v5_1.db.pre_v6_conflict.*")))

                c1 = get_conversation("c_v5_1")
                self.assertIsNotNone(c1)
                self.assertEqual(c1["workspace_dir"], "C:/proj")
                self.assertEqual(c1["agent_mode"], "auto")
                self.assertEqual(c1["messages"][1]["worked_ms"], 1230)
                self.assertEqual(c1["messages"][1]["tool_trace"][0]["tool"], "read_file")
                self.assertEqual(c1["token_ledger"][0]["ddg_search_requests"], 3)
                self.assertEqual(c1["token_stats"]["grand_total"]["total_cost"], 0.42)
                self.assertEqual(c1["token_stats"]["grand_total"]["web_search_requests"], 4)

                # Simulate partial v6 index restore where c_v5_missing.db was omitted on disk
                get_conv_db_path("c_v5_missing").unlink()
                c_miss = get_conversation("c_v5_missing")
                self.assertIsNotNone(c_miss)
                self.assertEqual(c_miss["messages"], [])
                self.assertTrue(get_conv_db_path("c_v5_missing").is_file())
            finally:
                db_mod.DB_PATH = orig_db
                cfg_mod.DB_PATH = orig_cfg

        # 2. Race-safe concurrent writes across chats & anti-resurrection on delete
        ca = create_conversation("Concurrent A")
        cb = create_conversation("Concurrent B")

        def _worker(cid: str, idx: int):
            add_message(cid, "user", f"Q{idx}")
            add_message(cid, "assistant", f"A{idx}", provider_id="openai", model_id="gpt-4o", prompt_tokens=10, completion_tokens=5)
            record_token_usage(cid, "websearch", "", 0, 0, ddg_search_requests=1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(_worker, ca["id"] if i % 2 == 0 else cb["id"], i) for i in range(8)]
            for f in futs:
                f.result()

        self.assertEqual(len(get_conversation(ca["id"])["messages"]), 8)
        self.assertEqual(len(get_conversation(cb["id"])["messages"]), 8)

        self.assertTrue(delete_conversation(ca["id"]))
        self.assertFalse(get_conv_db_path(ca["id"]).exists())
        self.assertIsNone(add_message(ca["id"], "assistant", "Zombie reply"))
        self.assertFalse(record_token_usage(ca["id"], "openai", "gpt-4o", 50, 10))
        self.assertFalse(get_conv_db_path(ca["id"]).exists())

        # 3. Incremental auto_vacuum reclaims bytes on delete_messages_from
        c_vac = create_conversation("Vacuum Chat")
        m_keep = add_message(c_vac["id"], "user", "Keep me")
        add_message(c_vac["id"], "assistant", "Short reply")
        big_payload = "Z" * 350_000
        m_big = add_message(c_vac["id"], "user", big_payload)
        size_before = get_conv_db_size_bytes(c_vac["id"])
        delete_messages_from(c_vac["id"], m_big["id"], include_self=True)
        size_after = get_conv_db_size_bytes(c_vac["id"])
        self.assertLess(size_after, size_before)
        self.assertEqual(get_conversation(c_vac["id"])["messages"][0]["id"], m_keep["id"])

        # 4. Unbounded quota normalization & byte-limit enforcement (blocking new sends, allowing edit/regen)
        save_app_settings({"max_chat_size_mb": "0.001", "max_total_chats_size_mb": "1000000"})
        cfg = get_app_settings()
        self.assertEqual(cfg["max_chat_size_mb"], "0.001")
        self.assertEqual(cfg["max_total_chats_size_mb"], "1000000")
        save_app_settings({"max_total_chats_size_mb": "unlimited"})
        self.assertEqual(get_app_settings()["max_total_chats_size_mb"], "0")
        with self.assertRaises(ValueError):
            save_app_settings({"max_chat_size_mb": "-1"})

        bridge = DesktopBridge()
        # Mock provider ready for edit/regenerate on already-oversized chat
        with patch("chatint.client_manager.ClientManager.validate_provider_ready", return_value=None), patch.object(
            bridge, "_start_stream", return_value="mock-stream"
        ):
            blocked = bridge.send_message("t1", c_vac["id"], "Over limit prompt", "openai", "gpt-4o")
            self.assertIn("error", blocked)
            self.assertTrue(blocked.get("quota_exceeded"))
            # Edit and Regenerate MUST still work when over per-chat limit
            edited = bridge.edit_message("t1", c_vac["id"], m_keep["id"], "Edited OK", "openai", "gpt-4o")
            self.assertTrue(edited.get("success"))
            regen_target = add_message(c_vac["id"], "assistant", "Retryable")
            regen = bridge.regenerate_message("t1", c_vac["id"], regen_target["id"], "openai", "gpt-4o")
            self.assertTrue(regen.get("success"))

        # Total quota blocks new duplicate/import/send and recovers after delete
        save_app_settings({"max_chat_size_mb": "0", "max_total_chats_size_mb": "0.001"})
        summary = get_storage_summary()
        self.assertGreater(summary["total_bytes"], 0)
        dup_blocked = bridge.duplicate_conversation(cb["id"])
        self.assertTrue(dup_blocked.get("quota_exceeded"))
        imp_blocked = bridge.import_conversation_content(json.dumps({"title": "Blocked", "messages": [{"role": "user", "content": "hi"}]}), "b.json")
        self.assertTrue(imp_blocked.get("quota_exceeded"))

        # Raising total limit (or No limit) immediately unblocks duplicate & ledger import roundtrip
        save_app_settings({"max_total_chats_size_mb": "0"})
        imp_ok = bridge.import_conversation_content(
            json.dumps({
                "title": "Restored Ledger V6",
                "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}],
                "token_ledger": [{"provider_id": "openai", "provider_name": "OpenAI", "model_id": "gpt-4o", "prompt_tokens": 12, "completion_tokens": 8, "ddg_search_requests": 2, "cost_usd": 0.03}],
            }),
            "restored.json",
        )
        self.assertTrue(imp_ok.get("success"))
        self.assertEqual(imp_ok["conversation"]["token_stats"]["grand_total"]["ddg_search_requests"], 2)
        self.assertAlmostEqual(imp_ok["conversation"]["token_stats"]["grand_total"]["total_cost"], 0.03)

        # 5. Sandbox blocks data/conversations and live DB paths without blocking user project .db files
        with tempfile.TemporaryDirectory() as ws_tmp:
            ws = Path(ws_tmp)
            (ws / "data" / "conversations").mkdir(parents=True)
            (ws / "data" / "conversations" / "chat1.db").write_text("secret", encoding="utf-8")
            (ws / "user_project").mkdir()
            (ws / "user_project" / "app.db").write_text("user ok", encoding="utf-8")

            with self.assertRaises(PermissionError):
                resolve_workspace_path(ws, "data/conversations/chat1.db")
            self.assertFalse(tool_list_dir(ws, "data/conversations")["ok"])
            ok_user_db = resolve_workspace_path(ws, "user_project/app.db")
            self.assertTrue(ok_user_db.is_file())
            with self.assertRaises(PermissionError):
                resolve_workspace_path(get_conversations_dir().parent, f"conversations/{cb['id']}.db")
            self.assertGreater(get_total_conv_dbs_size_bytes(), 0)


if __name__ == "__main__":
    unittest.main()
