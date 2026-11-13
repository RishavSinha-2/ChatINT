/**
 * Multi-tab bar, up-to-5 split-pane layout, live streaming row renderer,
 * message actions (Send, Retry, Regenerate, Inline Edit), and token stats footer.
 */

import { callApi, persistTabsState } from "./api.js";
import { copyText, showConfirmDialog, showNoticeDialog } from "./dialogs.js";
import { renderMarkdownWithEnhancements } from "./markdown.js";
import {
  THINKING_LEVELS,
  VALID_THINKING_LEVELS,
  createTabObject,
  el,
  escapeHtml,
  formatCost,
  formatTokens,
  generateClientStreamId,
  getDefaultThinkingLevel,
  getDropdownProviders,
  getVerifiedProviderModels,
  getWebSearchAvailability,
  refreshPaneHeaderLabels,
  renderIcons,
  resetTabStreamBuffers,
  sanitizeSplitTabIds,
  state,
  uiHooks,
} from "./state.js";

export const pendingStreamRenderTabs = new Set();
let streamRenderRaf = null;

export function disconnectPaneResizeObservers(root = el.panesContainer) {
  if (!root) return;
  root.querySelectorAll(".chat-pane").forEach((oldPane) => {
    if (oldPane._resizeObserver) {
      oldPane._resizeObserver.disconnect();
      oldPane._resizeObserver = null;
    }
  });
}

export function clearWelcomeAndStaleBanners(messagesContainer) {
  if (!messagesContainer) return;
  messagesContainer.querySelectorAll(".welcome-placeholder").forEach((node) => node.remove());
  messagesContainer.querySelectorAll(".stream-error-banner").forEach((banner) => {
    const parentRow = banner.parentElement;
    banner.remove();
    if (parentRow && parentRow.classList.contains("message-row") && parentRow.children.length === 0) {
      parentRow.remove();
    }
  });
}

function removeDownstreamSiblings(anchorRow, preserveLiveId = null, removeAnchor = false) {
  if (!anchorRow || !anchorRow.parentElement) return;
  let cursor = removeAnchor ? anchorRow : anchorRow.nextElementSibling;
  while (cursor) {
    const next = cursor.nextElementSibling;
    if (!preserveLiveId || cursor.id !== preserveLiveId) {
      cursor.remove();
    }
    cursor = next;
  }
}

export function focusTabInUI(tabId) {
  if (state.activeTabId === tabId) return;
  state.activeTabId = tabId;
  persistTabsState();
  renderTabs();
  if (uiHooks.renderConversationsList) uiHooks.renderConversationsList();
  el.panesContainer.querySelectorAll(".chat-pane").forEach((p) => {
    p.classList.toggle("active-pane", p.dataset.tabId === tabId);
  });
}

export async function createNewTab(conversationId = null, addToSplit = false) {
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
    const activeSlotIdx = state.splitTabIds.indexOf(prevActiveTabId);
    if (activeSlotIdx !== -1) {
      state.splitTabIds[activeSlotIdx] = newTab.id;
    } else {
      state.splitTabIds[state.splitTabIds.length - 1] = newTab.id;
    }
  }

  persistTabsState();
  renderTabs();
  if (uiHooks.renderConversationsList) uiHooks.renderConversationsList();
  renderActivePanes();
}

export async function closeTab(tabId, { skipConfirm = false } = {}) {
  let index = state.tabs.findIndex((t) => t.id === tabId);
  if (index === -1) return;

  const candidateTab = state.tabs[index];
  const isUnlistedDraftConv = Boolean(
    candidateTab.conversationId && !state.conversations.some((c) => c.id === candidateTab.conversationId)
  );
  const hasUnsentInput = Boolean((candidateTab.draftInput || "").trim());
  let hasDraftWork =
    hasUnsentInput ||
    (isUnlistedDraftConv && (candidateTab.hasUnansweredPrompt || Boolean((candidateTab.systemPrompt || "").trim()))) ||
    (!candidateTab.conversationId && Boolean(candidateTab.pendingUserDraft));

  if (!skipConfirm && !hasDraftWork && isUnlistedDraftConv) {
    try {
      const draftConv = await callApi("get_conversation", candidateTab.conversationId);
      if (
        draftConv &&
        ((Array.isArray(draftConv.messages) && draftConv.messages.length > 0) ||
          Boolean((draftConv.system_prompt || "").trim()))
      ) {
        hasDraftWork = true;
      }
    } catch (_) {
      // Ignore lookup errors when deciding confirmation
    }
    index = state.tabs.findIndex((t) => t.id === tabId);
    if (index === -1) return;
  }

  if (!skipConfirm && hasDraftWork) {
    const confirmed = await showConfirmDialog(
      "Close this tab and discard your unsent or unanswered draft?",
      "Discard & Close",
      true
    );
    if (!confirmed) return;
    index = state.tabs.findIndex((t) => t.id === tabId);
    if (index === -1) return;
  }

  const closedTab = state.tabs[index];
  if (closedTab.isStreaming) {
    callApi("stop_streaming", tabId, closedTab.activeStreamId || null).catch(() => {});
    closedTab.isStreaming = false;
    closedTab.activeStreamId = null;
  }

  if (closedTab.conversationId && !state.conversations.some((c) => c.id === closedTab.conversationId)) {
    state.recentlyDeletedConvIds.add(closedTab.conversationId);
    callApi("delete_conversation", closedTab.conversationId, true).catch(() => {});
  }

  state.tabs.splice(index, 1);
  state.splitTabIds = state.splitTabIds.filter((id) => id !== tabId);

  if (state.tabs.length === 0) {
    await createNewTab();
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
  if (uiHooks.renderConversationsList) uiHooks.renderConversationsList();
  renderActivePanes();
}

export function assignTabToPaneSlot(slotIndex, targetTabId) {
  sanitizeSplitTabIds();
  if (slotIndex < 0 || slotIndex >= state.splitTabIds.length) {
    slotIndex = Math.max(0, state.splitTabIds.length - 1);
  }
  const currentTabIdInSlot = state.splitTabIds[slotIndex];
  const existingPaneIdx = state.splitTabIds.indexOf(targetTabId);

  if (existingPaneIdx !== -1 && existingPaneIdx !== slotIndex) {
    state.splitTabIds[existingPaneIdx] = currentTabIdInSlot;
    state.splitTabIds[slotIndex] = targetTabId;
  } else if (existingPaneIdx === -1) {
    state.splitTabIds[slotIndex] = targetTabId;
  }

  state.activeTabId = targetTabId;
  sanitizeSplitTabIds();
  persistTabsState();
  renderTabs();
  if (uiHooks.renderConversationsList) uiHooks.renderConversationsList();
  renderActivePanes();
}

export function switchActiveTab(tabId, targetSlotIndex = null) {
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
    if (uiHooks.renderConversationsList) uiHooks.renderConversationsList();
    renderActivePanes();
  } else if (state.splitTabIds.length > 1 && activeSlotIdx !== -1 && existingIdx !== activeSlotIdx) {
    assignTabToPaneSlot(activeSlotIdx, tabId);
  } else {
    state.activeTabId = tabId;
    persistTabsState();
    renderTabs();
    if (uiHooks.renderConversationsList) uiHooks.renderConversationsList();
    el.panesContainer.querySelectorAll(".chat-pane").forEach((p) => {
      p.classList.toggle("active-pane", p.dataset.tabId === tabId);
    });
  }
}

