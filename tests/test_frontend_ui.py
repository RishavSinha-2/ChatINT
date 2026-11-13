"""
Behavioral frontend unit tests (`R8`) executing ChatInt's real ES modules (`src/chatint/ui/js/*.js`)
with real `DOMParser`, `marked.min.js`, `katex.min.js`, and `highlight.min.js` inside headless
Microsoft Edge Chromium (WebView2's engine) over a local Python HTTP server — zero Node/npm required.
"""
from __future__ import annotations

import functools
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

UI_ROOT = Path(__file__).resolve().parent.parent / "src" / "chatint" / "ui"


def _find_edge_binary() -> str | None:
    env_override = os.environ.get("CHATINT_BROWSER_BIN", "").strip()
    if env_override and Path(env_override).is_file():
        return env_override

    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    for name in ("msedge", "msedge.exe", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _build_csp_e2e_html() -> str:
    """Keep index.html's real Content-Security-Policy intact and load external mock + real app.js."""
    raw_index = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    return raw_index.replace(
        '<script type="module" src="app.js"></script>',
        '<script src="__csp_mock_api.js"></script>\n  <script type="module" src="app.js"></script>\n  <script type="module" src="__csp_e2e_check.js"></script>',
    )


CSP_MOCK_API_JS = r"""
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (e) => {
  window.__cspViolations.push(`${e.violatedDirective}:${e.blockedURI}`);
});
// Mirror pywebview's webview/js/api.js `_createApi`, which uses `new Function(...)` to build `window.pywebview.api.*`
const __pywebviewFactory = new Function("fn", "return function() { return fn.apply(this, arguments); };");
window.pywebview = {
  api: {
    get_initial_data: __pywebviewFactory(async () => ({
      ok: true,
      error: null,
      data: {
        providers: [
          {
            id: "openai",
            name: "OpenAI",
            base_url: "https://api.openai.com/v1",
            is_enabled: true,
            has_api_key: true,
            api_key_mask: "sk-t••••1234",
            sync_status: "ready",
            sync_error: "",
            is_accessible: true,
            fetched_model_ids: ["gpt-5"],
            models: [{ id: "gpt-5", name: "gpt-5", input_price: 1.0, cached_input_price: 0.1, output_price: 5.0 }],
          },
        ],
        settings: {
          theme: "dark",
          global_system_prompt: "Helpful",
          default_provider: "openai",
          default_model: "gpt-5",
          default_thinking: "medium",
        },
        conversations: [{ id: "conv_dup_1", title: "Saved Topic", system_prompt: "", max_context_messages: 30, updated_at: 1 }],
        tabs_state: {
          tabs: [
            { id: "t_dup_a", conversationId: "conv_dup_1", title: "Saved Topic", providerId: "openai", modelId: "gpt-5", thinkingLevel: "medium" },
            { id: "t_dup_b", conversationId: "conv_dup_1", title: "Saved Topic", providerId: "openai", modelId: "gpt-5", thinkingLevel: "medium" },
          ],
          activeTabId: "t_dup_a",
          splitTabIds: ["t_dup_a", "t_dup_b"],
        },
        google_search_totals: { month: "2026-11", monthly: 0, all_time: 0 },
        web_search_models: [{ id: "gemini-3.1-flash-lite", name: "gemini-3.1-flash-lite", input_price: 0, cached_input_price: 0, output_price: 0 }],
      },
    })),
    get_conversation: __pywebviewFactory(async (convId) => ({
      ok: true,
      error: null,
      data: {
        id: convId,
        title: "Saved Topic",
        system_prompt: "",
        max_context_messages: 30,
        messages: [{ id: "m1", role: "user", content: "Hello" }, { id: "m2", role: "assistant", content: "![Ext](https://untrusted.example/pic.png)" }],
        token_stats: { models: [], grand_total: { prompt_tokens: 0, completion_tokens: 0, cached_tokens: 0, total_tokens: 0, total_cost: 0 } },
      },
    })),
    get_pending_events: __pywebviewFactory(async () => ({ ok: true, error: null, data: [] })),
    save_tabs_state: __pywebviewFactory(async () => ({ ok: true, error: null, data: { success: true } })),
    open_external_url: __pywebviewFactory(async () => ({ ok: true, error: null, data: { success: true } })),
  },
};
"""

CSP_E2E_CHECK_JS = r"""
function assert(cond, msg) {
  if (!cond) throw new Error(msg || "Assertion failed");
}

async function runE2eChecks() {
  const prevRaw = sessionStorage.getItem("__unit_results__");
  const results = prevRaw ? JSON.parse(prevRaw) : [];

  try {
    // Wait until app.js bootstrapData populates window.ChatInt.state.tabs
    for (let i = 0; i < 40; i++) {
      if (window.ChatInt && window.ChatInt.state.tabs.length > 0 && document.querySelector(".blocked-external-image")) {
        break;
      }
      await new Promise((r) => setTimeout(r, 30));
    }

    assert(window.__cspViolations.length === 0, `Unexpected CSP violations: ${window.__cspViolations.join(", ")}`);

    // 1. NEW-L2: Duplicate conversationId in tabs_state was deduplicated to 1 tab
    const dupTabs = window.ChatInt.state.tabs.filter((t) => t.conversationId === "conv_dup_1");
    assert(dupTabs.length === 1, `Expected 1 deduplicated tab for conv_dup_1, got ${dupTabs.length}`);

    // 2. C3: Clicking .blocked-external-image swaps in <img referrerpolicy="no-referrer" loading="lazy">
    const blockedBtn = document.querySelector(".blocked-external-image");
    assert(Boolean(blockedBtn), "Expected .blocked-external-image in rendered assistant message");
    blockedBtn.click();
    const swappedImg = document.querySelector('.assistant-markdown-content img[src="https://untrusted.example/pic.png"]');
    assert(Boolean(swappedImg), "Clicking blocked external image button must swap in <img>");
    assert(swappedImg.getAttribute("referrerpolicy") === "no-referrer", "Swapped image must set no-referrer");

    // 3. C1: Global dragover calls preventDefault()
    const dragEv = new Event("dragover", { bubbles: true, cancelable: true });
    window.dispatchEvent(dragEv);
    assert(dragEv.defaultPrevented === true, "Global dragover must call e.preventDefault()");

    // 4. NEW-L2: Escape on <select> inside Settings modal must NOT close Settings modal
    document.getElementById("global-settings-btn").click();
    const settingsModal = document.getElementById("settings-modal");
    assert(settingsModal.classList.contains("open"), "Settings modal opened");
    const selectEl = document.getElementById("setting-default-provider");
    selectEl.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    assert(settingsModal.classList.contains("open"), "Escape on <select> must NOT close Settings modal");

    // Pressing Escape on modal itself closes it
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    assert(!settingsModal.classList.contains("open"), "Escape outside <select> closes Settings modal");

    // 5. NEW-L2: Ctrl+T with CapsLock ('T') opens a new tab
    const countBefore = window.ChatInt.state.tabs.length;
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "T", ctrlKey: true, bubbles: true, cancelable: true }));
    assert(window.ChatInt.state.tabs.length === countBefore + 1, "Ctrl+T with uppercase T (CapsLock) must open new tab");

    results.push({ name: "test_e2e_csp_bootstrap_dedup_click_to_load_drag_and_shortcuts", ok: true });
  } catch (err) {
    results.push({
      name: "test_e2e_csp_bootstrap_dedup_click_to_load_drag_and_shortcuts",
      ok: false,
      error: String(err && err.stack ? err.stack : err),
    });
  }

  await fetch("/__results__", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ results }),
  });
}

runE2eChecks();
"""


def _build_runner_html() -> str:
    raw_index = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    # Strip CSP meta tag and production app.js bootstrap so inline test runner module can drive the real DOM
    cleaned = []
    for line in raw_index.splitlines():
        if 'http-equiv="Content-Security-Policy"' in line:
            continue
        if 'src="app.js"' in line:
            continue
        cleaned.append(line)
    base_html = "\n".join(cleaned)
    return base_html.replace("</body>", FRONTEND_RUNNER_SCRIPT + "\n</body>")


FRONTEND_RUNNER_SCRIPT = r"""
  <script>
    window.__reportBootError = (msg) => {
      fetch("/__results__", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ boot_error: String(msg) }),
      }).catch(() => {});
    };
    window.addEventListener("error", (e) => {
      window.__reportBootError(e.error && e.error.stack ? e.error.stack : (e.message || e));
    });
    window.addEventListener("unhandledrejection", (e) => {
      window.__reportBootError(e.reason && e.reason.stack ? e.reason.stack : e.reason);
    });
  </script>

  <script type="module">
    try {
      const { _isSafeUrl, renderMarkdownWithEnhancements } = await import("./js/markdown.js");
      const { createTabObject, el, state, uiHooks } = await import("./js/state.js");
      const { callApi, flushTabsState, isTabsStateDirty, persistTabsState } = await import("./js/api.js");
      const { copyText } = await import("./js/dialogs.js");
      const {
        applyLiveStreamRender,
        createMessageElement,
        populatePaneContent,
        renderActivePanes,
        renderTabs,
      } = await import("./js/panes.js");
      const { renderConversationsList } = await import("./js/sidebar.js");
      const {
        openSettingsModal,
        renderSettingsContent,
        saveActiveProviderInputs,
        saveGeneralTabInputs,
        refreshProviderAccessibility,
      } = await import("./js/settings.js");
      const { onStreamEvent } = await import("./js/streaming.js");

    function assert(cond, msg) {
      if (!cond) throw new Error(msg || "Assertion failed");
    }

    function assertEqual(actual, expected, msg) {
      if (actual !== expected) {
        throw new Error(`${msg || "Mismatch"}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
      }
    }

    const rpcLog = [];
    window.pywebview = {
      api: {
        save_tabs_state: async (payload) => {
          rpcLog.push(["save_tabs_state", payload]);
          return { ok: true, data: { success: true }, error: null };
        },
        get_conversation: async (convId) => {
          rpcLog.push(["get_conversation", convId]);
          return { ok: true, data: null, error: null };
        },
        echo_ok: async (val) => ({ ok: true, data: { echoed: val }, error: null }),
        echo_err: async () => ({ ok: false, data: null, error: "Simulated RPC error" }),
      },
    };

    uiHooks.renderConversationsList = renderConversationsList;
    uiHooks.renderTabs = renderTabs;
    uiHooks.renderActivePanes = renderActivePanes;
    uiHooks.renderSettingsContent = renderSettingsContent;
    uiHooks.saveGeneralTabInputs = saveGeneralTabInputs;
    uiHooks.saveActiveProviderInputs = saveActiveProviderInputs;
    uiHooks.refreshProviderAccessibility = refreshProviderAccessibility;

    state.providers = [
      {
        id: "openai",
        name: "OpenAI",
        base_url: "https://api.openai.com/v1",
        is_enabled: true,
        has_api_key: true,
        api_key_mask: "sk-t••••1234",
        sync_status: "ready",
        sync_error: "",
        is_accessible: true,
        fetched_model_ids: ["gpt-5", "gpt-4o"],
        models: [
          { id: "gpt-5", name: "gpt-5", input_price: 1.25, cached_input_price: 0.125, output_price: 10.0 },
          { id: "gpt-4o", name: "gpt-4o", input_price: 2.5, cached_input_price: 1.25, output_price: 10.0 },
        ],
      },
    ];
    state.settings = {
      theme: "dark",
      global_system_prompt: "Helpful assistant",
      default_provider: "openai",
      default_model: "gpt-5",
      default_thinking: "medium",
    };

    const tests = [
      ["test_c3_is_safe_url_blocks_relative_unc_and_dangerous_schemes", async () => {
        assertEqual(_isSafeUrl("../../../secret.png"), false, "relative traversal must fail");
        assertEqual(_isSafeUrl("other.html"), false, "relative html must fail");
        assertEqual(_isSafeUrl("/etc/passwd"), false, "root-relative path must fail");
        assertEqual(_isSafeUrl("//attacker.host/share/img.png"), false, "protocol-relative / UNC must fail");
        assertEqual(_isSafeUrl("\\\\attacker\\share\\img.png"), false, "backslash UNC must fail");
        assertEqual(_isSafeUrl("javascript:alert(1)"), false, "javascript scheme must fail");
        assertEqual(_isSafeUrl("file:///C:/Windows/win.ini"), false, "file scheme must fail");
        assertEqual(_isSafeUrl("https://example.com/path?q=1"), true, "https must pass");
        assertEqual(_isSafeUrl("mailto:dev@example.com", true), true, "mailto allowed on links");
        assertEqual(_isSafeUrl("mailto:dev@example.com", false), false, "mailto blocked on images");
        assertEqual(_isSafeUrl("#heading-1", true), true, "hash anchor allowed on links");
        assertEqual(_isSafeUrl("#heading-1", false), false, "hash anchor blocked on images");
      }],

      ["test_c3_katex_styles_mathml_preserved_and_ui_redressing_blocked", async () => {
        const host = document.createElement("div");
        host.className = "assistant-markdown-content";
        const md = [
          "Display math: $$\\frac{a}{b} + \\cancel{x} + \\overline{y}$$",
          "",
          '<div class="modal-overlay open btn-primary" style="position: fixed; top: 0; left: 0; z-index: 9999;" onclick="window.__pwned=1">Spoof</div>',
          "<script>window.__pwned = 2;<\/script>",
        ].join("\n");
        host.innerHTML = renderMarkdownWithEnhancements(md, { isStreaming: false });

        assertEqual(window.__pwned, undefined, "inline script/handler must never execute");
        assertEqual(host.querySelectorAll("script").length, 0, "script tags must be stripped");

        const displayBlock = host.querySelector("span.katex-display-block");
        assert(Boolean(displayBlock), "display math must use span.katex-display-block");
        const fracLine = host.querySelector(".katex .frac-line");
        assert(Boolean(fracLine), "KaTeX .frac-line must exist");
        assert(
          (fracLine.getAttribute("style") || "").includes("border-bottom-width"),
          "KaTeX inline border-bottom-width must be preserved so fraction bars are not 3px"
        );
        assert(Boolean(host.querySelector("menclose")), "MathML <menclose> for \\cancel must be preserved");

        const spoof = Array.from(host.querySelectorAll("div")).find((d) => d.textContent.trim() === "Spoof");
        assert(Boolean(spoof), "safe text div remains");
        assert(!spoof.classList.contains("modal-overlay"), "modal-overlay class must be stripped");
        assert(!spoof.classList.contains("open"), "open class must be stripped");
        assert(!spoof.classList.contains("btn-primary"), "btn-primary class must be stripped");
        assert(!(spoof.getAttribute("style") || "").includes("fixed"), "position: fixed must be stripped");
        assertEqual(spoof.getAttribute("onclick"), null, "onclick attribute must be stripped");
      }],

      ["test_c3_external_tracking_pixel_gated_while_wikimedia_autoloads", async () => {
        const host = document.createElement("div");
        host.innerHTML = renderMarkdownWithEnhancements(
          "![Wiki](https://upload.wikimedia.org/wikipedia/commons/a/a9/Example.jpg)\n\n![Track](https://attacker.example/pixel.png)",
          { isStreaming: false }
        );
        const imgs = Array.from(host.querySelectorAll("img"));
        assertEqual(imgs.length, 1, "only trusted Wikimedia image should auto-render as <img>");
        assertEqual(imgs[0].getAttribute("referrerpolicy"), "no-referrer", "referrerpolicy must be no-referrer");
        assertEqual(imgs[0].getAttribute("loading"), "lazy", "loading must be lazy");

        const blocked = host.querySelector(".blocked-external-image");
        assert(Boolean(blocked), "untrusted external image must render click-to-load placeholder");
        assertEqual(blocked.dataset.externalSrc, "https://attacker.example/pixel.png", "external URL stored on dataset");
      }],

      ["test_new_m7_currency_urls_image_alt_task_lists_and_table_scroll", async () => {
        const host = document.createElement("div");
        const sample = [
          "Budget is $100–$200 or $5-$10 or $5/$10 per unit.",
          "",
          "![Plot of $y=x^2$](https://upload.wikimedia.org/plot.png)",
          "",
          "[API Link](https://api.example.com/v1?$filter=a&$select=b)",
          "",
          "- [x] Completed item",
          "- [ ] Pending item",
          "",
          "| Col1 | Col2 |",
          "| --- | --- |",
          "| v1 | v2 |",
        ].join("\n");
        host.innerHTML = renderMarkdownWithEnhancements(sample, { isStreaming: false });

        const text = host.textContent || "";
        assert(text.includes("$100–$200"), "currency range $100–$200 must keep dollar signs");
        assert(text.includes("$5-$10"), "currency range $5-$10 must keep dollar signs");
        assert(text.includes("$5/$10"), "currency ratio $5/$10 must keep dollar signs");
        assert(!host.innerHTML.includes("@@CIT_"), "no placeholder token may leak into HTML");

        const img = host.querySelector("img");
        assertEqual(img.getAttribute("alt"), "Plot of $y=x^2$", "image alt text must not leak placeholder");
        const link = host.querySelector("a[href]");
        assertEqual(
          link.getAttribute("href"),
          "https://api.example.com/v1?$filter=a&$select=b",
          "URL with two $ query params must remain intact"
        );

        const checkboxes = host.querySelectorAll('li input[type="checkbox"][disabled]');
        assertEqual(checkboxes.length, 2, "GFM task-list checkboxes must be preserved & disabled");
        assertEqual(checkboxes[0].checked, true, "first task item checked");
        assertEqual(checkboxes[1].checked, false, "second task item unchecked");

        assert(Boolean(host.querySelector(".markdown-table-scroll > table")), "table must be wrapped in .markdown-table-scroll");
      }],

      ["test_new_h1_welcome_placeholder_clears_on_start_without_wiping_user_bubble", async () => {
        const tab = createTabObject();
        state.tabs = [tab];
        state.activeTabId = tab.id;
        state.splitTabIds = [tab.id];
        renderActivePanes();

        const msgBox = document.getElementById(`messages-${tab.id}`);
        assert(Boolean(msgBox.querySelector(".welcome-placeholder")), "welcome placeholder initially rendered");

        // Simulate sendMessageFromPane appending user row + receiving 'start'
        const pane = el.panesContainer.querySelector(`.chat-pane[data-tab-id="${tab.id}"]`);
        msgBox.appendChild(createMessageElement(tab, { id: "u1", role: "user", content: "First question!" }, pane));
        tab.isStreaming = true;
        tab.activeStreamId = "stream-1";

        onStreamEvent({
          type: "start",
          tab_id: tab.id,
          stream_id: "stream-1",
          provider_id: "openai",
          model_id: "gpt-5",
          thinking_level: "medium",
        });

        assertEqual(msgBox.querySelector(".welcome-placeholder"), null, "welcome placeholder must be removed on start");
        assert(Boolean(msgBox.querySelector(".user-message-row")), "user message bubble must NOT be wiped on start");
        assert(Boolean(msgBox.querySelector(`#live-msg-${tab.id}`)), "live streaming row must be mounted");
      }],

      ["test_new_h2_render_active_panes_preserves_draft_input_and_tool_status", async () => {
        const tab = state.tabs[0];
        tab.isStreaming = true;
        tab.activeStreamId = "stream-2";
        tab.pendingUserDraft = { id: "u_draft", role: "user", content: "Pending question" };
        tab.activeToolStatus = { status: "searching", tool: "web_search", query: "latest astrophysics" };

        const inputBefore = document.getElementById(`input-${tab.id}`);
        inputBefore.value = "My unsent multi-sentence follow-up draft";
        inputBefore.dispatchEvent(new Event("input"));

        renderActivePanes();

        const inputAfter = document.getElementById(`input-${tab.id}`);
        assertEqual(inputAfter.value, "My unsent multi-sentence follow-up draft", "unsent textarea draft must survive re-render");
        const toolBanner = document.getElementById(`live-tool-status-${tab.id}`);
        assert(Boolean(toolBanner && toolBanner.textContent.includes("latest astrophysics")), "active tool status pill must survive re-render");
      }],

      ["test_h5_chat_named_ignores_recently_deleted_conversations", async () => {
        state.conversations = [];
        state.recentlyDeletedConvIds.add("deleted-conv-99");
        onStreamEvent({
          type: "chat_named",
          conversation_id: "deleted-conv-99",
          title: "Resurrected Ghost",
        });
        assertEqual(state.conversations.length, 0, "deleted conversation must never be re-inserted by late chat_named");
      }],

      ["test_new_m2_multi_round_thinking_reexpands_live_accordion", async () => {
        const tab = createTabObject();
        tab.isStreaming = true;
        tab.activeStreamId = "s-multi";
        state.tabs = [tab];
        state.activeTabId = tab.id;
        state.splitTabIds = [tab.id];
        renderActivePanes();

        onStreamEvent({
          type: "start",
          tab_id: tab.id,
          stream_id: "s-multi",
          provider_id: "openai",
          model_id: "gpt-5",
          thinking_level: "high",
        });
        onStreamEvent({ type: "thinking_chunk", tab_id: tab.id, stream_id: "s-multi", chunk: "Round 0 thought." });
        applyLiveStreamRender(tab.id);
        onStreamEvent({ type: "content_chunk", tab_id: tab.id, stream_id: "s-multi", chunk: "Checking search..." });
        applyLiveStreamRender(tab.id);

        const thinkBox = document.getElementById(`live-thinking-${tab.id}`);
        assert(thinkBox.classList.contains("collapsed"), "Round 0 content auto-collapses initial thought");

        // Now Round 1 starts emitting new thinking_chunk after tool call
        onStreamEvent({ type: "thinking_chunk", tab_id: tab.id, stream_id: "s-multi", chunk: "\n\nRound 1 post-tool thought." });
        applyLiveStreamRender(tab.id);

        assert(!thinkBox.classList.contains("collapsed"), "Round 1 thinking_chunk must re-expand live thinking accordion");
        const label = thinkBox.querySelector(".thinking-title-left span");
        assert(label.textContent.includes("Thinking"), "Header must revert from Complete back to live Thinking...");
      }],

      ["test_l3_r7_dirty_tabs_flush_and_uniform_rpc_envelope", async () => {
        // Drain any pending dirty flag first
        flushTabsState();
        const beforeCount = rpcLog.filter((x) => x[0] === "save_tabs_state").length;
        assertEqual(isTabsStateDirty(), false, "clean after flush");
        assertEqual(flushTabsState(), false, "flushTabsState must be a no-op when clean");
        assertEqual(rpcLog.filter((x) => x[0] === "save_tabs_state").length, beforeCount, "zero IPC calls when clean");

        persistTabsState();
        assertEqual(isTabsStateDirty(), true, "marked dirty after persistTabsState");
        assertEqual(flushTabsState(), true, "flushes dirty state");
        assertEqual(rpcLog.filter((x) => x[0] === "save_tabs_state").length, beforeCount + 1, "one IPC call when dirty");

        const okData = await callApi("echo_ok", 42);
        assertEqual(okData.echoed, 42, "callApi unwraps {ok: true, data}");
        const errData = await callApi("echo_err");
        assertEqual(errData.ok, false, "callApi preserves {ok: false}");
        assertEqual(errData.error, "Simulated RPC error", "callApi surfaces error message");
      }],

      ["test_new_l1_copy_button_double_click_preserves_original_html", async () => {
        const btn = document.createElement("button");
        btn.innerHTML = '<span class="orig-copy">Copy</span>';
        copyText("hello", btn);
        copyText("hello again", btn);
        await new Promise((r) => setTimeout(r, 30));
        assertEqual(btn._origHtml, '<span class="orig-copy">Copy</span>', "rapid double-click must never capture Copied! as _origHtml");
      }],

      ["test_new_m4_new_m5_r6_settings_default_model_logs_button_and_dirty_buffer_guard", async () => {
        openSettingsModal();
        assert(Boolean(document.getElementById("setting-default-model")), "Default Model <select> must exist in Settings -> General");
        assert(Boolean(document.getElementById("open-logs-folder-btn")), "Open Logs Folder button must exist in Settings -> General");

        // Mark OpenAI key as edited in settingsBuffer and verify background sync event does NOT clobber it
        const bufOpenai = state.settingsBuffer.providers.find((p) => p.id === "openai");
        bufOpenai._keyEdited = true;
        bufOpenai.sync_status = "missing_key";
        bufOpenai.fetched_model_ids = ["custom-preview-model"];

        onStreamEvent({
          type: "provider_models_updated",
          provider: {
            id: "openai",
            name: "OpenAI",
            base_url: "https://api.openai.com/v1",
            is_enabled: true,
            has_api_key: true,
            api_key_mask: "sk-old",
            sync_status: "ready",
            sync_error: "",
            is_accessible: true,
            fetched_model_ids: ["gpt-4o"],
            models: bufOpenai.models,
          },
        });

        assertEqual(bufOpenai.sync_status, "missing_key", "dirty provider buffer sync_status must not be clobbered");
        assertEqual(bufOpenai.fetched_model_ids[0], "custom-preview-model", "dirty provider fetched_model_ids must not be clobbered");
        el.settingsModal.classList.remove("open");
      }],
    ];

    const results = [];
    for (const [name, fn] of tests) {
      try {
        await fn();
        results.push({ name, ok: true });
      } catch (err) {
        results.push({ name, ok: false, error: String(err && err.stack ? err.stack : err) });
      }
    }

      sessionStorage.setItem("__unit_results__", JSON.stringify(results));
      window.location.href = "/__csp_app_e2e__.html";
    } catch (bootErr) {
      window.__reportBootError(bootErr && bootErr.stack ? bootErr.stack : bootErr);
    }
  </script>
"""


class TestFrontendBehavioralUI(unittest.TestCase):
    def test_r1_modular_codebase_structure(self):
        """Verify R1 modular split of app.js, db.py, and streamer.py into clean focused modules."""
        repo_root = UI_ROOT.parent.parent.parent
        app_js_lines = len((UI_ROOT / "app.js").read_text(encoding="utf-8").splitlines())
        db_py_lines = len((repo_root / "src" / "chatint" / "db.py").read_text(encoding="utf-8").splitlines())
        streamer_py_lines = len((repo_root / "src" / "chatint" / "streamer.py").read_text(encoding="utf-8").splitlines())

        self.assertLess(app_js_lines, 700, f"app.js should be modular entry point, got {app_js_lines} lines")
        self.assertLess(db_py_lines, 200, f"db.py should be clean facade, got {db_py_lines} lines")
        self.assertLess(streamer_py_lines, 200, f"streamer.py should be clean facade, got {streamer_py_lines} lines")

        expected_js_modules = [
            "state.js",
            "api.js",
            "dialogs.js",
            "markdown.js",
            "panes.js",
            "sidebar.js",
            "settings.js",
            "streaming.js",
        ]
        for mod_name in expected_js_modules:
            self.assertTrue((UI_ROOT / "js" / mod_name).is_file(), f"Missing ES module js/{mod_name}")

    def test_r8_browser_es_modules_sanitizer_katex_and_streaming_state(self):
        """Execute behavioral DOM, sanitizer, KaTeX, and streaming tests in headless Edge Chromium."""
        edge_bin = _find_edge_binary()
        if not edge_bin:
            self.skipTest("Headless Chromium/Edge binary not found on host.")

        received_payload: dict = {}
        request_paths: list[str] = []
        done_event = threading.Event()

        class _TestHandler(http.server.SimpleHTTPRequestHandler):
            extensions_map = {
                **http.server.SimpleHTTPRequestHandler.extensions_map,
                ".js": "text/javascript; charset=utf-8",
                ".mjs": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".html": "text/html; charset=utf-8",
                ".woff2": "font/woff2",
            }

            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(UI_ROOT), **kwargs)

            def log_message(self, format, *args):  # noqa: A002
                pass

            def do_GET(self):
                request_paths.append(self.path)
                clean_p = self.path.split("?", 1)[0]
                if clean_p == "/__test_runner__.html":
                    body = _build_runner_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if clean_p == "/__csp_app_e2e__.html":
                    body = _build_csp_e2e_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if clean_p == "/__csp_mock_api.js":
                    body = CSP_MOCK_API_JS.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if clean_p == "/__csp_e2e_check.js":
                    body = CSP_E2E_CHECK_JS.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                super().do_GET()

            def do_POST(self):
                request_paths.append(f"POST {self.path}")
                if self.path == "/__results__":
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length)
                    received_payload.update(json.loads(raw.decode("utf-8")))
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    done_event.set()
                    return
                self.send_response(404)
                self.end_headers()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TestHandler)
        port = server.server_address[1]
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()

        profile_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        proc = None
        stderr_text = ""
        try:
            url = f"http://127.0.0.1:{port}/__test_runner__.html"
            cmd = [
                edge_bin,
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-background-networking",
                "--no-proxy-server",
                f"--user-data-dir={profile_dir.name}",
                url,
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            finished = done_event.wait(timeout=15.0)
            if not finished and proc.poll() is not None:
                _, err_bytes = proc.communicate(timeout=2.0)
                stderr_text = (err_bytes or b"").decode("utf-8", errors="replace")
            self.assertTrue(
                finished,
                f"Timed out waiting for headless Edge test runner. Requests={request_paths}, exit={proc.poll()}, stderr={stderr_text[:500]}",
            )
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3.0)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=2.0)
                    except Exception:
                        pass
                for stream in (proc.stdout, proc.stderr):
                    if stream:
                        try:
                            stream.close()
                        except Exception:
                            pass
            server.shutdown()
            server.server_close()
            profile_dir.cleanup()

        if "boot_error" in received_payload:
            self.fail(f"Frontend test runner failed during module boot:\n{received_payload['boot_error']}")

        results = received_payload.get("results", [])
        self.assertGreaterEqual(len(results), 12, f"Expected >= 12 frontend tests, got {results}")
        failures = [r for r in results if not r.get("ok")]
        if failures:
            formatted = "\n\n".join(f"[{f['name']}]\n{f.get('error')}" for f in failures)
            self.fail(f"{len(failures)} frontend behavioral test(s) failed:\n{formatted}")


if __name__ == "__main__":
    unittest.main()
