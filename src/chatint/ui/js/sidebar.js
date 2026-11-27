/**
 * Sidebar conversation list, Conversation Settings modal, Export modal, and Import handlers.
 */

import { callApi, persistTabsState } from "./api.js";
import { showConfirmDialog, showNoticeDialog, showPromptDialog } from "./dialogs.js";
import {
  assignTabToPaneSlot,
  renderActivePanes,
  renderTabs,
  switchActiveTab,
} from "./panes.js";
import {
  createTabObject,
  el,
  escapeHtml,
  getEffectiveAgentMode,
  refreshPaneHeaderLabels,
  renderIcons,
  resetTabStreamBuffers,
  sanitizeSplitTabIds,
  state,
  uiHooks,
} from "./state.js";

export const MAX_IMPORT_BYTES = 25 * 1024 * 1024;

export function renderConversationsList() {
  if (!el.conversationsList) return;
  el.conversationsList.innerHTML = "";
  const filter = el.convSearchInput ? el.convSearchInput.value.toLowerCase().trim() : "";

  state.conversations.forEach((conv) => {
    if (filter && !conv.title.toLowerCase().includes(filter)) return;

    const item = document.createElement("div");
    const isCurrent = state.tabs.some((t) => t.id === state.activeTabId && t.conversationId === conv.id);
    item.className = "conversation-item" + (isCurrent ? " active" : "");

    item.innerHTML = `
      <span class="conv-title">${escapeHtml(conv.title)}</span>
      <div class="conv-actions">
        <button class="conv-action-btn branch-conv-btn" title="Branch Chat">
          <i data-lucide="git-branch" style="width: 12px; height: 12px;"></i>
        </button>
        <button class="conv-action-btn edit-title-btn" title="Rename">
          <i data-lucide="pencil" style="width: 12px; height: 12px;"></i>
        </button>
        <button class="conv-action-btn delete-conv-btn" title="Delete">
          <i data-lucide="trash-2" style="width: 12px; height: 12px;"></i>
        </button>
      </div>
    `;

    item.onclick = async (e) => {
      if (e.target.closest(".branch-conv-btn")) {
        e.stopPropagation();
        const res = await callApi("duplicate_conversation", conv.id);
        if (res && res.conversation) {
          const newConv = res.conversation;
          state.recentlyDeletedConvIds.delete(newConv.id);
          if (!state.conversations.some((c) => c.id === newConv.id)) {
            state.conversations.unshift(newConv);
          }
          const activeTab = state.tabs.find((t) => t.id === state.activeTabId);
          const newTab = createTabObject({
            conversationId: newConv.id,
            title: newConv.title,
            systemPrompt: newConv.system_prompt || (activeTab ? activeTab.systemPrompt : ""),
            maxContextMessages: newConv.max_context_messages || (activeTab ? activeTab.maxContextMessages : 30),
            workspaceDir: newConv.workspace_dir !== undefined ? newConv.workspace_dir : (activeTab ? activeTab.workspaceDir : ""),
            agentMode: newConv.agent_mode || (activeTab ? activeTab.agentMode : "manual"),
            agentEnabled: activeTab ? activeTab.agentEnabled : false,
            providerId: activeTab ? (activeTab._preferredProviderId || activeTab.providerId) : undefined,
            modelId: activeTab ? (activeTab._preferredModelId || activeTab.modelId) : undefined,
            thinkingLevel: activeTab ? (activeTab._preferredThinkingLevel || activeTab.thinkingLevel) : undefined,
            webSearchEnabled: activeTab ? activeTab.webSearchEnabled : false,
            userSelectedModel: activeTab ? activeTab._userSelectedModel : false,
            userSelectedThinking: activeTab ? activeTab._userSelectedThinking : false,
          });
          state.tabs.push(newTab);
          state.activeTabId = newTab.id;
          if (!state.splitTabIds.includes(newTab.id)) {
            if (state.splitTabIds.length < 5) {
              state.splitTabIds.push(newTab.id);
            } else {
              const replaceIdx =
                state.splitTabIds[state.splitTabIds.length - 1] === (activeTab ? activeTab.id : null)
                  ? 0
                  : state.splitTabIds.length - 1;
              state.splitTabIds[replaceIdx] = newTab.id;
            }
          }
          persistTabsState();
          renderConversationsList();
          renderTabs();
          renderActivePanes();
        } else {
          showNoticeDialog(res && res.error ? res.error : "Failed to branch conversation.");
        }
      } else if (e.target.closest(".edit-title-btn")) {
        e.stopPropagation();
        const newTitle = await showPromptDialog("Rename chat", conv.title);
        if (newTitle && newTitle.trim()) {
          const requestedTitle = newTitle.trim();
          conv.title = requestedTitle;
          state.tabs.forEach((t) => {
            if (t.conversationId === conv.id) t.title = requestedTitle;
          });
          persistTabsState();
          renderConversationsList();
          renderTabs();
          refreshPaneHeaderLabels();

          callApi("rename_conversation", conv.id, requestedTitle).then((res) => {
            if (res && res.title && res.title !== requestedTitle) {
              conv.title = res.title;
              state.tabs.forEach((t) => {
                if (t.conversationId === conv.id) t.title = res.title;
              });
              persistTabsState();
              renderConversationsList();
              renderTabs();
              refreshPaneHeaderLabels();
            }
          });
        }
      } else if (e.target.closest(".delete-conv-btn")) {
        e.stopPropagation();
        await deleteConversationById(conv.id, conv.title);
      } else {
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
              callApi("stop_streaming", activeTab.id, activeTab.activeStreamId || null).catch(() => {});
              activeTab.isStreaming = false;
            }
            activeTab.activeStreamId = null;
            activeTab.lastStreamError = null;
            activeTab.conversationId = conv.id;
            activeTab.title = conv.title;
            activeTab.systemPrompt = conv.system_prompt || "";
            activeTab.maxContextMessages = conv.max_context_messages || 30;
            activeTab.workspaceDir = conv.workspace_dir || "";
            activeTab.agentMode = conv.agent_mode || (state.settings?.default_agent_mode === "auto" ? "auto" : "manual");
            activeTab.hasUnansweredPrompt = false;
            activeTab.pendingUserDraft = null;
            resetTabStreamBuffers(activeTab);
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

  renderIcons(el.conversationsList);
}

export async function deleteConversationById(convId, convTitle = "this chat") {
  if (!convId) return false;
  const confirmed = await showConfirmDialog(`Delete chat "${convTitle}"?`);
  if (!confirmed) return false;

  // Track deleted ID so late `chat_named` events never resurrect this chat (`H5`)
  state.recentlyDeletedConvIds.add(convId);
  await callApi("delete_conversation", convId, false).catch(() => {});
  state.conversations = state.conversations.filter((c) => c.id !== convId);

  let affectedOpenPane = false;
  state.tabs.forEach((t) => {
    if (t.conversationId === convId) {
      if (state.splitTabIds.includes(t.id)) {
        affectedOpenPane = true;
      }
      if (t.isStreaming) {
        callApi("stop_streaming", t.id, t.activeStreamId || null).catch(() => {});
        t.isStreaming = false;
      }
      t.activeStreamId = null;
      t.lastStreamError = null;
      t.conversationId = null;
      t.title = "New Chat";
      t.systemPrompt = "";
      t.maxContextMessages = 30;
      t.workspaceDir = "";
      t.agentMode = state.settings?.default_agent_mode === "auto" ? "auto" : "manual";
      t.hasUnansweredPrompt = false;
      t.pendingUserDraft = null;
      t.dbSizeBytes = 0;
      t.quotaNotice = null;
      t.quotaBannerDismissed = false;
      resetTabStreamBuffers(t);
    }
  });
  persistTabsState();
  renderConversationsList();
  renderTabs();
  if (affectedOpenPane) {
    renderActivePanes();
  } else {
    refreshPaneHeaderLabels();
  }
  return true;
}

export async function openConvSettingsModal(tabOrId) {
  const tab =
    typeof tabOrId === "string"
      ? (state.tabs.find((t) => t.id === tabOrId) || state.tabs.find((t) => t.conversationId === tabOrId))
      : tabOrId;
  if (!tab) return;

  state.activeConvSettingsTabId = tab.id;

  let currentTitle = tab.title || "New Chat";
  let currentPrompt = tab.systemPrompt || "";
  let currentLimit = tab.maxContextMessages || 30;
  let currentWorkspace = tab.workspaceDir || "";
  let currentAgentMode = getEffectiveAgentMode(tab);

  if (tab.conversationId) {
    try {
      const conv = await callApi("get_conversation", tab.conversationId);
      if (conv && !conv.error) {
        currentTitle = conv.title || currentTitle;
        currentPrompt = conv.system_prompt !== undefined ? conv.system_prompt : currentPrompt;
        currentLimit = conv.max_context_messages || currentLimit;
        currentWorkspace = conv.workspace_dir !== undefined ? (conv.workspace_dir || "") : currentWorkspace;
        currentAgentMode = conv.agent_mode === "auto" ? "auto" : (conv.agent_mode === "manual" ? "manual" : currentAgentMode);
      }
    } catch (e) {
      console.warn("[openConvSettingsModal] Error fetching conv:", e);
    }
  }

  el.convTitleInput.value = currentTitle;
  el.convSystemPromptInput.value = currentPrompt;
  el.convContextSlider.value = currentLimit;
  el.convContextLimitVal.textContent = currentLimit;

  const convWsInput = document.getElementById("conv-workspace-input");
  const convWsBrowseBtn = document.getElementById("conv-workspace-browse-btn");
  const convWsClearBtn = document.getElementById("conv-workspace-clear-btn");
  const convAgentModeSelect = document.getElementById("conv-agent-mode-select");
  if (convWsInput) convWsInput.value = currentWorkspace;
  if (convAgentModeSelect) convAgentModeSelect.value = currentAgentMode;
  if (convWsBrowseBtn) {
    convWsBrowseBtn.onclick = async () => {
      const res = await callApi("select_workspace_folder");
      if (res && res.error) {
        showNoticeDialog(res.error);
        return;
      }
      if (res && !res.cancelled && res.path && convWsInput) {
        convWsInput.value = res.path;
      }
    };
  }
  if (convWsClearBtn) {
    convWsClearBtn.onclick = () => {
      if (convWsInput) convWsInput.value = "";
    };
  }

  el.convContextSlider.oninput = () => {
    el.convContextLimitVal.textContent = el.convContextSlider.value;
  };

  el.convSettingsModal.classList.add("open");
  renderIcons(el.convSettingsModal);
  setTimeout(() => el.convTitleInput.focus(), 50);
}

export async function saveConvSettings() {
  if (!state.activeConvSettingsTabId) return;
  const tab = state.tabs.find((t) => t.id === state.activeConvSettingsTabId);
  if (!tab) return;

  const newTitle = el.convTitleInput.value.trim() || "New Chat";
  const newPrompt = el.convSystemPromptInput.value.trim();
  const newLimit = parseInt(el.convContextSlider.value, 10) || 30;
  const convWsInput = document.getElementById("conv-workspace-input");
  const convAgentModeSelect = document.getElementById("conv-agent-mode-select");
  const newWorkspace = convWsInput ? convWsInput.value.trim() : (tab.workspaceDir || "");
  const newAgentMode = convAgentModeSelect && convAgentModeSelect.value === "auto" ? "auto" : "manual";

  if (tab.conversationId) {
    const updRes = await callApi("update_conversation_settings", tab.conversationId, newPrompt, newLimit, newWorkspace, newAgentMode);
    if (updRes && updRes.error) {
      showNoticeDialog(updRes.error);
      return;
    }
    const renameRes = await callApi("rename_conversation", tab.conversationId, newTitle);
    const resolvedTitle = (renameRes && renameRes.title) || newTitle;
    tab.title = resolvedTitle;
    tab.systemPrompt = newPrompt;
    tab.maxContextMessages = newLimit;
    tab.workspaceDir = newWorkspace;
    tab.agentMode = newAgentMode;

    const conv = state.conversations.find((c) => c.id === tab.conversationId);
    if (conv) {
      conv.title = resolvedTitle;
      conv.system_prompt = newPrompt;
      conv.max_context_messages = newLimit;
      conv.workspace_dir = newWorkspace;
      conv.agent_mode = newAgentMode;
    }
    state.tabs.forEach((otherTab) => {
      if (otherTab.conversationId === tab.conversationId) {
        otherTab.workspaceDir = newWorkspace;
        otherTab.agentMode = newAgentMode;
      }
    });
  } else {
    tab.title = newTitle;
    tab.systemPrompt = newPrompt;
    tab.maxContextMessages = newLimit;
    tab.workspaceDir = newWorkspace;
    tab.agentMode = newAgentMode;
    const conv = await callApi("create_conversation", newTitle, newPrompt, newLimit, newWorkspace, newAgentMode);
    if (conv && conv.id) {
      tab.conversationId = conv.id;
      if (conv.title) tab.title = conv.title;
    }
  }

  persistTabsState();
  el.convSettingsModal.classList.remove("open");
  renderConversationsList();
  renderTabs();
  refreshPaneHeaderLabels();
  uiHooks.syncAllPaneAgentControls?.();
}

export function openExportModal(convId) {
  state.activeExportConvId = convId;
  el.exportModal.classList.add("open");
  renderIcons(el.exportModal);
}

export async function downloadExport(format) {
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
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Defer revoking blob URL so WebView2 finishes initiating the download (`NEW-L1`)
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }
}

export async function openUploadChat() {
  const res = await callApi("open_import_chat_dialog");
  if (res && res.cancelled) {
    return;
  }
  if (res && res.fallback) {
    if (el.uploadChatFileInput) el.uploadChatFileInput.click();
    return;
  }
  if (res && res.conversation) {
    handleImportedConversation(res.conversation);
  } else if (res && res.error) {
    showNoticeDialog(res.error);
  }
}

export function handleImportedConversation(newConv) {
  state.recentlyDeletedConvIds.delete(newConv.id);
  if (!state.conversations.some((c) => c.id === newConv.id)) {
    state.conversations.unshift(newConv);
  }
  const activeTab = state.tabs.find((t) => t.id === state.activeTabId);
  const newTab = createTabObject({
    conversationId: newConv.id,
    title: newConv.title,
    systemPrompt: newConv.system_prompt || "",
    maxContextMessages: newConv.max_context_messages || 30,
    workspaceDir: newConv.workspace_dir || "",
    agentMode: newConv.agent_mode || (state.settings?.default_agent_mode === "auto" ? "auto" : "manual"),
    agentEnabled: activeTab ? activeTab.agentEnabled : false,
    providerId: activeTab ? (activeTab._preferredProviderId || activeTab.providerId) : undefined,
    modelId: activeTab ? (activeTab._preferredModelId || activeTab.modelId) : undefined,
    thinkingLevel: activeTab ? (activeTab._preferredThinkingLevel || activeTab.thinkingLevel) : undefined,
    webSearchEnabled: activeTab ? activeTab.webSearchEnabled : false,
    userSelectedModel: activeTab ? activeTab._userSelectedModel : false,
    userSelectedThinking: activeTab ? activeTab._userSelectedThinking : false,
  });
  state.tabs.push(newTab);
  state.activeTabId = newTab.id;
  if (!state.splitTabIds.includes(newTab.id)) {
    if (state.splitTabIds.length < 5) {
      state.splitTabIds.push(newTab.id);
    } else {
      const replaceIdx =
        state.splitTabIds[state.splitTabIds.length - 1] === (activeTab ? activeTab.id : null)
          ? 0
          : state.splitTabIds.length - 1;
      state.splitTabIds[replaceIdx] = newTab.id;
    }
  }
  persistTabsState();
  renderConversationsList();
  renderTabs();
  renderActivePanes();
}
