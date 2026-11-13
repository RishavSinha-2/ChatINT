/**
 * PyWebView RPC bridge client (unwrapping uniform `{ok, data, error}` envelopes)
 * and dirty-tracked tab state persistence (`L3` & `R7`).
 */

import { state } from "./state.js";

let isPageUnloading = false;
let persistTabsTimer = null;
let tabsStateDirty = false;

export function setPageUnloading(val) {
  isPageUnloading = Boolean(val);
}

export function isPageUnloadingState() {
  return isPageUnloading;
}

export function isTabsStateDirty() {
  return tabsStateDirty;
}

export async function callApiEnvelope(method, ...args) {
  if (window.pywebview && window.pywebview.api && typeof window.pywebview.api[method] === "function") {
    try {
      const raw = await window.pywebview.api[method](...args);
      if (raw && typeof raw === "object" && typeof raw.ok === "boolean" && "data" in raw && "error" in raw) {
        return raw;
      }
      return { ok: true, data: raw, error: null };
    } catch (err) {
      console.error(`[API Call Error] ${method}:`, err);
      throw err;
    }
  }
  console.warn(`[pywebview.api not ready] Skipped call to ${method}`);
  return null;
}

export async function callApi(method, ...args) {
  const env = await callApiEnvelope(method, ...args);
  if (!env) return null;
  if (env.ok) {
    return env.data;
  }
  if (env.data && typeof env.data === "object" && !Array.isArray(env.data)) {
    return { ...env.data, ok: false, error: env.error || env.data.error || "Operation failed." };
  }
  return { ok: false, error: env.error || "Operation failed." };
}

export function waitForPyWebView(timeoutMs = 6000) {
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

export function serializeTabsState() {
  return {
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
}

export function flushTabsState() {
  if (!tabsStateDirty && persistTabsTimer === null) {
    return false;
  }
  if (persistTabsTimer) {
    clearTimeout(persistTabsTimer);
    persistTabsTimer = null;
  }
  tabsStateDirty = false;
  if (!state.tabs || state.tabs.length === 0) return false;
  callApi("save_tabs_state", serializeTabsState()).catch(() => {});
  return true;
}

export function persistTabsState() {
  tabsStateDirty = true;
  if (persistTabsTimer) {
    clearTimeout(persistTabsTimer);
  }
  persistTabsTimer = setTimeout(() => {
    persistTabsTimer = null;
    if (!isPageUnloading && tabsStateDirty) {
      tabsStateDirty = false;
      callApi("save_tabs_state", serializeTabsState()).catch(() => {});
    }
  }, 180);
}
