/**
 * FIFO-queued custom modal dialogs, theme switcher, and clipboard feedback helpers.
 */

import { callApi } from "./api.js";
import { el, renderIcons, state } from "./state.js";

let activeDialogResolve = null;
const dialogQueue = [];

function _drainDialogQueue() {
  if (activeDialogResolve || dialogQueue.length === 0 || !el.appDialogModal) return;
  const next = dialogQueue.shift();
  activeDialogResolve = next.resolve;
  next.openFn();
}

function _enqueueDialog(openFn) {
  return new Promise((resolve) => {
    dialogQueue.push({ openFn, resolve });
    _drainDialogQueue();
  });
}

export function closeAppDialog(result) {
  if (!el.appDialogModal) return;
  el.appDialogModal.classList.remove("open");
  if (el.appDialogInput) el.appDialogInput.onkeydown = null;
  if (el.appDialogCancelBtn) el.appDialogCancelBtn.onclick = null;
  if (el.appDialogOkBtn) el.appDialogOkBtn.onclick = null;
  if (activeDialogResolve) {
    const resolve = activeDialogResolve;
    activeDialogResolve = null;
    resolve(result);
  }
  if (dialogQueue.length > 0) {
    setTimeout(_drainDialogQueue, 10);
  }
}

export function showNoticeDialog(message) {
  return _enqueueDialog(() => {
    el.appDialogMessage.textContent = message;
    el.appDialogInput.style.display = "none";
    el.appDialogInput.onkeydown = null;
    el.appDialogCancelBtn.style.display = "none";
    el.appDialogCancelBtn.onclick = () => closeAppDialog(true);
    el.appDialogOkBtn.textContent = "OK";
    el.appDialogOkBtn.className = "btn-primary";
    el.appDialogModal.classList.add("open");
    setTimeout(() => el.appDialogOkBtn.focus(), 20);

    el.appDialogOkBtn.onclick = () => closeAppDialog(true);
  });
}

export function showConfirmDialog(message, confirmLabel = "Delete", isDanger = true) {
  return _enqueueDialog(() => {
    el.appDialogMessage.textContent = message;
    el.appDialogInput.style.display = "none";
    el.appDialogInput.onkeydown = null;
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

export function showPromptDialog(message, defaultValue = "", placeholder = "") {
  return _enqueueDialog(() => {
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
      if (e.key === "Enter" && !e.isComposing) {
        e.preventDefault();
        submitPrompt();
      }
    };
  });
}

export function applyTheme(theme, { persistState = true } = {}) {
  if (persistState) {
    state.settings.theme = theme;
  }
  document.documentElement.setAttribute("data-theme", theme);
  const hljsThemeLink = document.getElementById("hljs-theme");
  if (hljsThemeLink) {
    hljsThemeLink.href = theme === "light" ? "assets/github.min.css" : "assets/github-dark.min.css";
  }
  if (el.themeToggleBtn) {
    const iconName = theme === "light" ? "sun" : "moon";
    el.themeToggleBtn.innerHTML = `<i data-lucide="${iconName}" id="theme-icon" style="width: 16px; height: 16px;"></i>`;
    renderIcons(el.themeToggleBtn);
  } else if (el.themeIcon) {
    el.themeIcon.setAttribute("data-lucide", theme === "light" ? "sun" : "moon");
    renderIcons(document);
  }
}

export function toggleTheme() {
  const nextTheme = state.settings.theme === "dark" ? "light" : "dark";
  applyTheme(nextTheme);
  callApi("set_theme", nextTheme);
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

export function copyText(text, btnElement) {
  function showFeedback() {
    if (!btnElement) return;
    // Preserve original icon/label across rapid double-clicks (fixes NEW-L1)
    if (btnElement._origHtml === undefined) {
      btnElement._origHtml = btnElement.innerHTML;
    }
    if (btnElement._copyTimer) {
      clearTimeout(btnElement._copyTimer);
      btnElement._copyTimer = null;
    }
    btnElement.innerHTML =
      '<i data-lucide="check" style="width: 11px; height: 11px; color: #34a853;"></i> <span style="color: #34a853;">Copied!</span>';
    renderIcons(btnElement);
    btnElement._copyTimer = setTimeout(() => {
      btnElement._copyTimer = null;
      btnElement.innerHTML = btnElement._origHtml;
      renderIcons(btnElement);
    }, 1800);
  }

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(showFeedback).catch(() => {
      fallbackCopy(text, showFeedback);
    });
  } else {
    fallbackCopy(text, showFeedback);
  }
}
