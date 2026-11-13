/**
 * Settings modal (General, Providers, Web Search) and Model Pricing/Thinking sub-modal
 * (`NEW-M4`, `NEW-M5`, `R6`).
 */

import { callApi } from "./api.js";
import { applyTheme, showConfirmDialog, showNoticeDialog, showPromptDialog } from "./dialogs.js";
import {
  renderTabs,
  renderTokenStats,
  syncAllPaneModelDropdowns,
  syncAllPaneWebSearchButtons,
} from "./panes.js";
import {
  PRIMARY_WEB_SEARCH_MODEL,
  THINKING_LEVELS,
  VALID_THINKING_LEVELS,
  el,
  escapeHtml,
  getDefaultThinkingLevel,
  getVerifiedProviderModels,
  getWebSearchAvailability,
  isLoopbackBaseUrl,
  normalizeWebSearchModelsClient,
  renderIcons,
  state,
  stripModelPrefix,
} from "./state.js";

let activeConfigModelRef = null;

export function sanitizeProviderNameAlpha(raw) {
  return String(raw || "").replace(/[^A-Za-z]/g, "");
}

export function validateAllProviders(providersList = null) {
  const list = providersList || (state.settingsBuffer ? state.settingsBuffer.providers : state.providers);
  const seen = new Set();
  for (const p of list) {
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

export function canShowProviderModels(prov) {
  if (!prov || prov.clear_api_key) return false;
  const hasFetched = Array.isArray(prov.fetched_model_ids) && prov.fetched_model_ids.length > 0;
  const loopbackOk = isLoopbackBaseUrl(prov.base_url) && (prov.sync_status === "ready" || hasFetched);
  const hasKey = Boolean((prov.api_key && prov.api_key.trim()) || prov.has_api_key || loopbackOk);
  return hasKey && (prov.sync_status === "ready" || hasFetched);
}

export function refreshProviderAccessibility(prov) {
  if (!prov) return;
  const typedKey = (prov.api_key || "").trim();
  const hasFetched = Array.isArray(prov.fetched_model_ids) && prov.fetched_model_ids.length > 0;
  const loopbackReady = !prov.clear_api_key && isLoopbackBaseUrl(prov.base_url) && (prov.sync_status === "ready" || hasFetched);
  const hasCredentials = Boolean(typedKey || (!prov.clear_api_key && prov.has_api_key) || loopbackReady);
  const verifiedModels = getVerifiedProviderModels(prov);
  prov.has_api_key = Boolean(hasCredentials);
  prov.is_accessible = Boolean(
    prov.is_enabled && hasCredentials && (prov.sync_status === "ready" || hasFetched) && verifiedModels.length > 0
  );
}

export function openSettingsModal() {
  state.webSearchModels = normalizeWebSearchModelsClient(state.webSearchModels);
  const clonedProviders = JSON.parse(JSON.stringify(state.providers));
  clonedProviders.forEach((p) => {
    p._saved_has_api_key = Boolean(p.has_api_key);
    p._keyEdited = false;
    p._urlEdited = false;
  });
  state.settingsBuffer = {
    providers: clonedProviders,
    settings: JSON.parse(JSON.stringify(state.settings)),
    webSearchModels: JSON.parse(JSON.stringify(state.webSearchModels)),
    activeProviderId: state.activeProviderId || (state.providers[0] ? state.providers[0].id : null),
    masterSettingsTab: state.masterSettingsTab || "general",
  };
  updateMasterTabsUI();
  renderSettingsContent({ resetScroll: true });
  el.settingsModal.classList.add("open");
  renderIcons(el.settingsModal);
}

export function closeSettingsModal(saved = false) {
  if (!saved) {
    applyTheme(state.settings.theme || "dark");
  }
  state.settingsBuffer = null;
  el.settingsModal.classList.remove("open");
}

export function updateMasterTabsUI() {
  const curTab = state.settingsBuffer ? state.settingsBuffer.masterSettingsTab : state.masterSettingsTab;
  el.masterTabGeneral.classList.toggle("active", curTab === "general");
  el.masterTabProviders.classList.toggle("active", curTab === "providers");
  if (el.masterTabWebSearch) {
    el.masterTabWebSearch.classList.toggle("active", curTab === "websearch");
  }
}

export function openModelConfigModal(prov, modelObj, options = {}) {
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

export function closeModelConfigModal(save = false) {
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

function applyFetchedModelsToProvider(prov, res, { autoAdd = false } = {}) {
  const fetched = res.models || [];
  const fetchedIds = fetched.map((m) => m.id);
  const cleanFetchedIds = new Set(fetchedIds.map((id) => stripModelPrefix(id).toLowerCase()));
  const retained = (prov.models || []).filter(
    (m) => m && m.id && cleanFetchedIds.has(stripModelPrefix(m.id).toLowerCase())
  );

  if (autoAdd && retained.length < 5) {
    const cleanRetainedIds = new Set(retained.map((m) => stripModelPrefix(m.id).toLowerCase()));
    for (const candidate of fetched) {
      if (retained.length >= 5) break;
      const cleanCandId = stripModelPrefix(candidate.id).toLowerCase();
      if (cleanRetainedIds.has(cleanCandId)) continue;
      const isThinking = /o1|o3|r1|reasoning|thinking/i.test(candidate.id)
        ? 1
        : (candidate.supports_thinking || 0);
      retained.push({
        id: candidate.id,
        name: candidate.name || candidate.id,
        supports_thinking: isThinking,
        input_price: candidate.input_price || 0.0,
        cached_input_price: candidate.cached_input_price || 0.0,
        output_price: candidate.output_price || 0.0,
      });
      cleanRetainedIds.add(cleanCandId);
    }
  }

  prov.models = autoAdd ? retained.slice(0, 5) : retained;
  prov.fetched_model_ids = fetchedIds;
  prov.has_api_key = true;
  prov._keyEdited = true;
  prov.sync_status = res.sync_status || "ready";
  prov.sync_error = res.sync_error || "";
  refreshProviderAccessibility(prov);
  return { fetched, cleanFetchedIds };
}

function applyFetchedModelsToWebSearch(buf, fetched, cleanFetchedIds, { autoAdd = false } = {}) {
  const [wsPrimary, ...wsFallbacks] = normalizeWebSearchModelsClient(buf.webSearchModels);
  const retainedWs = wsFallbacks.filter((m) => cleanFetchedIds.has(stripModelPrefix(m.id).toLowerCase()));
  const wsModels = [wsPrimary, ...retainedWs];

  if (autoAdd && wsModels.length < 5) {
    const configuredWsIds = new Set(wsModels.map((m) => stripModelPrefix(m.id).toLowerCase()));
    for (const candidate of fetched) {
      if (wsModels.length >= 5) break;
      const canonicalId = stripModelPrefix(candidate.id);
      if (configuredWsIds.has(canonicalId.toLowerCase())) continue;
      wsModels.push({
        id: canonicalId,
        name: candidate.name || canonicalId,
        input_price: candidate.input_price || 0.0,
        cached_input_price: candidate.cached_input_price || 0.0,
        output_price: candidate.output_price || 0.0,
      });
      configuredWsIds.add(canonicalId.toLowerCase());
    }
  }

  buf.webSearchModels = normalizeWebSearchModelsClient(wsModels);
}

async function runProviderFetchAction({
  prov,
  btn,
  idleHtml,
  useTypedBaseUrl = false,
  onSuccess,
  failPrefix = "Could not fetch models",
}) {
  const typedKey = prov ? ((prov.api_key || "").trim() || null) : null;
  // Pass `""` (not `null`) when `useTypedBaseUrl` is true and user cleared `#prov-base-url-input` (`NEW-M5`)
  const typedBaseUrl = useTypedBaseUrl && prov ? String(prov.base_url ?? "").trim() : null;
  const isEnabled = prov ? Boolean(prov.is_enabled) : true;
  const clearApiKey = Boolean(prov && prov.clear_api_key && !typedKey);
  const provName = prov ? (prov.name || null) : null;

  btn.disabled = true;
  btn.innerHTML = '<i data-lucide="loader" style="width: 11px; height: 11px;"></i> Fetching...';
  renderIcons(btn);

  let res = null;
  let callErr = null;
  try {
    res = await callApi(
      "fetch_provider_models",
      prov ? prov.id : "",
      typedKey,
      typedBaseUrl,
      isEnabled,
      clearApiKey,
      provName
    );
  } catch (err) {
    callErr = err;
  } finally {
    btn.disabled = false;
    btn.innerHTML = idleHtml;
    renderIcons(btn);
  }

  if (res && !res.error && res.models && res.models.length > 0 && prov) {
    onSuccess(res);
    return;
  }

  if (prov) {
    prov._keyEdited = true;
    prov.is_accessible = false;
    prov.fetched_model_ids = [];
    prov.sync_status = (res && res.sync_status) || "error";
    prov.sync_error = (res && res.sync_error) || "FETCH ERROR";
  }
  renderSettingsContent();
  const errMsg =
    (res && res.error) ||
    (callErr && (callErr.message || String(callErr))) ||
    "Endpoint unreachable or invalid API key";
  showNoticeDialog(`${failPrefix}: ${errMsg}`);
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

function buildDefaultModelOptionsHtml(buf, providerId, selectedModelId) {
  const defProv = buf.providers.find((p) => p.id === providerId) || buf.providers[0] || null;
  const models = Array.isArray(defProv?.models) ? defProv.models : [];
  const cleanSelected = String(selectedModelId || "").trim();
  const options = [
    `<option value="" ${!cleanSelected ? "selected" : ""}>Top Priority Model (Automatic)</option>`,
  ];
  let matched = !cleanSelected;
  for (const m of models) {
    if (!m || !m.id) continue;
    const isSel = m.id === cleanSelected;
    if (isSel) matched = true;
    options.push(`<option value="${escapeHtml(m.id)}" ${isSel ? "selected" : ""}>${escapeHtml(m.name || m.id)}</option>`);
  }
  if (!matched && cleanSelected) {
    options.push(`<option value="${escapeHtml(cleanSelected)}" selected>${escapeHtml(cleanSelected)}</option>`);
  }
  return options.join("");
}

export function renderSettingsContent(options = {}) {
  if (!state.settingsBuffer) return;
  const buf = state.settingsBuffer;
  const resetScroll = Boolean(options.resetScroll);
  const prevTab = el.settingsTabContent.dataset.renderedTab || "";
  const prevProvId = el.settingsTabContent.dataset.renderedProvId || "";
  const modalBody = el.settingsTabContent.closest(".modal-body");
  const prevModelsList = document.getElementById("prov-models-list") || document.getElementById("ws-models-list");
  const prevSubNav = el.settingsTabContent.querySelector(".providers-sub-nav");

  const prevModalScroll = !resetScroll && modalBody ? modalBody.scrollTop : 0;
  const prevSubNavScroll = !resetScroll && prevSubNav ? prevSubNav.scrollTop : 0;
  const targetProvId = buf.masterSettingsTab === "providers" ? (buf.activeProviderId || buf.providers[0]?.id || "") : "";
  const sameProviderView =
    !resetScroll &&
    prevTab === "providers" &&
    buf.masterSettingsTab === "providers" &&
    prevProvId === targetProvId;
  const sameWebSearchView = !resetScroll && prevTab === "websearch" && buf.masterSettingsTab === "websearch";
  const prevModelsScroll = (sameProviderView || sameWebSearchView) && prevModelsList ? prevModelsList.scrollTop : 0;
  const sameGeneralView = !resetScroll && prevTab === "general" && buf.masterSettingsTab === "general";

  el.settingsTabContent.innerHTML = "";

  if (buf.masterSettingsTab === "general") {
    const defProvId = buf.settings.default_provider || buf.providers[0]?.id || "openai";
    const defModelOptions = buildDefaultModelOptionsHtml(buf, defProvId, buf.settings.default_model);

    el.settingsTabContent.innerHTML = `
      <div class="settings-group">
        <label class="settings-label">Global Default System Prompt</label>
        <textarea class="settings-input" id="setting-global-system-prompt" rows="3">${escapeHtml(buf.settings.global_system_prompt || "")}</textarea>
      </div>

      <div class="settings-group">
        <label class="settings-label">Default Provider</label>
        <select class="settings-input" id="setting-default-provider">
          ${buf.providers.map((p) => `<option value="${escapeHtml(p.id)}" ${p.id === defProvId ? "selected" : ""}>${escapeHtml(p.name)}</option>`).join("")}
        </select>
      </div>

      <div class="settings-group">
        <label class="settings-label">Default Model</label>
        <select class="settings-input" id="setting-default-model">
          ${defModelOptions}
        </select>
      </div>

      <div class="settings-group">
        <label class="settings-label">Default Thinking</label>
        <select class="settings-input" id="setting-default-thinking">
          ${THINKING_LEVELS.map(
            (lvl) => `<option value="${lvl.value}" ${lvl.value === (buf.settings.default_thinking || "medium") ? "selected" : ""}>${lvl.label}</option>`
          ).join("")}
        </select>
      </div>

      <div class="settings-group">
        <label class="settings-label">Appearance Theme</label>
        <select class="settings-input" id="setting-theme">
          <option value="dark" ${buf.settings.theme === "dark" ? "selected" : ""}>Dark Theme</option>
          <option value="light" ${buf.settings.theme === "light" ? "selected" : ""}>Light Theme</option>
        </select>
      </div>

      <div class="settings-group" style="padding-top: 6px; border-top: 1px solid var(--border-subtle);">
        <div style="display: flex; align-items: center; justify-content: space-between; gap: 12px;">
          <div style="display: flex; flex-direction: column; gap: 2px;">
            <label class="settings-label">Diagnostics &amp; Logs</label>
            <span style="font-size: 11.5px; color: var(--text-muted);">Rotating logs with automatic API key scrubbing are saved in <code>data/logs/chatint.log</code>.</span>
          </div>
          <button type="button" class="btn-secondary" id="open-logs-folder-btn" style="display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;">
            <i data-lucide="folder-open" style="width: 13px; height: 13px;"></i>
            <span>Open Logs Folder</span>
          </button>
        </div>
      </div>
    `;

    const defProvSelect = document.getElementById("setting-default-provider");
    const defModelSelect = document.getElementById("setting-default-model");
    if (defProvSelect && defModelSelect) {
      defProvSelect.onchange = () => {
        const nextPid = defProvSelect.value;
        buf.settings.default_provider = nextPid;
        const nextProv = buf.providers.find((p) => p.id === nextPid);
        const nextDefaultModel = nextProv?.models?.[0]?.id || "";
        buf.settings.default_model = nextDefaultModel;
        defModelSelect.innerHTML = buildDefaultModelOptionsHtml(buf, nextPid, nextDefaultModel);
      };
      defModelSelect.onchange = () => {
        buf.settings.default_model = defModelSelect.value;
      };
    }

    const themeSelect = document.getElementById("setting-theme");
    if (themeSelect) {
      themeSelect.onchange = () => {
        buf.settings.theme = themeSelect.value;
        applyTheme(themeSelect.value, { persistState: false });
      };
    }

    const openLogsBtn = document.getElementById("open-logs-folder-btn");
    if (openLogsBtn) {
      openLogsBtn.onclick = async () => {
        const res = await callApi("open_logs_folder");
        if (res && res.error) {
          showNoticeDialog(res.error);
        }
      };
    }
  } else if (buf.masterSettingsTab === "websearch") {
    buf.webSearchModels = normalizeWebSearchModelsClient(buf.webSearchModels);
    const geminiProv = buf.providers.find((p) => p.id === "gemini");
    const { available: wsAvail, reason: wsReason } = getWebSearchAvailability(geminiProv, buf.webSearchModels);
    const wsStatusLine = wsAvail
      ? '<span style="color: #34a853; font-weight: 600;">Ready</span> · Using your Gemini provider API key.'
      : `<span style="color: var(--danger-text); font-weight: 600;">Unavailable (${escapeHtml(wsReason)})</span> · Configure Gemini in Providers.`;

    const wsModels = buf.webSearchModels;
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
              <button class="btn-secondary ws-auto-add-models-btn" style="padding: 2px 8px; font-size: 11px; display: flex; align-items: center; gap: 4px;" title="Fetch Gemini models, prune missing models, and auto-add available models up to 5 total">
                <i data-lucide="plus-circle" style="width: 11px; height: 11px;"></i>
                <span>Auto add</span>
              </button>
              <button class="btn-secondary ws-fetch-models-btn" style="padding: 2px 8px; font-size: 11px; display: flex; align-items: center; gap: 4px;" title="Fetch available Gemini models and prune missing fallback models (never auto-adds)">
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

    el.settingsTabContent.querySelectorAll(".ws-model-config-btn").forEach((btn) => {
      btn.onclick = () => {
        const mid = btn.dataset.modelId;
        const target = buf.webSearchModels.find((m) => m.id === mid);
        if (target) {
          openModelConfigModal(geminiProv || null, target, { isWebSearch: true });
        }
      };
    });

    const swapWsModel = (mid, delta) => {
      const swapped = swapModelInArray(buf.webSearchModels, mid, delta, 1);
      if (swapped) {
        buf.webSearchModels = swapped;
        renderSettingsContent();
      }
    };
    el.settingsTabContent.querySelectorAll(".ws-move-up-btn").forEach((btn) => {
      btn.onclick = () => swapWsModel(btn.dataset.modelId, -1);
    });
    el.settingsTabContent.querySelectorAll(".ws-move-down-btn").forEach((btn) => {
      btn.onclick = () => swapWsModel(btn.dataset.modelId, 1);
    });

    el.settingsTabContent.querySelectorAll(".ws-delete-model-btn").forEach((btn) => {
      btn.onclick = () => {
        const mid = btn.dataset.modelId;
        if (mid.toLowerCase() === PRIMARY_WEB_SEARCH_MODEL) return;
        buf.webSearchModels = buf.webSearchModels.filter((m) => m.id !== mid);
        renderSettingsContent();
      };
    });

    const wsAddBtn = document.getElementById("ws-add-model-btn");
    const wsAddInput = document.getElementById("ws-new-model-id-input");
    if (wsAddBtn && wsAddInput) {
      const triggerAddWsModel = () => {
        const mid = stripModelPrefix(wsAddInput.value.trim());
        if (!mid) return;
        const fetchedIds =
          geminiProv && Array.isArray(geminiProv.fetched_model_ids) ? geminiProv.fetched_model_ids : [];
        const cleanFetched = new Map(
          fetchedIds.map((fid) => [stripModelPrefix(fid).toLowerCase(), stripModelPrefix(fid)])
        );
        if (cleanFetched.size === 0) {
          showNoticeDialog("Please click 'Fetch Gemini Models' first (requires a valid Gemini API key).");
          return;
        }
        const canonical = cleanFetched.get(mid.toLowerCase());
        if (!canonical) {
          showNoticeDialog(
            `Model "${mid}" was not found in fetched Gemini models. Only verified Gemini models can be added.`
          );
          return;
        }
        if (buf.webSearchModels.some((m) => m.id.toLowerCase() === canonical.toLowerCase())) {
          wsAddInput.value = "";
          return;
        }
        if (buf.webSearchModels.length >= 5) {
          showNoticeDialog("Maximum 5 models can be configured for Web Search. Remove a fallback model first.");
          return;
        }
        buf.webSearchModels.push({
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
        if (e.key === "Enter" && !e.isComposing) {
          e.preventDefault();
          triggerAddWsModel();
        }
      };
    }

    const wsAutoAddBtn = el.settingsTabContent.querySelector(".ws-auto-add-models-btn");
    if (wsAutoAddBtn) {
      wsAutoAddBtn.onclick = async () => {
        saveGeneralTabInputs();
        saveActiveProviderInputs();
        await runProviderFetchAction({
          prov: geminiProv,
          btn: wsAutoAddBtn,
          idleHtml: '<i data-lucide="plus-circle" style="width: 11px; height: 11px;"></i> Auto add',
          failPrefix: "Could not fetch Gemini models",
          onSuccess: (res) => {
            const { fetched, cleanFetchedIds } = applyFetchedModelsToProvider(geminiProv, res, { autoAdd: false });
            applyFetchedModelsToWebSearch(buf, fetched, cleanFetchedIds, { autoAdd: true });
            renderSettingsContent();
            showNoticeDialog(
              `Fetched ${fetched.length} Gemini models. Configured search models: ${buf.webSearchModels.length}/5.`
            );
          },
        });
      };
    }

    const wsFetchBtn = el.settingsTabContent.querySelector(".ws-fetch-models-btn");
    if (wsFetchBtn) {
      wsFetchBtn.onclick = async () => {
        saveGeneralTabInputs();
        saveActiveProviderInputs();
        await runProviderFetchAction({
          prov: geminiProv,
          btn: wsFetchBtn,
          idleHtml: '<i data-lucide="refresh-cw" style="width: 11px; height: 11px;"></i> Fetch Gemini Models',
          failPrefix: "Could not fetch Gemini models",
          onSuccess: (res) => {
            const { fetched, cleanFetchedIds } = applyFetchedModelsToProvider(geminiProv, res, { autoAdd: false });
            applyFetchedModelsToWebSearch(buf, fetched, cleanFetchedIds, { autoAdd: false });
            renderSettingsContent();
            showNoticeDialog(
              `Fetched ${fetched.length} Gemini models. Preserved ${buf.webSearchModels.length} search models.`
            );
          },
        });
      };
    }
  } else {
    if (!buf.activeProviderId && buf.providers.length > 0) {
      buf.activeProviderId = buf.providers[0].id;
    }
    const activeProv = buf.providers.find((p) => p.id === buf.activeProviderId) || buf.providers[0];

    let subNavHtml = buf.providers
      .map(
        (p) => `
      <button class="provider-sub-btn ${p.id === activeProv?.id ? "active" : ""}" data-prov-id="${escapeHtml(p.id)}">
        <span>${escapeHtml(p.name || "(Unnamed)")}</span>
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
      const keyPlaceholder =
        activeProv.has_api_key && !activeProv.clear_api_key
          ? (activeProv.api_key_mask
              ? `Saved (${activeProv.api_key_mask}) — type a new key to replace`
              : `Saved in .env (${envKeyName}) — type a new key to replace`)
          : `Saved in .env as ${envKeyName}...`;

      const provModelsVisible = canShowProviderModels(activeProv);
      const provModelsHtml = provModelsVisible
        ? (activeProv.models || []).map((m, idx, list) => renderConfiguredModelRow(m, idx, list.length, false)).join("")
        : `<div style="padding: 10px; font-size: 11.5px; color: var(--text-muted); text-align: center;">${escapeHtml(
            !activeProv.has_api_key || activeProv.clear_api_key
              ? "Enter a valid API key (or local endpoint) and fetch models to view configured models. Any previously saved models remain preserved."
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
                <button class="btn-secondary auto-add-models-btn" style="padding: 2px 8px; font-size: 11px; display: flex; align-items: center; gap: 4px;" title="Fetch models from endpoint, prune missing models, and auto-add available models up to 5 total">
                  <i data-lucide="plus-circle" style="width: 11px; height: 11px;"></i>
                  <span>Auto add</span>
                </button>
                <button class="btn-secondary fetch-models-btn" style="padding: 2px 8px; font-size: 11px; display: flex; align-items: center; gap: 4px;" title="Fetch models from endpoint and prune missing models (never auto-adds)">
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

    const provNameInput = document.getElementById("prov-name-input");
    if (provNameInput && activeProv) {
      provNameInput.oninput = () => {
        const cleaned = sanitizeProviderNameAlpha(provNameInput.value);
        if (provNameInput.value !== cleaned) {
          provNameInput.value = cleaned;
        }
        // Assign unconditionally so clearing `#prov-name-input` to "" fails validation cleanly (`NEW-M5`)
        activeProv.name = cleaned;
        const subBtnSpan = el.settingsTabContent.querySelector(
          `.provider-sub-btn[data-prov-id="${activeProv.id}"] span`
        );
        if (subBtnSpan) subBtnSpan.textContent = cleaned || "(Unnamed)";
        const envLbl = document.getElementById("prov-env-key-label");
        if (envLbl) {
          envLbl.textContent = `API Key (.env: ${(cleaned || "PROVIDER").toUpperCase()}_API_KEY)`;
        }
      };
    }

    const bUrlInput = document.getElementById("prov-base-url-input");
    if (bUrlInput && activeProv) {
      bUrlInput.oninput = () => {
        activeProv._urlEdited = true;
        activeProv.base_url = bUrlInput.value.trim();
      };
    }

    el.settingsTabContent.querySelectorAll(".provider-sub-btn").forEach((btn) => {
      btn.onclick = () => {
        saveActiveProviderInputs();
        buf.activeProviderId = btn.dataset.provId;
        renderSettingsContent();
      };
    });

    const addProvBtn = document.getElementById("add-new-provider-btn");
    if (addProvBtn) {
      addProvBtn.onclick = async () => {
        saveActiveProviderInputs();
        const rawName = await showPromptDialog(
          "Enter Provider Name (English letters only, e.g. Groq):",
          "CustomProvider",
          "Letters A-Z only"
        );
        if (!rawName) return;
        const cleanName = sanitizeProviderNameAlpha(rawName);
        if (!cleanName || cleanName !== rawName.trim()) {
          await showNoticeDialog(
            "Provider name may only contain English alphabet letters (A-Z, a-z) with no spaces, dashes, underscores, numbers, or special characters."
          );
          return;
        }
        if (buf.providers.some((p) => (p.name || "").toUpperCase() === cleanName.toUpperCase())) {
          await showNoticeDialog(
            `A provider named "${cleanName}" already exists (case-insensitive). Please choose a unique name.`
          );
          return;
        }
        const newId = "provider_" + Date.now();
        const newProv = {
          id: newId,
          name: cleanName,
          base_url: "https://api.openai.com/v1",
          has_api_key: false,
          _saved_has_api_key: false,
          _keyEdited: true,
          _urlEdited: true,
          is_enabled: true,
          models: [],
          fetched_model_ids: [],
          is_accessible: false,
        };
        buf.providers.push(newProv);
        buf.activeProviderId = newId;
        renderSettingsContent();
      };
    }

    const clearKeyBtn = document.getElementById("clear-api-key-btn");
    if (clearKeyBtn && activeProv) {
      clearKeyBtn.onclick = () => {
        activeProv._keyEdited = true;
        activeProv.api_key = "";
        activeProv.api_key_mask = "";
        activeProv.clear_api_key = true;
        activeProv.has_api_key = false;
        activeProv.sync_status = "no_key";
        activeProv.sync_error = "NO API KEY";
        activeProv.fetched_model_ids = [];
        refreshProviderAccessibility(activeProv);
        renderSettingsContent();
      };
    }

    const keyInput = document.getElementById("prov-api-key-input");
    const toggleBtn = el.settingsTabContent.querySelector(".toggle-key-visibility");
    if (keyInput && activeProv) {
      keyInput.oninput = () => {
        activeProv._keyEdited = true;
      };
    }
    if (toggleBtn && keyInput && activeProv) {
      toggleBtn.title =
        activeProv.has_api_key && !activeProv.clear_api_key && !keyInput.value
          ? "Show/Hide typed key (stored keys remain masked)"
          : "Show/Hide typed key";
      toggleBtn.onclick = () => {
        const reveal = keyInput.type === "password";
        keyInput.type = reveal ? "text" : "password";
        toggleBtn.innerHTML = `<i data-lucide="${reveal ? "eye-off" : "eye"}" style="width: 14px; height: 14px;"></i>`;
        renderIcons(toggleBtn);
      };
    }

    const addMBtn = document.getElementById("add-model-btn");
    const newMInput = document.getElementById("new-model-id-input");
    if (addMBtn && newMInput && activeProv) {
      const triggerAddModel = () => {
        saveActiveProviderInputs();
        const mid = newMInput.value.trim();
        if (!mid) return;
        const fetchedList = Array.isArray(activeProv.fetched_model_ids) ? activeProv.fetched_model_ids : [];
        const fetchedByClean = new Map(fetchedList.map((fid) => [stripModelPrefix(fid).toLowerCase(), fid]));
        const canonical = fetchedByClean.get(stripModelPrefix(mid).toLowerCase());
        if (!canonical) {
          showNoticeDialog(
            fetchedByClean.size === 0
              ? "Please fetch models from the endpoint first (requires a valid API key)."
              : `Model "${mid}" was not found in the models fetched from ${activeProv.name}. Only successfully fetched models can be added.`
          );
          return;
        }
        if (!activeProv.models) activeProv.models = [];
        const cleanCanonical = stripModelPrefix(canonical).toLowerCase();
        if (activeProv.models.some((m) => stripModelPrefix(m.id).toLowerCase() === cleanCanonical)) {
          newMInput.value = "";
          return;
        }
        if (activeProv.models.length >= 5) {
          showNoticeDialog("Maximum 5 models can be configured per provider. Remove an existing model first.");
          return;
        }
        activeProv.models.push({
          id: canonical,
          name: canonical,
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
        if (e.key === "Enter" && !e.isComposing) {
          e.preventDefault();
          triggerAddModel();
        }
      };
    }

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

    const swapModel = (mid, delta) => {
      saveActiveProviderInputs();
      const swapped = swapModelInArray(activeProv.models, mid, delta, 0);
      if (swapped) {
        activeProv.models = swapped;
        renderSettingsContent();
      }
    };
    el.settingsTabContent.querySelectorAll(".move-model-up-btn").forEach((btn) => {
      btn.onclick = () => swapModel(btn.dataset.modelId, -1);
    });
    el.settingsTabContent.querySelectorAll(".move-model-down-btn").forEach((btn) => {
      btn.onclick = () => swapModel(btn.dataset.modelId, 1);
    });

    el.settingsTabContent.querySelectorAll(".delete-model-btn").forEach((btn) => {
      btn.onclick = () => {
        saveActiveProviderInputs();
        const mid = btn.dataset.modelId;
        activeProv.models = (activeProv.models || []).filter((m) => m.id !== mid);
        refreshProviderAccessibility(activeProv);
        renderSettingsContent();
      };
    });

    const delProvBtn = document.getElementById("delete-provider-btn");
    if (delProvBtn && activeProv) {
      delProvBtn.onclick = async () => {
        const confirmed = await showConfirmDialog(`Delete provider "${activeProv.name}"?`);
        if (confirmed) {
          buf.providers = buf.providers.filter((p) => p.id !== activeProv.id);
          if (buf.settings.default_provider === activeProv.id) {
            buf.settings.default_provider = buf.providers[0]?.id || "openai";
            buf.settings.default_model = buf.providers[0]?.models?.[0]?.id || "";
          }
          buf.activeProviderId = buf.providers[0]?.id || null;
          renderSettingsContent();
        }
      };
    }

    const autoAddBtn = el.settingsTabContent.querySelector(".auto-add-models-btn");
    if (autoAddBtn && activeProv) {
      autoAddBtn.onclick = async () => {
        saveActiveProviderInputs();
        const validationErr = validateAllProviders(buf.providers);
        if (validationErr) {
          showNoticeDialog(validationErr);
          return;
        }
        await runProviderFetchAction({
          prov: activeProv,
          btn: autoAddBtn,
          idleHtml: '<i data-lucide="plus-circle" style="width: 11px; height: 11px;"></i> Auto add',
          useTypedBaseUrl: true,
          onSuccess: (res) => {
            const { fetched, cleanFetchedIds } = applyFetchedModelsToProvider(activeProv, res, { autoAdd: true });
            if (activeProv.id === "gemini") {
              applyFetchedModelsToWebSearch(buf, fetched, cleanFetchedIds, { autoAdd: false });
            }
            renderSettingsContent();
            showNoticeDialog(
              `Fetched ${fetched.length} models for ${activeProv.name}. Configured: ${activeProv.models.length}/5.`
            );
          },
        });
      };
    }

    const fetchBtn = el.settingsTabContent.querySelector(".fetch-models-btn");
    if (fetchBtn && activeProv) {
      fetchBtn.onclick = async () => {
        saveActiveProviderInputs();
        const validationErr = validateAllProviders(buf.providers);
        if (validationErr) {
          showNoticeDialog(validationErr);
          return;
        }
        await runProviderFetchAction({
          prov: activeProv,
          btn: fetchBtn,
          idleHtml: '<i data-lucide="refresh-cw" style="width: 11px; height: 11px;"></i> Fetch from Endpoint',
          useTypedBaseUrl: true,
          onSuccess: (res) => {
            const { fetched, cleanFetchedIds } = applyFetchedModelsToProvider(activeProv, res, { autoAdd: false });
            if (activeProv.id === "gemini") {
              applyFetchedModelsToWebSearch(buf, fetched, cleanFetchedIds, { autoAdd: false });
            }
            renderSettingsContent();
            showNoticeDialog(
              `Fetched ${fetched.length} models for ${activeProv.name}. Preserved ${activeProv.models.length} configured models.`
            );
          },
        });
      };
    }
  }

  renderIcons(el.settingsTabContent);

  el.settingsTabContent.dataset.renderedTab = buf.masterSettingsTab;
  el.settingsTabContent.dataset.renderedProvId =
    buf.masterSettingsTab === "providers" ? (buf.activeProviderId || buf.providers[0]?.id || "") : "";

  const restoreScrollPositions = () => {
    if (
      modalBody &&
      (sameProviderView ||
        sameGeneralView ||
        sameWebSearchView ||
        (prevTab === "providers" && buf.masterSettingsTab === "providers"))
    ) {
      modalBody.scrollTop = prevModalScroll;
    }
    const newSubNav = el.settingsTabContent.querySelector(".providers-sub-nav");
    if (newSubNav && prevSubNavScroll > 0 && buf.masterSettingsTab === "providers") {
      newSubNav.scrollTop = prevSubNavScroll;
    }
    const newModelsList = document.getElementById("prov-models-list") || document.getElementById("ws-models-list");
    if (newModelsList && (sameProviderView || sameWebSearchView) && prevModelsScroll > 0) {
      newModelsList.scrollTop = prevModelsScroll;
    }
  };

  restoreScrollPositions();
  if (typeof requestAnimationFrame === "function") {
    requestAnimationFrame(restoreScrollPositions);
  }
}

export function saveGeneralTabInputs() {
  if (!state.settingsBuffer) return;
  const buf = state.settingsBuffer;
  if (buf.masterSettingsTab === "general") {
    const sysPrompt = document.getElementById("setting-global-system-prompt");
    if (sysPrompt) buf.settings.global_system_prompt = sysPrompt.value.trim();

    const defProv = document.getElementById("setting-default-provider");
    if (defProv) buf.settings.default_provider = defProv.value;

    const defModel = document.getElementById("setting-default-model");
    if (defModel) buf.settings.default_model = defModel.value;

    const defThink = document.getElementById("setting-default-thinking");
    if (defThink && VALID_THINKING_LEVELS.has(defThink.value)) {
      buf.settings.default_thinking = defThink.value;
    }

    const themeSelect = document.getElementById("setting-theme");
    if (themeSelect) {
      buf.settings.theme = themeSelect.value;
      applyTheme(themeSelect.value, { persistState: false });
    }
  }
}

export function saveActiveProviderInputs() {
  if (!state.settingsBuffer) return;
  const buf = state.settingsBuffer;
  if (buf.masterSettingsTab === "providers" && buf.activeProviderId) {
    const prov = buf.providers.find((p) => p.id === buf.activeProviderId);
    if (prov) {
      const nameInput = document.getElementById("prov-name-input");
      if (nameInput) {
        prov.name = sanitizeProviderNameAlpha(nameInput.value);
      }

      const enCheck = document.getElementById("prov-enabled-check");
      if (enCheck) prov.is_enabled = enCheck.checked;

      const bUrl = document.getElementById("prov-base-url-input");
      if (bUrl && !bUrl.disabled) {
        if (bUrl.value.trim() !== String(prov.base_url || "").trim()) {
          prov._urlEdited = true;
        }
        prov.base_url = bUrl.value.trim();
      }

      const aKey = document.getElementById("prov-api-key-input");
      if (aKey) {
        // Do NOT `.trim()` intentional whitespace if typed, only check non-empty
        const rawTyped = aKey.value;
        if (rawTyped && rawTyped.trim().length > 0) {
          prov._keyEdited = true;
          prov.api_key = rawTyped;
          prov.has_api_key = true;
          prov.clear_api_key = false;
        } else {
          delete prov.api_key;
          const hasFetched = Array.isArray(prov.fetched_model_ids) && prov.fetched_model_ids.length > 0;
          const loopbackReady = !prov.clear_api_key && isLoopbackBaseUrl(prov.base_url) && hasFetched;
          prov.has_api_key = Boolean((prov._saved_has_api_key && !prov.clear_api_key) || loopbackReady);
        }
      }

      refreshProviderAccessibility(prov);
    }
  }
}

export async function saveSettingsFromModal() {
  if (!state.settingsBuffer) return;
  saveGeneralTabInputs();
  saveActiveProviderInputs();

  const buf = state.settingsBuffer;
  const validationErr = validateAllProviders(buf.providers);
  if (validationErr) {
    showNoticeDialog(validationErr);
    return;
  }

  const cleanProvidersPayload = buf.providers.map((p) => {
    const copy = { ...p };
    delete copy._saved_has_api_key;
    delete copy._keyEdited;
    delete copy._urlEdited;
    return copy;
  });

  const res = await callApi("save_settings", cleanProvidersPayload, buf.settings, buf.webSearchModels);
  if (res && res.error) {
    showNoticeDialog(res.error);
    return;
  }

  if (res && Array.isArray(res.providers)) {
    state.providers = res.providers;
  } else {
    state.providers = cleanProvidersPayload;
    state.providers.forEach((p) => {
      delete p.api_key;
      delete p.clear_api_key;
    });
  }

  state.settings = buf.settings;

  if (res && Array.isArray(res.web_search_models)) {
    state.webSearchModels = normalizeWebSearchModelsClient(res.web_search_models);
  } else {
    state.webSearchModels = normalizeWebSearchModelsClient(buf.webSearchModels);
  }

  state.activeProviderId = buf.activeProviderId;
  state.masterSettingsTab = buf.masterSettingsTab;

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
        if (stats && !stats.error) tab.tokenStats = stats;
      } catch (err) {
        console.warn("[saveSettingsFromModal] Failed refreshing token stats:", err);
      }
    }
  }

  syncAllPaneModelDropdowns();
  syncAllPaneWebSearchButtons();
  renderTabs();
  if (el.panesContainer) {
    el.panesContainer.querySelectorAll(".chat-pane").forEach((paneEl) => {
      const tabId = paneEl.dataset.tabId;
      const t = state.tabs.find((item) => item.id === tabId);
      if (t) {
        renderTokenStats(paneEl, t.id, t.tokenStats || null);
      }
    });
  }
}
