/**
 * Shared client state, DOM element registry, and model/provider helpers.
 */

export const THINKING_LEVELS = [
  { value: "none", label: "None" },
  { value: "minimal", label: "Minimal" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "XHigh" },
  { value: "max", label: "Max" },
];

export const VALID_THINKING_LEVELS = new Set(THINKING_LEVELS.map((t) => t.value));
export const PRIMARY_WEB_SEARCH_MODEL = "gemini-3.1-flash-lite";

export const stripModelPrefix = (id) => (id && id.startsWith("models/") ? id.slice(7) : id || "");

export function normalizeWebSearchModelsClient(list) {
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
}

export function isLoopbackBaseUrl(rawUrl) {
  const s = String(rawUrl || "").trim();
  if (!s) return false;
  try {
    const parsed = new URL(s);
    const host = (parsed.hostname || "").toLowerCase();
    return host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]";
  } catch (e) {
    return false;
  }
}

export function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

export function formatCost(cost) {
  const val = Number(cost) || 0;
  if (val === 0) return "$0.00";
  if (val < 0.0001) return "$" + val.toFixed(6);
  if (val < 0.01) return "$" + val.toFixed(4);
  return "$" + val.toFixed(2);
}

export function formatTokens(count) {
  if (!count) return "0";
  if (count >= 1000000) return (count / 1000000).toFixed(1) + "M";
  if (count >= 1000) return (count / 1000).toFixed(1) + "k";
  return count.toString();
}

export function generateClientStreamId() {
  return "stream_" + Date.now() + "_" + Math.random().toString(36).slice(2, 10);
}

export function renderIcons(root = document) {
  if (!window.lucide || !root) return;
  if (root !== document && (!root.querySelector || !root.querySelector("[data-lucide]"))) {
    return;
  }
  try {
    window.lucide.createIcons({ root });
  } catch (e) {
    window.lucide.createIcons();
  }
}

export const state = {
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
  recentlyDeletedConvIds: new Set(),
  tabs: [],
  activeTabId: null,
  splitTabIds: [],
  masterSettingsTab: "general",
  activeProviderId: null,
  activeConvSettingsTabId: null,
  activeExportConvId: null,
  googleSearchTotals: { month: "", monthly: 0, all_time: 0 },
  settingsBuffer: null,
};

