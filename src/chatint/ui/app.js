/**
 * ChatInt - Minimalist Multi-Tab Multi-Provider Desktop Client
 */

(function () {
  // Global State
  const state = {
    providers: [],
    settings: {
      theme: "dark",
      global_system_prompt: "You are a helpful, brilliant, and precise assistant.",
      default_provider: "openai",
      default_model: "gpt-4o",
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
  function startEventPolling() {
    setInterval(async () => {
      if (isPollingEvents) return;
      isPollingEvents = true;
      try {
        const events = await callApi("get_pending_events");
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

        applyTheme(state.settings.theme || "dark");

        if (state.providers.length > 0) {
          state.activeProviderId = state.providers[0].id;
        }

        // Restore tabs state
        const savedTabs = data.tabs_state;
        if (savedTabs && Array.isArray(savedTabs.tabs) && savedTabs.tabs.length > 0) {
          state.tabs = savedTabs.tabs.map((t) => ({
            id: t.id || "tab_" + Date.now() + "_" + Math.random().toString(36).substr(2, 4),
            conversationId: t.conversationId || null,
            title: t.title || "New Chat",
            systemPrompt: t.systemPrompt || "",
            maxContextMessages: t.maxContextMessages || 30,
            providerId: t.providerId || "",
            modelId: t.modelId || "",
            thinkingLevel: t.thinkingLevel || "none",
            isStreaming: false,
            streamingContent: "",
            streamingThinking: "",
            hasAutoCollapsedThinking: false,
            tokenStats: null,
          }));
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
          const activeProviders = getDropdownProviders();
          const defProvider = activeProviders.find((p) => p.id === state.settings.default_provider) || activeProviders[0] || null;
          const defModelObj = defProvider?.models?.find((m) => m.id === state.settings.default_model) || defProvider?.models?.[0] || null;
          const defModel = defModelObj?.id || "";
          const supportsThinking = defModelObj?.supports_thinking ? "medium" : "none";

          const initialTab = {
            id: "tab_" + Date.now() + "_" + Math.random().toString(36).substr(2, 4),
            conversationId: null,
            title: "New Chat",
            systemPrompt: "",
            maxContextMessages: 30,
            providerId: defProvider ? defProvider.id : "",
            modelId: defModel,
            thinkingLevel: supportsThinking,
            isStreaming: false,
            streamingContent: "",
            streamingThinking: "",
            hasAutoCollapsedThinking: false,
            tokenStats: null,
          };
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
        providerId: t.providerId,
        modelId: t.modelId,
        thinkingLevel: t.thinkingLevel,
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

  // Tab Operations
  async function createNewTab(conversationId = null, addToSplit = false) {
    const activeProviders = getDropdownProviders();
    const defProvider = activeProviders.find((p) => p.id === state.settings.default_provider) || activeProviders[0] || null;
    const defModelObj = defProvider?.models?.find((m) => m.id === state.settings.default_model) || defProvider?.models?.[0] || null;
    const defModel = defModelObj?.id || "";
    const supportsThinking = defModelObj?.supports_thinking ? "medium" : "none";

    let title = "New Chat";
    let systemPrompt = "";
    let maxContextMessages = 30;
    if (conversationId) {
      const existing = state.conversations.find((c) => c.id === conversationId);
      if (existing) {
        title = existing.title;
        systemPrompt = existing.system_prompt || "";
        maxContextMessages = existing.max_context_messages || 30;
      }
    }

    const prevActiveTabId = state.activeTabId;
    const newTab = {
      id: "tab_" + Date.now() + "_" + Math.random().toString(36).substr(2, 4),
      conversationId: conversationId, // null for draft chats until first prompt is answered or settings saved
      title: title,
      systemPrompt: systemPrompt,
      maxContextMessages: maxContextMessages,
      providerId: defProvider ? defProvider.id : "",
      modelId: defModel,
      thinkingLevel: supportsThinking,
      isStreaming: false,
      streamingContent: "",
      streamingThinking: "",
      hasAutoCollapsedThinking: false,
      tokenStats: null,
    };

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
              <option value="none">Thinking: None</option>
              <option value="minimal">Thinking: Minimal</option>
              <option value="low">Thinking: Low</option>
              <option value="medium">Thinking: Medium</option>
              <option value="high">Thinking: High</option>
              <option value="xhigh">Thinking: XHigh</option>
              <option value="max">Thinking: Max</option>
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
              <span>Press Enter to send</span>
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
      tab.providerId = "";
      tab.modelId = "";
      tab.thinkingLevel = "none";
      thinkingWrapper.style.display = "none";
      syncLabels();
      return;
    } else {
      let matched = false;
      activeProviders.forEach((prov) => {
        const fetchedIds = Array.isArray(prov.fetched_model_ids) ? new Set(prov.fetched_model_ids) : null;
        const verifiedModels = (prov.models || []).filter((m) => !fetchedIds || fetchedIds.has(m.id)).slice(0, 5);
        if (verifiedModels.length === 0) return;

        const optgroup = document.createElement("optgroup");
        optgroup.label = prov.name;
        verifiedModels.forEach((m) => {
          const opt = document.createElement("option");
          opt.value = `${prov.id}::${m.id}`;
          opt.textContent = m.name || m.id;
          if (prov.id === tab.providerId && m.id === tab.modelId) {
            opt.selected = true;
            matched = true;
          }
          optgroup.appendChild(opt);
        });
        modelDropdown.appendChild(optgroup);
      });

      if (!matched) {
        const fallbackProv = activeProviders.find((p) => p.id === state.settings.default_provider) || activeProviders[0];
        const fetchedIds = Array.isArray(fallbackProv?.fetched_model_ids) ? new Set(fallbackProv.fetched_model_ids) : null;
        const verifiedModels = (fallbackProv?.models || []).filter((m) => !fetchedIds || fetchedIds.has(m.id)).slice(0, 5);
        const fallbackModel = verifiedModels.find((m) => m.id === state.settings.default_model) || verifiedModels[0];
        if (fallbackProv && fallbackModel) {
          tab.providerId = fallbackProv.id;
          tab.modelId = fallbackModel.id;
          modelDropdown.value = `${tab.providerId}::${tab.modelId}`;
        } else {
          tab.providerId = "";
          tab.modelId = "";
        }
      }
    }

    const prov = state.providers.find((p) => p.id === tab.providerId);
    const mod = prov?.models?.find((m) => m.id === tab.modelId);
    const validEfforts = new Set(["none", "minimal", "low", "medium", "high", "xhigh", "max"]);
    if (mod && mod.supports_thinking) {
      thinkingWrapper.style.display = "flex";
      thinkingDropdown.value = validEfforts.has(tab.thinkingLevel) ? tab.thinkingLevel : "medium";
      tab.thinkingLevel = thinkingDropdown.value;
    } else {
      thinkingWrapper.style.display = "none";
      tab.thinkingLevel = "none";
    }
    syncLabels();

    modelDropdown.onchange = () => {
      const val = modelDropdown.value;
      if (!val) return;
      const [provId, mId] = val.split("::");
      tab.providerId = provId;
      tab.modelId = mId;

      const p = state.providers.find((item) => item.id === tab.providerId);
      const m = p?.models?.find((item) => item.id === tab.modelId);
      if (m && m.supports_thinking) {
        thinkingWrapper.style.display = "flex";
        thinkingDropdown.value = validEfforts.has(tab.thinkingLevel) ? tab.thinkingLevel : "medium";
        tab.thinkingLevel = thinkingDropdown.value;
      } else {
        thinkingWrapper.style.display = "none";
        tab.thinkingLevel = "none";
      }
      syncLabels();
      persistTabsState();
    };

    thinkingDropdown.onchange = () => {
      tab.thinkingLevel = thinkingDropdown.value;
      syncLabels();
      persistTabsState();
    };
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

    // 1. Populate Model Dropdown and Thinking selector
    updatePaneModelDropdown(pane, tab);

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

        const res = await callApi(
          "regenerate_message",
          tab.id,
          tab.conversationId,
          msg.id,
          tab.providerId,
          tab.modelId,
          tab.thinkingLevel
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

      const res = await callApi(
        "edit_message",
        tab.id,
        tab.conversationId,
        msg.id,
        newText,
        tab.providerId,
        tab.modelId,
        tab.thinkingLevel
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

    // Toggle button state to streaming
    tab.isStreaming = true;
    const sendBtn = pane.querySelector(`#send-btn-${tab.id}`);
    const stopBtn = pane.querySelector(`#stop-btn-${tab.id}`);
    if (sendBtn) sendBtn.style.display = "none";
    if (stopBtn) stopBtn.style.display = "flex";

    // Call backend send_message
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
      tab.maxContextMessages || 30
    );
    if (res && res.conversation_id) {
      tab.conversationId = res.conversation_id;
    }
    if (res && res.user_message && res.user_message.id) {
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

    if (!stats || !stats.per_model || stats.per_model.length === 0) {
      chipsEl.innerHTML = '<span style="color: var(--text-muted);">Tokens: 0 in · 0 out · $0.00</span>';
      grandTotalEl.textContent = "0 total · Est. $0.00";
      detailsEl.innerHTML = '<span style="color: var(--text-muted);">No tokens recorded yet for this chat.</span>';
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

    const gt = stats.grand_total;
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
    `;

    summaryEl.onclick = () => {
      barEl.classList.toggle("expanded");
    };
  }

  // Stream Event Handler (Dispatched from Python)
  window.ChatIntUI = {
    onStreamEvent: function (event) {
      if (event.type === "provider_models_updated") {
        const updated = event.provider;
        if (el.settingsModal.classList.contains("open")) {
          saveGeneralTabInputs();
          saveActiveProviderInputs();
        }
        const idx = state.providers.findIndex((p) => p.id === updated.id);
        if (idx !== -1) {
          state.providers[idx] = updated;
        } else {
          state.providers.push(updated);
        }

        // Live update model dropdowns across all visible panes
        el.panesContainer.querySelectorAll(".chat-pane").forEach((paneEl) => {
          const tabId = paneEl.dataset.tabId;
          const targetTab = state.tabs.find((t) => t.id === tabId);
          if (targetTab) {
            updatePaneModelDropdown(paneEl, targetTab);
          }
        });

        // Update settings modal view if open and user is not actively typing in an input
        if (el.settingsModal.classList.contains("open")) {
          const activeEl = document.activeElement;
          const isTypingInModal = activeEl && el.settingsModal.contains(activeEl) && (activeEl.tagName === "INPUT" || activeEl.tagName === "TEXTAREA");
          if (!isTypingInModal) {
            renderSettingsContent();
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
            renderTokenStats(pane, tab.id, event.token_stats);
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
              if (prevMsgId && !prevMsgId.startsWith("user_temp_") && tab.conversationId) {
                tab.isStreaming = true;
                if (sendBtn) sendBtn.style.display = "none";
                if (stopBtn) stopBtn.style.display = "flex";
                await callApi(
                  "edit_message",
                  tab.id,
                  tab.conversationId,
                  prevMsgId,
                  prevText,
                  tab.providerId,
                  tab.modelId,
                  tab.thinkingLevel
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

  // Settings Modal Management (General & Providers only)
  function openSettingsModal() {
    state.settingsSnapshot = JSON.parse(
      JSON.stringify({
        providers: state.providers,
        settings: state.settings,
      })
    );
    updateMasterTabsUI();
    renderSettingsContent();
    el.settingsModal.classList.add("open");
    lucide.createIcons();
  }

  function closeSettingsModal(saved = false) {
    if (!saved && state.settingsSnapshot) {
      state.providers = state.settingsSnapshot.providers;
      state.settings = state.settingsSnapshot.settings;
      applyTheme(state.settings.theme || "dark");
    }
    state.settingsSnapshot = null;
    el.settingsModal.classList.remove("open");
  }

  function updateMasterTabsUI() {
    if (state.masterSettingsTab === "general") {
      el.masterTabGeneral.classList.add("active");
      el.masterTabProviders.classList.remove("active");
    } else {
      el.masterTabGeneral.classList.remove("active");
      el.masterTabProviders.classList.add("active");
    }
  }

  // Model Pricing & Thinking Configuration Sub-Modal
  let activeConfigModelRef = null;

  function openModelConfigModal(prov, modelObj) {
    activeConfigModelRef = { prov, modelObj };
    el.modelConfigTitle.textContent = `${modelObj.name || modelObj.id}`;
    el.modelInputPrice.value = Number(modelObj.input_price || 0);
    el.modelCachedPrice.value = Number(modelObj.cached_input_price || 0);
    el.modelOutputPrice.value = Number(modelObj.output_price || 0);
    el.modelSupportsThinkingCheck.checked = Boolean(modelObj.supports_thinking);
    el.modelConfigModal.classList.add("open");
  }

  function closeModelConfigModal(save = false) {
    if (save && activeConfigModelRef) {
      const { modelObj } = activeConfigModelRef;
      const inVal = parseFloat(el.modelInputPrice.value);
      const cacheVal = parseFloat(el.modelCachedPrice.value);
      const outVal = parseFloat(el.modelOutputPrice.value);
      modelObj.input_price = !isNaN(inVal) && inVal >= 0 ? inVal : 0.0;
      modelObj.cached_input_price = !isNaN(cacheVal) && cacheVal >= 0 ? cacheVal : 0.0;
      modelObj.output_price = !isNaN(outVal) && outVal >= 0 ? outVal : 0.0;
      modelObj.supports_thinking = el.modelSupportsThinkingCheck.checked ? 1 : 0;
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

  function refreshProviderAccessibility(prov) {
    if (!prov) return;
    const typedKey = (prov.api_key || "").trim();
    const hasCredentials = prov.clear_api_key ? false : Boolean(typedKey || prov.has_api_key);
    const fetchedIds = Array.isArray(prov.fetched_model_ids) ? new Set(prov.fetched_model_ids) : new Set();
    const verifiedModels = Array.isArray(prov.models) ? prov.models.filter((m) => fetchedIds.has(m.id)) : [];
    if (!hasCredentials) {
      prov.models = [];
      prov.fetched_model_ids = [];
      prov.has_api_key = false;
      prov.is_accessible = false;
      return;
    }
    prov.has_api_key = true;
    prov.models = verifiedModels;
    prov.is_accessible = Boolean(prov.is_enabled && hasCredentials && verifiedModels.length > 0);
  }

  function renderSettingsContent() {
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
          <label class="settings-label">Appearance Theme</label>
          <select class="settings-input" id="setting-theme">
            <option value="dark" ${state.settings.theme === "dark" ? "selected" : ""}>Dark Theme</option>
            <option value="light" ${state.settings.theme === "light" ? "selected" : ""}>Light Theme</option>
          </select>
        </div>
      `;
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
          ${p.is_accessible ? '<span style="color: #34a853; font-size: 10px;">●</span>' : ""}
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

        detailHtml = `
          <div class="provider-detail-pane">
            <div style="display: flex; align-items: center; justify-content: space-between;">
              <div style="display: flex; align-items: center; gap: 8px;">
                <input type="text" class="settings-input" id="prov-name-input" value="${escapeHtml(activeProv.name)}" ${isNativeSdkProv ? "disabled title=\"Built-in Native SDK Provider\"" : "title=\"English letters only (A-Z, a-z)\""} style="font-weight: 600; width: 200px;">
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
                <button class="btn-secondary fetch-models-btn" style="padding: 2px 8px; font-size: 11px; display: flex; align-items: center; gap: 4px;">
                  <i data-lucide="refresh-cw" style="width: 11px; height: 11px;"></i>
                  <span>Fetch from Endpoint</span>
                </button>
              </div>

              <div class="models-manager-list" id="prov-models-list">
                ${(activeProv.models || [])
                  .map((m) => {
                    const inP = Number(m.input_price || 0);
                    const cP = Number(m.cached_input_price || 0);
                    const outP = Number(m.output_price || 0);
                    const thinkTag = m.supports_thinking ? " · Thinking" : "";
                    return `
                  <div class="model-row-item">
                    <div style="display: flex; flex-direction: column; min-width: 0;">
                      <span style="font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(m.name || m.id)}</span>
                      <span style="color: var(--text-muted); font-size: 10.5px;">$${inP}/M in · $${cP}/M cached · $${outP}/M out${thinkTag}</span>
                    </div>
                    <div style="display: flex; align-items: center; gap: 6px; flex-shrink: 0;">
                      <button class="btn-secondary model-config-btn" data-model-id="${escapeHtml(m.id)}" style="padding: 3px 8px; font-size: 11px; display: flex; align-items: center; gap: 4px;" title="Configure Pricing & Thinking">
                        <i data-lucide="sliders-horizontal" style="width: 11px; height: 11px;"></i>
                        <span>Pricing &amp; Thinking</span>
                      </button>
                      <button class="conv-action-btn delete-model-btn" data-model-id="${escapeHtml(m.id)}" title="Remove Model">
                        <i data-lucide="trash-2" style="width: 12px; height: 12px;"></i>
                      </button>
                    </div>
                  </div>
                `;
                  })
                  .join("")}
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
          <div style="flex: 1;">${detailHtml}</div>
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
          refreshProviderAccessibility(activeProv);
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

      // Delete Model
      el.settingsTabContent.querySelectorAll(".delete-model-btn").forEach((btn) => {
        btn.onclick = () => {
          saveActiveProviderInputs();
          const mid = btn.dataset.modelId;
          activeProv.models = activeProv.models.filter((m) => m.id !== mid);
          refreshProviderAccessibility(activeProv);
          renderSettingsContent();
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
            el.panesContainer.querySelectorAll(".chat-pane").forEach((paneEl) => {
              const tabId = paneEl.dataset.tabId;
              const targetTab = state.tabs.find((t) => t.id === tabId);
              if (targetTab) {
                updatePaneModelDropdown(paneEl, targetTab);
              }
            });
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
            const retained = current.filter((m) => fetchedIds.has(m.id));
            const retainedIds = new Set(retained.map((m) => m.id));

            // 2. Add new models until 5 with $0 pricing and thinking unchecked
            for (const fm of fetched) {
              if (retained.length >= 5) break;
              if (!retainedIds.has(fm.id)) {
                retained.push({
                  id: fm.id,
                  name: fm.name || fm.id,
                  supports_thinking: 0,
                  input_price: 0.0,
                  cached_input_price: 0.0,
                  output_price: 0.0,
                });
                retainedIds.add(fm.id);
              }
            }

            activeProv.models = retained.slice(0, 5);
            activeProv.fetched_model_ids = Array.from(fetchedIds);
            activeProv.has_api_key = true;
            activeProv.is_accessible = true;
            delete activeProv.api_key;
            delete activeProv.clear_api_key;

            await callApi("save_settings", state.providers, state.settings);
            if (state.settingsSnapshot) {
              state.settingsSnapshot.providers = JSON.parse(JSON.stringify(state.providers));
            }
            renderSettingsContent();
            el.panesContainer.querySelectorAll(".chat-pane").forEach((paneEl) => {
              const tabId = paneEl.dataset.tabId;
              const targetTab = state.tabs.find((t) => t.id === tabId);
              if (targetTab) {
                updatePaneModelDropdown(paneEl, targetTab);
              }
            });
            showNoticeDialog(`Synced ${activeProv.models.length} models for ${activeProv.name}!`);
          } else {
            activeProv.models = [];
            activeProv.fetched_model_ids = [];
            activeProv.is_accessible = false;
            await callApi("save_settings", state.providers, state.settings);
            renderSettingsContent();
            el.panesContainer.querySelectorAll(".chat-pane").forEach((paneEl) => {
              const tabId = paneEl.dataset.tabId;
              const targetTab = state.tabs.find((t) => t.id === tabId);
              if (targetTab) {
                updatePaneModelDropdown(paneEl, targetTab);
              }
            });
            showNoticeDialog(`Could not fetch models: ${res?.error || "Endpoint unreachable or invalid API key"}`);
          }
        };
      }
    }

    lucide.createIcons();
  }

  function saveGeneralTabInputs() {
    if (state.masterSettingsTab === "general") {
      const sysPrompt = document.getElementById("setting-global-system-prompt");
      if (sysPrompt) state.settings.global_system_prompt = sysPrompt.value.trim();

      const defProv = document.getElementById("setting-default-provider");
      if (defProv) state.settings.default_provider = defProv.value;

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

    const res = await callApi("save_settings", state.providers, state.settings);
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
    closeSettingsModal(true);
    for (const tab of state.tabs) {
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
