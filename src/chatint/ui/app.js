/**
 * ChatInt - Minimalist Multi-Tab Multi-Provider Desktop Client
 */

(function () {
  const THINKING_LEVELS = [
    { value: "none", label: "None" },
    { value: "minimal", label: "Minimal" },
    { value: "low", label: "Low" },
    { value: "medium", label: "Medium" },
    { value: "high", label: "High" },
    { value: "xhigh", label: "XHigh" },
    { value: "max", label: "Max" },
  ];
  const VALID_THINKING_LEVELS = new Set(THINKING_LEVELS.map((t) => t.value));
  const stripModelPrefix = (id) => (id && id.startsWith("models/") ? id.slice(7) : id || "");

  const PRIMARY_WEB_SEARCH_MODEL = "gemini-3.1-flash-lite";
  const normalizeWebSearchModelsClient = (list) => {
    let primary = null;
    const fallbacks = [];
    const seen = new Set();
    for (const raw of Array.isArray(list) ? list : []) {
      if (!raw || !raw.id) continue;
      const mid = stripModelPrefix(String(raw.id).trim());
      if (!mid) continue;
      const entry = {
        id: mid,
        name: stripModelPrefix(String(raw.name || mid).trim()) || mid,
        input_price: Math.max(0, Number(raw.input_price || 0)),
        cached_input_price: Math.max(0, Number(raw.cached_input_price || 0)),
        output_price: Math.max(0, Number(raw.output_price || 0)),
      };
      if (mid.toLowerCase() === PRIMARY_WEB_SEARCH_MODEL) {
        if (!primary) primary = { ...entry, id: PRIMARY_WEB_SEARCH_MODEL, name: PRIMARY_WEB_SEARCH_MODEL };
        continue;
      }
      if (!seen.has(mid.toLowerCase())) {
        seen.add(mid.toLowerCase());
        fallbacks.push(entry);
      }
    }
    if (!primary) {
      primary = {
        id: PRIMARY_WEB_SEARCH_MODEL,
        name: PRIMARY_WEB_SEARCH_MODEL,
        input_price: 0,
        cached_input_price: 0,
        output_price: 0,
      };
    }
    return [primary, ...fallbacks.slice(0, 4)];
  };

  // Global State
  const state = {
    providers: [],
    webSearchModels: normalizeWebSearchModelsClient([]),
    settings: {
      theme: "dark",
      global_system_prompt: "You are a helpful, brilliant, and precise assistant.",
      default_provider: "openai",
      default_model: "gpt-4o",
      default_thinking: "medium",
    },
    conversations: [],
    tabs: [],
    activeTabId: null,
    // Up to 5 side-by-side split tab IDs
    splitTabIds: [],
    masterSettingsTab: "general", // 'general' or 'providers'
    activeProviderId: null,
    activeConvSettingsId: null,
    activeExportConvId: null,
    googleSearchTotals: { month: "", monthly: 0, all_time: 0 },
  };

  // Configure marked for Markdown rendering
  marked.setOptions({
    gfm: true,
    breaks: true,
  });

  // DOM Elements
  const el = {
    appContainer: document.getElementById("app-container"),
    sidebar: document.getElementById("sidebar"),
    sidebarCollapseBtn: document.getElementById("sidebar-collapse-btn"),
    sidebarExpandBtn: document.getElementById("sidebar-expand-btn"),
    newChatBtn: document.getElementById("new-chat-btn"),
    convSearchInput: document.getElementById("conv-search-input"),
    conversationsList: document.getElementById("conversations-list"),
    themeToggleBtn: document.getElementById("theme-toggle-btn"),
    themeIcon: document.getElementById("theme-icon"),
    globalSettingsBtn: document.getElementById("global-settings-btn"),
    tabsScrollArea: document.getElementById("tabs-scroll-area"),
    newTabBtn: document.getElementById("new-tab-btn"),
    splitViewBtn: document.getElementById("split-view-btn"),
    panesContainer: document.getElementById("panes-container"),
    settingsModal: document.getElementById("settings-modal"),
    masterTabGeneral: document.getElementById("master-tab-general"),
    masterTabProviders: document.getElementById("master-tab-providers"),
    masterTabWebSearch: document.getElementById("master-tab-websearch"),
    settingsTabContent: document.getElementById("settings-tab-content"),
    saveSettingsBtn: document.getElementById("save-settings-btn"),
    convSettingsModal: document.getElementById("conv-settings-modal"),
    convTitleInput: document.getElementById("conv-title-input"),
    convSystemPromptInput: document.getElementById("conv-system-prompt-input"),
    convContextSlider: document.getElementById("conv-context-slider"),
    convContextLimitVal: document.getElementById("conv-context-limit-val"),
    saveConvSettingsBtn: document.getElementById("save-conv-settings-btn"),
    exportModal: document.getElementById("export-modal"),
    exportMarkdownBtn: document.getElementById("export-markdown-btn"),
    exportJsonBtn: document.getElementById("export-json-btn"),
    allTabsDropdownWrapper: document.getElementById("all-tabs-dropdown-wrapper"),
    allTabsMenuBtn: document.getElementById("all-tabs-menu-btn"),
    allTabsPopover: document.getElementById("all-tabs-popover"),
    modelConfigModal: document.getElementById("model-config-modal"),
    modelConfigTitle: document.getElementById("model-config-title"),
    modelInputPrice: document.getElementById("model-input-price"),
    modelCachedPrice: document.getElementById("model-cached-price"),
    modelOutputPrice: document.getElementById("model-output-price"),
    modelSupportsThinkingCheck: document.getElementById("model-supports-thinking-check"),
    modelThinkingConfigRow: document.getElementById("model-thinking-config-row"),
    cancelModelConfigBtn: document.getElementById("cancel-model-config-btn"),
    saveModelConfigBtn: document.getElementById("save-model-config-btn"),
    appDialogModal: document.getElementById("app-dialog-modal"),
    appDialogMessage: document.getElementById("app-dialog-message"),
    appDialogInput: document.getElementById("app-dialog-input"),
    appDialogCancelBtn: document.getElementById("app-dialog-cancel-btn"),
    appDialogOkBtn: document.getElementById("app-dialog-ok-btn"),
  };

  // Single-Space Dark/Light Theme Aware Custom Dialogs (replaces browser alert/confirm/prompt)
  let activeDialogResolve = null;

  function closeAppDialog(result) {
    if (!el.appDialogModal) return;
    el.appDialogModal.classList.remove("open");
    if (activeDialogResolve) {
      const resolve = activeDialogResolve;
      activeDialogResolve = null;
      resolve(result);
    }
  }

  function showNoticeDialog(message) {
    return new Promise((resolve) => {
      activeDialogResolve = resolve;
      el.appDialogMessage.textContent = message;
      el.appDialogInput.style.display = "none";
      el.appDialogCancelBtn.style.display = "none";
      el.appDialogOkBtn.textContent = "OK";
      el.appDialogOkBtn.className = "btn-primary";
      el.appDialogModal.classList.add("open");
      setTimeout(() => el.appDialogOkBtn.focus(), 20);

      el.appDialogOkBtn.onclick = () => closeAppDialog(true);
    });
  }

  function showConfirmDialog(message, confirmLabel = "Delete", isDanger = true) {
    return new Promise((resolve) => {
      activeDialogResolve = resolve;
      el.appDialogMessage.textContent = message;
      el.appDialogInput.style.display = "none";
      el.appDialogCancelBtn.style.display = "inline-flex";
      el.appDialogCancelBtn.textContent = "Cancel";
      el.appDialogOkBtn.textContent = confirmLabel;
      el.appDialogOkBtn.className = isDanger ? "btn-danger" : "btn-primary";
      el.appDialogModal.classList.add("open");
      setTimeout(() => el.appDialogOkBtn.focus(), 20);

      el.appDialogCancelBtn.onclick = () => closeAppDialog(false);
      el.appDialogOkBtn.onclick = () => closeAppDialog(true);
    });
  }

  function showPromptDialog(message, defaultValue = "", placeholder = "") {
    return new Promise((resolve) => {
      activeDialogResolve = resolve;
      el.appDialogMessage.textContent = message;
      el.appDialogInput.style.display = "block";
      el.appDialogInput.value = defaultValue || "";
      el.appDialogInput.placeholder = placeholder || "";
      el.appDialogCancelBtn.style.display = "inline-flex";
      el.appDialogCancelBtn.textContent = "Cancel";
      el.appDialogOkBtn.textContent = "OK";
      el.appDialogOkBtn.className = "btn-primary";
      el.appDialogModal.classList.add("open");
      setTimeout(() => {
        el.appDialogInput.focus();
        el.appDialogInput.select();
      }, 20);

      const submitPrompt = () => closeAppDialog(el.appDialogInput.value);
      el.appDialogCancelBtn.onclick = () => closeAppDialog(null);
      el.appDialogOkBtn.onclick = submitPrompt;
      el.appDialogInput.onkeydown = (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          submitPrompt();
        }
      };
    });
  }

  // Format currency cost
  function formatCost(cost) {
    const val = Number(cost) || 0;
    if (val === 0) return "$0.00";
    if (val < 0.0001) return "$" + val.toFixed(6);
    if (val < 0.01) return "$" + val.toFixed(4);
    return "$" + val.toFixed(2);
  }

  // Safe wrapper for backend pywebview API calls
  async function callApi(method, ...args) {
    if (window.pywebview && window.pywebview.api && window.pywebview.api[method]) {
      try {
        return await window.pywebview.api[method](...args);
      } catch (err) {
        console.error(`[API Call Error] ${method}:`, err);
        throw err;
      }
    } else {
      console.warn(`[pywebview.api not ready] Skipped call to ${method}`);
      return null;
    }
  }

  // Format token counts
  function formatTokens(count) {
    if (!count) return "0";
    if (count >= 1000000) return (count / 1000000).toFixed(1) + "M";
    if (count >= 1000) return (count / 1000).toFixed(1) + "k";
    return count.toString();
  }

  // Robust pywebview API readiness check
  function waitForPyWebView(timeoutMs = 6000) {
    return new Promise((resolve) => {
      if (window.pywebview && window.pywebview.api && Object.keys(window.pywebview.api).length > 0) {
        resolve(window.pywebview.api);
        return;
      }

      let resolved = false;
      const onReady = () => {
        if (!resolved && window.pywebview && window.pywebview.api) {
          resolved = true;
          window.removeEventListener("pywebviewready", onReady);
          clearInterval(interval);
          resolve(window.pywebview.api);
        }
      };

      window.addEventListener("pywebviewready", onReady);

      const interval = setInterval(() => {
        if (window.pywebview && window.pywebview.api && Object.keys(window.pywebview.api).length > 0) {
          onReady();
        }
      }, 25);

      setTimeout(() => {
        if (!resolved) {
          resolved = true;
          window.removeEventListener("pywebviewready", onReady);
          clearInterval(interval);
          resolve(window.pywebview ? window.pywebview.api : null);
        }
      }, timeoutMs);
    });
  }

  // Event Poller for real-time events from Python bridge (chunks, naming, model updates)
  let isPollingEvents = false;
  let isPageUnloading = false;
  window.addEventListener("beforeunload", () => {
    isPageUnloading = true;
  });

  function startEventPolling() {
    setInterval(async () => {
      if (isPollingEvents || isPageUnloading) return;
      isPollingEvents = true;
      try {
        const events = await callApi("get_pending_events");
        if (isPageUnloading) return;
        if (Array.isArray(events) && events.length > 0) {
          for (const ev of events) {
            if (window.ChatIntUI && window.ChatIntUI.onStreamEvent) {
              window.ChatIntUI.onStreamEvent(ev);
            }
          }
        }
      } catch (err) {
        // Throttled or window closing
      } finally {
        isPollingEvents = false;
      }
    }, 45);
  }

  // Initialize App
  async function initApp() {
    setupEventListeners();
    await waitForPyWebView();
    await bootstrapData();
    startEventPolling();
  }

  async function bootstrapData() {
    try {
      const data = await callApi("get_initial_data");
      if (data) {
        state.providers = data.providers || [];
        state.settings = { ...state.settings, ...(data.settings || {}) };
        state.conversations = data.conversations || [];
        if (data.google_search_totals) {
          state.googleSearchTotals = data.google_search_totals;
        }
        if (Array.isArray(data.web_search_models)) {
          state.webSearchModels = normalizeWebSearchModelsClient(data.web_search_models);
        }

        applyTheme(state.settings.theme || "dark");

        if (state.providers.length > 0) {
          state.activeProviderId = state.providers[0].id;
        }

        // Restore tabs state
        const savedTabs = data.tabs_state;
        if (savedTabs && Array.isArray(savedTabs.tabs) && savedTabs.tabs.length > 0) {
          state.tabs = savedTabs.tabs.map((t) => createTabObject(t));
          const validTabIds = new Set(state.tabs.map((t) => t.id));
          state.activeTabId = validTabIds.has(savedTabs.activeTabId) ? savedTabs.activeTabId : state.tabs[0].id;
          const seenSplitIds = new Set();
          const validSplitIds = Array.isArray(savedTabs.splitTabIds)
            ? savedTabs.splitTabIds
                .filter((id) => {
                  if (!validTabIds.has(id) || seenSplitIds.has(id)) return false;
                  seenSplitIds.add(id);
                  return true;
                })
                .slice(0, 5)
            : [];
          state.splitTabIds = validSplitIds.length > 0 ? validSplitIds : [state.activeTabId];
        } else {
          // Initialize first tab (draft state, no DB entry yet)
          const initialTab = createTabObject();
          state.tabs = [initialTab];
          state.activeTabId = initialTab.id;
          state.splitTabIds = [initialTab.id];
          persistTabsState();
        }

        renderConversationsList();
        renderTabs();
        renderActivePanes();
        lucide.createIcons();
      }
    } catch (e) {
      console.error("[Bootstrap Error]", e);
    }
  }

  // Theme Management
  function applyTheme(theme) {
    state.settings.theme = theme;
    document.documentElement.setAttribute("data-theme", theme);
    const hljsThemeLink = document.getElementById("hljs-theme");
    if (hljsThemeLink) {
      hljsThemeLink.href = theme === "light" ? "assets/github.min.css" : "assets/github-dark.min.css";
    }
    if (el.themeIcon) {
      el.themeIcon.setAttribute("data-lucide", theme === "light" ? "sun" : "moon");
      lucide.createIcons();
    }
  }

  function toggleTheme() {
    const nextTheme = state.settings.theme === "dark" ? "light" : "dark";
    applyTheme(nextTheme);
    callApi("save_settings", state.providers, state.settings);
  }

  // Tab State Persistence
  function persistTabsState() {
    const serialized = {
      tabs: state.tabs.map((t) => ({
        id: t.id,
        conversationId: t.conversationId,
        title: t.title,
        systemPrompt: t.systemPrompt || "",
        maxContextMessages: t.maxContextMessages || 30,
        providerId: t._usingTempFallback ? (t._preferredProviderId || t.providerId) : t.providerId,
        modelId: t._usingTempFallback ? (t._preferredModelId || t.modelId) : t.modelId,
        thinkingLevel: t._preferredThinkingLevel || t.thinkingLevel,
        userSelectedModel: Boolean(t._userSelectedModel),
        userSelectedThinking: Boolean(t._userSelectedThinking),
        webSearchEnabled: Boolean(t.webSearchEnabled),
      })),
      activeTabId: state.activeTabId,
      splitTabIds: state.splitTabIds,
    };
    callApi("save_tabs_state", serialized);
  }

  // Get accessible/configured providers for dropdown (Strict: ONLY if enabled, has API key in .env, and models successfully fetched)
  function getDropdownProviders() {
    return state.providers.filter((p) => {
      if (!p.is_enabled) return false;
      const hasKey = Boolean(p.has_api_key || (p.api_key && p.api_key.trim()));
      if (!hasKey) return false;
      const fetchedIds = Array.isArray(p.fetched_model_ids) ? new Set(p.fetched_model_ids) : null;
      const validModels = Array.isArray(p.models)
        ? p.models.filter((m) => !fetchedIds || fetchedIds.has(m.id))
        : [];
      return p.is_accessible === true && validModels.length > 0;
    });
  }

  // Update active tab focus indicators without re-rendering panes
  function focusTabInUI(tabId) {
    if (state.activeTabId === tabId) return;
    state.activeTabId = tabId;
    persistTabsState();
    renderTabs();
    renderConversationsList();
    el.panesContainer.querySelectorAll(".chat-pane").forEach((p) => {
      p.classList.toggle("active-pane", p.dataset.tabId === tabId);
    });
  }

  function getDefaultThinkingLevel() {
    const val = state.settings.default_thinking;
    return VALID_THINKING_LEVELS.has(val) ? val : "medium";
  }

  function createTabObject(overrides = {}) {
    const activeProviders = getDropdownProviders();
    const defProvider =
      activeProviders.find((p) => p.id === state.settings.default_provider) ||
      activeProviders[0] ||
      state.providers.find((p) => p.id === state.settings.default_provider) ||
      null;
    const defModelObj =
      defProvider?.models?.find((m) => m.id === state.settings.default_model) ||
      defProvider?.models?.[0] ||
      null;

    const isUnconfiguredDraft = !overrides.conversationId && !overrides.userSelectedModel;
    const prefProvId = isUnconfiguredDraft
      ? (state.settings.default_provider || "")
      : (overrides.providerId || state.settings.default_provider || "");
    const prefModelId = isUnconfiguredDraft
      ? (state.settings.default_model || defModelObj?.id || "")
      : (overrides.modelId || state.settings.default_model || defModelObj?.id || "");
    const prefThinking =
      overrides.userSelectedThinking || (overrides.conversationId && VALID_THINKING_LEVELS.has(overrides.thinkingLevel))
        ? (overrides.thinkingLevel || getDefaultThinkingLevel())
        : getDefaultThinkingLevel();

    return {
      id: overrides.id || "tab_" + Date.now() + "_" + Math.random().toString(36).substr(2, 4),
      conversationId: overrides.conversationId || null,
      title: overrides.title || "New Chat",
      systemPrompt: overrides.systemPrompt || "",
      maxContextMessages: overrides.maxContextMessages || 30,
      providerId: isUnconfiguredDraft ? "" : (overrides.providerId || ""),
      modelId: isUnconfiguredDraft ? "" : (overrides.modelId || ""),
      thinkingLevel: prefThinking,
      _preferredProviderId: prefProvId,
      _preferredModelId: prefModelId,
      _preferredThinkingLevel: prefThinking,
      _userSelectedModel: Boolean(overrides.userSelectedModel),
      _userSelectedThinking: Boolean(overrides.userSelectedThinking),
      _usingTempFallback: false,
      webSearchEnabled: Boolean(overrides.webSearchEnabled),
      isStreaming: false,
      streamingContent: "",
      streamingThinking: "",
      hasAutoCollapsedThinking: false,
      tokenStats: null,
    };
  }

  // Tab Operations
  async function createNewTab(conversationId = null, addToSplit = false) {
    const existing = conversationId ? state.conversations.find((c) => c.id === conversationId) : null;
    const prevActiveTabId = state.activeTabId;
    const newTab = createTabObject({
      conversationId,
      title: existing ? existing.title : "New Chat",
      systemPrompt: existing ? (existing.system_prompt || "") : "",
      maxContextMessages: existing ? (existing.max_context_messages || 30) : 30,
    });

    state.tabs.push(newTab);
    state.activeTabId = newTab.id;

    if (addToSplit) {
      if (!state.splitTabIds.includes(newTab.id) && state.splitTabIds.length < 5) {
        state.splitTabIds.push(newTab.id);
      }
    } else if (state.splitTabIds.length <= 1) {
      state.splitTabIds = [newTab.id];
    } else {
      // Replace currently focused split pane slot with the new tab
      const activeSlotIdx = state.splitTabIds.indexOf(prevActiveTabId);
      if (activeSlotIdx !== -1) {
        state.splitTabIds[activeSlotIdx] = newTab.id;
      } else {
        state.splitTabIds[state.splitTabIds.length - 1] = newTab.id;
      }
    }

    persistTabsState();
    renderTabs();
    renderConversationsList();
    renderActivePanes();
    lucide.createIcons();
  }

  function closeTab(tabId) {
    const index = state.tabs.findIndex((t) => t.id === tabId);
    if (index === -1) return;

    if (state.tabs[index].isStreaming) {
      callApi("stop_streaming", tabId);
    }

    const closedTab = state.tabs[index];
    if (closedTab && closedTab.conversationId && !state.conversations.some((c) => c.id === closedTab.conversationId)) {
      callApi("delete_conversation", closedTab.conversationId);
    }

    state.tabs.splice(index, 1);
    state.splitTabIds = state.splitTabIds.filter((id) => id !== tabId);

    if (state.tabs.length === 0) {
      createNewTab();
      return;
    }

    if (state.activeTabId === tabId) {
      state.activeTabId = state.splitTabIds[0] || state.tabs[Math.max(0, index - 1)].id;
    }

    if (state.splitTabIds.length === 0) {
      state.splitTabIds = [state.activeTabId];
    }

    persistTabsState();
    renderTabs();
    renderConversationsList();
    renderActivePanes();
    lucide.createIcons();
  }

  // Ensure splitTabIds has no duplicates and never exceeds 5 panes
  function sanitizeSplitTabIds() {
    const validTabIds = new Set(state.tabs.map((t) => t.id));
    const seen = new Set();
    const clean = [];
    for (const id of state.splitTabIds) {
      if (validTabIds.has(id) && !seen.has(id)) {
        seen.add(id);
        clean.push(id);
      }
      if (clean.length >= 5) break;
    }
    if (clean.length === 0 && state.tabs.length > 0) {
      const fallback = validTabIds.has(state.activeTabId) ? state.activeTabId : state.tabs[0].id;
      clean.push(fallback);
    }
    state.splitTabIds = clean;
  }

  // Open or switch to a tab inside a specific split pane slot (with automatic exchange if already open in another pane)
  function assignTabToPaneSlot(slotIndex, targetTabId) {
    sanitizeSplitTabIds();
    if (slotIndex < 0 || slotIndex >= state.splitTabIds.length) {
      slotIndex = Math.max(0, state.splitTabIds.length - 1);
    }
    const currentTabIdInSlot = state.splitTabIds[slotIndex];
    const existingPaneIdx = state.splitTabIds.indexOf(targetTabId);

    if (existingPaneIdx !== -1 && existingPaneIdx !== slotIndex) {
      // Exchange the two split panes so no two split panes ever have the same tab open
      state.splitTabIds[existingPaneIdx] = currentTabIdInSlot;
      state.splitTabIds[slotIndex] = targetTabId;
    } else if (existingPaneIdx === -1) {
      state.splitTabIds[slotIndex] = targetTabId;
    }

    state.activeTabId = targetTabId;
    sanitizeSplitTabIds();
    persistTabsState();
    renderTabs();
    renderConversationsList();
    renderActivePanes();
    lucide.createIcons();
  }

  function switchActiveTab(tabId, targetSlotIndex = null) {
    sanitizeSplitTabIds();
    const prevActiveTabId = state.activeTabId;
    const activeSlotIdx =
      targetSlotIndex !== null && targetSlotIndex >= 0 && targetSlotIndex < state.splitTabIds.length
        ? targetSlotIndex
        : state.splitTabIds.indexOf(prevActiveTabId);

    if (targetSlotIndex !== null) {
      assignTabToPaneSlot(activeSlotIdx !== -1 ? activeSlotIdx : 0, tabId);
      return;
    }

    const existingIdx = state.splitTabIds.indexOf(tabId);
    if (existingIdx === -1) {
      if (state.splitTabIds.length <= 1) {
        state.splitTabIds = [tabId];
      } else {
        const slotIdx = activeSlotIdx !== -1 ? activeSlotIdx : state.splitTabIds.length - 1;
        state.splitTabIds[slotIdx] = tabId;
      }
      state.activeTabId = tabId;
      sanitizeSplitTabIds();
      persistTabsState();
      renderTabs();
      renderConversationsList();
      renderActivePanes();
      lucide.createIcons();
    } else if (state.splitTabIds.length > 1 && activeSlotIdx !== -1 && existingIdx !== activeSlotIdx) {
      // If user selects a tab that is already open in another split pane, exchange the tabs!
      assignTabToPaneSlot(activeSlotIdx, tabId);
    } else {
      state.activeTabId = tabId;
      persistTabsState();
      renderTabs();
      renderConversationsList();
      el.panesContainer.querySelectorAll(".chat-pane").forEach((p) => {
        p.classList.toggle("active-pane", p.dataset.tabId === tabId);
      });
    }
  }

  // Split View Management (Up to 5 panes)
  function addSplitPane() {
    if (state.splitTabIds.length >= 5) {
      showNoticeDialog("Maximum 5 side-by-side tabs reached.");
      return;
    }

    // Find a tab not currently in split view
    const candidate = state.tabs.find((t) => !state.splitTabIds.includes(t.id));
    if (candidate) {
      state.splitTabIds.push(candidate.id);
      state.activeTabId = candidate.id;
      persistTabsState();
      renderTabs();
      renderConversationsList();
      renderActivePanes();
    } else {
      // Create a new tab and append it directly as a split pane
      createNewTab(null, true);
    }
  }

  function removeSplitPane(tabId) {
    if (state.splitTabIds.length <= 1) return;
    state.splitTabIds = state.splitTabIds.filter((id) => id !== tabId);
    if (!state.splitTabIds.includes(state.activeTabId)) {
      state.activeTabId = state.splitTabIds[0];
    }
    persistTabsState();
    renderTabs();
    renderActivePanes();
  }

  function movePane(tabId, direction) {
    const idx = state.splitTabIds.indexOf(tabId);
    if (idx === -1) return;
    const targetIdx = idx + direction;
    if (targetIdx < 0 || targetIdx >= state.splitTabIds.length) return;

    // Swap positions
    const temp = state.splitTabIds[idx];
    state.splitTabIds[idx] = state.splitTabIds[targetIdx];
    state.splitTabIds[targetIdx] = temp;

    persistTabsState();
    renderTabs();
    renderActivePanes();
  }

  // Render All-Tabs Scrollable Popover List
  function renderAllTabsPopover() {
    if (!el.allTabsPopover) return;
    el.allTabsPopover.innerHTML = "";

    state.tabs.forEach((tab, idx) => {
      const conv = state.conversations.find((c) => c.id === tab.conversationId);
      const title = conv ? conv.title : (tab.title || "New Chat");
      const isInSplit = state.splitTabIds.includes(tab.id);
      const isActive = tab.id === state.activeTabId;

      const row = document.createElement("div");
      row.className = "all-tabs-popover-item" + (isActive ? " active" : "") + (isInSplit && state.splitTabIds.length > 1 ? " in-split" : "");
      row.innerHTML = `
        <span class="all-tabs-idx">${idx + 1}.</span>
        <span class="all-tabs-title">${escapeHtml(title)}</span>
        <button class="all-tabs-close-btn" title="Close Tab">
          <i data-lucide="x" style="width: 12px; height: 12px;"></i>
        </button>
      `;

      row.onclick = (e) => {
        if (e.target.closest(".all-tabs-close-btn")) {
          e.stopPropagation();
          closeTab(tab.id);
          renderAllTabsPopover();
          return;
        }
        el.allTabsPopover.classList.remove("open");
        switchActiveTab(tab.id);
      };

      el.allTabsPopover.appendChild(row);
    });

    lucide.createIcons();
  }

  // Render Top Tab Bar
  function renderTabs() {
    el.tabsScrollArea.innerHTML = "";
    let activeTabElement = null;

    state.tabs.forEach((tab) => {
      const conv = state.conversations.find((c) => c.id === tab.conversationId);
      const title = conv ? conv.title : (tab.title || "New Chat");

      const tabEl = document.createElement("div");
      const isInSplit = state.splitTabIds.includes(tab.id);
      const isActive = tab.id === state.activeTabId;

      tabEl.className = "tab-item" + (isActive ? " active" : "") + (isInSplit && state.splitTabIds.length > 1 ? " split-active" : "");
      tabEl.title = title;

      tabEl.innerHTML = `
        <span class="tab-title">${escapeHtml(title)}</span>
        <button class="tab-close-btn" title="Close Tab">
          <i data-lucide="x" style="width: 13px; height: 13px;"></i>
        </button>
      `;

      tabEl.addEventListener("click", (e) => {
        if (e.target.closest(".tab-close-btn")) {
          e.stopPropagation();
          closeTab(tab.id);
        } else {
          switchActiveTab(tab.id);
        }
      });

      if (isActive) activeTabElement = tabEl;
      el.tabsScrollArea.appendChild(tabEl);
    });

    if (activeTabElement && typeof activeTabElement.scrollIntoView === "function") {
      activeTabElement.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
    }

    if (el.allTabsPopover && el.allTabsPopover.classList.contains("open")) {
      renderAllTabsPopover();
    }

    if (state.splitTabIds.length > 1) {
      el.splitViewBtn.classList.add("active");
      el.splitViewBtn.title = `Split View (${state.splitTabIds.length}/5 panes active). Click to add pane.`;
    } else {
      el.splitViewBtn.classList.remove("active");
      el.splitViewBtn.title = "Add Split Pane (up to 5 panes side-by-side, Ctrl+\\)";
    }

    lucide.createIcons();
  }

  // Render Active Panes (1 to 5 Equal Sized Columns)
  function renderActivePanes() {
    el.panesContainer.innerHTML = "";
    sanitizeSplitTabIds();

    state.splitTabIds.forEach((tabId, index) => {
      const tab = state.tabs.find((t) => t.id === tabId);
      if (!tab) return;

      const paneEl = createPaneElement(tab, index, state.splitTabIds.length);
      el.panesContainer.appendChild(paneEl);
      populatePaneContent(paneEl, tab);
    });

    lucide.createIcons();
  }

  // Create Individual Pane Shell
  function createPaneElement(tab, index, totalPanes) {
    const pane = document.createElement("section");
    pane.className = "chat-pane" + (tab.id === state.activeTabId ? " active-pane" : "");
    pane.dataset.tabId = tab.id;

    pane.addEventListener("mousedown", () => focusTabInUI(tab.id));
    pane.addEventListener("focusin", () => focusTabInUI(tab.id));

    const isFirst = index === 0;
    const isLast = index === totalPanes - 1;
    const showClose = totalPanes > 1;
    const showTabSwitcher = state.tabs.length > 1 && totalPanes > 1;
    const tabOptionsHtml = state.tabs
      .map((t) => {
        const c = state.conversations.find((item) => item.id === t.conversationId);
        const tTitle = c ? c.title : t.title || "New Chat";
        return `<option value="${escapeHtml(t.id)}" ${t.id === tab.id ? "selected" : ""}>${escapeHtml(tTitle)}</option>`;
      })
      .join("");

    pane.innerHTML = `
      <header class="pane-header">
        <div class="pane-header-left">
          ${
            showTabSwitcher
              ? `<div class="pane-tab-select-wrapper" id="pane-tab-wrapper-${tab.id}" title="Switch Tab in this Split Pane">
                  <i data-lucide="app-window" style="width: 12px; height: 12px; color: var(--text-muted); flex-shrink: 0;"></i>
                  <span class="pane-tab-label" id="pane-tab-label-${tab.id}"></span>
                  <select class="pane-tab-dropdown" id="pane-tab-dropdown-${tab.id}">${tabOptionsHtml}</select>
                </div>`
              : ""
          }
          <div class="model-select-wrapper" id="model-wrapper-${tab.id}" title="Switch Model">
            <i data-lucide="cpu" style="width: 13px; height: 13px; color: var(--accent-color); flex-shrink: 0;"></i>
            <span class="model-select-label" id="model-label-${tab.id}"></span>
            <select class="model-dropdown" id="model-dropdown-${tab.id}"></select>
          </div>

          <div class="thinking-select-wrapper" id="thinking-wrapper-${tab.id}" style="display: none;" title="Thinking Strength">
            <i data-lucide="brain" style="width: 13px; height: 13px; flex-shrink: 0;"></i>
            <span class="thinking-select-label" id="thinking-label-${tab.id}"></span>
            <select class="thinking-dropdown" id="thinking-dropdown-${tab.id}">
              ${THINKING_LEVELS.map((lvl) => `<option value="${lvl.value}">Thinking: ${lvl.label}</option>`).join("")}
            </select>
          </div>
        </div>

        <div class="pane-header-right">
          ${totalPanes > 1 ? `<span class="pane-pos-indicator">${index + 1}/${totalPanes}</span>` : ""}
          ${!isFirst ? `<button class="icon-btn pane-move-left" title="Move Left"><i data-lucide="chevron-left" style="width: 14px; height: 14px;"></i></button>` : ""}
          ${!isLast ? `<button class="icon-btn pane-move-right" title="Move Right"><i data-lucide="chevron-right" style="width: 14px; height: 14px;"></i></button>` : ""}
          <button class="icon-btn pane-conv-settings-btn" title="Settings & Context"><i data-lucide="sliders-horizontal" style="width: 14px; height: 14px;"></i></button>
          <button class="icon-btn pane-export-btn" title="Export Chat"><i data-lucide="download" style="width: 14px; height: 14px;"></i></button>
          ${showClose ? `<button class="icon-btn pane-close-btn" title="Close this Pane"><i data-lucide="x" style="width: 14px; height: 14px;"></i></button>` : ""}
        </div>
      </header>

      <div class="messages-container" id="messages-${tab.id}"></div>

      <div class="input-section">
        <div class="input-box-wrapper">
          <textarea class="chat-input" id="input-${tab.id}" rows="1" placeholder="Ask anything..."></textarea>
          <div class="input-controls">
            <div class="input-model-hint" id="hint-${tab.id}">
              <button type="button" class="web-search-toggle-btn" id="web-search-btn-${tab.id}" aria-pressed="false" title="Toggle Web Search">
                <i data-lucide="globe" style="width: 13px; height: 13px;"></i>
                <span>Web Search</span>
              </button>
            </div>
            <div style="display: flex; gap: 6px;">
              <button class="stop-btn" id="stop-btn-${tab.id}" style="display: none;" title="Stop Generating">
                <i data-lucide="square" style="width: 11px; height: 11px; fill: currentColor;"></i>
              </button>
              <button class="send-btn" id="send-btn-${tab.id}" title="Send Message">
                <i data-lucide="arrow-up" style="width: 15px; height: 15px;"></i>
              </button>
            </div>
          </div>
        </div>
      </div>

      <footer class="token-stats-bar" id="token-bar-${tab.id}">
        <div class="token-stats-summary" id="token-summary-${tab.id}" title="Click to view cache breakdown">
          <div class="token-chips-list" id="token-chips-${tab.id}">
            <span style="color: var(--text-muted);">Tokens: 0 in · 0 out</span>
          </div>
          <div style="display: flex; align-items: center; gap: 5px;">
            <span id="token-grand-${tab.id}" style="font-weight: 500;">0 total</span>
            <i data-lucide="chevron-down" style="width: 12px; height: 12px;"></i>
          </div>
        </div>
        <div class="token-stats-details" id="token-details-${tab.id}"></div>
      </footer>
    `;

    // Pane reordering handlers
    const leftBtn = pane.querySelector(".pane-move-left");
    if (leftBtn) leftBtn.onclick = () => movePane(tab.id, -1);

    const rightBtn = pane.querySelector(".pane-move-right");
    if (rightBtn) rightBtn.onclick = () => movePane(tab.id, 1);

    const closeBtn = pane.querySelector(".pane-close-btn");
    if (closeBtn) closeBtn.onclick = () => removeSplitPane(tab.id);

    const paneTabSelect = pane.querySelector(`#pane-tab-dropdown-${tab.id}`);
    const paneTabLabel = pane.querySelector(`#pane-tab-label-${tab.id}`);
    if (paneTabSelect) {
      if (paneTabLabel) {
        const selOpt = paneTabSelect.options[paneTabSelect.selectedIndex];
        paneTabLabel.textContent = selOpt ? selOpt.textContent : "";
      }
      paneTabSelect.onchange = () => {
        const selectedTabId = paneTabSelect.value;
        if (selectedTabId && selectedTabId !== tab.id) {
          assignTabToPaneSlot(index, selectedTabId);
        }
      };
    }

    // Observe pane width so when the pane or window shrinks (e.g. 5 split tabs or narrow window),
    // the model selector collapses cleanly to just the processor icon without overflowing into adjacent buttons.
    if (typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver((entries) => {
        for (const entry of entries) {
          const w = entry.contentRect.width;
          pane.classList.toggle("compact-pane", w > 0 && w < 390);
          pane.classList.toggle("icon-only-model", w > 0 && w < 310);
        }
      });
      ro.observe(pane);
    }

    return pane;
  }

  // Update Model Dropdown & Thinking Visibility for a Pane
  function updatePaneModelDropdown(pane, tab) {
    const modelWrapper = pane.querySelector(`#model-wrapper-${tab.id}`);
    const modelDropdown = pane.querySelector(`#model-dropdown-${tab.id}`);
    const modelLabel = pane.querySelector(`#model-label-${tab.id}`);
    const thinkingWrapper = pane.querySelector(`#thinking-wrapper-${tab.id}`);
    const thinkingDropdown = pane.querySelector(`#thinking-dropdown-${tab.id}`);
    const thinkingLabel = pane.querySelector(`#thinking-label-${tab.id}`);
    if (!modelDropdown) return;

    const syncLabels = () => {
      if (modelLabel) {
        const selOpt = modelDropdown.options[modelDropdown.selectedIndex];
        const txt = selOpt ? selOpt.textContent : "";
        modelLabel.textContent = txt;
        if (modelWrapper) {
          modelWrapper.title = txt ? `Model: ${txt}` : "Switch Model";
          modelWrapper.classList.toggle("empty-model", !txt);
        }
      }
      if (thinkingLabel && thinkingDropdown) {
        const selThink = thinkingDropdown.options[thinkingDropdown.selectedIndex];
        thinkingLabel.textContent = selThink ? selThink.textContent : "";
      }
    };

    modelDropdown.innerHTML = "";
    const activeProviders = getDropdownProviders();

    if (activeProviders.length === 0) {
      // Wait for background sync without wiping tab's preferred provider/model/thinking
      thinkingWrapper.style.display = "none";
      syncLabels();
      return;
    }

    const getVerifiedModels = (prov) => {
      const fetchedIds = Array.isArray(prov?.fetched_model_ids) ? new Set(prov.fetched_model_ids) : null;
      return (prov?.models || []).filter((m) => !fetchedIds || fetchedIds.has(m.id)).slice(0, 5);
    };

    const desiredProvId = tab.isStreaming
      ? (tab.providerId || state.settings.default_provider)
      : (tab._userSelectedModel || tab.conversationId
          ? (tab._preferredProviderId || tab.providerId || state.settings.default_provider)
          : state.settings.default_provider);
    const desiredModelId = tab.isStreaming
      ? (tab.modelId || state.settings.default_model)
      : (tab._userSelectedModel || tab.conversationId
          ? (tab._preferredModelId || tab.modelId || state.settings.default_model)
          : state.settings.default_model);

    let selectedProv = null;
    let selectedModel = null;
    let usingTempFallback = false;

    const preferredProv = activeProviders.find((p) => p.id === desiredProvId);
    if (preferredProv) {
      const pModels = getVerifiedModels(preferredProv);
      if (pModels.length > 0) {
        selectedProv = preferredProv;
        selectedModel = pModels.find((m) => m.id === desiredModelId) || pModels[0];
      }
    }

    if (!selectedProv || !selectedModel) {
      const fbProv = activeProviders.find((p) => p.id === state.settings.default_provider) || activeProviders[0];
      const fbModels = getVerifiedModels(fbProv);
      if (fbProv && fbModels.length > 0) {
        selectedProv = fbProv;
        selectedModel = fbModels.find((m) => m.id === state.settings.default_model) || fbModels[0];
        usingTempFallback = Boolean(desiredProvId && desiredProvId !== selectedProv.id);
      }
    }

    activeProviders.forEach((prov) => {
      const verifiedModels = getVerifiedModels(prov);
      if (verifiedModels.length === 0) return;

      const optgroup = document.createElement("optgroup");
      optgroup.label = prov.name;
      verifiedModels.forEach((m) => {
        const opt = document.createElement("option");
        opt.value = `${prov.id}::${m.id}`;
        opt.textContent = m.name || m.id;
        if (selectedProv && selectedModel && prov.id === selectedProv.id && m.id === selectedModel.id) {
          opt.selected = true;
        }
        optgroup.appendChild(opt);
      });
      modelDropdown.appendChild(optgroup);
    });

    if (selectedProv && selectedModel) {
      tab.providerId = selectedProv.id;
      tab.modelId = selectedModel.id;
      tab._usingTempFallback = usingTempFallback;
      modelDropdown.value = `${selectedProv.id}::${selectedModel.id}`;
    } else {
      tab.providerId = "";
      tab.modelId = "";
      tab._usingTempFallback = true;
    }

    const findModelObj = (pid, mid) => state.providers.find((p) => p.id === pid)?.models?.find((m) => m.id === mid);
    const applyModelThinking = (modObj) => {
      if (modObj && modObj.supports_thinking) {
        const desiredThink = tab._userSelectedThinking
          ? tab._preferredThinkingLevel
          : (tab.conversationId && VALID_THINKING_LEVELS.has(tab._preferredThinkingLevel)
              ? tab._preferredThinkingLevel
              : getDefaultThinkingLevel());
        thinkingWrapper.style.display = "flex";
        thinkingDropdown.value = VALID_THINKING_LEVELS.has(desiredThink) ? desiredThink : getDefaultThinkingLevel();
        tab.thinkingLevel = thinkingDropdown.value;
      } else {
        // Hide without overwriting _preferredThinkingLevel so preferred level applies when a thinking model loads
        thinkingWrapper.style.display = "none";
        thinkingDropdown.value = "none";
        tab.thinkingLevel = "none";
      }
    };

    applyModelThinking(findModelObj(tab.providerId, tab.modelId));
    syncLabels();

    modelDropdown.onchange = () => {
      const val = modelDropdown.value;
      if (!val) return;
      const [provId, mId] = val.split("::");
      tab.providerId = provId;
      tab.modelId = mId;
      tab._preferredProviderId = provId;
      tab._preferredModelId = mId;
      tab._userSelectedModel = true;
      tab._usingTempFallback = false;

      applyModelThinking(findModelObj(tab.providerId, tab.modelId));
      syncLabels();
      persistTabsState();
    };

    thinkingDropdown.onchange = () => {
      tab.thinkingLevel = thinkingDropdown.value;
      tab._preferredThinkingLevel = thinkingDropdown.value;
      tab._userSelectedThinking = true;
      syncLabels();
      persistTabsState();
    };
  }

  function getWebSearchAvailability() {
    const gemini = state.providers.find((p) => p.id === "gemini");
    if (!gemini || !gemini.has_api_key) {
      return { available: false, reason: "NO API KEY" };
    }
    if (!gemini.is_enabled) {
      return { available: false, reason: "GEMINI DISABLED" };
    }
    const status = gemini.sync_status || "";
    if (status === "checking") {
      return { available: false, reason: "CHECKING..." };
    }
    if (status === "invalid_key") {
      return { available: false, reason: "INVALID KEY" };
    }
    if (status === "network_error") {
      return { available: false, reason: "NETWORK ERROR" };
    }
    if (status === "invalid_model") {
      return { available: false, reason: "INVALID MODEL" };
    }
    if (status === "error") {
      return { available: false, reason: gemini.sync_error || "FETCH ERROR" };
    }
    if ((Array.isArray(gemini.fetched_model_ids) && gemini.fetched_model_ids.length > 0) || status === "ready") {
      const fetchedClean = new Set((gemini.fetched_model_ids || []).map((id) => stripModelPrefix(id).toLowerCase()));
      const wsIds = normalizeWebSearchModelsClient(state.webSearchModels).map((m) => stripModelPrefix(m.id).toLowerCase());
      const hasAnySearchModel = wsIds.some((id) => fetchedClean.has(id));
      if (!hasAnySearchModel) {
        return { available: false, reason: "INVALID MODEL" };
      }
    }
    return { available: true, reason: "Web Search (Google Search via configured Gemini search models)" };
  }

  function updatePaneWebSearchButton(pane, tab) {
    const hintEl = pane.querySelector(`#hint-${tab.id}`);
    const btn = pane.querySelector(`#web-search-btn-${tab.id}`);
    if (!btn) return;
    const { available, reason } = getWebSearchAvailability();
    const active = available && Boolean(tab.webSearchEnabled);
    btn.disabled = !available;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
    btn.title = reason;
    if (hintEl) {
      hintEl.title = reason;
      if (!available && reason) {
        hintEl.setAttribute("data-tooltip", reason);
      } else {
        hintEl.removeAttribute("data-tooltip");
      }
    }
  }

  function syncAllPaneWebSearchButtons() {
    el.panesContainer.querySelectorAll(".chat-pane").forEach((paneEl) => {
      const tabId = paneEl.dataset.tabId;
      const targetTab = state.tabs.find((t) => t.id === tabId);
      if (targetTab) {
        updatePaneWebSearchButton(paneEl, targetTab);
      }
    });
  }

  function syncAllPaneModelDropdowns() {
    el.panesContainer.querySelectorAll(".chat-pane").forEach((paneEl) => {
      const tabId = paneEl.dataset.tabId;
      const targetTab = state.tabs.find((t) => t.id === tabId);
      if (targetTab) {
        updatePaneModelDropdown(paneEl, targetTab);
      }
    });
    syncAllPaneWebSearchButtons();
  }

  function getModelRecencyScore(m) {
    const mid = (m && m.id ? m.id : String(m || "")).toLowerCase();
    const created = typeof m.created === "number" ? m.created : 0;

    let dateVal = 0;
    const mDate = mid.match(/(202\d)[-_]?([01]\d)[-_]?([0-3]\d)/);
    if (mDate) {
      dateVal = parseInt(mDate[1] + mDate[2] + mDate[3], 10) || 0;
    }

    let verVal = 0;
    const mDec = mid.match(/[-_v]?(\d+\.\d+)/);
    if (mDec) {
      verVal = parseFloat(mDec[1]) || 0;
    } else {
      const mDash = mid.match(/[-_](\d+)[-_](\d+)(?:[-_]|$)/);
      if (mDash && parseInt(mDash[1], 10) <= 10) {
        verVal = parseFloat(`${mDash[1]}.${mDash[2]}`) || 0;
      } else {
        const mSingle = mid.match(/(?:gpt|gemini|claude|llama|deepseek|qwen|v)[-_]?(\d+)/);
        if (mSingle) {
          verVal = parseFloat(mSingle[1]) || 0;
        }
      }
    }

    let bonus = 0;
    if (/o3|o1|r1|reasoner/i.test(mid)) bonus += 50;
    if (/4o|4\.5|argon|carbon/i.test(mid)) bonus += 40;
    if (/latest|preview/i.test(mid)) bonus += 20;
    if (/deprecated|legacy|0301|0314|0613/i.test(mid)) bonus -= 100;
    if (/embedding|moderation|tts|whisper|audio|realtime/i.test(mid)) bonus -= 500;

    return { created, dateVal, verVal, bonus };
  }

  function sortModelsNewerFirst(models) {
    return [...models].sort((a, b) => {
      const scoreA = getModelRecencyScore(a);
      const scoreB = getModelRecencyScore(b);
      if (scoreB.created !== scoreA.created) return scoreB.created - scoreA.created;
      if (scoreB.dateVal !== scoreA.dateVal) return scoreB.dateVal - scoreA.dateVal;
      if (scoreB.verVal !== scoreA.verVal) return scoreB.verVal - scoreA.verVal;
      if (scoreB.bonus !== scoreA.bonus) return scoreB.bonus - scoreA.bonus;
      return (a.id || "").localeCompare(b.id || "");
    });
  }

  // Build Live Streaming Row Element for Active Streams
  function createLiveStreamingRow(tab) {
    const liveRow = document.createElement("div");
    liveRow.className = "message-row assistant-message-row live-streaming-row";
    liveRow.id = `live-msg-${tab.id}`;

    const provTag = (tab.streamingProviderId || tab.providerId || "API").toUpperCase();
    const modelTag = tab.streamingModelId || tab.modelId || "Model";
    const thinkLvl = tab.streamingThinkingLevel || tab.thinkingLevel;
    const thinkBadge = thinkLvl && thinkLvl !== "none" ? ` · ${thinkLvl.toUpperCase()} THINK` : "";

    const hasThinking = Boolean(tab.streamingThinking);
    const isCollapsed = Boolean(tab.hasAutoCollapsedThinking);
    const thinkTitle = isCollapsed ? "Thought Process (Complete)" : "Thinking...";

    liveRow.innerHTML = `
      <div class="assistant-header-badge">
        <span class="provider-tag">${escapeHtml(provTag)}</span>
        <span>${escapeHtml(modelTag)}${escapeHtml(thinkBadge)}</span>
      </div>
      <div class="thinking-accordion${isCollapsed ? " collapsed" : ""}" id="live-thinking-${tab.id}" style="display: ${hasThinking ? "block" : "none"};">
        <div class="thinking-accordion-header">
          <div class="thinking-title-left">
            ${isCollapsed ? "" : '<div class="thinking-pulse"></div>'}
            <span style="font-weight: 500;">${thinkTitle}</span>
          </div>
          <i data-lucide="chevron-down" class="thinking-toggle-icon" style="width: 13px; height: 13px;"></i>
        </div>
        <div class="thinking-body" id="live-thinking-body-${tab.id}"></div>
      </div>
      <div class="tool-status-banner" id="live-tool-status-${tab.id}" style="display: none;"></div>
      <div class="assistant-markdown-content" id="live-content-${tab.id}"></div>
    `;

    if (hasThinking) {
      const thinkBody = liveRow.querySelector(`#live-thinking-body-${tab.id}`);
      if (thinkBody) thinkBody.textContent = tab.streamingThinking;
    }
    if (tab.streamingContent) {
      const contentDiv = liveRow.querySelector(`#live-content-${tab.id}`);
      if (contentDiv) contentDiv.innerHTML = renderMarkdownWithEnhancements(tab.streamingContent);
    }

    return liveRow;
  }

  // Populate Pane Content & Attach Handlers
  async function populatePaneContent(pane, tab) {
    const messagesContainer = pane.querySelector(`#messages-${tab.id}`);
    const chatInput = pane.querySelector(`#input-${tab.id}`);
    const sendBtn = pane.querySelector(`#send-btn-${tab.id}`);
    const stopBtn = pane.querySelector(`#stop-btn-${tab.id}`);
    const convSettingsBtn = pane.querySelector(".pane-conv-settings-btn");
    const exportBtn = pane.querySelector(".pane-export-btn");

    // 1. Populate Model Dropdown, Thinking selector, and Web Search button
    updatePaneModelDropdown(pane, tab);
    updatePaneWebSearchButton(pane, tab);

    const webSearchBtn = pane.querySelector(`#web-search-btn-${tab.id}`);
    if (webSearchBtn) {
      webSearchBtn.onclick = () => {
        if (!getWebSearchAvailability().available) return;
        tab.webSearchEnabled = !tab.webSearchEnabled;
        updatePaneWebSearchButton(pane, tab);
        persistTabsState();
      };
    }

    // Settings & Export Actions
    convSettingsBtn.onclick = () => openConvSettingsModal(tab);

    exportBtn.onclick = () => {
      if (tab.conversationId) openExportModal(tab.conversationId);
      else showNoticeDialog("Send a message first to export this conversation.");
    };

    const renderWelcomePlaceholder = () => {
      messagesContainer.innerHTML = `
        <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; color: var(--text-muted); gap: 12px; margin-top: 50px;">
          <div style="width: 44px; height: 44px; border-radius: 50%; background: var(--bg-tertiary); display: flex; align-items: center; justify-content: center;">
            <i data-lucide="sparkles" style="width: 22px; height: 22px; color: var(--accent-color);"></i>
          </div>
          <div style="font-size: 15px; font-weight: 600; color: var(--text-primary);">Start a new conversation</div>
          <div style="font-size: 12.5px; max-width: 320px; text-align: center;">Ask any question. The conversation will be automatically named and saved once generation begins.</div>
        </div>
      `;
      renderTokenStats(pane, tab.id, null);
    };

    // 2. Load Messages (or Draft Empty View)
    if (!tab.conversationId) {
      renderWelcomePlaceholder();
      if (tab.isStreaming) {
        messagesContainer.innerHTML = "";
        messagesContainer.appendChild(createLiveStreamingRow(tab));
        lucide.createIcons();
      }
    } else {
      messagesContainer.innerHTML = '<div style="color: var(--text-muted); font-size: 13px; text-align: center; margin-top: 30px;">Loading chat...</div>';
      const conv = await callApi("get_conversation", tab.conversationId);
      if (!conv) {
        tab.conversationId = null;
        persistTabsState();
        renderWelcomePlaceholder();
      } else {
        messagesContainer.innerHTML = "";
        tab.systemPrompt = conv.system_prompt || tab.systemPrompt || "";
        tab.maxContextMessages = conv.max_context_messages || tab.maxContextMessages || 30;

        if (conv.messages && conv.messages.length > 0) {
          tab.tokenStats = conv.token_stats;
          renderTokenStats(pane, tab.id, conv.token_stats);

          conv.messages.forEach((msg) => {
            const msgEl = createMessageElement(tab, msg, pane);
            messagesContainer.appendChild(msgEl);
          });
        } else if (!tab.isStreaming) {
          renderWelcomePlaceholder();
        } else {
          renderTokenStats(pane, tab.id, null);
        }

        if (tab.isStreaming) {
          const existingLive = messagesContainer.querySelector(`#live-msg-${tab.id}`);
          if (!existingLive) {
            messagesContainer.appendChild(createLiveStreamingRow(tab));
          }
        }
        lucide.createIcons();
        messagesContainer.scrollTop = messagesContainer.scrollHeight;
      }
    }

    // 3. Input handlers
    chatInput.oninput = () => {
      chatInput.style.height = "auto";
      chatInput.style.height = Math.min(chatInput.scrollHeight, 150) + "px";
    };

    chatInput.onkeydown = (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessageFromPane(pane, tab);
      }
    };

    sendBtn.onclick = () => sendMessageFromPane(pane, tab);
    stopBtn.onclick = () => callApi("stop_streaming", tab.id);

    if (tab.isStreaming) {
      sendBtn.style.display = "none";
      stopBtn.style.display = "flex";
    } else {
      sendBtn.style.display = "flex";
      stopBtn.style.display = "none";
    }
  }

  // Create Message Element
  function createMessageElement(tab, msg, pane) {
    const row = document.createElement("div");
    row.className = `message-row ${msg.role === "user" ? "user-message-row" : "assistant-message-row"}`;
    row.dataset.msgId = msg.id;

    if (msg.role === "user") {
      row.innerHTML = `
        <div class="user-bubble">${escapeHtml(msg.content)}</div>
        <div class="user-actions">
          <button class="user-action-btn edit-msg-btn" title="Edit message">
            <i data-lucide="pencil" style="width: 11px; height: 11px;"></i>
            <span>Edit</span>
          </button>
          <button class="user-action-btn copy-msg-btn" title="Copy text">
            <i data-lucide="copy" style="width: 11px; height: 11px;"></i>
            <span>Copy</span>
          </button>
        </div>
      `;

      row.querySelector(".copy-msg-btn").onclick = () => copyText(msg.content, row.querySelector(".copy-msg-btn"));
      row.querySelector(".edit-msg-btn").onclick = () => inlineEditUserMessage(row, tab, msg, pane);
    } else {
      const provTag = msg.provider_id ? msg.provider_id.toUpperCase() : "API";
      const modelTag = msg.model_id || "Assistant";
      const thinkBadge = msg.thinking_level && msg.thinking_level !== "none" ? ` · ${msg.thinking_level.toUpperCase()} THINK` : "";

      let thinkingHtml = "";
      if (msg.thinking_content) {
        thinkingHtml = `
          <div class="thinking-accordion collapsed">
            <div class="thinking-accordion-header">
              <div class="thinking-title-left">
                <i data-lucide="brain" style="width: 12px; height: 12px; color: var(--accent-color);"></i>
                <span style="font-weight: 500;">Thinking Process</span>
              </div>
              <i data-lucide="chevron-down" class="thinking-toggle-icon" style="width: 13px; height: 13px;"></i>
            </div>
            <div class="thinking-body">${escapeHtml(msg.thinking_content)}</div>
          </div>
        `;
      }

      let tokensTag = "";
      if (msg.prompt_tokens || msg.completion_tokens) {
        const cachedStr = msg.cached_tokens ? ` (${formatTokens(msg.cached_tokens)} cached)` : "";
        tokensTag = `<span style="font-size: 11px; color: var(--text-muted);">${formatTokens(msg.prompt_tokens)} in / ${formatTokens(msg.completion_tokens)} out${cachedStr}</span>`;
      }

      row.innerHTML = `
        <div class="assistant-header-badge">
          <span class="provider-tag">${escapeHtml(provTag)}</span>
          <span>${escapeHtml(modelTag)}${escapeHtml(thinkBadge)}</span>
          ${tokensTag}
        </div>
        ${thinkingHtml}
        <div class="assistant-markdown-content">${renderMarkdownWithEnhancements(msg.content)}</div>
        <div class="assistant-actions">
          <button class="assistant-action-btn copy-msg-btn" title="Copy response">
            <i data-lucide="copy" style="width: 11px; height: 11px;"></i>
            <span>Copy</span>
          </button>
          <button class="assistant-action-btn regen-msg-btn" title="Regenerate from here">
            <i data-lucide="rotate-cw" style="width: 11px; height: 11px;"></i>
            <span>Regenerate</span>
          </button>
        </div>
      `;

      row.querySelector(".copy-msg-btn").onclick = () => copyText(msg.content, row.querySelector(".copy-msg-btn"));
      row.querySelector(".regen-msg-btn").onclick = async () => {
        if (tab.isStreaming) return;
        if (!tab.modelId || !tab.providerId) {
          showNoticeDialog("No model is available. Please configure an API key in Settings (Ctrl+,) first.");
          return;
        }
        // Remove all subsequent messages and this assistant message from DOM
        while (row.nextElementSibling) {
          row.nextElementSibling.remove();
        }
        row.remove();

        tab.isStreaming = true;
        const sendBtn = pane.querySelector(`#send-btn-${tab.id}`);
        const stopBtn = pane.querySelector(`#stop-btn-${tab.id}`);
        if (sendBtn) sendBtn.style.display = "none";
        if (stopBtn) stopBtn.style.display = "flex";

        const realMsgId = row.dataset.msgId || msg.id;
        const useWebSearch = Boolean(tab.webSearchEnabled && getWebSearchAvailability().available);
        const res = await callApi(
          "regenerate_message",
          tab.id,
          tab.conversationId,
          realMsgId,
          tab.providerId,
          tab.modelId,
          tab.thinkingLevel,
          useWebSearch
        );
        if (res && res.token_stats) {
          tab.tokenStats = res.token_stats;
          renderTokenStats(pane, tab.id, res.token_stats);
        }
      };
    }

    return row;
  }

  // Render Markdown with Code Highlighting, Copy Buttons & Protected KaTeX Math
  function renderMarkdownWithEnhancements(markdownText) {
    if (!markdownText || typeof markdownText !== "string") return "";

    // 1. Protect fenced code blocks and inline code spans before math extraction
    const codeTokens = [];
    let protectedText = markdownText.replace(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`)/g, (match) => {
      const idx = codeTokens.length;
      codeTokens.push(match);
      return `@@CHATINTCODE${idx}@@`;
    });

    // 2. Extract and render KaTeX math into safe placeholders so marked.parse doesn't mangle MathML/SVG
    const mathTokens = [];
    if (window.katex) {
      const pushMathToken = (expr, displayMode, fallback) => {
        try {
          const rendered = window.katex.renderToString(expr.trim(), {
            displayMode: displayMode,
            throwOnError: false,
          });
          const htmlOut = displayMode ? `<div class="katex-display-block">${rendered}</div>` : rendered;
          const idx = mathTokens.length;
          mathTokens.push(htmlOut);
          return `@@CHATINTMATH${idx}@@`;
        } catch (e) {
          return fallback;
        }
      };

      // Block math: $$ ... $$ or \[ ... \]
      protectedText = protectedText.replace(/\$\$([\s\S]+?)\$\$/g, (m, expr) => pushMathToken(expr, true, m));
      protectedText = protectedText.replace(/\\\[([\s\S]+?)\\\]/g, (m, expr) => pushMathToken(expr, true, m));

      // Inline math: \( ... \) or $ ... $
      protectedText = protectedText.replace(/\\\(([\s\S]+?)\\\)/g, (m, expr) => pushMathToken(expr, false, m));
      protectedText = protectedText.replace(/(^|[^\$\\])\$([^\$\n\r]+?)\$(?!\$)/g, (m, prefix, expr) => {
        if (expr.trim() !== expr || /^\d+(\.\d+)?$/.test(expr)) {
          return m;
        }
        return prefix + pushMathToken(expr, false, `$${expr}$`);
      });
    }

    // 3. Restore code blocks before running marked.parse
    protectedText = protectedText.replace(/@@CHATINTCODE(\d+)@@/g, (_, idx) => codeTokens[Number(idx)]);

    let html = "";
    try {
      html = marked.parse(protectedText);
    } catch (e) {
      return `<pre>${escapeHtml(markdownText)}</pre>`;
    }

    // 4. Restore rendered KaTeX math tokens
    if (mathTokens.length > 0) {
      html = html.replace(/@@CHATINTMATH(\d+)@@/g, (_, idx) => mathTokens[Number(idx)]);
    }

    const parser = new DOMParser();
    const doc = parser.parseFromString(`<div>${html}</div>`, "text/html");

    doc.querySelectorAll("pre").forEach((pre) => {
      const code = pre.querySelector("code");
      let lang = "code";
      if (code && code.className) {
        const m = code.className.match(/language-([^\s]+)/);
        if (m) lang = m[1];
      }

      if (window.hljs && code) {
        const rawCode = code.textContent || "";
        try {
          if (lang && lang !== "code" && hljs.getLanguage(lang)) {
            code.innerHTML = hljs.highlight(rawCode, { language: lang, ignoreIllegals: true }).value;
          } else {
            code.innerHTML = hljs.highlightAuto(rawCode).value;
          }
          code.classList.add("hljs");
        } catch (e) {}
      }

      const wrapper = document.createElement("div");
      wrapper.className = "code-block-wrapper";
      wrapper.innerHTML = `
        <div class="code-block-header">
          <span>${escapeHtml(lang)}</span>
          <button class="copy-code-btn" title="Copy code">
            <i data-lucide="copy" style="width: 11px; height: 11px;"></i>
            <span>Copy</span>
          </button>
        </div>
      `;

      pre.parentNode.insertBefore(wrapper, pre);
      wrapper.appendChild(pre);
    });

    return doc.body.firstElementChild ? doc.body.firstElementChild.innerHTML : html;
  }

  // Inline User Message Editing
  function inlineEditUserMessage(row, tab, msg, pane) {
    if (tab.isStreaming) return;
    const bubble = row.querySelector(".user-bubble");
    if (!bubble || bubble.querySelector("textarea")) return;
    const originalText = msg.content;

    bubble.innerHTML = `
      <textarea class="settings-input" style="width: 100%; min-height: 60px; resize: vertical;">${escapeHtml(originalText)}</textarea>
      <div style="display: flex; justify-content: flex-end; gap: 6px; margin-top: 6px;">
        <button class="btn-secondary cancel-edit-btn" style="padding: 3px 8px; font-size: 11.5px;">Cancel</button>
        <button class="btn-primary save-edit-btn" style="padding: 3px 10px; font-size: 11.5px;">Save & Resend</button>
      </div>
    `;

    const txt = bubble.querySelector("textarea");
    txt.focus();
    txt.setSelectionRange(txt.value.length, txt.value.length);

    bubble.querySelector(".cancel-edit-btn").onclick = (e) => {
      e.stopPropagation();
      bubble.textContent = originalText;
    };

    bubble.querySelector(".save-edit-btn").onclick = async (e) => {
      e.stopPropagation();
      if (tab.isStreaming) return;
      const newText = txt.value.trim();
      if (!newText) return;
      if (!tab.modelId || !tab.providerId) {
        showNoticeDialog("No model is available. Please configure an API key in Settings (Ctrl+,) first.");
        return;
      }

      msg.content = newText;
      bubble.textContent = newText;

      // Remove all subsequent messages in DOM
      while (row.nextElementSibling) {
        row.nextElementSibling.remove();
      }

      tab.isStreaming = true;
      const sendBtn = pane.querySelector(`#send-btn-${tab.id}`);
      const stopBtn = pane.querySelector(`#stop-btn-${tab.id}`);
      if (sendBtn) sendBtn.style.display = "none";
      if (stopBtn) stopBtn.style.display = "flex";

      const realMsgId = row.dataset.msgId || msg.id;
      msg.id = realMsgId;
      row.dataset.msgId = realMsgId;

      const useWebSearch = Boolean(tab.webSearchEnabled && getWebSearchAvailability().available);
      const res = await callApi(
        "edit_message",
        tab.id,
        tab.conversationId,
        realMsgId,
        newText,
        tab.providerId,
        tab.modelId,
        tab.thinkingLevel,
        useWebSearch
      );
      if (res && res.token_stats) {
        tab.tokenStats = res.token_stats;
        renderTokenStats(pane, tab.id, res.token_stats);
      }
    };
  }

  // Send Message from Pane
  async function sendMessageFromPane(pane, tab) {
    if (tab.isStreaming) return;

    if (!tab.modelId || !tab.providerId) {
      showNoticeDialog("No model is available. Please configure an API key in Settings (Ctrl+,) to fetch models before sending.");
      return;
    }

    const chatInput = pane.querySelector(`#input-${tab.id}`);
    const text = chatInput.value.trim();
    if (!text) return;

    chatInput.value = "";
    chatInput.style.height = "auto";

    const messagesContainer = pane.querySelector(`#messages-${tab.id}`);

    // If welcome placeholder is present, clear it immediately
    const welcome = messagesContainer.querySelector('div[style*="Start a new conversation"]');
    if (welcome) messagesContainer.innerHTML = "";

    // IMMEDIATELY render the user's prompt on the chat screen!
    const tempUserMsg = {
      id: "user_temp_" + Date.now(),
      role: "user",
      content: text,
      created_at: Date.now() / 1000,
    };
    const userRow = createMessageElement(tab, tempUserMsg, pane);
    messagesContainer.appendChild(userRow);
    lucide.createIcons();
    messagesContainer.scrollTop = messagesContainer.scrollHeight;

    // Toggle button state to streaming & lock selected model/thinking
    tab.isStreaming = true;
    tab._userSelectedModel = true;
    tab._userSelectedThinking = true;
    tab._preferredProviderId = tab.providerId;
    tab._preferredModelId = tab.modelId;
    tab._preferredThinkingLevel = tab.thinkingLevel;
    tab._usingTempFallback = false;
    const sendBtn = pane.querySelector(`#send-btn-${tab.id}`);
    const stopBtn = pane.querySelector(`#stop-btn-${tab.id}`);
    if (sendBtn) sendBtn.style.display = "none";
    if (stopBtn) stopBtn.style.display = "flex";

    // Call backend send_message
    const useWebSearch = Boolean(tab.webSearchEnabled && getWebSearchAvailability().available);
    const res = await callApi(
      "send_message",
      tab.id,
      tab.conversationId,
      text,
      tab.providerId,
      tab.modelId,
      tab.thinkingLevel,
      tab.title || "New Chat",
      tab.systemPrompt || "",
      tab.maxContextMessages || 30,
      useWebSearch
    );
    if (res && res.conversation_id) {
      tab.conversationId = res.conversation_id;
    }
    if (res && res.user_message && res.user_message.id) {
      tempUserMsg.id = res.user_message.id;
      userRow.dataset.msgId = res.user_message.id;
    }
  }

  // Token Stats & Cost Rendering
  function renderTokenStats(pane, tabId, stats) {
    const chipsEl = pane.querySelector(`#token-chips-${tabId}`);
    const grandTotalEl = pane.querySelector(`#token-grand-${tabId}`);
    const detailsEl = pane.querySelector(`#token-details-${tabId}`);
    const barEl = pane.querySelector(`#token-bar-${tabId}`);
    const summaryEl = pane.querySelector(`#token-summary-${tabId}`);

    if (summaryEl && barEl) {
      summaryEl.onclick = () => {
        barEl.classList.toggle("expanded");
      };
    }

    if (stats && stats.google_search_totals) {
      state.googleSearchTotals = stats.google_search_totals;
    }
    const gt = (stats && stats.grand_total) || {};
    const chatSearches = Number(gt.google_search_requests || 0);
    const gTotals = (stats && stats.google_search_totals) || state.googleSearchTotals || { monthly: 0, all_time: 0 };
    const monthSearches = Number(gTotals.monthly || 0);
    const allTimeSearches = Number(gTotals.all_time || 0);
    const searchSummaryHtml = `
      <div class="search-requests-row">
        <span class="search-requests-label"><i data-lucide="globe" style="width: 12px; height: 12px;"></i> Google Search Requests</span>
        <span class="search-requests-values">${chatSearches.toLocaleString()} this chat · ${monthSearches.toLocaleString()} / 5,000 this month · ${allTimeSearches.toLocaleString()} total</span>
      </div>
    `;

    if (!stats || !stats.per_model || stats.per_model.length === 0) {
      chipsEl.innerHTML = '<span style="color: var(--text-muted);">Tokens: 0 in · 0 out · $0.00</span>';
      grandTotalEl.textContent = "0 total · Est. $0.00";
      detailsEl.innerHTML = `<span style="color: var(--text-muted);">No tokens recorded yet for this chat.</span>${searchSummaryHtml}`;
      lucide.createIcons();
      return;
    }

    chipsEl.innerHTML = "";
    stats.per_model.forEach((m) => {
      const chip = document.createElement("div");
      chip.className = "token-chip";
      const cacheText = m.cached_tokens > 0 ? `<span class="token-chip-cache">· ${formatTokens(m.cached_tokens)} cache (${m.cache_hit_rate}%)</span>` : "";
      const costText = `<span class="token-chip-cost">· ${formatCost(m.cost)}</span>`;
      chip.innerHTML = `
        <span class="token-chip-model">${escapeHtml(m.model_id)}:</span>
        <span>${formatTokens(m.prompt_tokens)} in / ${formatTokens(m.completion_tokens)} out</span>
        ${cacheText}
        ${costText}
      `;
      chipsEl.appendChild(chip);
    });

    grandTotalEl.textContent = `${formatTokens(gt.total_tokens)} total · Est. ${formatCost(gt.total_cost)}`;

    let tableRows = stats.per_model
      .map(
        (m) => `
        <tr>
          <td style="font-weight: 500;">${escapeHtml(m.model_id)}</td>
          <td>${escapeHtml(m.provider_id)}</td>
          <td>${m.prompt_tokens.toLocaleString()}</td>
          <td>${m.completion_tokens.toLocaleString()}</td>
          <td style="color: #34a853;">${m.cached_tokens.toLocaleString()}</td>
          <td>${m.cache_miss_tokens.toLocaleString()}</td>
          <td>${m.cache_hit_rate}%</td>
          <td style="font-weight: 600;">${formatCost(m.cost)}</td>
        </tr>
      `
      )
      .join("");

    detailsEl.innerHTML = `
      <table class="stats-table">
        <thead>
          <tr>
            <th>Model</th>
            <th>Provider</th>
            <th>In Tokens</th>
            <th>Out Tokens</th>
            <th>Cache Hits</th>
            <th>Cache Misses</th>
            <th>Hit Rate</th>
            <th>Cost</th>
          </tr>
        </thead>
        <tbody>
          ${tableRows}
          <tr style="border-top: 1px solid var(--border-subtle); font-weight: 600;">
            <td colspan="2">Est. Total</td>
            <td>${gt.prompt_tokens.toLocaleString()}</td>
            <td>${gt.completion_tokens.toLocaleString()}</td>
            <td style="color: #34a853;">${gt.cached_tokens.toLocaleString()}</td>
            <td>${gt.cache_miss_tokens.toLocaleString()}</td>
            <td>${gt.cache_hit_rate}%</td>
            <td>${formatCost(gt.total_cost)}</td>
          </tr>
        </tbody>
      </table>
      ${searchSummaryHtml}
    `;
    lucide.createIcons();
  }

  // Stream Event Handler (Dispatched from Python)
  window.ChatIntUI = {
    onStreamEvent: function (event) {
      if (event.type === "provider_models_updated") {
        const updated = event.provider;
        const isSettingsOpen = el.settingsModal.classList.contains("open");
        if (isSettingsOpen) {
          saveGeneralTabInputs();
          saveActiveProviderInputs();
        }

        const idx = state.providers.findIndex((p) => p.id === updated.id);
        let activeViewNeedsRefresh = false;

        if (idx !== -1) {
          const currentProv = state.providers[idx];
          const snapProv = state.settingsSnapshot?.providers?.find((p) => p.id === updated.id);
          const hasLocalEdits =
            isSettingsOpen &&
            snapProv &&
            (snapProv.name !== currentProv.name ||
              Boolean(snapProv.is_enabled) !== Boolean(currentProv.is_enabled) ||
              (snapProv.base_url || "") !== (currentProv.base_url || "") ||
              Boolean(currentProv.api_key) ||
              Boolean(currentProv.clear_api_key) ||
              JSON.stringify(snapProv.models || []) !== JSON.stringify(currentProv.models || []));

          if (hasLocalEdits) {
            // Preserve the user's in-progress Settings edits; only refresh remote metadata
            currentProv.has_api_key = Boolean(updated.has_api_key);
            currentProv.sync_status = updated.sync_status || "";
            currentProv.sync_error = updated.sync_error || "";
            if (Array.isArray(updated.fetched_model_ids) && updated.fetched_model_ids.length > 0) {
              currentProv.fetched_model_ids = updated.fetched_model_ids;
            }
            refreshProviderAccessibility(currentProv);
          } else {
            const modelsChanged = JSON.stringify(currentProv.models || []) !== JSON.stringify(updated.models || []);
            const accChanged = Boolean(currentProv.is_accessible) !== Boolean(updated.is_accessible);
            const keyChanged = Boolean(currentProv.has_api_key) !== Boolean(updated.has_api_key);
            const statusChanged = (currentProv.sync_status || "") !== (updated.sync_status || "");

            // Mutate in-place to preserve active object references held by UI event closures
            for (const k of Object.keys(currentProv)) {
              if (!(k in updated)) delete currentProv[k];
            }
            Object.assign(currentProv, updated);
            if (isSettingsOpen && state.settingsSnapshot?.providers) {
              const sIdx = state.settingsSnapshot.providers.findIndex((p) => p.id === updated.id);
              if (sIdx !== -1) {
                state.settingsSnapshot.providers[sIdx] = JSON.parse(JSON.stringify(updated));
              }
            }
            if (updated.id === state.activeProviderId && (modelsChanged || accChanged || keyChanged || statusChanged)) {
              activeViewNeedsRefresh = true;
            }
          }
        } else {
          state.providers.push(updated);
          if (isSettingsOpen && state.settingsSnapshot?.providers) {
            state.settingsSnapshot.providers.push(JSON.parse(JSON.stringify(updated)));
          }
        }

        // Live update model dropdowns across all visible panes
        if (updated.id === "gemini" && Array.isArray(updated.web_search_models)) {
          const snapWs = state.settingsSnapshot?.webSearchModels;
          const wsLocallyEdited =
            isSettingsOpen &&
            snapWs &&
            JSON.stringify(snapWs) !== JSON.stringify(state.webSearchModels || []);
          if (!wsLocallyEdited) {
            state.webSearchModels = normalizeWebSearchModelsClient(updated.web_search_models);
            if (isSettingsOpen && state.settingsSnapshot) {
              state.settingsSnapshot.webSearchModels = JSON.parse(JSON.stringify(state.webSearchModels));
            }
          }
        }
        syncAllPaneModelDropdowns();

        // Update settings modal view cleanly without destroying scroll state
        if (isSettingsOpen && state.masterSettingsTab === "websearch" && updated.id === "gemini") {
          const isModelModalOpen = el.modelConfigModal && el.modelConfigModal.classList.contains("open");
          const activeEl = document.activeElement;
          const isTypingInModal = activeEl && el.settingsModal.contains(activeEl) && activeEl.tagName === "INPUT";
          if (!isModelModalOpen && !isTypingInModal) {
            renderSettingsContent();
          }
        } else if (isSettingsOpen && state.masterSettingsTab === "providers") {
          const provObj = state.providers.find((p) => p.id === updated.id);
          const subBtn = el.settingsTabContent.querySelector(`.provider-sub-btn[data-prov-id="${CSS.escape(updated.id)}"]`);
          if (subBtn && provObj) {
            let dot = subBtn.querySelector(".prov-status-dot");
            if (provObj.is_accessible && !dot) {
              dot = document.createElement("span");
              dot.className = "prov-status-dot";
              dot.style.cssText = "color: #34a853; font-size: 10px;";
              dot.textContent = "●";
              subBtn.appendChild(dot);
            } else if (!provObj.is_accessible && dot) {
              dot.remove();
            }
          }

          const isModelModalOpen = el.modelConfigModal && el.modelConfigModal.classList.contains("open");
          if (activeViewNeedsRefresh && !isModelModalOpen) {
            const activeEl = document.activeElement;
            const isTypingInModal =
              activeEl &&
              el.settingsModal.contains(activeEl) &&
              (activeEl.tagName === "INPUT" || activeEl.tagName === "TEXTAREA");
            if (!isTypingInModal) {
              renderSettingsContent();
            }
          }
        }
        return;
      }

      const tab = state.tabs.find((t) => t.id === event.tab_id);
      if (!tab) return;

      const pane = el.panesContainer.querySelector(`.chat-pane[data-tab-id="${tab.id}"]`);
      const messagesContainer = pane ? pane.querySelector(`#messages-${tab.id}`) : null;

      if (event.type === "chat_named") {
        // Auto-naming triggered when first response streams!
        tab.title = event.title;
        tab.conversationId = event.conversation_id;

        // Ensure in state.conversations
        let conv = state.conversations.find((c) => c.id === event.conversation_id);
        if (conv) {
          conv.title = event.title;
          conv.updated_at = Date.now() / 1000;
        } else {
          state.conversations.unshift({
            id: event.conversation_id,
            title: event.title,
            system_prompt: tab.systemPrompt || "",
            max_context_messages: tab.maxContextMessages || 30,
            updated_at: Date.now() / 1000,
          });
        }

        persistTabsState();
        renderConversationsList();
        renderTabs();

        if (event.token_stats && pane) {
          tab.tokenStats = event.token_stats;
          renderTokenStats(pane, tab.id, event.token_stats);
        }
      } else if (event.type === "start") {
        tab.isStreaming = true;
        tab.streamingContent = "";
        tab.streamingThinking = "";
        tab.hasAutoCollapsedThinking = false;
        tab.streamingProviderId = event.provider_id || tab.providerId;
        tab.streamingModelId = event.model_id || tab.modelId;
        tab.streamingThinkingLevel = event.thinking_level || tab.thinkingLevel;
        tab.streamingStartTime = Date.now();

        if (pane && messagesContainer) {
          const sendBtn = pane.querySelector(`#send-btn-${tab.id}`);
          const stopBtn = pane.querySelector(`#stop-btn-${tab.id}`);
          if (sendBtn) sendBtn.style.display = "none";
          if (stopBtn) stopBtn.style.display = "flex";

          // If this was an empty view, clear welcome screen
          const welcome = messagesContainer.querySelector('div[style*="Start a new conversation"]');
          if (welcome) messagesContainer.innerHTML = "";

          // Remove any stale live row if present
          const staleLive = document.getElementById(`live-msg-${tab.id}`);
          if (staleLive) staleLive.removeAttribute("id");

          const liveRow = createLiveStreamingRow(tab);
          messagesContainer.appendChild(liveRow);
          lucide.createIcons();
          messagesContainer.scrollTop = messagesContainer.scrollHeight;
        }
      } else if (event.type === "tool_status") {
        if (pane && messagesContainer) {
          const toolBanner = document.getElementById(`live-tool-status-${tab.id}`);
          if (toolBanner) {
            if (event.status === "searching") {
              toolBanner.style.display = "inline-flex";
              toolBanner.innerHTML = `<i data-lucide="globe" style="width: 12px; height: 12px;"></i><span>Searching Google${event.query ? `: "${escapeHtml(event.query)}"` : "..."}</span>`;
              lucide.createIcons();
            } else if (event.status === "done") {
              const reqUsed = Number(event.search_requests_used || 0);
              if (event.ok) {
                toolBanner.style.display = "inline-flex";
                toolBanner.innerHTML = `<i data-lucide="check" style="width: 12px; height: 12px; color: #34a853;"></i><span>Google Search complete (${reqUsed} ${reqUsed === 1 ? "request" : "requests"})</span>`;
              } else {
                toolBanner.style.display = "none";
              }
              lucide.createIcons();
            }
            messagesContainer.scrollTop = messagesContainer.scrollHeight;
          }
        }
      } else if (event.type === "thinking_chunk") {
        tab.streamingThinking += event.chunk;
        if (pane && messagesContainer) {
          const thinkAcc = document.getElementById(`live-thinking-${tab.id}`);
          const thinkBody = document.getElementById(`live-thinking-body-${tab.id}`);
          if (thinkAcc && thinkBody) {
            thinkAcc.style.display = "block";
            thinkBody.textContent = tab.streamingThinking;
            const isNearBottom = messagesContainer.scrollHeight - messagesContainer.scrollTop - messagesContainer.clientHeight < 120;
            if (isNearBottom) {
              messagesContainer.scrollTop = messagesContainer.scrollHeight;
            }
          }
        }
      } else if (event.type === "content_chunk") {
        tab.streamingContent += event.chunk;
        if (pane && messagesContainer) {
          const thinkAcc = document.getElementById(`live-thinking-${tab.id}`);
          if (thinkAcc && !tab.hasAutoCollapsedThinking) {
            tab.hasAutoCollapsedThinking = true;
            thinkAcc.classList.add("collapsed");
            const titleSpan = thinkAcc.querySelector(".thinking-title-left span");
            if (titleSpan) titleSpan.textContent = "Thought Process (Complete)";
            const pulse = thinkAcc.querySelector(".thinking-pulse");
            if (pulse) pulse.remove();
          }

          const contentDiv = document.getElementById(`live-content-${tab.id}`);
          if (contentDiv) {
            contentDiv.innerHTML = renderMarkdownWithEnhancements(tab.streamingContent);
            lucide.createIcons();
            const isNearBottom = messagesContainer.scrollHeight - messagesContainer.scrollTop - messagesContainer.clientHeight < 120;
            if (isNearBottom) {
              messagesContainer.scrollTop = messagesContainer.scrollHeight;
            }
          }
        }
      } else if (event.type === "done") {
        tab.isStreaming = false;
        if (pane && messagesContainer) {
          const sendBtn = pane.querySelector(`#send-btn-${tab.id}`);
          const stopBtn = pane.querySelector(`#stop-btn-${tab.id}`);
          if (sendBtn) sendBtn.style.display = "flex";
          if (stopBtn) stopBtn.style.display = "none";

          // Smoothly replace live streaming row with permanent message element
          const liveRow = document.getElementById(`live-msg-${tab.id}`);
          if (event.message) {
            const finalEl = createMessageElement(tab, event.message, pane);
            if (liveRow) {
              liveRow.replaceWith(finalEl);
            } else if (!messagesContainer.querySelector(`[data-msg-id="${event.message.id}"]`)) {
              messagesContainer.appendChild(finalEl);
            }
            lucide.createIcons();
          } else if (liveRow) {
            liveRow.remove();
          }

          if (event.token_stats) {
            tab.tokenStats = event.token_stats;
            if (event.token_stats.google_search_totals) {
              state.googleSearchTotals = event.token_stats.google_search_totals;
            }
            renderTokenStats(pane, tab.id, event.token_stats);
            // Keep Google Search Requests count updated across any other open split panes
            el.panesContainer.querySelectorAll(".chat-pane").forEach((otherPane) => {
              const otherId = otherPane.dataset.tabId;
              if (otherId && otherId !== tab.id) {
                const otherTab = state.tabs.find((t) => t.id === otherId);
                if (otherTab) {
                  renderTokenStats(otherPane, otherId, otherTab.tokenStats || null);
                }
              }
            });
          }
        }
      } else if (event.type === "error") {
        tab.isStreaming = false;
        if (pane && messagesContainer) {
          const sendBtn = pane.querySelector(`#send-btn-${tab.id}`);
          const stopBtn = pane.querySelector(`#stop-btn-${tab.id}`);
          if (sendBtn) sendBtn.style.display = "flex";
          if (stopBtn) stopBtn.style.display = "none";

          let targetRow = document.getElementById(`live-msg-${tab.id}`);
          if (!targetRow) {
            targetRow = document.createElement("div");
            targetRow.className = "message-row assistant-message-row";
            messagesContainer.appendChild(targetRow);
          } else {
            // Strip live streaming IDs so subsequent streams never collide with this error row
            targetRow.removeAttribute("id");
            targetRow.classList.remove("live-streaming-row");
            targetRow.querySelectorAll("[id]").forEach((elWithId) => elWithId.removeAttribute("id"));
          }

          const errBox = document.createElement("div");
          errBox.style.cssText =
            "background: rgba(229, 57, 53, 0.1); border: 1px solid #e53935; border-radius: 8px; padding: 10px 14px; color: #ff8a80; font-size: 13px; margin-top: 8px; display: flex; align-items: center; justify-content: space-between; gap: 12px;";
          errBox.innerHTML = `
            <div><strong>Error:</strong> ${escapeHtml(event.error)}</div>
            <button class="btn-secondary retry-error-btn" style="padding: 4px 10px; font-size: 11.5px; white-space: nowrap; flex-shrink: 0;">Retry</button>
          `;
          targetRow.appendChild(errBox);

          const retryBtn = errBox.querySelector(".retry-error-btn");
          if (retryBtn) {
            retryBtn.onclick = async () => {
              if (tab.isStreaming) return;
              if (!tab.modelId || !tab.providerId) {
                showNoticeDialog("Please configure a valid provider & model in Settings (Ctrl+,) first.");
                return;
              }
              const prevRow = targetRow.previousElementSibling;
              const prevMsgId = prevRow ? prevRow.dataset.msgId : null;
              const prevBubble = prevRow ? prevRow.querySelector(".user-bubble") : null;
              const prevText = prevBubble ? prevBubble.textContent : "";

              targetRow.remove();
              if (prevMsgId && tab.conversationId) {
                tab.isStreaming = true;
                if (sendBtn) sendBtn.style.display = "none";
                if (stopBtn) stopBtn.style.display = "flex";
                const useWebSearch = Boolean(tab.webSearchEnabled && getWebSearchAvailability().available);
                await callApi(
                  "edit_message",
                  tab.id,
                  tab.conversationId,
                  prevMsgId,
                  prevText,
                  tab.providerId,
                  tab.modelId,
                  tab.thinkingLevel,
                  useWebSearch
                );
              }
            };
          }
          messagesContainer.scrollTop = messagesContainer.scrollHeight;
        }
      }
    },
  };

  // Conversations List in Sidebar (Only conversations with at least 1 Q&A)
  function renderConversationsList() {
    el.conversationsList.innerHTML = "";
    const filter = el.convSearchInput.value.toLowerCase().trim();

    state.conversations.forEach((conv) => {
      if (filter && !conv.title.toLowerCase().includes(filter)) return;

      const item = document.createElement("div");
      const isCurrent = state.tabs.some((t) => t.id === state.activeTabId && t.conversationId === conv.id);
      item.className = "conversation-item" + (isCurrent ? " active" : "");

      item.innerHTML = `
        <span class="conv-title">${escapeHtml(conv.title)}</span>
        <div class="conv-actions">
          <button class="conv-action-btn edit-title-btn" title="Rename">
            <i data-lucide="pencil" style="width: 12px; height: 12px;"></i>
          </button>
          <button class="conv-action-btn delete-conv-btn" title="Delete">
            <i data-lucide="trash-2" style="width: 12px; height: 12px;"></i>
          </button>
        </div>
      `;

      item.onclick = async (e) => {
        if (e.target.closest(".edit-title-btn")) {
          e.stopPropagation();
          const newTitle = await showPromptDialog("Rename chat", conv.title);
          if (newTitle && newTitle.trim()) {
            conv.title = newTitle.trim();
            state.tabs.forEach((t) => {
              if (t.conversationId === conv.id) t.title = conv.title;
            });
            callApi("rename_conversation", conv.id, conv.title);
            persistTabsState();
            renderConversationsList();
            renderTabs();
          }
        } else if (e.target.closest(".delete-conv-btn")) {
          e.stopPropagation();
          const confirmed = await showConfirmDialog(`Delete chat "${conv.title}"?`);
          if (confirmed) {
            callApi("delete_conversation", conv.id);
            state.conversations = state.conversations.filter((c) => c.id !== conv.id);
            state.tabs.forEach((t) => {
              if (t.conversationId === conv.id) {
                if (t.isStreaming) {
                  callApi("stop_streaming", t.id);
                  t.isStreaming = false;
                }
                t.conversationId = null;
                t.title = "New Chat";
                t.systemPrompt = "";
                t.maxContextMessages = 30;
              }
            });
            persistTabsState();
            renderConversationsList();
            renderTabs();
            renderActivePanes();
          }
        } else {
          // Open conversation in currently focused active split pane (or exchange if already open in another tab/pane)
          sanitizeSplitTabIds();
          const activeSlotIdx = state.splitTabIds.indexOf(state.activeTabId);
          const existingTabWithConv = state.tabs.find((t) => t.conversationId === conv.id);

          if (existingTabWithConv) {
            if (activeSlotIdx !== -1 && state.splitTabIds.length > 1) {
              assignTabToPaneSlot(activeSlotIdx, existingTabWithConv.id);
            } else {
              switchActiveTab(existingTabWithConv.id);
            }
          } else {
            const activeTab = state.tabs.find((t) => t.id === state.activeTabId);
            if (activeTab) {
              if (activeTab.isStreaming && activeTab.conversationId !== conv.id) {
                callApi("stop_streaming", activeTab.id);
                activeTab.isStreaming = false;
              }
              activeTab.conversationId = conv.id;
              activeTab.title = conv.title;
              activeTab.systemPrompt = conv.system_prompt || "";
              activeTab.maxContextMessages = conv.max_context_messages || 30;
              persistTabsState();
              renderConversationsList();
              renderTabs();
              renderActivePanes();
            }
          }
        }
      };

      el.conversationsList.appendChild(item);
    });

    lucide.createIcons();
  }

  // Settings Modal Management (General, Providers & Web Search)
  function openSettingsModal() {
    state.webSearchModels = normalizeWebSearchModelsClient(state.webSearchModels);
    state.settingsSnapshot = JSON.parse(
      JSON.stringify({
        providers: state.providers,
        settings: state.settings,
        webSearchModels: state.webSearchModels,
      })
    );
    updateMasterTabsUI();
    renderSettingsContent({ resetScroll: true });
    el.settingsModal.classList.add("open");
    lucide.createIcons();
  }

  function closeSettingsModal(saved = false) {
    if (!saved && state.settingsSnapshot) {
      state.providers = state.settingsSnapshot.providers;
      state.settings = state.settingsSnapshot.settings;
      if (Array.isArray(state.settingsSnapshot.webSearchModels)) {
        state.webSearchModels = normalizeWebSearchModelsClient(state.settingsSnapshot.webSearchModels);
      }
      applyTheme(state.settings.theme || "dark");
    }
    state.settingsSnapshot = null;
    el.settingsModal.classList.remove("open");
  }

  function updateMasterTabsUI() {
    el.masterTabGeneral.classList.toggle("active", state.masterSettingsTab === "general");
    el.masterTabProviders.classList.toggle("active", state.masterSettingsTab === "providers");
    if (el.masterTabWebSearch) {
      el.masterTabWebSearch.classList.toggle("active", state.masterSettingsTab === "websearch");
    }
  }

  // Model Pricing & Thinking Configuration Sub-Modal
  let activeConfigModelRef = null;

  function openModelConfigModal(prov, modelObj, options = {}) {
    const isWebSearch = Boolean(options.isWebSearch);
    activeConfigModelRef = { prov, modelObj, isWebSearch };
    el.modelConfigTitle.textContent = `${modelObj.name || modelObj.id}${isWebSearch ? " (Pricing)" : ""}`;
    el.modelInputPrice.value = Number(modelObj.input_price || 0);
    el.modelCachedPrice.value = Number(modelObj.cached_input_price || 0);
    el.modelOutputPrice.value = Number(modelObj.output_price || 0);
    el.modelSupportsThinkingCheck.checked = Boolean(modelObj.supports_thinking);
    if (el.modelThinkingConfigRow) {
      el.modelThinkingConfigRow.style.display = isWebSearch ? "none" : "flex";
    }
    el.modelConfigModal.classList.add("open");
  }

  function closeModelConfigModal(save = false) {
    if (save && activeConfigModelRef) {
      const { modelObj, isWebSearch } = activeConfigModelRef;
      const inVal = parseFloat(el.modelInputPrice.value);
      const cacheVal = parseFloat(el.modelCachedPrice.value);
      const outVal = parseFloat(el.modelOutputPrice.value);
      modelObj.input_price = !isNaN(inVal) && inVal >= 0 ? inVal : 0.0;
      modelObj.cached_input_price = !isNaN(cacheVal) && cacheVal >= 0 ? cacheVal : 0.0;
      modelObj.output_price = !isNaN(outVal) && outVal >= 0 ? outVal : 0.0;
      if (!isWebSearch) {
        modelObj.supports_thinking = el.modelSupportsThinkingCheck.checked ? 1 : 0;
      }
      renderSettingsContent();
    }
    activeConfigModelRef = null;
    el.modelConfigModal.classList.remove("open");
  }

  function sanitizeProviderNameAlpha(raw) {
    return String(raw || "").replace(/[^A-Za-z]/g, "");
  }

  function validateAllProviders() {
    const seen = new Set();
    for (const p of state.providers) {
      const name = (p.name || "").trim();
      if (!name || !/^[A-Za-z]+$/.test(name)) {
        return `Provider name "${name || "(empty)"}" must contain only English alphabet letters (A-Z, a-z) with no spaces, numbers, dashes, underscores, or special characters.`;
      }
      const upper = name.toUpperCase();
      if (seen.has(upper)) {
        return `Duplicate provider name "${name}". No two providers can have the same name (even with different case).`;
      }
      seen.add(upper);
    }
    return null;
  }

  function canShowProviderModels(prov) {
    if (!prov || prov.clear_api_key) return false;
    const hasKey = Boolean((prov.api_key && prov.api_key.trim()) || prov.has_api_key);
    return hasKey && prov.sync_status === "ready";
  }

  function refreshProviderAccessibility(prov) {
    if (!prov) return;
    const typedKey = (prov.api_key || "").trim();
    const hasCredentials = prov.clear_api_key ? false : Boolean(typedKey || prov.has_api_key);
    const fetchedIds = Array.isArray(prov.fetched_model_ids) ? new Set(prov.fetched_model_ids) : new Set();
    const models = Array.isArray(prov.models) ? prov.models : [];
    const verifiedModels = fetchedIds.size > 0 ? models.filter((m) => fetchedIds.has(m.id)) : models;
    prov.has_api_key = Boolean(hasCredentials);
    prov.is_accessible = Boolean(
      prov.is_enabled && hasCredentials && prov.sync_status === "ready" && verifiedModels.length > 0
    );
  }

  function shuffleModelArray(models) {
    const arr = [...(models || [])];
    for (let i = arr.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [arr[i], arr[j]] = [arr[j], arr[i]];
    }
    if (arr.length > 1 && arr.every((m, i) => m.id === models[i].id)) {
      arr.push(arr.shift());
    }
    return arr;
  }

  function swapModelInArray(models, mid, delta, minIdx = 0) {
    const list = models || [];
    const idx = list.findIndex((m) => m.id === mid);
    const targetIdx = idx + delta;
    if (idx < minIdx || targetIdx < minIdx || targetIdx >= list.length) {
      return null;
    }
    const arr = [...list];
    [arr[idx], arr[targetIdx]] = [arr[targetIdx], arr[idx]];
    return arr;
  }

  function syncSearchModelsWithGeminiFetch(fetchedIds) {
    const cleanFetchedSet = new Set(Array.from(fetchedIds || [], (id) => stripModelPrefix(id).toLowerCase()));
    const [wsPrimary, ...wsFallbacks] = normalizeWebSearchModelsClient(state.webSearchModels);
    const keptWs = wsFallbacks.filter((m) => cleanFetchedSet.has(stripModelPrefix(m.id).toLowerCase()));
    state.webSearchModels = normalizeWebSearchModelsClient([wsPrimary, ...keptWs]);
  }

  function renderConfiguredModelRow(m, idx, totalM, isWebSearch = false) {
    const inP = Number(m.input_price || 0);
    const cP = Number(m.cached_input_price || 0);
    const outP = Number(m.output_price || 0);
    const isPrimaryWs = isWebSearch && idx === 0;
    const minMoveIdx = isWebSearch ? 1 : 0;

    const upDisabled = idx <= minMoveIdx ? "disabled style='opacity:0.25;cursor:default;'" : "";
    const downDisabled = isPrimaryWs || idx === totalM - 1 ? "disabled style='opacity:0.25;cursor:default;'" : "";
    const delDisabled = isPrimaryWs ? "disabled style='opacity:0.25;cursor:not-allowed;'" : "";

    const clsPrefix = isWebSearch ? "ws-" : "";
    const upCls = isWebSearch ? "ws-move-up-btn" : "move-model-up-btn";
    const downCls = isWebSearch ? "ws-move-down-btn" : "move-model-down-btn";
    const cfgCls = isWebSearch ? "ws-model-config-btn" : "model-config-btn";
    const delCls = isWebSearch ? "ws-delete-model-btn" : "delete-model-btn";

    const badgeHtml = isWebSearch
      ? (isPrimaryWs
          ? '<span class="ws-priority-badge primary">Primary · Fixed</span>'
          : `<span class="ws-priority-badge">Fallback #${idx}</span>`)
      : "";
    const thinkTag = !isWebSearch && m.supports_thinking ? " · Thinking" : "";
    const cfgLabel = isWebSearch ? "Pricing" : "Pricing &amp; Thinking";
    const cfgTitle = isWebSearch ? "Configure Pricing" : "Configure Pricing & Thinking";
    const delTitle = isPrimaryWs ? "Primary model cannot be removed" : "Remove Model";

    return `
      <div class="model-row-item" data-list-prefix="${clsPrefix}">
        <div style="display: flex; flex-direction: column; min-width: 0; gap: 2px;">
          <div style="display: flex; align-items: center; gap: 6px; min-width: 0;">
            <span style="font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(m.name || m.id)}</span>
            ${badgeHtml}
          </div>
          <span style="color: var(--text-muted); font-size: 10.5px;">$${inP}/M in · $${cP}/M cached · $${outP}/M out${thinkTag}</span>
        </div>
        <div style="display: flex; align-items: center; gap: 4px; flex-shrink: 0;">
          <button class="conv-action-btn ${upCls}" data-model-id="${escapeHtml(m.id)}" title="Move Up" ${upDisabled}>
            <i data-lucide="arrow-up" style="width: 12px; height: 12px;"></i>
          </button>
          <button class="conv-action-btn ${downCls}" data-model-id="${escapeHtml(m.id)}" title="Move Down" ${downDisabled}>
            <i data-lucide="arrow-down" style="width: 12px; height: 12px;"></i>
          </button>
          <button class="btn-secondary ${cfgCls}" data-model-id="${escapeHtml(m.id)}" style="padding: 3px 8px; font-size: 11px; display: flex; align-items: center; gap: 4px;" title="${cfgTitle}">
            <i data-lucide="sliders-horizontal" style="width: 11px; height: 11px;"></i>
            <span>${cfgLabel}</span>
          </button>
          <button class="conv-action-btn ${delCls}" data-model-id="${escapeHtml(m.id)}" title="${delTitle}" ${delDisabled}>
            <i data-lucide="trash-2" style="width: 12px; height: 12px;"></i>
          </button>
        </div>
      </div>
    `;
  }

  function renderSettingsContent(options = {}) {
    const resetScroll = Boolean(options.resetScroll);
    const prevTab = el.settingsTabContent.dataset.renderedTab || "";
    const prevProvId = el.settingsTabContent.dataset.renderedProvId || "";
    const modalBody = el.settingsTabContent.closest(".modal-body");
    const prevModelsList = document.getElementById("prov-models-list") || document.getElementById("ws-models-list");
    const prevSubNav = el.settingsTabContent.querySelector(".providers-sub-nav");

    const prevModalScroll = !resetScroll && modalBody ? modalBody.scrollTop : 0;
    const prevSubNavScroll = !resetScroll && prevSubNav ? prevSubNav.scrollTop : 0;
    const targetProvId = state.masterSettingsTab === "providers" ? (state.activeProviderId || state.providers[0]?.id || "") : "";
    const sameProviderView =
      !resetScroll &&
      prevTab === "providers" &&
      state.masterSettingsTab === "providers" &&
      prevProvId === targetProvId;
    const sameWebSearchView = !resetScroll && prevTab === "websearch" && state.masterSettingsTab === "websearch";
    const prevModelsScroll = (sameProviderView || sameWebSearchView) && prevModelsList ? prevModelsList.scrollTop : 0;
    const sameGeneralView = !resetScroll && prevTab === "general" && state.masterSettingsTab === "general";

    el.settingsTabContent.innerHTML = "";

    if (state.masterSettingsTab === "general") {
      el.settingsTabContent.innerHTML = `
        <div class="settings-group">
          <label class="settings-label">Global Default System Prompt</label>
          <textarea class="settings-input" id="setting-global-system-prompt" rows="3">${escapeHtml(state.settings.global_system_prompt || "")}</textarea>
        </div>

        <div class="settings-group">
          <label class="settings-label">Default Provider</label>
          <select class="settings-input" id="setting-default-provider">
            ${state.providers.map((p) => `<option value="${p.id}" ${p.id === state.settings.default_provider ? "selected" : ""}>${escapeHtml(p.name)}</option>`).join("")}
          </select>
        </div>

        <div class="settings-group">
          <label class="settings-label">Default Thinking</label>
          <select class="settings-input" id="setting-default-thinking">
            ${THINKING_LEVELS.map(
              (lvl) => `<option value="${lvl.value}" ${lvl.value === getDefaultThinkingLevel() ? "selected" : ""}>${lvl.label}</option>`
            ).join("")}
          </select>
        </div>

        <div class="settings-group">
          <label class="settings-label">Appearance Theme</label>
          <select class="settings-input" id="setting-theme">
            <option value="dark" ${state.settings.theme === "dark" ? "selected" : ""}>Dark Theme</option>
            <option value="light" ${state.settings.theme === "light" ? "selected" : ""}>Light Theme</option>
          </select>
        </div>
      `;
    } else if (state.masterSettingsTab === "websearch") {
      state.webSearchModels = normalizeWebSearchModelsClient(state.webSearchModels);
      const geminiProv = state.providers.find((p) => p.id === "gemini");
      const { available: wsAvail, reason: wsReason } = getWebSearchAvailability();
      const wsStatusLine = wsAvail
        ? '<span style="color: #34a853; font-weight: 600;">Ready</span> · Using your Gemini provider API key.'
        : `<span style="color: #ff8a80; font-weight: 600;">Unavailable (${escapeHtml(wsReason)})</span> · Configure Gemini in Providers.`;

      const wsModels = state.webSearchModels;
      const totalWs = wsModels.length;

      el.settingsTabContent.innerHTML = `
        <div class="provider-detail-pane">
          <div class="websearch-info-banner">
            <div><strong>Status:</strong> ${wsStatusLine}</div>
          </div>

          <div class="settings-group" style="margin-top: 4px;">
            <div style="display: flex; align-items: center; justify-content: space-between;">
              <label class="settings-label">Configured Search Models (Max 5 · Priority Order)</label>
              <div style="display: flex; gap: 6px; align-items: center;">
                <button class="btn-secondary ws-shuffle-models-btn" style="padding: 2px 8px; font-size: 11px; display: flex; align-items: center; gap: 4px;" title="Shuffle fallback models (keeps gemini-3.1-flash-lite pinned at top)">
                  <i data-lucide="shuffle" style="width: 11px; height: 11px;"></i>
                  <span>Shuffle</span>
                </button>
                <button class="btn-secondary ws-fetch-models-btn" style="padding: 2px 8px; font-size: 11px; display: flex; align-items: center; gap: 4px;" title="Fetch available Gemini models and discard missing fallback models">
                  <i data-lucide="refresh-cw" style="width: 11px; height: 11px;"></i>
                  <span>Fetch Gemini Models</span>
                </button>
              </div>
            </div>

            <div class="models-manager-list" id="ws-models-list">
              ${wsModels.map((m, idx) => renderConfiguredModelRow(m, idx, totalWs, true)).join("")}
            </div>

            <div style="display: flex; gap: 6px; margin-top: 6px;">
              <input type="text" class="settings-input" id="ws-new-model-id-input" placeholder="Add Gemini fallback model ID (e.g. gemini-3.8-flash)..." style="font-size: 12px;">
              <button class="btn-secondary" id="ws-add-model-btn" style="padding: 5px 10px; font-size: 11.5px; white-space: nowrap;">Add Model</button>
            </div>
          </div>
        </div>
      `;

      // Web Search Pricing Modal Trigger (without Thinking checkbox)
      el.settingsTabContent.querySelectorAll(".ws-model-config-btn").forEach((btn) => {
        btn.onclick = () => {
          const mid = btn.dataset.modelId;
          const target = state.webSearchModels.find((m) => m.id === mid);
          if (target) {
            openModelConfigModal(geminiProv || null, target, { isWebSearch: true });
          }
        };
      });

      // Shuffle fallback models only (index 0 stays fixed)
      const wsShuffleBtn = el.settingsTabContent.querySelector(".ws-shuffle-models-btn");
      if (wsShuffleBtn) {
        wsShuffleBtn.onclick = () => {
          if (state.webSearchModels.length <= 2) return;
          const [primary, ...fallbacks] = state.webSearchModels;
          state.webSearchModels = [primary, ...shuffleModelArray(fallbacks)];
          renderSettingsContent();
        };
      }

      // Move fallback models up/down (index 0 cannot move, index 1 cannot move above index 0)
      const swapWsModel = (mid, delta) => {
        const swapped = swapModelInArray(state.webSearchModels, mid, delta, 1);
        if (swapped) {
          state.webSearchModels = swapped;
          renderSettingsContent();
        }
      };
      el.settingsTabContent.querySelectorAll(".ws-move-up-btn").forEach((btn) => {
        btn.onclick = () => swapWsModel(btn.dataset.modelId, -1);
      });
      el.settingsTabContent.querySelectorAll(".ws-move-down-btn").forEach((btn) => {
        btn.onclick = () => swapWsModel(btn.dataset.modelId, 1);
      });

      // Remove fallback model (primary at index 0 cannot be deleted)
      el.settingsTabContent.querySelectorAll(".ws-delete-model-btn").forEach((btn) => {
        btn.onclick = () => {
          const mid = btn.dataset.modelId;
          if (mid.toLowerCase() === PRIMARY_WEB_SEARCH_MODEL) return;
          state.webSearchModels = state.webSearchModels.filter((m) => m.id !== mid);
          renderSettingsContent();
        };
      });

      // Add Gemini fallback model (must exist in fetched Gemini models; no duplicates; max 5 total)
      const wsAddBtn = document.getElementById("ws-add-model-btn");
      const wsAddInput = document.getElementById("ws-new-model-id-input");
      if (wsAddBtn && wsAddInput) {
        const triggerAddWsModel = () => {
          const mid = stripModelPrefix(wsAddInput.value.trim());
          if (!mid) return;
          const fetchedIds = geminiProv && Array.isArray(geminiProv.fetched_model_ids) ? geminiProv.fetched_model_ids : [];
          const cleanFetched = new Map(fetchedIds.map((fid) => [stripModelPrefix(fid).toLowerCase(), stripModelPrefix(fid)]));
          if (cleanFetched.size === 0) {
            showNoticeDialog("Please click 'Fetch Gemini Models' first (requires a valid Gemini API key).");
            return;
          }
          const canonical = cleanFetched.get(mid.toLowerCase());
          if (!canonical) {
            showNoticeDialog(`Model "${mid}" was not found in fetched Gemini models. Only verified Gemini models can be added.`);
            return;
          }
          if (state.webSearchModels.some((m) => m.id.toLowerCase() === canonical.toLowerCase())) {
            wsAddInput.value = "";
            return;
          }
          if (state.webSearchModels.length >= 5) {
            showNoticeDialog("Maximum 5 models can be configured for Web Search. Remove a fallback model first.");
            return;
          }
          state.webSearchModels.push({
            id: canonical,
            name: canonical,
            input_price: 0.0,
            cached_input_price: 0.0,
            output_price: 0.0,
          });
          renderSettingsContent();
        };
        wsAddBtn.onclick = triggerAddWsModel;
        wsAddInput.onkeydown = (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            triggerAddWsModel();
          }
        };
      }

      // Fetch Gemini Models from Web Search tab (discards missing fallbacks, never auto-adds)
      const wsFetchBtn = el.settingsTabContent.querySelector(".ws-fetch-models-btn");
      if (wsFetchBtn) {
        wsFetchBtn.onclick = async () => {
          const validationErr = validateAllProviders();
          if (validationErr) {
            showNoticeDialog(validationErr);
            return;
          }
          const saveRes = await callApi("save_settings", state.providers, state.settings, state.webSearchModels);
          if (saveRes && saveRes.error) {
            showNoticeDialog(saveRes.error);
            return;
          }
          state.providers.forEach((p) => {
            delete p.api_key;
            delete p.clear_api_key;
          });

          wsFetchBtn.disabled = true;
          wsFetchBtn.innerHTML = '<i data-lucide="loader" style="width: 11px; height: 11px;"></i> Fetching...';
          lucide.createIcons();

          const res = await callApi("fetch_provider_models", "gemini");
          wsFetchBtn.disabled = false;
          wsFetchBtn.innerHTML = '<i data-lucide="refresh-cw" style="width: 11px; height: 11px;"></i> Fetch Gemini Models';
          lucide.createIcons();

          if (res && res.models && res.models.length > 0 && geminiProv) {
            const fetchedIds = res.models.map((m) => m.id);
            geminiProv.fetched_model_ids = fetchedIds;
            geminiProv.has_api_key = true;
            geminiProv.sync_status = res.sync_status || "ready";
            geminiProv.sync_error = res.sync_error || "";

            syncSearchModelsWithGeminiFetch(fetchedIds);

            await callApi("save_settings", state.providers, state.settings, state.webSearchModels);
            if (state.settingsSnapshot) {
              state.settingsSnapshot.providers = JSON.parse(JSON.stringify(state.providers));
              state.settingsSnapshot.webSearchModels = JSON.parse(JSON.stringify(state.webSearchModels));
            }
            syncAllPaneWebSearchButtons();
            renderSettingsContent();
            showNoticeDialog(`Verified Gemini search models (${fetchedIds.length} remote models fetched).`);
          } else {
            if (geminiProv) {
              geminiProv.is_accessible = false;
              geminiProv.fetched_model_ids = [];
              geminiProv.sync_status = res?.sync_status || "error";
              geminiProv.sync_error = res?.sync_error || "FETCH ERROR";
            }
            syncAllPaneWebSearchButtons();
            renderSettingsContent();
            showNoticeDialog(`Could not fetch Gemini models: ${res?.error || "Missing/invalid Gemini API key or network error"}`);
          }
        };
      }
    } else {
      // Providers Tab Layout
      if (!state.activeProviderId && state.providers.length > 0) {
        state.activeProviderId = state.providers[0].id;
      }
      const activeProv = state.providers.find((p) => p.id === state.activeProviderId) || state.providers[0];

      let subNavHtml = state.providers
        .map(
          (p) => `
        <button class="provider-sub-btn ${p.id === activeProv?.id ? "active" : ""}" data-prov-id="${escapeHtml(p.id)}">
          <span>${escapeHtml(p.name)}</span>
          ${p.is_accessible ? '<span class="prov-status-dot" style="color: #34a853; font-size: 10px;">●</span>' : ""}
        </button>
      `
        )
        .join("");

      subNavHtml += `
        <button class="add-provider-pill-btn" id="add-new-provider-btn">
          <i data-lucide="plus" style="width: 13px; height: 13px;"></i>
          <span>Add Provider</span>
        </button>
      `;

      let detailHtml = "";
      if (activeProv) {
        const isBuiltin = ["openai", "anthropic", "gemini", "openrouter", "deepseek", "ollama"].includes(activeProv.id);
        const isNativeSdkProv =
          ["anthropic", "gemini"].includes((activeProv.id || "").toLowerCase()) ||
          ["anthropic", "gemini"].includes(sanitizeProviderNameAlpha(activeProv.name).toLowerCase());
        const envKeyName = `${sanitizeProviderNameAlpha(activeProv.name).toUpperCase() || "PROVIDER"}_API_KEY`;
        const keyPlaceholder = activeProv.has_api_key && !activeProv.clear_api_key
          ? `Saved in .env (${envKeyName}) — type to replace or click eye to view`
          : `Saved in .env as ${envKeyName}...`;

        const provModelsVisible = canShowProviderModels(activeProv);
        const provModelsHtml = provModelsVisible
          ? (activeProv.models || []).map((m, idx, list) => renderConfiguredModelRow(m, idx, list.length, false)).join("")
          : `<div style="padding: 10px; font-size: 11.5px; color: var(--text-muted); text-align: center;">${escapeHtml(
              !activeProv.has_api_key || activeProv.clear_api_key
                ? "Enter a valid API key and fetch models to view configured models. Any previously saved models remain preserved."
                : `Configured models are hidden until verification succeeds (${activeProv.sync_error || activeProv.sync_status || "UNVERIFIED"}). Saved models & pricing remain preserved.`
            )}</div>`;

        detailHtml = `
          <div class="provider-detail-pane">
            <div style="display: flex; align-items: center; justify-content: space-between;">
              <div style="display: flex; align-items: center; gap: 8px;">
                <input type="text" class="settings-input" id="prov-name-input" value="${escapeHtml(activeProv.name)}" title="English letters only (A-Z, a-z)" style="font-weight: 600; width: 200px;">
              </div>
              <div style="display: flex; align-items: center; gap: 10px;">
                <label style="display: flex; align-items: center; gap: 6px; font-size: 13px; cursor: pointer;">
                  <input type="checkbox" id="prov-enabled-check" ${activeProv.is_enabled ? "checked" : ""}>
                  <span>Enabled</span>
                </label>
                ${!isBuiltin ? `<button class="btn-danger" id="delete-provider-btn" title="Remove Provider">Delete</button>` : ""}
              </div>
            </div>

            <div class="settings-group">
              <label class="settings-label">Endpoint Base URL</label>
              <input type="text" class="settings-input" id="prov-base-url-input" value="${escapeHtml(isNativeSdkProv ? "" : (activeProv.base_url || ""))}" placeholder="${isNativeSdkProv ? "Managed automatically by official SDK (no endpoint needed)" : "https://api.openai.com/v1"}" ${isNativeSdkProv ? "disabled" : ""}>
            </div>

            <div class="settings-group">
              <div style="display: flex; align-items: center; justify-content: space-between;">
                <label class="settings-label" id="prov-env-key-label">API Key (.env: ${escapeHtml(envKeyName)})</label>
                ${
                  activeProv.has_api_key && !activeProv.clear_api_key
                    ? `<button class="btn-secondary" id="clear-api-key-btn" style="padding: 1px 7px; font-size: 10.5px;">Clear Key</button>`
                    : ""
                }
              </div>
              <div style="position: relative;">
                <input type="password" class="settings-input" id="prov-api-key-input" value="${escapeHtml(activeProv.api_key || "")}" placeholder="${escapeHtml(keyPlaceholder)}">
                <button class="icon-btn toggle-key-visibility" style="position: absolute; right: 8px; top: 6px;" title="Show/Hide">
                  <i data-lucide="eye" style="width: 14px; height: 14px;"></i>
                </button>
              </div>
            </div>

            <div class="settings-group" style="margin-top: 4px;">
              <div style="display: flex; align-items: center; justify-content: space-between;">
                <label class="settings-label">Configured Models (Max 5 shown in dropdown)</label>
                <div style="display: flex; gap: 6px; align-items: center;">
                  <button class="btn-secondary shuffle-models-btn" style="padding: 2px 8px; font-size: 11px; display: flex; align-items: center; gap: 4px;" title="Shuffle the order of configured models">
                    <i data-lucide="shuffle" style="width: 11px; height: 11px;"></i>
                    <span>Shuffle</span>
                  </button>
                  <button class="btn-secondary fetch-models-btn" style="padding: 2px 8px; font-size: 11px; display: flex; align-items: center; gap: 4px;">
                    <i data-lucide="refresh-cw" style="width: 11px; height: 11px;"></i>
                    <span>Fetch from Endpoint</span>
                  </button>
                </div>
              </div>

              <div class="models-manager-list" id="prov-models-list">
                ${provModelsHtml}
              </div>

              <div style="display: flex; gap: 6px; margin-top: 6px;">
                <input type="text" class="settings-input" id="new-model-id-input" placeholder="Add model ID (e.g. gpt-4.5-preview)..." style="font-size: 12px;">
                <button class="btn-secondary" id="add-model-btn" style="padding: 5px 10px; font-size: 11.5px; white-space: nowrap;">Add Model</button>
              </div>
            </div>
          </div>
        `;
      }

      el.settingsTabContent.innerHTML = `
        <div class="providers-container-layout">
          <div class="providers-sub-nav">${subNavHtml}</div>
          <div style="flex: 1; min-width: 0;">${detailHtml}</div>
        </div>
      `;

      // Enforce English alphabet only on provider name input & live update sub-nav and .env key label
      const provNameInput = document.getElementById("prov-name-input");
      if (provNameInput && activeProv) {
        provNameInput.oninput = () => {
          const cleaned = sanitizeProviderNameAlpha(provNameInput.value);
          if (provNameInput.value !== cleaned) {
            provNameInput.value = cleaned;
          }
          if (cleaned) {
            activeProv.name = cleaned;
          }
          const subBtnSpan = el.settingsTabContent.querySelector(`.provider-sub-btn[data-prov-id="${activeProv.id}"] span`);
          if (subBtnSpan) subBtnSpan.textContent = activeProv.name;
          const envLbl = document.getElementById("prov-env-key-label");
          if (envLbl) {
            envLbl.textContent = `API Key (.env: ${(cleaned || activeProv.name).toUpperCase()}_API_KEY)`;
          }
        };
      }

      // Provider sub-nav click handlers
      el.settingsTabContent.querySelectorAll(".provider-sub-btn").forEach((btn) => {
        btn.onclick = () => {
          saveActiveProviderInputs();
          state.activeProviderId = btn.dataset.provId;
          renderSettingsContent();
        };
      });

      // Add New Provider button (+)
      const addProvBtn = document.getElementById("add-new-provider-btn");
      if (addProvBtn) {
        addProvBtn.onclick = async () => {
          saveActiveProviderInputs();
          const rawName = await showPromptDialog(
            "Enter Provider Name (English letters only, e.g. Anthropic):",
            "CustomProvider",
            "Letters A-Z only"
          );
          if (!rawName) return;
          const cleanName = sanitizeProviderNameAlpha(rawName);
          if (!cleanName || cleanName !== rawName.trim()) {
            await showNoticeDialog("Provider name may only contain English alphabet letters (A-Z, a-z) with no spaces, dashes, underscores, numbers, or special characters.");
            return;
          }
          if (state.providers.some((p) => p.name.toUpperCase() === cleanName.toUpperCase())) {
            await showNoticeDialog(`A provider named "${cleanName}" already exists (case-insensitive). Please choose a unique name.`);
            return;
          }
          const newId = "provider_" + Date.now();
          const newProv = {
            id: newId,
            name: cleanName,
            base_url: "https://api.openai.com/v1",
            has_api_key: false,
            is_enabled: true,
            models: [],
            fetched_model_ids: [],
            is_accessible: false,
          };
          state.providers.push(newProv);
          state.activeProviderId = newId;
          renderSettingsContent();
        };
      }

      // Clear API Key button
      const clearKeyBtn = document.getElementById("clear-api-key-btn");
      if (clearKeyBtn && activeProv) {
        clearKeyBtn.onclick = () => {
          activeProv.api_key = "";
          activeProv.clear_api_key = true;
          activeProv.has_api_key = false;
          activeProv.sync_status = "no_key";
          activeProv.sync_error = "NO API KEY";
          activeProv.fetched_model_ids = [];
          refreshProviderAccessibility(activeProv);
          syncAllPaneModelDropdowns();
          renderSettingsContent();
        };
      }

      // Password visibility toggle (fetches from .env on demand if not yet loaded into input)
      const keyInput = document.getElementById("prov-api-key-input");
      const toggleBtn = el.settingsTabContent.querySelector(".toggle-key-visibility");
      if (toggleBtn && keyInput && activeProv) {
        toggleBtn.onclick = async () => {
          if (keyInput.type === "password") {
            if (!keyInput.value && activeProv.has_api_key && !activeProv.clear_api_key) {
              const res = await callApi("get_provider_api_key", activeProv.name);
              if (res && res.api_key) {
                keyInput.value = res.api_key;
              }
            }
            keyInput.type = "text";
          } else {
            keyInput.type = "password";
          }
        };
      }

      // Add Model (Only allow models that have been successfully fetched from the endpoint; default $0 pricing and thinking unchecked)
      const addMBtn = document.getElementById("add-model-btn");
      const newMInput = document.getElementById("new-model-id-input");
      if (addMBtn && newMInput && activeProv) {
        const triggerAddModel = () => {
          saveActiveProviderInputs();
          const mid = newMInput.value.trim();
          if (!mid) return;
          const fetchedIds = Array.isArray(activeProv.fetched_model_ids) ? new Set(activeProv.fetched_model_ids) : new Set();
          if (!fetchedIds.has(mid)) {
            showNoticeDialog(
              fetchedIds.size === 0
                ? "Please fetch models from the endpoint first (requires a valid API key)."
                : `Model "${mid}" was not found in the models fetched from ${activeProv.name}. Only successfully fetched models can be added.`
            );
            return;
          }
          if (!activeProv.models) activeProv.models = [];
          if (activeProv.models.some((m) => m.id === mid)) {
            newMInput.value = "";
            return;
          }
          if (activeProv.models.length >= 5) {
            showNoticeDialog("Maximum 5 models can be configured per provider. Remove an existing model first.");
            return;
          }
          activeProv.models.push({
            id: mid,
            name: mid,
            supports_thinking: 0,
            input_price: 0.0,
            cached_input_price: 0.0,
            output_price: 0.0,
          });
          refreshProviderAccessibility(activeProv);
          renderSettingsContent();
        };
        addMBtn.onclick = triggerAddModel;
        newMInput.onkeydown = (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            triggerAddModel();
          }
        };
      }

      // Open Model Pricing & Thinking Configuration Modal
      el.settingsTabContent.querySelectorAll(".model-config-btn").forEach((btn) => {
        btn.onclick = () => {
          saveActiveProviderInputs();
          const mid = btn.dataset.modelId;
          const target = (activeProv.models || []).find((m) => m.id === mid);
          if (target) {
            openModelConfigModal(activeProv, target);
          }
        };
      });

      // Shuffle Models
      const shuffleBtn = el.settingsTabContent.querySelector(".shuffle-models-btn");
      if (shuffleBtn && activeProv) {
        shuffleBtn.onclick = () => {
          saveActiveProviderInputs();
          if (!activeProv.models || activeProv.models.length <= 1) return;
          activeProv.models = shuffleModelArray(activeProv.models);
          renderSettingsContent();
          syncAllPaneModelDropdowns();
        };
      }

      // Move Model Up / Down
      const swapModel = (mid, delta) => {
        saveActiveProviderInputs();
        const swapped = swapModelInArray(activeProv.models, mid, delta, 0);
        if (swapped) {
          activeProv.models = swapped;
          renderSettingsContent();
          syncAllPaneModelDropdowns();
        }
      };
      el.settingsTabContent.querySelectorAll(".move-model-up-btn").forEach((btn) => {
        btn.onclick = () => swapModel(btn.dataset.modelId, -1);
      });
      el.settingsTabContent.querySelectorAll(".move-model-down-btn").forEach((btn) => {
        btn.onclick = () => swapModel(btn.dataset.modelId, 1);
      });

      // Delete Model
      el.settingsTabContent.querySelectorAll(".delete-model-btn").forEach((btn) => {
        btn.onclick = () => {
          saveActiveProviderInputs();
          const mid = btn.dataset.modelId;
          activeProv.models = activeProv.models.filter((m) => m.id !== mid);
          refreshProviderAccessibility(activeProv);
          renderSettingsContent();
          syncAllPaneModelDropdowns();
        };
      });

      // Delete Provider
      const delProvBtn = document.getElementById("delete-provider-btn");
      if (delProvBtn && activeProv) {
        delProvBtn.onclick = async () => {
          const confirmed = await showConfirmDialog(`Delete provider "${activeProv.name}"?`);
          if (confirmed) {
            callApi("delete_provider", activeProv.id);
            state.providers = state.providers.filter((p) => p.id !== activeProv.id);
            if (state.settings.default_provider === activeProv.id) {
              state.settings.default_provider = state.providers[0]?.id || "openai";
            }
            state.activeProviderId = state.providers[0]?.id || null;
            if (state.settingsSnapshot) {
              state.settingsSnapshot.providers = JSON.parse(JSON.stringify(state.providers));
              state.settingsSnapshot.settings = JSON.parse(JSON.stringify(state.settings));
            }
            renderSettingsContent();
            syncAllPaneModelDropdowns();
          }
        };
      }

      // Fetch from Endpoint
      const fetchBtn = el.settingsTabContent.querySelector(".fetch-models-btn");
      if (fetchBtn && activeProv) {
        fetchBtn.onclick = async () => {
          saveActiveProviderInputs();
          const validationErr = validateAllProviders();
          if (validationErr) {
            showNoticeDialog(validationErr);
            return;
          }
          const saveRes = await callApi("save_settings", state.providers, state.settings);
          if (saveRes && saveRes.error) {
            showNoticeDialog(saveRes.error);
            return;
          }
          delete activeProv.api_key;
          delete activeProv.clear_api_key;

          fetchBtn.disabled = true;
          fetchBtn.innerHTML = '<i data-lucide="loader" style="width: 11px; height: 11px;"></i> Fetching...';
          lucide.createIcons();

          const res = await callApi("fetch_provider_models", activeProv.id);
          fetchBtn.disabled = false;
          fetchBtn.innerHTML = '<i data-lucide="refresh-cw" style="width: 11px; height: 11px;"></i> Fetch from Endpoint';
          lucide.createIcons();

          if (res && res.models && res.models.length > 0) {
            // Keep at most 5 models; new models default to $0 pricing and thinking=0
            const fetched = res.models;
            const fetchedIds = new Set(fetched.map((m) => m.id));
            const current = activeProv.models || [];

            // 1. Keep existing models that are present in fetch (preserving user-configured pricing/thinking)
            const cleanFetchedIds = new Set(Array.from(fetchedIds, stripModelPrefix));
            const retained = current.filter((m) => fetchedIds.has(m.id) || cleanFetchedIds.has(stripModelPrefix(m.id)));
            const retainedIds = new Set(retained.map((m) => m.id));

            // 2. If any slots for configured models is empty out of maximum 5,
            // add unconfigured fetched models, newer ones first!
            const cleanRetainedIds = new Set(Array.from(retainedIds, stripModelPrefix));
            if (retained.length < 5) {
              const unconfigured = fetched.filter(
                (fm) => !retainedIds.has(fm.id) && !cleanRetainedIds.has(stripModelPrefix(fm.id))
              );
              for (const candidate of sortModelsNewerFirst(unconfigured)) {
                if (retained.length >= 5) break;
                const isThinking = /o1|o3|r1|reasoning|thinking/i.test(candidate.id) ? 1 : (candidate.supports_thinking || 0);
                retained.push({
                  id: candidate.id,
                  name: candidate.name || candidate.id,
                  supports_thinking: isThinking,
                  input_price: candidate.input_price || 0.0,
                  cached_input_price: candidate.cached_input_price || 0.0,
                  output_price: candidate.output_price || 0.0,
                });
                retainedIds.add(candidate.id);
                cleanRetainedIds.add(stripModelPrefix(candidate.id));
              }
            }

            activeProv.models = retained.slice(0, 5);
            activeProv.fetched_model_ids = Array.from(fetchedIds);
            activeProv.has_api_key = true;
            activeProv.is_accessible = activeProv.is_enabled && activeProv.models.length > 0;
            activeProv.sync_status = res.sync_status || "ready";
            activeProv.sync_error = res.sync_error || "";

            if (activeProv.id === "gemini") {
              syncSearchModelsWithGeminiFetch(fetchedIds);
            }

            await callApi("save_settings", state.providers, state.settings, state.webSearchModels);
            if (state.settingsSnapshot) {
              state.settingsSnapshot.providers = JSON.parse(JSON.stringify(state.providers));
              state.settingsSnapshot.webSearchModels = JSON.parse(JSON.stringify(state.webSearchModels));
            }
            renderSettingsContent();
            syncAllPaneModelDropdowns();
            showNoticeDialog(`Synced ${activeProv.models.length} models for ${activeProv.name}!`);
          } else {
            activeProv.is_accessible = false;
            activeProv.fetched_model_ids = [];
            activeProv.sync_status = res?.sync_status || "error";
            activeProv.sync_error = res?.sync_error || "FETCH ERROR";
            syncAllPaneWebSearchButtons();
            renderSettingsContent();
            showNoticeDialog(`Could not fetch models: ${res?.error || "Endpoint unreachable or invalid API key"}`);
          }
        };
      }
    }

    lucide.createIcons();

    el.settingsTabContent.dataset.renderedTab = state.masterSettingsTab;
    el.settingsTabContent.dataset.renderedProvId =
      state.masterSettingsTab === "providers" ? (state.activeProviderId || state.providers[0]?.id || "") : "";

    const restoreScrollPositions = () => {
      if (modalBody && (sameProviderView || sameGeneralView || sameWebSearchView || (prevTab === "providers" && state.masterSettingsTab === "providers"))) {
        modalBody.scrollTop = prevModalScroll;
      }
      const newSubNav = el.settingsTabContent.querySelector(".providers-sub-nav");
      if (newSubNav && prevSubNavScroll > 0 && state.masterSettingsTab === "providers") {
        newSubNav.scrollTop = prevSubNavScroll;
      }
      const newModelsList = document.getElementById("prov-models-list") || document.getElementById("ws-models-list");
      if (newModelsList && (sameProviderView || sameWebSearchView) && prevModelsScroll > 0) {
        newModelsList.scrollTop = prevModelsScroll;
      }
    };

    restoreScrollPositions();
    requestAnimationFrame(restoreScrollPositions);
  }

  function saveGeneralTabInputs() {
    if (state.masterSettingsTab === "general") {
      const sysPrompt = document.getElementById("setting-global-system-prompt");
      if (sysPrompt) state.settings.global_system_prompt = sysPrompt.value.trim();

      const defProv = document.getElementById("setting-default-provider");
      if (defProv) state.settings.default_provider = defProv.value;

      const defThink = document.getElementById("setting-default-thinking");
      if (defThink && VALID_THINKING_LEVELS.has(defThink.value)) {
        state.settings.default_thinking = defThink.value;
      }

      const themeSelect = document.getElementById("setting-theme");
      if (themeSelect) applyTheme(themeSelect.value);
    }
  }

  function saveActiveProviderInputs() {
    if (state.masterSettingsTab === "providers" && state.activeProviderId) {
      const prov = state.providers.find((p) => p.id === state.activeProviderId);
      if (prov) {
        const nameInput = document.getElementById("prov-name-input");
        if (nameInput) {
          const cleaned = sanitizeProviderNameAlpha(nameInput.value);
          if (cleaned) prov.name = cleaned;
        }

        const enCheck = document.getElementById("prov-enabled-check");
        if (enCheck) prov.is_enabled = enCheck.checked;

        const bUrl = document.getElementById("prov-base-url-input");
        if (bUrl && !bUrl.disabled) prov.base_url = bUrl.value.trim();

        const aKey = document.getElementById("prov-api-key-input");
        if (aKey) {
          const typed = aKey.value.trim();
          if (typed) {
            prov.api_key = typed;
            prov.has_api_key = true;
            prov.clear_api_key = false;
          }
        }

        refreshProviderAccessibility(prov);
      }
    }
  }

  async function saveSettingsFromModal() {
    saveGeneralTabInputs();
    saveActiveProviderInputs();

    const validationErr = validateAllProviders();
    if (validationErr) {
      showNoticeDialog(validationErr);
      return;
    }

    const res = await callApi("save_settings", state.providers, state.settings, state.webSearchModels);
    if (res && res.error) {
      showNoticeDialog(res.error);
      return;
    }
    if (res && Array.isArray(res.providers)) {
      state.providers = res.providers;
    } else {
      state.providers.forEach((p) => {
        delete p.api_key;
        delete p.clear_api_key;
      });
    }
    if (res && Array.isArray(res.web_search_models)) {
      state.webSearchModels = normalizeWebSearchModelsClient(res.web_search_models);
    }
    closeSettingsModal(true);
    for (const tab of state.tabs) {
      if (!tab.conversationId) {
        if (!tab._userSelectedModel) {
          tab._preferredProviderId = state.settings.default_provider || "";
          tab._preferredModelId = state.settings.default_model || "";
        }
        if (!tab._userSelectedThinking) {
          tab._preferredThinkingLevel = getDefaultThinkingLevel();
        }
      }
      if (tab.conversationId) {
        try {
          const stats = await callApi("get_conversation_token_stats", tab.conversationId);
          if (stats) tab.tokenStats = stats;
        } catch (err) {
          console.warn("[saveSettingsFromModal] Failed refreshing token stats:", err);
        }
      }
    }
    renderActivePanes();
  }

  // Conversation Context Settings Modal
  async function openConvSettingsModal(tabOrId) {
    const tab = typeof tabOrId === "string"
      ? (state.tabs.find((t) => t.id === tabOrId) || state.tabs.find((t) => t.conversationId === tabOrId))
      : tabOrId;
    if (!tab) return;

    state.activeConvSettingsTabId = tab.id;

    let currentTitle = tab.title || "New Chat";
    let currentPrompt = tab.systemPrompt || "";
    let currentLimit = tab.maxContextMessages || 30;

    if (tab.conversationId) {
      try {
        const conv = await callApi("get_conversation", tab.conversationId);
        if (conv) {
          currentTitle = conv.title || currentTitle;
          currentPrompt = conv.system_prompt !== undefined ? conv.system_prompt : currentPrompt;
          currentLimit = conv.max_context_messages || currentLimit;
        }
      } catch (e) {
        console.warn("[openConvSettingsModal] Error fetching conv:", e);
      }
    }

    el.convTitleInput.value = currentTitle;
    el.convSystemPromptInput.value = currentPrompt;
    el.convContextSlider.value = currentLimit;
    el.convContextLimitVal.textContent = currentLimit;

    el.convContextSlider.oninput = () => {
      el.convContextLimitVal.textContent = el.convContextSlider.value;
    };

    el.convSettingsModal.classList.add("open");
    setTimeout(() => el.convTitleInput.focus(), 50);
  }

  async function saveConvSettings() {
    if (!state.activeConvSettingsTabId) return;
    const tab = state.tabs.find((t) => t.id === state.activeConvSettingsTabId);
    if (!tab) return;

    const newTitle = el.convTitleInput.value.trim() || "New Chat";
    const newPrompt = el.convSystemPromptInput.value.trim();
    const newLimit = parseInt(el.convContextSlider.value, 10) || 30;

    tab.title = newTitle;
    tab.systemPrompt = newPrompt;
    tab.maxContextMessages = newLimit;

    if (tab.conversationId) {
      // Conversation already exists in DB -> update it
      await callApi("rename_conversation", tab.conversationId, newTitle);
      await callApi("update_conversation_settings", tab.conversationId, newPrompt, newLimit);

      const conv = state.conversations.find((c) => c.id === tab.conversationId);
      if (conv) {
        conv.title = newTitle;
        conv.system_prompt = newPrompt;
        conv.max_context_messages = newLimit;
      }
    } else {
      // Conversation has not started yet: create it in DB with these settings.
      // NOTE: get_conversations() strictly filters conversations that have at least 1 assistant message,
      // so this conversation will NOT be shown on the left panel until generation begins.
      const conv = await callApi("create_conversation", newTitle, newPrompt, newLimit);
      if (conv && conv.id) {
        tab.conversationId = conv.id;
      }
    }

    persistTabsState();
    el.convSettingsModal.classList.remove("open");
    renderConversationsList();
    renderTabs();
    renderActivePanes();
  }

  // Export Modal
  function openExportModal(convId) {
    state.activeExportConvId = convId;
    el.exportModal.classList.add("open");
    lucide.createIcons();
  }

  async function downloadExport(format) {
    if (!state.activeExportConvId) return;
    const convId = state.activeExportConvId;
    el.exportModal.classList.remove("open");

    const res = await callApi("export_conversation", convId, format);
    if (!res) return;
    if (res.cancelled) return;
    if (res.error) {
      showNoticeDialog(res.error);
      return;
    }
    if (res.saved) {
      return;
    }
    if (res.content) {
      const ext = format === "json" ? "json" : "md";
      const defaultName = res.filename || `conversation_${convId.slice(0, 8)}.${ext}`;
      if (window.showSaveFilePicker) {
        try {
          const handle = await window.showSaveFilePicker({
            suggestedName: defaultName,
            types: [
              {
                description: format === "json" ? "JSON File" : "Markdown File",
                accept: format === "json" ? { "application/json": [".json"] } : { "text/markdown": [".md"] },
              },
            ],
          });
          const writable = await handle.createWritable();
          await writable.write(res.content);
          await writable.close();
          return;
        } catch (err) {
          if (err && err.name === "AbortError") return;
        }
      }
      const blob = new Blob([res.content], { type: format === "json" ? "application/json" : "text/markdown" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = defaultName;
      a.click();
      URL.revokeObjectURL(url);
    }
  }

  // Clipboard Helper with Reliable Fallback
  function copyText(text, btnElement) {
    function showFeedback() {
      if (btnElement) {
        const origHtml = btnElement.innerHTML;
        btnElement.innerHTML = '<i data-lucide="check" style="width: 11px; height: 11px; color: #34a853;"></i> <span style="color: #34a853;">Copied!</span>';
        lucide.createIcons();
        setTimeout(() => {
          btnElement.innerHTML = origHtml;
          lucide.createIcons();
        }, 1800);
      }
    }

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(showFeedback).catch(() => {
        fallbackCopy(text, showFeedback);
      });
    } else {
      fallbackCopy(text, showFeedback);
    }
  }

  function fallbackCopy(text, cb) {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      if (cb) cb();
    } catch (e) {
      console.warn("[fallbackCopy] Failed:", e);
    }
  }

  // HTML Escape Helper
  function escapeHtml(str) {
    if (!str) return "";
    return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
  }

  // Global Event Listeners & Shortcuts
  function setupEventListeners() {
    el.sidebarCollapseBtn.onclick = () => {
      el.sidebar.classList.add("collapsed");
      el.sidebarExpandBtn.style.display = "flex";
    };
    el.sidebarExpandBtn.onclick = () => {
      el.sidebar.classList.remove("collapsed");
      el.sidebarExpandBtn.style.display = "none";
    };

    el.newChatBtn.onclick = () => createNewTab();
    el.convSearchInput.oninput = () => renderConversationsList();
    el.themeToggleBtn.onclick = toggleTheme;
    el.globalSettingsBtn.onclick = openSettingsModal;

    el.newTabBtn.onclick = () => createNewTab();
    if (el.allTabsMenuBtn && el.allTabsPopover) {
      el.allTabsMenuBtn.onclick = (e) => {
        e.stopPropagation();
        const isOpen = el.allTabsPopover.classList.toggle("open");
        if (isOpen) {
          renderAllTabsPopover();
        }
      };
    }
    el.splitViewBtn.onclick = addSplitPane;

    if (el.cancelModelConfigBtn) {
      el.cancelModelConfigBtn.onclick = () => closeModelConfigModal(false);
    }
    if (el.saveModelConfigBtn) {
      el.saveModelConfigBtn.onclick = () => closeModelConfigModal(true);
    }

    // Delegated click listeners for code block copying, thinking accordions, and closing all-tabs menu
    document.addEventListener("click", (e) => {
      if (el.allTabsPopover && el.allTabsPopover.classList.contains("open")) {
        if (!e.target.closest("#all-tabs-dropdown-wrapper")) {
          el.allTabsPopover.classList.remove("open");
        }
      }

      const copyCodeBtn = e.target.closest(".copy-code-btn");
      if (copyCodeBtn) {
        const wrapper = copyCodeBtn.closest(".code-block-wrapper");
        if (wrapper) {
          const codeEl = wrapper.querySelector("code") || wrapper.querySelector("pre");
          if (codeEl) {
            copyText(codeEl.textContent || codeEl.innerText, copyCodeBtn);
            return;
          }
        }
      }

      const thinkHdr = e.target.closest(".thinking-accordion-header");
      if (thinkHdr) {
        const acc = thinkHdr.closest(".thinking-accordion");
        if (acc) {
          acc.classList.toggle("collapsed");
          return;
        }
      }

      const mdLink = e.target.closest(".assistant-markdown-content a[href]");
      if (mdLink) {
        isPageUnloading = true;
        persistTabsState();
      }
    });

    // Master Settings Tabs
    el.masterTabGeneral.onclick = () => {
      saveActiveProviderInputs();
      state.masterSettingsTab = "general";
      updateMasterTabsUI();
      renderSettingsContent();
    };

    el.masterTabProviders.onclick = () => {
      saveGeneralTabInputs();
      state.masterSettingsTab = "providers";
      updateMasterTabsUI();
      renderSettingsContent();
    };

    if (el.masterTabWebSearch) {
      el.masterTabWebSearch.onclick = () => {
        saveGeneralTabInputs();
        saveActiveProviderInputs();
        state.masterSettingsTab = "websearch";
        updateMasterTabsUI();
        renderSettingsContent();
      };
    }

    el.saveSettingsBtn.onclick = saveSettingsFromModal;
    document.querySelectorAll(".close-modal-btn").forEach((btn) => {
      btn.onclick = () => closeSettingsModal(false);
    });

    el.saveConvSettingsBtn.onclick = saveConvSettings;
    document.querySelectorAll(".close-conv-settings-btn").forEach((btn) => {
      btn.onclick = () => el.convSettingsModal.classList.remove("open");
    });

    el.exportMarkdownBtn.onclick = () => downloadExport("markdown");
    el.exportJsonBtn.onclick = () => downloadExport("json");
    document.querySelectorAll(".close-export-modal-btn").forEach((btn) => {
      btn.onclick = () => el.exportModal.classList.remove("open");
    });

    // Keyboard Shortcuts
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (el.appDialogModal && el.appDialogModal.classList.contains("open")) {
          el.appDialogCancelBtn.click();
          return;
        }
        if (el.modelConfigModal && el.modelConfigModal.classList.contains("open")) {
          closeModelConfigModal(false);
          return;
        }
        if (el.allTabsPopover && el.allTabsPopover.classList.contains("open")) {
          el.allTabsPopover.classList.remove("open");
          return;
        }
        if (el.settingsModal.classList.contains("open")) closeSettingsModal(false);
        el.convSettingsModal.classList.remove("open");
        el.exportModal.classList.remove("open");
      } else if (e.ctrlKey && e.key === "t") {
        e.preventDefault();
        createNewTab();
      } else if (e.ctrlKey && e.key === "w") {
        e.preventDefault();
        if (state.activeTabId) closeTab(state.activeTabId);
      } else if (e.ctrlKey && e.key === "\\") {
        e.preventDefault();
        addSplitPane();
      } else if (e.ctrlKey && e.key === ",") {
        e.preventDefault();
        openSettingsModal();
      }
    });
  }

  // Kickoff App
  initApp();
})();