export const el = {
  appContainer: document.getElementById("app-container"),
  sidebar: document.getElementById("sidebar"),
  sidebarCollapseBtn: document.getElementById("sidebar-collapse-btn"),
  sidebarExpandBtn: document.getElementById("sidebar-expand-btn"),
  newChatBtn: document.getElementById("new-chat-btn"),
  convSearchInput: document.getElementById("conv-search-input"),
  conversationsList: document.getElementById("conversations-list"),
  uploadChatBtn: document.getElementById("upload-chat-btn"),
  uploadChatFileInput: document.getElementById("upload-chat-file-input"),
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

// Cross-module UI callbacks so lower-level modules never need circular imports.
export const uiHooks = {
  scheduleNextEventPoll: null,
  renderConversationsList: null,
  openConvSettingsModal: null,
  openExportModal: null,
  saveGeneralTabInputs: null,
  saveActiveProviderInputs: null,
  renderSettingsContent: null,
  refreshProviderAccessibility: null,
};

export function getDefaultThinkingLevel() {
  const val = state.settings.default_thinking;
  return VALID_THINKING_LEVELS.has(val) ? val : "medium";
}

export function getVerifiedProviderModels(prov, limit = null) {
  if (!prov || !Array.isArray(prov.models)) return [];
  const fetchedClean = Array.isArray(prov.fetched_model_ids)
    ? new Set(prov.fetched_model_ids.map((id) => stripModelPrefix(id).toLowerCase()))
    : null;
  const filtered = prov.models.filter(
    (m) => m && m.id && (!fetchedClean || fetchedClean.has(stripModelPrefix(m.id).toLowerCase()))
  );
  return typeof limit === "number" ? filtered.slice(0, limit) : filtered;
}

export function getDropdownProviders() {
  return state.providers.filter((p) => {
    if (!p.is_enabled) return false;
    const hasKey = Boolean(p.has_api_key || (p.api_key && p.api_key.trim()));
    if (!hasKey) return false;
    const validModels = getVerifiedProviderModels(p);
    return p.is_accessible === true && validModels.length > 0;
  });
}

export function getWebSearchAvailability(geminiProvider = null, wsModelsList = null) {
  const gemini = geminiProvider || state.providers.find((p) => p.id === "gemini");
  const hasKey =
    gemini && !gemini.clear_api_key && Boolean(gemini.has_api_key || (gemini.api_key && gemini.api_key.trim()));
  if (!gemini || !hasKey) {
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
  const modelsSource = wsModelsList || state.webSearchModels;
  if ((Array.isArray(gemini.fetched_model_ids) && gemini.fetched_model_ids.length > 0) || status === "ready") {
    const fetchedClean = new Set((gemini.fetched_model_ids || []).map((id) => stripModelPrefix(id).toLowerCase()));
    const wsIds = normalizeWebSearchModelsClient(modelsSource).map((m) => stripModelPrefix(m.id).toLowerCase());
    const hasAnySearchModel = wsIds.some((id) => fetchedClean.has(id));
    if (!hasAnySearchModel) {
      return { available: false, reason: "INVALID MODEL" };
    }
  }
  return { available: true, reason: "Web Search (Google Search via configured Gemini search models)" };
}

export function resetTabStreamBuffers(tab) {
  if (!tab) return;
  tab.streamingContent = "";
  tab.streamingThinking = "";
  tab.hasAutoCollapsedThinking = false;
  tab.isActivelyThinking = false;
  tab.activeToolStatus = null;
}

export function createTabObject(overrides = {}) {
  const activeProviders = getDropdownProviders();
  const defProvider =
    activeProviders.find((p) => p.id === state.settings.default_provider) ||
    activeProviders[0] ||
    state.providers.find((p) => p.id === state.settings.default_provider) ||
    null;
  const defModelSetting = state.settings.default_model || "";
  const defModelObj =
    (defModelSetting ? defProvider?.models?.find((m) => m.id === defModelSetting) : null) ||
    defProvider?.models?.[0] ||
    null;

  const isUnconfiguredDraft = !overrides.conversationId && !overrides.userSelectedModel;
  const prefProvId = isUnconfiguredDraft
    ? (state.settings.default_provider || "")
    : (overrides.providerId || state.settings.default_provider || "");
  const prefModelId = isUnconfiguredDraft
    ? (defModelSetting || defModelObj?.id || "")
    : (overrides.modelId || defModelSetting || defModelObj?.id || "");
  const prefThinking =
    overrides.userSelectedThinking || (overrides.conversationId && VALID_THINKING_LEVELS.has(overrides.thinkingLevel))
      ? (overrides.thinkingLevel || getDefaultThinkingLevel())
      : getDefaultThinkingLevel();

  return {
    id: overrides.id || "tab_" + Date.now() + "_" + Math.random().toString(36).slice(2, 6),
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
    draftInput: overrides.draftInput || "",
    hasUnansweredPrompt: Boolean(overrides.hasUnansweredPrompt),
    pendingUserDraft: null,
    activeToolStatus: null,
    _streamGen: 0,
    isStreaming: false,
    activeStreamId: null,
    lastStreamError: null,
    streamingContent: "",
    streamingThinking: "",
    hasAutoCollapsedThinking: false,
    isActivelyThinking: false,
    tokenStats: null,
  };
}

export function sanitizeSplitTabIds() {
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

export function refreshPaneHeaderLabels() {
  if (!el.panesContainer) return;
  el.panesContainer.querySelectorAll(".chat-pane").forEach((paneEl) => {
    const paneTabId = paneEl.dataset.tabId;
    const selectEl = paneEl.querySelector(`#pane-tab-dropdown-${paneTabId}`);
    const labelEl = paneEl.querySelector(`#pane-tab-label-${paneTabId}`);
    if (!selectEl) return;
    Array.from(selectEl.options).forEach((opt) => {
      const t = state.tabs.find((item) => item.id === opt.value);
      if (!t) return;
      const c = state.conversations.find((item) => item.id === t.conversationId);
      opt.textContent = c ? c.title : (t.title || "New Chat");
    });
    if (labelEl) {
      const selOpt = selectEl.options[selectEl.selectedIndex];
      labelEl.textContent = selOpt ? selOpt.textContent : "";
    }
  });
}