export function addSplitPane() {
  if (state.splitTabIds.length >= 5) {
    showNoticeDialog("Maximum 5 side-by-side tabs reached.");
    return;
  }

  const candidate = state.tabs.find((t) => !state.splitTabIds.includes(t.id));
  if (candidate) {
    state.splitTabIds.push(candidate.id);
    state.activeTabId = candidate.id;
    persistTabsState();
    renderTabs();
    if (uiHooks.renderConversationsList) uiHooks.renderConversationsList();
    renderActivePanes();
  } else {
    createNewTab(null, true);
  }
}

export function removeSplitPane(tabId) {
  if (state.splitTabIds.length <= 1) return;
  state.splitTabIds = state.splitTabIds.filter((id) => id !== tabId);
  if (!state.splitTabIds.includes(state.activeTabId)) {
    state.activeTabId = state.splitTabIds[0];
  }
  persistTabsState();
  renderTabs();
  renderActivePanes();
}

export function movePane(tabId, direction) {
  const idx = state.splitTabIds.indexOf(tabId);
  if (idx === -1) return;
  const targetIdx = idx + direction;
  if (targetIdx < 0 || targetIdx >= state.splitTabIds.length) return;

  const temp = state.splitTabIds[idx];
  state.splitTabIds[idx] = state.splitTabIds[targetIdx];
  state.splitTabIds[targetIdx] = temp;

  persistTabsState();
  renderTabs();
  renderActivePanes();
}

export async function branchActiveTabToSplitPane() {
  const activeTab = state.tabs.find((t) => t.id === state.activeTabId);
  if (!activeTab) return;
  if (!activeTab.conversationId) {
    showNoticeDialog("Cannot branch an empty chat. Send a message first.");
    return;
  }

  const res = await callApi("duplicate_conversation", activeTab.conversationId);
  if (!res || !res.conversation) {
    showNoticeDialog(res && res.error ? res.error : "Failed to branch conversation.");
    return;
  }

  const newConv = res.conversation;
  state.recentlyDeletedConvIds.delete(newConv.id);
  if (!state.conversations.some((c) => c.id === newConv.id)) {
    state.conversations.unshift(newConv);
  }

  const newTab = createTabObject({
    conversationId: newConv.id,
    title: newConv.title,
    systemPrompt: newConv.system_prompt || activeTab.systemPrompt || "",
    maxContextMessages: newConv.max_context_messages || activeTab.maxContextMessages || 30,
    providerId: activeTab._preferredProviderId || activeTab.providerId,
    modelId: activeTab._preferredModelId || activeTab.modelId,
    thinkingLevel: activeTab._preferredThinkingLevel || activeTab.thinkingLevel,
    webSearchEnabled: activeTab.webSearchEnabled,
    userSelectedModel: activeTab._userSelectedModel,
    userSelectedThinking: activeTab._userSelectedThinking,
  });
  state.tabs.push(newTab);

  if (state.splitTabIds.length < 5) {
    state.splitTabIds.push(newTab.id);
  } else {
    const replaceIdx =
      state.splitTabIds[state.splitTabIds.length - 1] === activeTab.id ? 0 : state.splitTabIds.length - 1;
    state.splitTabIds[replaceIdx] = newTab.id;
  }

  state.activeTabId = newTab.id;
  persistTabsState();
  renderTabs();
  if (uiHooks.renderConversationsList) uiHooks.renderConversationsList();
  renderActivePanes();
}

export function renderAllTabsPopover() {
  if (!el.allTabsPopover) return;
  el.allTabsPopover.innerHTML = "";

  state.tabs.forEach((tab, idx) => {
    const conv = state.conversations.find((c) => c.id === tab.conversationId);
    const title = conv ? conv.title : (tab.title || "New Chat");
    const isInSplit = state.splitTabIds.includes(tab.id);
    const isActive = tab.id === state.activeTabId;

    const row = document.createElement("div");
    row.className =
      "all-tabs-popover-item" +
      (isActive ? " active" : "") +
      (isInSplit && state.splitTabIds.length > 1 ? " split-active" : "");
    row.innerHTML = `
      <span class="all-tabs-idx">${idx + 1}.</span>
      <span class="all-tabs-title">${escapeHtml(title)}</span>
      <button class="all-tabs-close-btn" title="Close Tab">
        <i data-lucide="x" style="width: 12px; height: 12px;"></i>
      </button>
    `;

    row.onclick = async (e) => {
      if (e.target.closest(".all-tabs-close-btn")) {
        e.stopPropagation();
        await closeTab(tab.id);
        renderAllTabsPopover();
        return;
      }
      el.allTabsPopover.classList.remove("open");
      switchActiveTab(tab.id);
    };

    el.allTabsPopover.appendChild(row);
  });

  renderIcons(el.allTabsPopover);
}

