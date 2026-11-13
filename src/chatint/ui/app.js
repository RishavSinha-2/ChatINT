/**
 * ChatInt - Minimalist Multi-Tab Multi-Provider Desktop Client
 * Native ES Module Entrypoint (`R1`, `C1`, `C3`, `L3`, `NEW-L2`).
 */

import {
  callApi,
  callApiEnvelope,
  flushTabsState,
  isPageUnloadingState,
  isTabsStateDirty,
  persistTabsState,
  setPageUnloading,
  waitForPyWebView,
} from "./js/api.js";
import { applyTheme, copyText, showNoticeDialog, toggleTheme } from "./js/dialogs.js";
import {
  _isSafeUrl,
  _isTrustedAutoLoadImageUrl,
  _sanitizeCssStyle,
  renderMarkdownWithEnhancements,
  sanitizeMarkdownDom,
} from "./js/markdown.js";
import {
  addSplitPane,
  branchActiveTabToSplitPane,
  closeTab,
  createNewTab,
  disconnectPaneResizeObservers,
  renderActivePanes,
  renderAllTabsPopover,
  renderTabs,
} from "./js/panes.js";
import {
  closeModelConfigModal,
  closeSettingsModal,
  openSettingsModal,
  refreshProviderAccessibility,
  renderSettingsContent,
  saveActiveProviderInputs,
  saveGeneralTabInputs,
  saveSettingsFromModal,
  updateMasterTabsUI,
} from "./js/settings.js";
import {
  MAX_IMPORT_BYTES,
  downloadExport,
  handleImportedConversation,
  openConvSettingsModal,
  openExportModal,
  openUploadChat,
  renderConversationsList,
  saveConvSettings,
} from "./js/sidebar.js";
import {
  createTabObject,
  el,
  normalizeWebSearchModelsClient,
  renderIcons,
  state,
  uiHooks,
} from "./js/state.js";
import { onStreamEvent } from "./js/streaming.js";

let isPollingEvents = false;
let eventPollTimer = null;

function isAppActivelyBusy() {
  if (state.tabs.some((t) => t.isStreaming)) return true;
  if (state.providers.some((p) => p.sync_status === "checking")) return true;
  return false;
}

export function scheduleNextEventPoll(delayMs = null) {
  if (isPageUnloadingState()) return;
  if (eventPollTimer) {
    clearTimeout(eventPollTimer);
    eventPollTimer = null;
  }
  const nextDelay = typeof delayMs === "number" ? delayMs : (isAppActivelyBusy() ? 35 : 250);
  eventPollTimer = setTimeout(pollPendingEventsOnce, nextDelay);
}

async function pollPendingEventsOnce() {
  eventPollTimer = null;
  if (isPollingEvents || isPageUnloadingState()) {
    scheduleNextEventPoll();
    return;
  }
  isPollingEvents = true;
  try {
    const events = await callApi("get_pending_events");
    if (isPageUnloadingState()) return;
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
    scheduleNextEventPoll();
  }
}

function startEventPolling() {
  scheduleNextEventPoll(35);
}

// Register cross-module hooks
uiHooks.scheduleNextEventPoll = scheduleNextEventPoll;
uiHooks.renderConversationsList = renderConversationsList;
uiHooks.openConvSettingsModal = openConvSettingsModal;
uiHooks.openExportModal = openExportModal;
uiHooks.saveGeneralTabInputs = saveGeneralTabInputs;
uiHooks.saveActiveProviderInputs = saveActiveProviderInputs;
uiHooks.renderSettingsContent = renderSettingsContent;
uiHooks.refreshProviderAccessibility = refreshProviderAccessibility;

window.ChatIntUI = { onStreamEvent };
window.ChatInt = {
  state,
  el,
  callApi,
  callApiEnvelope,
  flushTabsState,
  persistTabsState,
  isTabsStateDirty,
  onStreamEvent,
  renderMarkdownWithEnhancements,
  sanitizeMarkdownDom,
  _isSafeUrl,
  _isTrustedAutoLoadImageUrl,
  _sanitizeCssStyle,
  createNewTab,
  closeTab,
  renderActivePanes,
  renderTabs,
  renderConversationsList,
};

async function bootstrapData() {
  try {
    const data = await callApi("get_initial_data");
    if (!data || data.error) return;

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

    // Restore tabs state while deduplicating `conversationId` across tabs (`NEW-L2`)
    const savedTabs = data.tabs_state;
    if (savedTabs && Array.isArray(savedTabs.tabs) && savedTabs.tabs.length > 0) {
      const seenConvIds = new Set();
      const restoredTabs = [];
      for (const rawTab of savedTabs.tabs) {
        const candidate = createTabObject(rawTab);
        if (candidate.conversationId) {
          if (seenConvIds.has(candidate.conversationId)) {
            continue;
          }
          seenConvIds.add(candidate.conversationId);
        }
        restoredTabs.push(candidate);
      }
      if (restoredTabs.length === 0) {
        restoredTabs.push(createTabObject());
      }
      state.tabs = restoredTabs;
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
      const initialTab = createTabObject();
      state.tabs = [initialTab];
      state.activeTabId = initialTab.id;
      state.splitTabIds = [initialTab.id];
      persistTabsState();
    }

    renderConversationsList();
    renderTabs();
    renderActivePanes();
    renderIcons(document);
  } catch (e) {
    console.error("[Bootstrap Error]", e);
  }
}

