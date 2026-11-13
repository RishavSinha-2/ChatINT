/**
 * Real-time backend stream & provider-sync event dispatcher (`H1`, `H5`, `NEW-H1`, `NEW-H2`, `NEW-M2`, `NEW-M5`).
 */

import { persistTabsState } from "./api.js";
import {
  appendStreamErrorBanner,
  applyLiveStreamRender,
  clearWelcomeAndStaleBanners,
  createLiveStreamingRow,
  createMessageElement,
  pendingStreamRenderTabs,
  renderTabs,
  renderTokenStats,
  renderToolStatusBanner,
  scheduleStreamRender,
  syncAllPaneModelDropdowns,
} from "./panes.js";
import { renderConversationsList } from "./sidebar.js";
import {
  el,
  normalizeWebSearchModelsClient,
  refreshPaneHeaderLabels,
  renderIcons,
  resetTabStreamBuffers,
  state,
  uiHooks,
} from "./state.js";

export function onStreamEvent(event) {
  if (!event || !event.type) return;

  if (event.type === "provider_models_updated") {
    const updated = event.provider;
    if (!updated) return;
    const isSettingsOpen = Boolean(el.settingsModal && el.settingsModal.classList.contains("open") && state.settingsBuffer);
    if (isSettingsOpen) {
      if (uiHooks.saveGeneralTabInputs) uiHooks.saveGeneralTabInputs();
      if (uiHooks.saveActiveProviderInputs) uiHooks.saveActiveProviderInputs();
    }

    const idx = state.providers.findIndex((p) => p.id === updated.id);
    if (idx !== -1) {
      const currentProv = state.providers[idx];
      for (const k of Object.keys(currentProv)) {
        if (!(k in updated)) delete currentProv[k];
      }
      Object.assign(currentProv, updated);
    } else {
      state.providers.push(updated);
    }

    if (updated.id === "gemini" && Array.isArray(updated.web_search_models)) {
      state.webSearchModels = normalizeWebSearchModelsClient(updated.web_search_models);
    }

    syncAllPaneModelDropdowns();

    // Update Settings buffer ONLY if the user has NOT made uncommitted key/URL edits on that provider (`NEW-M5`)
    if (isSettingsOpen && state.settingsBuffer) {
      const bufProv = state.settingsBuffer.providers.find((p) => p.id === updated.id);
      if (bufProv && !bufProv._keyEdited && !bufProv._urlEdited) {
        bufProv.has_api_key = Boolean(updated.has_api_key);
        bufProv._saved_has_api_key = Boolean(updated.has_api_key);
        bufProv.api_key_mask = updated.api_key_mask || "";
        bufProv.sync_status = updated.sync_status || "";
        bufProv.sync_error = updated.sync_error || "";
        if (Array.isArray(updated.fetched_model_ids)) {
          bufProv.fetched_model_ids = updated.fetched_model_ids;
        }
        if (uiHooks.refreshProviderAccessibility) {
          uiHooks.refreshProviderAccessibility(bufProv);
        }
      }

      const isModelModalOpen = Boolean(el.modelConfigModal && el.modelConfigModal.classList.contains("open"));
      const activeEl = document.activeElement;
      const isInteractingInModal =
        activeEl &&
        el.settingsModal.contains(activeEl) &&
        (activeEl.tagName === "INPUT" || activeEl.tagName === "TEXTAREA" || activeEl.tagName === "SELECT");
      const draftModelInput = document.getElementById("new-model-id-input") || document.getElementById("ws-new-model-id-input");
      const hasDraftModelId = Boolean(draftModelInput && draftModelInput.value && draftModelInput.value.trim());

      if (!isModelModalOpen && !isInteractingInModal && !hasDraftModelId) {
        if (uiHooks.renderSettingsContent) uiHooks.renderSettingsContent();
      }
    }
    return;
  }

  if (event.type === "chat_named") {
    // Ignore late `chat_named` events for deleted conversations so they never resurrect in the sidebar (`H5`)
    if (event.conversation_id && state.recentlyDeletedConvIds.has(event.conversation_id)) {
      return;
    }

    let conv = state.conversations.find((c) => c.id === event.conversation_id);
    const matchingTab = state.tabs.find((t) => t.id === event.tab_id);
    if (conv) {
      conv.title = event.title;
      conv.updated_at = Date.now() / 1000;
    } else if (event.conversation_id) {
      state.conversations.unshift({
        id: event.conversation_id,
        title: event.title,
        system_prompt: matchingTab ? (matchingTab.systemPrompt || "") : "",
        max_context_messages: matchingTab ? (matchingTab.maxContextMessages || 30) : 30,
        updated_at: Date.now() / 1000,
      });
    }

    if (matchingTab && (!matchingTab.conversationId || matchingTab.conversationId === event.conversation_id)) {
      matchingTab.title = event.title;
      matchingTab.conversationId = event.conversation_id;
      matchingTab.hasUnansweredPrompt = false;
      matchingTab.pendingUserDraft = null;
      if (event.token_stats) {
        matchingTab.tokenStats = event.token_stats;
      }
      const matchingPane = el.panesContainer
        ? el.panesContainer.querySelector(`.chat-pane[data-tab-id="${matchingTab.id}"]`)
        : null;
      if (event.token_stats && matchingPane) {
        renderTokenStats(matchingPane, matchingTab.id, event.token_stats);
      }
    }

    persistTabsState();
    renderConversationsList();
    renderTabs();
    refreshPaneHeaderLabels();
    return;
  }

  const tab = state.tabs.find((t) => t.id === event.tab_id);
  if (!tab) return;

  if (event.stream_id && tab.activeStreamId && event.stream_id !== tab.activeStreamId) {
    return;
  }
  if (event.type !== "start" && event.stream_id && !tab.activeStreamId) {
    return;
  }
  if (event.conversation_id && tab.conversationId && event.conversation_id !== tab.conversationId) {
    return;
  }

  const pane = el.panesContainer ? el.panesContainer.querySelector(`.chat-pane[data-tab-id="${tab.id}"]`) : null;
  const messagesContainer = pane ? pane.querySelector(`#messages-${tab.id}`) : null;

  if (event.type === "start") {
    tab._streamGen = (tab._streamGen || 0) + 1;
    if (event.conversation_id && !tab.conversationId) {
      tab.conversationId = event.conversation_id;
      tab.pendingUserDraft = null;
      persistTabsState();
    }
    tab.isStreaming = true;
    tab.activeStreamId = event.stream_id || tab.activeStreamId || null;
    tab.lastStreamError = null;
    resetTabStreamBuffers(tab);
    tab.streamingProviderId = event.provider_id || tab.providerId;
    tab.streamingModelId = event.model_id || tab.modelId;
    tab.streamingThinkingLevel =
      event.thinking_level !== undefined && event.thinking_level !== null ? event.thinking_level : "none";
    tab.streamingStartTime = Date.now();
    if (uiHooks.scheduleNextEventPoll) uiHooks.scheduleNextEventPoll(20);
    renderTabs();

    if (pane && messagesContainer) {
      const sendBtn = pane.querySelector(`#send-btn-${tab.id}`);
      const stopBtn = pane.querySelector(`#stop-btn-${tab.id}`);
      if (sendBtn) sendBtn.style.display = "none";
      if (stopBtn) stopBtn.style.display = "flex";

      // Remove ONLY `.welcome-placeholder` / stale error banners—never `messagesContainer.innerHTML = ""` (`NEW-H1`)
      clearWelcomeAndStaleBanners(messagesContainer);

      const staleLive = document.getElementById(`live-msg-${tab.id}`);
      if (staleLive) staleLive.remove();

      const liveRow = createLiveStreamingRow(tab);
      messagesContainer.appendChild(liveRow);
      renderIcons(liveRow);
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }
  } else if (event.type === "tool_status") {
    tab.activeToolStatus = {
      status: event.status,
      tool: event.tool,
      query: event.query || "",
      ok: Boolean(event.ok),
      images_found: Number(event.images_found || 0),
      search_requests_used: Number(event.search_requests_used || 0),
    };
    if (pane && messagesContainer) {
      const toolBanner = document.getElementById(`live-tool-status-${tab.id}`);
      if (toolBanner) {
        renderToolStatusBanner(toolBanner, tab.activeToolStatus);
        messagesContainer.scrollTop = messagesContainer.scrollHeight;
      }
    }
  } else if (event.type === "thinking_chunk") {
    tab.streamingThinking += event.chunk || "";
    if (event.chunk && String(event.chunk).trim().length > 0) {
      tab.isActivelyThinking = true;
      tab.hasAutoCollapsedThinking = false;
    }
    if (pane && messagesContainer) {
      scheduleStreamRender(tab.id);
    }
  } else if (event.type === "content_chunk") {
    tab.streamingContent += event.chunk || "";
    if (event.chunk && String(event.chunk).trim().length > 0) {
      tab.isActivelyThinking = false;
    }
    if (pane && messagesContainer) {
      scheduleStreamRender(tab.id);
    }
  } else if (event.type === "done") {
    pendingStreamRenderTabs.delete(tab.id);
    tab._streamGen = (tab._streamGen || 0) + 1;
    tab.isStreaming = false;
    tab.activeStreamId = null;
    tab.lastStreamError = null;
    tab.pendingUserDraft = null;
    if (event.message) {
      tab.hasUnansweredPrompt = false;
      if (tab.conversationId && !state.recentlyDeletedConvIds.has(tab.conversationId)) {
        if (!state.conversations.some((c) => c.id === tab.conversationId)) {
          state.conversations.unshift({
            id: tab.conversationId,
            title: tab.title || "New Chat",
            system_prompt: tab.systemPrompt || "",
            max_context_messages: tab.maxContextMessages || 30,
            updated_at: Date.now() / 1000,
          });
          renderConversationsList();
        }
      }
    }
    resetTabStreamBuffers(tab);

    if (event.token_stats) {
      tab.tokenStats = event.token_stats;
      if (event.token_stats.google_search_totals) {
        state.googleSearchTotals = event.token_stats.google_search_totals;
      }
    }

    if (pane && messagesContainer) {
      const sendBtn = pane.querySelector(`#send-btn-${tab.id}`);
      const stopBtn = pane.querySelector(`#stop-btn-${tab.id}`);
      if (sendBtn) sendBtn.style.display = "flex";
      if (stopBtn) stopBtn.style.display = "none";

      const liveRow = document.getElementById(`live-msg-${tab.id}`);
      if (event.message) {
        const finalEl = createMessageElement(tab, event.message, pane);
        if (liveRow) {
          liveRow.replaceWith(finalEl);
        } else if (!messagesContainer.querySelector(`[data-msg-id="${event.message.id}"]`)) {
          messagesContainer.appendChild(finalEl);
        }
        renderIcons(finalEl);
      } else if (liveRow) {
        liveRow.remove();
      }

      if (event.token_stats) {
        renderTokenStats(pane, tab.id, event.token_stats);
      }
    }

    if (event.token_stats && el.panesContainer) {
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
    renderTabs();
  } else if (event.type === "error") {
    pendingStreamRenderTabs.delete(tab.id);
    tab._streamGen = (tab._streamGen || 0) + 1;
    tab.isStreaming = false;
    tab.activeStreamId = null;
    tab.lastStreamError = event.error || "Streaming error";

    if (pane && messagesContainer) {
      if (tab.streamingThinking || tab.streamingContent) {
        tab.isActivelyThinking = false;
        applyLiveStreamRender(tab.id);
      }
      const sendBtn = pane.querySelector(`#send-btn-${tab.id}`);
      const stopBtn = pane.querySelector(`#stop-btn-${tab.id}`);
      if (sendBtn) sendBtn.style.display = "flex";
      if (stopBtn) stopBtn.style.display = "none";

      let targetRow = document.getElementById(`live-msg-${tab.id}`);
      if (targetRow) {
        targetRow.removeAttribute("id");
        targetRow.classList.remove("live-streaming-row");
        targetRow.querySelectorAll("[id]").forEach((elWithId) => elWithId.removeAttribute("id"));
      }

      appendStreamErrorBanner(tab, pane, messagesContainer, tab.lastStreamError, targetRow);
      messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }
    resetTabStreamBuffers(tab);
    renderTabs();
  }
}