export function renderTabs() {
  if (!el.tabsScrollArea) return;
  el.tabsScrollArea.innerHTML = "";
  let activeTabElement = null;

  state.tabs.forEach((tab) => {
    const conv = state.conversations.find((c) => c.id === tab.conversationId);
    const title = conv ? conv.title : (tab.title || "New Chat");

    const tabEl = document.createElement("div");
    const isInSplit = state.splitTabIds.includes(tab.id);
    const isActive = tab.id === state.activeTabId;

    tabEl.className =
      "tab-item" +
      (isActive ? " active" : "") +
      (isInSplit && state.splitTabIds.length > 1 ? " split-active" : "");
    tabEl.title = title;

    tabEl.innerHTML = `
      <span class="tab-title">${escapeHtml(title)}</span>
      ${tab.lastStreamError ? '<span class="tab-error-dot" title="Stream error occurred in this tab"></span>' : ""}
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

  if (el.splitViewBtn) {
    if (state.splitTabIds.length > 1) {
      el.splitViewBtn.classList.add("active");
      el.splitViewBtn.title = `Split View (${state.splitTabIds.length}/5 panes active). Click to add pane.`;
    } else {
      el.splitViewBtn.classList.remove("active");
      el.splitViewBtn.title = "Add Split Pane (up to 5 panes side-by-side, Ctrl+\\)";
    }
  }

  refreshPaneHeaderLabels();
  renderIcons(el.tabsScrollArea);
}

export function renderActivePanes() {
  if (!el.panesContainer) return;
  // Snapshot any live textarea text into `tab.draftInput` before unmounting panes (`NEW-H2`)
  el.panesContainer.querySelectorAll(".chat-pane").forEach((oldPane) => {
    const oldTabId = oldPane.dataset.tabId;
    const oldTab = state.tabs.find((t) => t.id === oldTabId);
    const oldInput = oldPane.querySelector(`#input-${oldTabId}`);
    if (oldTab && oldInput) {
      oldTab.draftInput = oldInput.value;
    }
  });

  disconnectPaneResizeObservers(el.panesContainer);
  el.panesContainer.innerHTML = "";
  sanitizeSplitTabIds();

  state.splitTabIds.forEach((tabId, index) => {
    const tab = state.tabs.find((t) => t.id === tabId);
    if (!tab) return;

    const paneEl = createPaneElement(tab, index, state.splitTabIds.length);
    el.panesContainer.appendChild(paneEl);
    populatePaneContent(paneEl, tab);
  });

  renderIcons(el.panesContainer);
}

export function createPaneElement(tab, index, totalPanes) {
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

  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const w = entry.contentRect.width;
        pane.classList.toggle("compact-pane", w > 0 && w < 390);
        pane.classList.toggle("icon-only-model", w > 0 && w < 310);
      }
    });
    ro.observe(pane);
    pane._resizeObserver = ro;
  }

  return pane;
}