function handleMarkdownLinkNavigation(e) {
  const mdLink = e.target && e.target.closest
    ? e.target.closest(".assistant-markdown-content a[href], .user-bubble a[href]")
    : null;
  if (!mdLink) return false;

  const href = (mdLink.getAttribute("href") || "").trim();
  if (/^(?:https?:\/\/|mailto:)/i.test(href)) {
    e.preventDefault();
    callApi("open_external_url", href).catch(() => {});
  } else if (href.startsWith("#")) {
    // Allow normal left-click anchor jump, but block middle-click new-window navigation
    if (e.type === "auxclick") {
      e.preventDefault();
    }
  } else {
    e.preventDefault();
  }
  return true;
}

function importFileFromInput(file) {
  if (!file) return;
  if (file.size > MAX_IMPORT_BYTES) {
    showNoticeDialog("Import file exceeds the 25 MB size limit.");
    return;
  }
  const reader = new FileReader();
  reader.onload = async (event) => {
    const content = event.target.result;
    const res = await callApi("import_conversation_content", content, file.name);
    if (res && res.conversation) {
      handleImportedConversation(res.conversation);
    } else if (res && res.error) {
      showNoticeDialog(res.error);
    }
    if (el.uploadChatFileInput) el.uploadChatFileInput.value = "";
  };
  reader.readAsText(file);
}

