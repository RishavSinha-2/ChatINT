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
  refreshPaneHeaderLabels,
  renderIcons,
  resetTabStreamBuffers,
  sanitizeSplitTabIds,
  state,
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
        const confirmed = await showConfirmDialog(`Delete chat "${conv.title}"?`);
        if (confirmed) {
          // Track deleted ID so late `chat_named` events never resurrect this chat (`H5`)
          state.recentlyDeletedConvIds.add(conv.id);
          callApi("delete_conversation", conv.id, false).catch(() => {});
          state.conversations = state.conversations.filter((c) => c.id !== conv.id);

          let affectedOpenPane = false;
          state.tabs.forEach((t) => {
            if (t.conversationId === conv.id) {
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
              t.hasUnansweredPrompt = false;
              t.pendingUserDraft = null;
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
        }
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

  if (tab.conversationId) {
    try {
      const conv = await callApi("get_conversation", tab.conversationId);
      if (conv && !conv.error) {
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

  tab.title = newTitle;
  tab.systemPrompt = newPrompt;
  tab.maxContextMessages = newLimit;

  if (tab.conversationId) {
    const renameRes = await callApi("rename_conversation", tab.conversationId, newTitle);
    await callApi("update_conversation_settings", tab.conversationId, newPrompt, newLimit);

    const resolvedTitle = (renameRes && renameRes.title) || newTitle;
    tab.title = resolvedTitle;
    const conv = state.conversations.find((c) => c.id === tab.conversationId);
    if (conv) {
      conv.title = resolvedTitle;
      conv.system_prompt = newPrompt;
      conv.max_context_messages = newLimit;
    }
  } else {
    const conv = await callApi("create_conversation", newTitle, newPrompt, newLimit);
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