export function updatePaneModelDropdown(pane, tab) {
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
    thinkingWrapper.style.display = "none";
    syncLabels();
    return;
  }

  const getVerifiedModels = (prov) => getVerifiedProviderModels(prov, 5);

  const desiredProvId = tab.isStreaming
    ? (tab.providerId || state.settings.default_provider)
    : (tab._userSelectedModel || tab.conversationId
        ? (tab._preferredProviderId || tab.providerId || state.settings.default_provider)
        : state.settings.default_provider);
  const desiredModelId = tab.isStreaming
    ? (tab.modelId || state.settings.default_model)
    : (tab._userSelectedModel || tab.conversationId
        ? (tab._preferredModelId || tab.modelId || state.settings.default_model)
        : (state.settings.default_model || ""));

  let selectedProv = null;
  let selectedModel = null;
  let usingTempFallback = false;

  const preferredProv = activeProviders.find((p) => p.id === desiredProvId);
  if (preferredProv) {
    const pModels = getVerifiedModels(preferredProv);
    if (pModels.length > 0) {
      selectedProv = preferredProv;
      selectedModel = (desiredModelId ? pModels.find((m) => m.id === desiredModelId) : null) || pModels[0];
    }
  }

  if (!selectedProv || !selectedModel) {
    const fbProv = activeProviders.find((p) => p.id === state.settings.default_provider) || activeProviders[0];
    const fbModels = getVerifiedModels(fbProv);
    if (fbProv && fbModels.length > 0) {
      selectedProv = fbProv;
      const defModel = state.settings.default_model || "";
      selectedModel = (defModel ? fbModels.find((m) => m.id === defModel) : null) || fbModels[0];
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

export function updatePaneWebSearchButton(pane, tab) {
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

export function syncAllPaneWebSearchButtons() {
  if (!el.panesContainer) return;
  el.panesContainer.querySelectorAll(".chat-pane").forEach((paneEl) => {
    const tabId = paneEl.dataset.tabId;
    const targetTab = state.tabs.find((t) => t.id === tabId);
    if (targetTab) {
      updatePaneWebSearchButton(paneEl, targetTab);
    }
  });
}

export function syncAllPaneModelDropdowns() {
  if (!el.panesContainer) return;
  el.panesContainer.querySelectorAll(".chat-pane").forEach((paneEl) => {
    const tabId = paneEl.dataset.tabId;
    const targetTab = state.tabs.find((t) => t.id === tabId);
    if (targetTab) {
      updatePaneModelDropdown(paneEl, targetTab);
    }
  });
  syncAllPaneWebSearchButtons();
}

export function renderToolStatusBanner(toolBanner, toolState) {
  if (!toolBanner) return;
  if (!toolState) {
    toolBanner.style.display = "none";
    return;
  }
  if (toolState.status === "searching") {
    toolBanner.style.display = "inline-flex";
    if (toolState.tool === "image_search") {
      toolBanner.innerHTML = `<i data-lucide="image" style="width: 12px; height: 12px; color: var(--accent-color);"></i><span>Searching images${toolState.query ? `: "${escapeHtml(toolState.query)}"` : "..."}</span>`;
    } else {
      toolBanner.innerHTML = `<i data-lucide="globe" style="width: 12px; height: 12px;"></i><span>Searching Google${toolState.query ? `: "${escapeHtml(toolState.query)}"` : "..."}</span>`;
    }
    renderIcons(toolBanner);
  } else if (toolState.status === "done") {
    if (toolState.tool === "image_search") {
      const imgCount = Number(toolState.images_found || 0);
      if (toolState.ok && imgCount > 0) {
        toolBanner.style.display = "inline-flex";
        toolBanner.innerHTML = `<i data-lucide="check" style="width: 12px; height: 12px; color: #34a853;"></i><span>Image search complete (${imgCount} ${imgCount === 1 ? "image" : "images"} found)</span>`;
      } else if (toolState.ok) {
        toolBanner.style.display = "inline-flex";
        toolBanner.innerHTML = `<i data-lucide="info" style="width: 12px; height: 12px; color: var(--text-muted);"></i><span>No images found</span>`;
      } else {
        toolBanner.style.display = "none";
      }
    } else {
      const reqUsed = Number(toolState.search_requests_used || 0);
      if (toolState.ok) {
        toolBanner.style.display = "inline-flex";
        toolBanner.innerHTML = `<i data-lucide="check" style="width: 12px; height: 12px; color: #34a853;"></i><span>Google Search complete (${reqUsed} ${reqUsed === 1 ? "request" : "requests"})</span>`;
      } else {
        toolBanner.style.display = "none";
      }
    }
    renderIcons(toolBanner);
  }
}

export function createLiveStreamingRow(tab) {
  const liveRow = document.createElement("div");
  liveRow.className = "message-row assistant-message-row live-streaming-row";
  liveRow.id = `live-msg-${tab.id}`;

  const provTag = (tab.streamingProviderId || tab.providerId || "API").toUpperCase();
  const modelTag = tab.streamingModelId || tab.modelId || "Model";
  const thinkLvl =
    tab.streamingThinkingLevel !== undefined && tab.streamingThinkingLevel !== null
      ? tab.streamingThinkingLevel
      : (tab.thinkingLevel || "none");
  const thinkBadge = thinkLvl && thinkLvl !== "none" ? ` · ${thinkLvl.toUpperCase()} THINK` : "";

  const hasThinking = Boolean(tab.streamingThinking);
  const isCollapsed = Boolean(tab.hasAutoCollapsedThinking && !tab.isActivelyThinking);
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
  if (tab.activeToolStatus) {
    renderToolStatusBanner(liveRow.querySelector(`#live-tool-status-${tab.id}`), tab.activeToolStatus);
  }
  if (tab.streamingContent) {
    const contentDiv = liveRow.querySelector(`#live-content-${tab.id}`);
    if (contentDiv) {
      contentDiv.innerHTML = renderMarkdownWithEnhancements(tab.streamingContent, { isStreaming: true });
    }
  }

  return liveRow;
}

export function applyLiveStreamRender(tabOrId) {
  const tabId = tabOrId && typeof tabOrId === "object" ? tabOrId.id : tabOrId;
  const tab = state.tabs.find((t) => t.id === tabId);
  if (!tab) return;
  const pane = el.panesContainer.querySelector(`.chat-pane[data-tab-id="${tab.id}"]`);
  const messagesContainer = pane ? pane.querySelector(`#messages-${tab.id}`) : null;
  if (!pane || !messagesContainer) return;

  const thinkAcc = document.getElementById(`live-thinking-${tab.id}`);
  const thinkBody = document.getElementById(`live-thinking-body-${tab.id}`);
  if (thinkAcc && thinkBody && tab.streamingThinking) {
    thinkAcc.style.display = "block";
    thinkBody.textContent = tab.streamingThinking;

    if (tab.isActivelyThinking) {
      thinkAcc.classList.remove("collapsed");
      const leftBox = thinkAcc.querySelector(".thinking-title-left");
      const titleSpan = leftBox ? leftBox.querySelector("span") : null;
      if (titleSpan) titleSpan.textContent = "Thinking...";
      if (leftBox && !leftBox.querySelector(".thinking-pulse")) {
        const pulse = document.createElement("div");
        pulse.className = "thinking-pulse";
        leftBox.insertBefore(pulse, leftBox.firstChild);
      }
      thinkBody.scrollTop = thinkBody.scrollHeight;
    }
  }

  if (tab.streamingContent) {
    if (thinkAcc && !tab.isActivelyThinking && tab.streamingContent.trim().length > 0 && !tab.hasAutoCollapsedThinking) {
      tab.hasAutoCollapsedThinking = true;
      thinkAcc.classList.add("collapsed");
      const titleSpan = thinkAcc.querySelector(".thinking-title-left span");
      if (titleSpan) titleSpan.textContent = "Thought Process (Complete)";
      const pulse = thinkAcc.querySelector(".thinking-pulse");
      if (pulse) pulse.remove();
    }
    const contentDiv = document.getElementById(`live-content-${tab.id}`);
    if (contentDiv) {
      contentDiv.innerHTML = renderMarkdownWithEnhancements(tab.streamingContent, { isStreaming: true });
      renderIcons(contentDiv);
    }
  }

  const isNearBottom =
    messagesContainer.scrollHeight - messagesContainer.scrollTop - messagesContainer.clientHeight < 120;
  if (isNearBottom) {
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
  }
}

function flushPendingStreamRenders() {
  streamRenderRaf = null;
  const tabIds = Array.from(pendingStreamRenderTabs);
  pendingStreamRenderTabs.clear();
  for (const tabId of tabIds) {
    applyLiveStreamRender(tabId);
  }
}

export function scheduleStreamRender(tabId) {
  pendingStreamRenderTabs.add(tabId);
  if (streamRenderRaf !== null) return;
  if (typeof requestAnimationFrame === "function") {
    streamRenderRaf = requestAnimationFrame(flushPendingStreamRenders);
  } else {
    flushPendingStreamRenders();
  }
}

export function setTabStreamingUI(pane, tab, streaming) {
  tab.isStreaming = Boolean(streaming);
  if (streaming && uiHooks.scheduleNextEventPoll) {
    uiHooks.scheduleNextEventPoll(20);
  }
  if (!pane) return;
  const sendBtn = pane.querySelector(`#send-btn-${tab.id}`);
  const stopBtn = pane.querySelector(`#stop-btn-${tab.id}`);
  if (sendBtn) sendBtn.style.display = streaming ? "none" : "flex";
  if (stopBtn) stopBtn.style.display = streaming ? "flex" : "none";
}

export function appendStreamErrorBanner(tab, pane, messagesContainer, errorText, existingRow = null) {
  let targetRow = existingRow;
  if (!targetRow) {
    targetRow = document.createElement("div");
    targetRow.className = "message-row assistant-message-row";
    messagesContainer.appendChild(targetRow);
  }

  const errBox = document.createElement("div");
  errBox.className = "stream-error-banner";
  errBox.innerHTML = `
    <div><strong>Error:</strong> ${escapeHtml(errorText || "Unknown error")}</div>
    <button class="btn-secondary stream-error-retry-btn">Retry</button>
  `;
  targetRow.appendChild(errBox);

  const retryBtn = errBox.querySelector(".stream-error-retry-btn");
  if (retryBtn) {
    retryBtn.onclick = async () => {
      if (tab.isStreaming) return;
      if (!tab.modelId || !tab.providerId) {
        showNoticeDialog("Please configure a valid provider & model in Settings (Ctrl+,) first.");
        return;
      }

      let prevRow = targetRow.previousElementSibling;
      while (prevRow && !prevRow.classList.contains("user-message-row")) {
        prevRow = prevRow.previousElementSibling;
      }
      const prevMsgId = prevRow ? prevRow.dataset.msgId : null;
      const prevBubble = prevRow ? prevRow.querySelector(".user-bubble") : null;
      const prevText =
        prevRow && typeof prevRow._rawContent === "string"
          ? prevRow._rawContent
          : (prevBubble ? prevBubble.textContent : "");

      if (!prevRow || !prevText) {
        showNoticeDialog("Unable to retry: could not find the prompt message to resend.");
        return;
      }

      retryBtn.disabled = true;
      const clientStreamId = generateClientStreamId();
      resetTabStreamBuffers(tab);
      tab.activeStreamId = clientStreamId;
      setTabStreamingUI(pane, tab, true);
      const useWebSearch = Boolean(tab.webSearchEnabled && getWebSearchAvailability().available);

      try {
        let res = null;
        if (tab.conversationId && prevMsgId && !prevMsgId.startsWith("user_temp_")) {
          res = await callApi(
            "edit_message",
            tab.id,
            tab.conversationId,
            prevMsgId,
            prevText,
            tab.providerId,
            tab.modelId,
            tab.thinkingLevel,
            useWebSearch,
            clientStreamId
          );
        } else {
          res = await callApi(
            "send_message",
            tab.id,
            tab.conversationId,
            prevText,
            tab.providerId,
            tab.modelId,
            tab.thinkingLevel,
            tab.title || "New Chat",
            tab.systemPrompt || "",
            tab.maxContextMessages || 30,
            useWebSearch,
            clientStreamId
          );
        }
        if (!res || res.error) {
          retryBtn.disabled = false;
          if (tab.activeStreamId === clientStreamId) {
            tab.activeStreamId = null;
            setTabStreamingUI(pane, tab, false);
          }
          showNoticeDialog((res && res.error) || "Failed to retry message.");
          return;
        }

        // Remove any orphaned partial assistant rows AND the error banner row AFTER RPC succeeds (`NEW-H3`)
        removeDownstreamSiblings(prevRow, `live-msg-${tab.id}`, false);
        tab.lastStreamError = null;
        renderTabs();

        if (res.conversation_id && (!tab.conversationId || tab.conversationId === res.conversation_id)) {
          const changed = tab.conversationId !== res.conversation_id;
          tab.conversationId = res.conversation_id;
          tab.pendingUserDraft = null;
          if (changed) persistTabsState();
        }
        if (res.user_message && res.user_message.id && prevRow) {
          prevRow.dataset.msgId = res.user_message.id;
        }
        if (tab.activeStreamId !== clientStreamId && tab.activeStreamId !== res.stream_id) {
          return;
        }
        if (res.stream_id) {
          tab.activeStreamId = res.stream_id;
        }
        if (res.token_stats) {
          tab.tokenStats = res.token_stats;
          renderTokenStats(pane, tab.id, res.token_stats);
        }
      } catch (err) {
        retryBtn.disabled = false;
        if (tab.activeStreamId === clientStreamId) {
          tab.activeStreamId = null;
          setTabStreamingUI(pane, tab, false);
        }
        showNoticeDialog(`Failed to retry message: ${err.message || err}`);
      }
    };
  }
  return targetRow;
}

export async function populatePaneContent(pane, tab) {
  const messagesContainer = pane.querySelector(`#messages-${tab.id}`);
  const chatInput = pane.querySelector(`#input-${tab.id}`);
  const sendBtn = pane.querySelector(`#send-btn-${tab.id}`);
  const stopBtn = pane.querySelector(`#stop-btn-${tab.id}`);
  const convSettingsBtn = pane.querySelector(".pane-conv-settings-btn");
  const exportBtn = pane.querySelector(".pane-export-btn");

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

  convSettingsBtn.onclick = () => {
    if (uiHooks.openConvSettingsModal) uiHooks.openConvSettingsModal(tab);
  };

  exportBtn.onclick = () => {
    if (tab.conversationId && uiHooks.openExportModal) uiHooks.openExportModal(tab.conversationId);
    else showNoticeDialog("Send a message first to export this conversation.");
  };

  const renderWelcomePlaceholder = () => {
    messagesContainer.innerHTML = `
      <div class="welcome-placeholder" style="display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; color: var(--text-muted); gap: 12px; margin-top: 50px;">
        <div style="width: 44px; height: 44px; border-radius: 50%; background: var(--bg-tertiary); display: flex; align-items: center; justify-content: center;">
          <i data-lucide="sparkles" style="width: 22px; height: 22px; color: var(--accent-color);"></i>
        </div>
        <div style="font-size: 15px; font-weight: 600; color: var(--text-primary);">Start a new conversation</div>
        <div style="font-size: 12.5px; max-width: 320px; text-align: center;">Ask any question. The conversation will be automatically named and saved once generation begins.</div>
      </div>
    `;
    renderIcons(messagesContainer);
    renderTokenStats(pane, tab.id, null);
  };

  const syncInputHeight = () => {
    chatInput.style.height = "auto";
    chatInput.style.height = Math.min(chatInput.scrollHeight, 150) + "px";
  };

  // Restore unsent draft input text & height across pane re-renders (`NEW-H2`)
  if (tab.draftInput) {
    chatInput.value = tab.draftInput;
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(syncInputHeight);
    } else {
      syncInputHeight();
    }
  }

  chatInput.oninput = () => {
    tab.draftInput = chatInput.value;
    syncInputHeight();
  };

  chatInput.onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendMessageFromPane(pane, tab);
    }
  };

  sendBtn.onclick = () => sendMessageFromPane(pane, tab);
  stopBtn.onclick = () => callApi("stop_streaming", tab.id, tab.activeStreamId || null).catch(() => {});
  setTabStreamingUI(pane, tab, tab.isStreaming);

  if (!tab.conversationId) {
    if (tab.isStreaming) {
      messagesContainer.innerHTML = "";
      if (tab.pendingUserDraft) {
        messagesContainer.appendChild(createMessageElement(tab, tab.pendingUserDraft, pane));
      }
      messagesContainer.appendChild(createLiveStreamingRow(tab));
      renderIcons(messagesContainer);
    } else if (tab.pendingUserDraft && tab.lastStreamError) {
      messagesContainer.innerHTML = "";
      messagesContainer.appendChild(createMessageElement(tab, tab.pendingUserDraft, pane));
      appendStreamErrorBanner(tab, pane, messagesContainer, tab.lastStreamError);
      renderIcons(messagesContainer);
    } else {
      renderWelcomePlaceholder();
      if (tab.lastStreamError) {
        appendStreamErrorBanner(tab, pane, messagesContainer, tab.lastStreamError);
      }
    }
    return;
  }

  messagesContainer.innerHTML =
    '<div style="color: var(--text-muted); font-size: 13px; text-align: center; margin-top: 30px;">Loading chat...</div>';

  // Re-fetch once if a stream finished (`_streamGen` incremented) while `get_conversation` was in flight (`NEW-H2`)
  for (let attempt = 0; attempt < 2; attempt++) {
    const expectedConvId = tab.conversationId;
    const genBefore = tab._streamGen || 0;
    const wasStreamingBefore = Boolean(tab.isStreaming);
    const conv = await callApi("get_conversation", expectedConvId);
    if (!pane.isConnected || tab.conversationId !== expectedConvId) {
      return;
    }
    if (wasStreamingBefore && (tab._streamGen || 0) !== genBefore && attempt === 0) {
      continue;
    }
    if (!conv || conv.error) {
      if (!conv) {
        tab.conversationId = null;
        persistTabsState();
      }
      renderWelcomePlaceholder();
      return;
    }

    messagesContainer.innerHTML = "";
    tab.systemPrompt = conv.system_prompt || tab.systemPrompt || "";
    tab.maxContextMessages = conv.max_context_messages || tab.maxContextMessages || 30;

    if (conv.messages && conv.messages.length > 0) {
      tab.tokenStats = conv.token_stats;
      renderTokenStats(pane, tab.id, conv.token_stats);
      const hasAssistant = conv.messages.some((m) => m.role === "assistant");
      tab.hasUnansweredPrompt = !hasAssistant;

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
    } else if (tab.lastStreamError) {
      appendStreamErrorBanner(tab, pane, messagesContainer, tab.lastStreamError);
    }
    renderIcons(messagesContainer);
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
    break;
  }
}

