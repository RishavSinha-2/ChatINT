import os
import sys
import tempfile
import sqlite3
import json
import uuid
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
    get_connection,
    create_conversation,
    get_conversation,
    get_conversations,
    update_conversation,
    update_message,
    delete_conversation,
    duplicate_conversation,
    import_conversation_data,
    get_unique_conversation_title,
    get_branch_title,
    record_token_usage,
    get_token_stats,
    add_message,
    get_providers,
    save_providers,
    update_provider_models,
    update_model_resolved_thinking,
    get_model_resolved_thinking,
    remove_model_resolved_thinking,
    get_api_key_from_env,
    set_api_key_in_env,
    get_app_settings,
)
from chatint.bridge import parse_chat_file_content, DesktopBridge
from chatint.client_manager import BackgroundModelSyncer, ClientManager, is_gemini_3_or_newer
from chatint.naming import generate_chat_title
from chatint.web_search_tool import (
    IMAGE_SEARCH_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    MAX_TOOL_ROUNDS,
    get_openai_image_tool_spec,
    get_anthropic_image_tool_spec,
    get_gemini_image_tool_declaration,
    get_web_search_system_hint,
    execute_image_search,
    execute_web_search,
)
from chatint.streamer import (
    _run_web_search_tool_call,
    _flush_think_buffer,
    _stream_anthropic,
    _stream_gemini,
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
        # Reset transient tables and isolate .env between tests
        with open_db() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM messages")
            c.execute("DELETE FROM token_ledger")
            c.execute("DELETE FROM conversations")
            c.execute("DELETE FROM providers")
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
        res_title = update_conversation(conv["id"], system_prompt="You are helpful.")
        updated = get_conversation(conv["id"])
        self.assertEqual(updated["title"], "Alpha")
        self.assertEqual(updated["system_prompt"], "You are helpful.")

    def test_update_conversation_rename_collision(self):
        c1 = create_conversation("Original")
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
        m1 = add_message(c1["id"], "user", "What is quantum computing?", prompt_tokens=25)
        m2 = add_message(
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

        # Web search model 404 must not overwrite gemini global sync_status
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


if __name__ == "__main__":
    unittest.main()