function setupEventListeners() {
  // Prevent top-level drag-and-drop navigation in WebView2 (`C1` bullet 4)
  window.addEventListener("dragover", (e) => {
    e.preventDefault();
    if (e.dataTransfer) {
      const targetInSidebar = Boolean(e.target && e.target.closest && e.target.closest("#sidebar"));
      e.dataTransfer.dropEffect = targetInSidebar ? "copy" : "none";
    }
  });

  window.addEventListener("drop", (e) => {
    e.preventDefault();
    const targetInSidebar = Boolean(e.target && e.target.closest && e.target.closest("#sidebar"));
    if (!targetInSidebar || document.querySelector(".modal-overlay.open")) return;
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (file && /\.(?:json|md|markdown)$/i.test(file.name || "")) {
      importFileFromInput(file);
    }
  });

  window.addEventListener("beforeunload", () => {
    setPageUnloading(true);
    // Disconnect active pane `_resizeObserver` instances cleanly on teardown (`L1`)
    if (el.panesContainer) {
      el.panesContainer.querySelectorAll(".chat-pane").forEach((pane) => {
        if (pane._resizeObserver) {
          pane._resizeObserver.disconnect();
          pane._resizeObserver = null;
        }
      });
    }
    disconnectPaneResizeObservers(el.panesContainer);
    flushTabsState();
  });

  if (el.sidebarCollapseBtn) {
    el.sidebarCollapseBtn.onclick = () => {
      el.sidebar.classList.add("collapsed");
      el.sidebarExpandBtn.style.display = "flex";
    };
  }
  if (el.sidebarExpandBtn) {
    el.sidebarExpandBtn.onclick = () => {
      el.sidebar.classList.remove("collapsed");
      el.sidebarExpandBtn.style.display = "none";
    };
  }

  if (el.newChatBtn) el.newChatBtn.onclick = () => createNewTab();
  if (el.convSearchInput) el.convSearchInput.oninput = () => renderConversationsList();
  if (el.uploadChatBtn) el.uploadChatBtn.onclick = openUploadChat;
  if (el.uploadChatFileInput) {
    el.uploadChatFileInput.onchange = (e) => {
      const file = e.target.files && e.target.files[0];
      importFileFromInput(file);
    };
  }
  if (el.themeToggleBtn) el.themeToggleBtn.onclick = toggleTheme;
  if (el.globalSettingsBtn) el.globalSettingsBtn.onclick = openSettingsModal;

  if (el.newTabBtn) el.newTabBtn.onclick = () => createNewTab();
  if (el.allTabsMenuBtn && el.allTabsPopover) {
    el.allTabsMenuBtn.onclick = (e) => {
      e.stopPropagation();
      const isOpen = el.allTabsPopover.classList.toggle("open");
      if (isOpen) {
        renderAllTabsPopover();
      }
    };
  }
  if (el.splitViewBtn) el.splitViewBtn.onclick = addSplitPane;

  if (el.cancelModelConfigBtn) {
    el.cancelModelConfigBtn.onclick = () => closeModelConfigModal(false);
  }
  if (el.saveModelConfigBtn) {
    el.saveModelConfigBtn.onclick = () => closeModelConfigModal(true);
  }

  // Delegated click listeners: popover close, code copy, thinking accordion, click-to-load external images, & external links
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

    const blockedImgBtn = e.target.closest(".blocked-external-image");
    if (blockedImgBtn) {
      const rawSrc = (blockedImgBtn.getAttribute("data-external-src") || "").trim();
      const rawAlt = blockedImgBtn.getAttribute("data-external-alt") || "";
      if (_isSafeUrl(rawSrc, false)) {
        const img = document.createElement("img");
        img.setAttribute("src", rawSrc);
        if (rawAlt) img.setAttribute("alt", rawAlt);
        img.setAttribute("referrerpolicy", "no-referrer");
        img.setAttribute("loading", "lazy");
        blockedImgBtn.replaceWith(img);
      }
      return;
    }

    handleMarkdownLinkNavigation(e);
  });

  // Intercept middle-click (`auxclick`) on links so WebView2 never navigates or opens raw local windows (`C1`)
  document.addEventListener("auxclick", (e) => {
    if (e.button === 1) {
      handleMarkdownLinkNavigation(e);
    }
  });

  // Master Settings Tabs
  if (el.masterTabGeneral) {
    el.masterTabGeneral.onclick = () => {
      saveActiveProviderInputs();
      if (state.settingsBuffer) state.settingsBuffer.masterSettingsTab = "general";
      state.masterSettingsTab = "general";
      updateMasterTabsUI();
      renderSettingsContent();
    };
  }

  if (el.masterTabProviders) {
    el.masterTabProviders.onclick = () => {
      saveGeneralTabInputs();
      if (state.settingsBuffer) state.settingsBuffer.masterSettingsTab = "providers";
      state.masterSettingsTab = "providers";
      updateMasterTabsUI();
      renderSettingsContent();
    };
  }

  if (el.masterTabWebSearch) {
    el.masterTabWebSearch.onclick = () => {
      saveGeneralTabInputs();
      saveActiveProviderInputs();
      if (state.settingsBuffer) state.settingsBuffer.masterSettingsTab = "websearch";
      state.masterSettingsTab = "websearch";
      updateMasterTabsUI();
      renderSettingsContent();
    };
  }

  if (el.saveSettingsBtn) el.saveSettingsBtn.onclick = saveSettingsFromModal;
  document.querySelectorAll(".close-modal-btn").forEach((btn) => {
    btn.onclick = () => closeSettingsModal(false);
  });
  if (el.settingsModal) {
    el.settingsModal.onclick = (e) => {
      if (e.target === el.settingsModal) {
        closeSettingsModal(false);
      }
    };
  }

  if (el.saveConvSettingsBtn) el.saveConvSettingsBtn.onclick = saveConvSettings;
  document.querySelectorAll(".close-conv-settings-btn").forEach((btn) => {
    btn.onclick = () => el.convSettingsModal.classList.remove("open");
  });

  if (el.exportMarkdownBtn) el.exportMarkdownBtn.onclick = () => downloadExport("markdown");
  if (el.exportJsonBtn) el.exportJsonBtn.onclick = () => downloadExport("json");
  document.querySelectorAll(".close-export-modal-btn").forEach((btn) => {
    btn.onclick = () => el.exportModal.classList.remove("open");
  });

  // Global Keyboard Shortcuts (`NEW-L2`)
  window.addEventListener("keydown", (e) => {
    if (e.isComposing) return;

    if (e.key === "Escape") {
      // Do NOT close modals when Escape was pressed on a `<select>` dropdown
      if (e.target && e.target.tagName === "SELECT") {
        return;
      }
      // Cancel active inline user-message edit first if focused inside `.user-bubble.editing`
      const editBubble = e.target && e.target.closest ? e.target.closest(".user-bubble.editing") : null;
      if (editBubble && typeof editBubble._cancelInlineEdit === "function") {
        e.preventDefault();
        editBubble._cancelInlineEdit();
        return;
      }
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
      if (el.settingsModal && el.settingsModal.classList.contains("open")) {
        closeSettingsModal(false);
        return;
      }
      if (el.convSettingsModal && el.convSettingsModal.classList.contains("open")) {
        el.convSettingsModal.classList.remove("open");
        return;
      }
      if (el.exportModal && el.exportModal.classList.contains("open")) {
        el.exportModal.classList.remove("open");
      }
    } else if (!document.querySelector(".modal-overlay.open") && !e.altKey) {
      const keyLower = String(e.key || "").toLowerCase();
      if (e.ctrlKey && e.shiftKey && (e.key === "=" || e.key === "+" || e.code === "Equal")) {
        e.preventDefault();
        branchActiveTabToSplitPane();
      } else if (e.ctrlKey && !e.shiftKey && keyLower === "t") {
        e.preventDefault();
        createNewTab();
      } else if (e.ctrlKey && !e.shiftKey && keyLower === "w") {
        e.preventDefault();
        if (state.activeTabId) closeTab(state.activeTabId);
      } else if (e.ctrlKey && !e.shiftKey && e.key === "\\") {
        e.preventDefault();
        addSplitPane();
      } else if (e.ctrlKey && !e.shiftKey && e.key === ",") {
        e.preventDefault();
        openSettingsModal();
      }
    }
  });
}

async function initApp() {
  renderIcons(document);
  setupEventListeners();
  await waitForPyWebView();
  await bootstrapData();
  startEventPolling();
}

initApp();