export function createMessageElement(tab, msg, pane) {
  const row = document.createElement("div");
  row.className = `message-row ${msg.role === "user" ? "user-message-row" : "assistant-message-row"}`;
  row.dataset.msgId = msg.id;

  if (msg.role === "user") {
    row._rawContent = msg.content || "";
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

    row.querySelector(".copy-msg-btn").onclick = () => copyText(row._rawContent, row.querySelector(".copy-msg-btn"));
    row.querySelector(".edit-msg-btn").onclick = () => inlineEditUserMessage(row, tab, msg, pane);
  } else {
    const isSystemMsg = msg.role === "system";
    const provTag = isSystemMsg ? "SYSTEM" : (msg.provider_id ? msg.provider_id.toUpperCase() : "API");
    const modelTag = isSystemMsg ? "Context Note" : (msg.model_id || "Assistant");
    const thinkBadge =
      !isSystemMsg && msg.thinking_level && msg.thinking_level !== "none"
        ? ` · ${msg.thinking_level.toUpperCase()} THINK`
        : "";

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
        ${
          !isSystemMsg
            ? `<button class="assistant-action-btn regen-msg-btn" title="Regenerate from here">
                <i data-lucide="rotate-cw" style="width: 11px; height: 11px;"></i>
                <span>Regenerate</span>
              </button>`
            : ""
        }
      </div>
    `;

    row.querySelector(".copy-msg-btn").onclick = () => copyText(msg.content, row.querySelector(".copy-msg-btn"));
    const regenBtn = row.querySelector(".regen-msg-btn");
    if (regenBtn) {
      regenBtn.onclick = async () => {
        if (tab.isStreaming) return;
        if (!tab.modelId || !tab.providerId) {
          showNoticeDialog("No model is available. Please configure an API key in Settings (Ctrl+,) first.");
          return;
        }

        regenBtn.disabled = true;
        const clientStreamId = generateClientStreamId();
        resetTabStreamBuffers(tab);
        tab.activeStreamId = clientStreamId;
        setTabStreamingUI(pane, tab, true);
        const realMsgId = row.dataset.msgId || msg.id;
        const useWebSearch = Boolean(tab.webSearchEnabled && getWebSearchAvailability().available);
        try {
          const res = await callApi(
            "regenerate_message",
            tab.id,
            tab.conversationId,
            realMsgId,
            tab.providerId,
            tab.modelId,
            tab.thinkingLevel,
            useWebSearch,
            clientStreamId
          );
          if (!res || res.error) {
            regenBtn.disabled = false;
            if (tab.activeStreamId === clientStreamId) {
              tab.activeStreamId = null;
              setTabStreamingUI(pane, tab, false);
            }
            showNoticeDialog((res && res.error) || "Failed to regenerate response.");
            return;
          }

          // Remove this assistant row & downstream rows ONLY after RPC succeeds (`NEW-H3`)
          removeDownstreamSiblings(row, `live-msg-${tab.id}`, true);
          tab.lastStreamError = null;
          renderTabs();

          if (tab.activeStreamId !== clientStreamId && tab.activeStreamId !== res.stream_id) {
            return;
          }
          if (res.stream_id) {
            tab.activeStreamId = res.stream_id;
          }
          if (res.token_stats) {
            tab.tokenStats = res.token_stats;
            renderTokenStats(pane, tab.id, res.token_stats);
          }
        } catch (err) {
          regenBtn.disabled = false;
          if (tab.activeStreamId === clientStreamId) {
            tab.activeStreamId = null;
            setTabStreamingUI(pane, tab, false);
          }
          showNoticeDialog(`Failed to regenerate response: ${err.message || err}`);
        }
      };
    }
  }

  return row;
}

export function inlineEditUserMessage(row, tab, msg, pane) {
  if (tab.isStreaming) return;
  const bubble = row.querySelector(".user-bubble");
  if (!bubble || bubble.querySelector("textarea")) return;
  const originalText = row._rawContent !== undefined ? row._rawContent : msg.content;

  bubble.classList.add("editing");
  bubble.innerHTML = "";

  const txt = document.createElement("textarea");
  txt.className = "settings-input";
  txt.style.cssText = "width: 100%; min-height: 60px; resize: vertical;";
  txt.value = originalText;

  const actionsDiv = document.createElement("div");
  actionsDiv.style.cssText = "display: flex; justify-content: flex-end; gap: 6px; margin-top: 6px;";

  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "btn-secondary cancel-edit-btn";
  cancelBtn.style.cssText = "padding: 3px 8px; font-size: 11.5px;";
  cancelBtn.textContent = "Cancel";

  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn-primary save-edit-btn";
  saveBtn.style.cssText = "padding: 3px 10px; font-size: 11.5px;";
  saveBtn.textContent = "Save & Resend";

  actionsDiv.appendChild(cancelBtn);
  actionsDiv.appendChild(saveBtn);
  bubble.appendChild(txt);
  bubble.appendChild(actionsDiv);

  const cancelInlineEdit = () => {
    bubble.classList.remove("editing");
    delete bubble._cancelInlineEdit;
    bubble.textContent = row._rawContent !== undefined ? row._rawContent : originalText;
  };
  bubble._cancelInlineEdit = cancelInlineEdit;

  txt.focus();
  txt.setSelectionRange(txt.value.length, txt.value.length);

  txt.onkeydown = (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      cancelInlineEdit();
    } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.isComposing) {
      e.preventDefault();
      saveBtn.click();
    }
  };

  cancelBtn.onclick = (e) => {
    e.stopPropagation();
    cancelInlineEdit();
  };

  saveBtn.onclick = async (e) => {
    e.stopPropagation();
    if (tab.isStreaming) return;
    const newText = txt.value.trim();
    if (!newText) return;
    if (!tab.modelId || !tab.providerId) {
      showNoticeDialog("No model is available. Please configure an API key in Settings (Ctrl+,) first.");
      return;
    }

    saveBtn.disabled = true;
    cancelBtn.disabled = true;
    const clientStreamId = generateClientStreamId();
    resetTabStreamBuffers(tab);
    tab.activeStreamId = clientStreamId;
    setTabStreamingUI(pane, tab, true);
    const realMsgId = row.dataset.msgId || msg.id;
    const useWebSearch = Boolean(tab.webSearchEnabled && getWebSearchAvailability().available);

    try {
      const res = await callApi(
        "edit_message",
        tab.id,
        tab.conversationId,
        realMsgId,
        newText,
        tab.providerId,
        tab.modelId,
        tab.thinkingLevel,
        useWebSearch,
        clientStreamId
      );
      if (!res || res.error) {
        saveBtn.disabled = false;
        cancelBtn.disabled = false;
        if (tab.activeStreamId === clientStreamId) {
          tab.activeStreamId = null;
          setTabStreamingUI(pane, tab, false);
        }
        showNoticeDialog((res && res.error) || "Failed to resend edited message.");
        return;
      }

      // Commit text & remove downstream DOM rows ONLY after RPC succeeds (`NEW-H3`)
      msg.id = realMsgId;
      row.dataset.msgId = realMsgId;
      msg.content = newText;
      row._rawContent = newText;
      bubble.classList.remove("editing");
      delete bubble._cancelInlineEdit;
      bubble.textContent = newText;
      removeDownstreamSiblings(row, `live-msg-${tab.id}`, false);
      tab.lastStreamError = null;
      renderTabs();

      if (tab.activeStreamId !== clientStreamId && tab.activeStreamId !== res.stream_id) {
        return;
      }
      if (res.stream_id) {
        tab.activeStreamId = res.stream_id;
      }
      if (res.token_stats) {
        tab.tokenStats = res.token_stats;
        renderTokenStats(pane, tab.id, res.token_stats);
      }
    } catch (err) {
      saveBtn.disabled = false;
      cancelBtn.disabled = false;
      if (tab.activeStreamId === clientStreamId) {
        tab.activeStreamId = null;
        setTabStreamingUI(pane, tab, false);
      }
      showNoticeDialog(`Failed to resend edited message: ${err.message || err}`);
    }
  };
}

export async function sendMessageFromPane(pane, tab) {
  if (tab.isStreaming) return;

  if (!tab.modelId || !tab.providerId) {
    showNoticeDialog(
      "No model is available. Please configure an API key in Settings (Ctrl+,) to fetch models before sending."
    );
    return;
  }

  const chatInput = pane.querySelector(`#input-${tab.id}`);
  const text = chatInput.value.trim();
  if (!text) return;

  chatInput.value = "";
  chatInput.style.height = "auto";
  tab.draftInput = "";

  const messagesContainer = pane.querySelector(`#messages-${tab.id}`);
  clearWelcomeAndStaleBanners(messagesContainer);

  const tempUserMsg = {
    id: "user_temp_" + Date.now(),
    role: "user",
    content: text,
    created_at: Date.now() / 1000,
  };
  if (!tab.conversationId) {
    tab.pendingUserDraft = tempUserMsg;
  }
  if (!state.conversations.some((c) => c.id === tab.conversationId)) {
    tab.hasUnansweredPrompt = true;
  }

  const userRow = createMessageElement(tab, tempUserMsg, pane);
  messagesContainer.appendChild(userRow);
  renderIcons(userRow);
  messagesContainer.scrollTop = messagesContainer.scrollHeight;

  const clientStreamId = generateClientStreamId();
  resetTabStreamBuffers(tab);
  tab.activeStreamId = clientStreamId;
  tab.lastStreamError = null;
  renderTabs();
  setTabStreamingUI(pane, tab, true);
  tab._userSelectedModel = true;
  tab._userSelectedThinking = true;
  tab._preferredProviderId = tab.providerId;
  tab._preferredModelId = tab.modelId;
  tab._preferredThinkingLevel = tab.thinkingLevel;
  tab._usingTempFallback = false;

  const useWebSearch = Boolean(tab.webSearchEnabled && getWebSearchAvailability().available);
  try {
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
      useWebSearch,
      clientStreamId
    );
    if (!res || res.error) {
      if (tab.activeStreamId === clientStreamId) {
        tab.activeStreamId = null;
        setTabStreamingUI(pane, tab, false);
      }
      showNoticeDialog((res && res.error) || "Failed to send message.");
      return;
    }
    if (res.user_message && res.user_message.id) {
      tempUserMsg.id = res.user_message.id;
      userRow.dataset.msgId = res.user_message.id;
    }
    // Persist newly created conversationId immediately even if stream finishes/stops before `chat_named` (`H1`)
    if (res.conversation_id && (!tab.conversationId || tab.conversationId === res.conversation_id)) {
      const changed = tab.conversationId !== res.conversation_id;
      tab.conversationId = res.conversation_id;
      tab.pendingUserDraft = null;
      if (changed) persistTabsState();
    }
    if (tab.activeStreamId !== clientStreamId && tab.activeStreamId !== res.stream_id) {
      return;
    }
    if (res.stream_id) {
      tab.activeStreamId = res.stream_id;
    }
  } catch (err) {
    if (tab.activeStreamId === clientStreamId) {
      tab.activeStreamId = null;
      setTabStreamingUI(pane, tab, false);
    }
    showNoticeDialog(`Failed to send message: ${err.message || err}`);
  }
}

export function renderTokenStats(pane, tabId, stats) {
  const chipsEl = pane.querySelector(`#token-chips-${tabId}`);
  const grandTotalEl = pane.querySelector(`#token-grand-${tabId}`);
  const detailsEl = pane.querySelector(`#token-details-${tabId}`);
  const barEl = pane.querySelector(`#token-bar-${tabId}`);
  const summaryEl = pane.querySelector(`#token-summary-${tabId}`);
  if (!chipsEl || !grandTotalEl || !detailsEl) return;

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
  const gTotals =
    (stats && stats.google_search_totals) || state.googleSearchTotals || { monthly: 0, all_time: 0 };
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
    renderIcons(pane);
    return;
  }

  chipsEl.innerHTML = "";
  stats.per_model.forEach((m) => {
    const chip = document.createElement("div");
    chip.className = "token-chip";
    const cacheText =
      m.cached_tokens > 0
        ? `<span class="token-chip-cache">· ${formatTokens(m.cached_tokens)} cache (${m.cache_hit_rate}%)</span>`
        : "";
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

  const tableRows = stats.per_model
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
  renderIcons(pane);
}
