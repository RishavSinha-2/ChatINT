/**
 * Strict Markdown + KaTeX + GFM renderer and DOM sanitizer (`C3`, `NEW-M7`, `NEW-L2`).
 */

import { escapeHtml } from "./state.js";

if (typeof window !== "undefined" && window.marked) {
  window.marked.setOptions({
    gfm: true,
    breaks: true,
  });
}

export const ALLOWED_MARKDOWN_TAGS = new Set([
  "a", "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6",
  "blockquote", "pre", "code", "em", "strong", "b", "i", "u", "s",
  "del", "ins", "sub", "sup", "mark", "small", "ul", "ol", "li",
  "dl", "dt", "dd", "table", "thead", "tbody", "tfoot", "tr", "th", "td",
  "div", "span", "img", "details", "summary", "input",
  // KaTeX / MathML / inline SVG fraction, cancel, & radical primitives
  "math", "semantics", "mrow", "mi", "mo", "mn", "ms", "mtext", "mspace",
  "msup", "msub", "msubsup", "mfrac", "msqrt", "mroot", "mover", "munder",
  "munderover", "mtable", "mlabeledtr", "mtr", "mtd", "mpadded", "mphantom",
  "menclose", "mglyph", "annotation",
  "svg", "path", "line", "rect", "circle", "polygon", "polyline", "g",
]);

export const ALLOWED_MARKDOWN_ATTRS = new Set([
  "class", "href", "src", "alt", "title", "width", "height",
  "colspan", "rowspan", "align", "open", "start", "type", "checked", "disabled",
  "style", "aria-hidden", "role", "referrerpolicy", "loading",
  "viewbox", "d", "x1", "y1", "x2", "y2", "x", "y", "cx", "cy", "r", "rx", "ry", "points",
  "fill", "stroke", "stroke-width", "preserveaspectratio", "xmlns", "encoding",
  "mathvariant", "stretchy", "fence", "separator", "lspace", "rspace", "accent",
  "columnalign", "rowalign", "columnspacing", "rowspacing", "displaystyle", "scriptlevel",
  "notation", "mathcolor", "mathbackground", "mathsize", "voffset", "depth",
  "linethickness", "movablelimits", "largeop", "symmetric", "maxsize", "minsize",
]);

export const ALLOWED_STYLE_PROPS = new Set([
  "color", "background-color", "text-align", "vertical-align",
  "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
  "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
  "top", "left", "right", "bottom",
  "width", "height", "min-width", "max-width", "min-height", "max-height", "line-height",
  "font-size", "font-weight", "font-style", "font-family",
  "border", "border-bottom", "border-top", "border-left", "border-right",
  "border-color", "border-style", "border-width",
  "border-bottom-width", "border-top-width", "border-left-width", "border-right-width",
  "display", "position",
]);

const FORBIDDEN_UI_CLASSES = new Set([
  "modal-overlay", "modal-card", "modal-header", "modal-body", "modal-footer",
  "open", "active", "settings-container", "settings-group", "settings-input",
  "btn-primary", "btn-secondary", "btn-danger", "icon-btn",
  "sidebar", "chat-pane", "pane-header", "tab-item", "all-tabs-popover",
  "stream-error-banner", "blocked-external-image", "live-streaming-row",
]);

const SAFE_UNTRUSTED_CLASS_RE = /^(?:hljs(?:-[a-z0-9_-]+)?|language-[a-z0-9_+-]+|task-list-item|markdown-table-scroll)$/i;

const SAFE_KATEX_CLASS_RE = /^(?:katex(?:-display|-html|-mathml|-display-block)?|base|strut|mord|mbin|mrel|mopen|mclose|mpunct|minner|mop|mfrac|vlist(?:-t|-t2|-r|-s)?|pstrut|frac-line|nulldelimiter|delimsizing(?:inner)?|delimcenter|size\d+|mult|op-symbol|small-op|large-op|msupsub|reset-size\d+|sizing|mtight|textbf|textit|textrm|textsf|texttt|text|mathrm|mathbf|mathit|mathbb|mathcal|mathfrak|mathscr|mathsf|mathtt|amsrm|boldsymbol|col-align-[lcr]|arraycolsep|mtable|vertical-separator|svg-align|hide-tail|halfarrow-(?:left|right)|brace-(?:left|center|right)|overline(?:-line)?|underline(?:-line)?|stretchy|x-arrow|accent(?:-body|-full)?|rule|hline|hdashline|sqrt(?:-sign|-line)?|root|op-limits|limits|boxpad|fbox|fcolorbox|cancel-(?:lap|pad)|sout|phase-line|angl|enclosing)$/i;

const KATEX_POSITION_PROPS = new Set(["position", "top", "left", "right", "bottom"]);

export function _isSafeUrl(rawUrl, optionsOrAllowMailto = false) {
  const opts =
    typeof optionsOrAllowMailto === "boolean"
      ? { allowMailtoAndHash: optionsOrAllowMailto, allowDataAndBlob: false }
      : (optionsOrAllowMailto || {});
  const allowMailtoAndHash = Boolean(opts.allowMailtoAndHash);
  const allowDataAndBlob = Boolean(opts.allowDataAndBlob);

  const trimmed = String(rawUrl || "").trim();
  if (!trimmed) return false;
  if (/[\u0000-\u001f\u007f\s\\]/.test(trimmed)) return false;
  // Explicitly reject protocol-relative URLs and Windows UNC paths (`//host/share`)
  if (trimmed.startsWith("//")) return false;

  if (allowMailtoAndHash && trimmed.startsWith("#")) {
    return true;
  }

  if (allowDataAndBlob && /^data:/i.test(trimmed)) {
    return /^data:image\/(?:png|jpeg|jpg|gif|webp|avif);base64,[a-z0-9+/=]+$/i.test(trimmed);
  }

  try {
    // NO dummy base URL: relative paths (`../../../secret.png`, `other.html`) throw TypeError and fail closed!
    const parsed = new URL(trimmed);
    const proto = parsed.protocol.toLowerCase();
    if (proto === "https:" || proto === "http:") {
      return Boolean(parsed.hostname);
    }
    if (allowMailtoAndHash && proto === "mailto:") {
      return Boolean(parsed.pathname && parsed.pathname.includes("@"));
    }
    if (allowDataAndBlob && proto === "blob:") {
      return true;
    }
    return false;
  } catch (e) {
    return false;
  }
}

export function _isTrustedAutoLoadImageUrl(rawUrl) {
  const trimmed = String(rawUrl || "").trim();
  if (!_isSafeUrl(trimmed, { allowMailtoAndHash: false, allowDataAndBlob: true })) {
    return false;
  }
  if (/^(?:data:|blob:)/i.test(trimmed)) {
    return true;
  }
  try {
    const parsed = new URL(trimmed);
    const host = (parsed.hostname || "").toLowerCase();
    return (
      parsed.protocol.toLowerCase() === "https:" &&
      (host === "upload.wikimedia.org" || host.endsWith(".wikimedia.org"))
    );
  } catch (e) {
    return false;
  }
}

export function _sanitizeCssStyle(rawStyle, { insideKatex = false } = {}) {
  if (!rawStyle) return "";
  const lower = String(rawStyle).toLowerCase();
  if (/expression\s*\(|javascript:|vbscript:|url\s*\(|@import|behavior\s*:|-moz-binding|var\s*\(/.test(lower)) {
    return "";
  }
  const cleanDecls = [];
  for (const decl of String(rawStyle).split(";")) {
    const colonIdx = decl.indexOf(":");
    if (colonIdx === -1) continue;
    const prop = decl.slice(0, colonIdx).trim().toLowerCase();
    const val = decl.slice(colonIdx + 1).trim();
    if (!ALLOWED_STYLE_PROPS.has(prop) || !val) continue;
    if (/[<>\\"']/.test(val)) continue;

    const valLower = val.toLowerCase();
    if (KATEX_POSITION_PROPS.has(prop)) {
      if (!insideKatex) continue;
      if (prop === "position" && !/^(?:relative|absolute|static)$/.test(valLower)) {
        continue;
      }
    }
    if (prop === "z-index") continue;

    cleanDecls.push(`${prop}: ${val}`);
  }
  return cleanDecls.join("; ");
}

function _sanitizeClassAttr(rawClass, { isTrustedKatex = false, isStandaloneKatexFallback = false } = {}) {
  if (!rawClass) return "";
  const tokens = String(rawClass)
    .split(/\s+/)
    .map((t) => t.trim())
    .filter(Boolean);
  const allowed = [];
  for (const tok of tokens) {
    if (FORBIDDEN_UI_CLASSES.has(tok.toLowerCase())) continue;
    if (isTrustedKatex) {
      if (/^[A-Za-z0-9_-]+$/.test(tok)) allowed.push(tok);
    } else if (SAFE_UNTRUSTED_CLASS_RE.test(tok)) {
      allowed.push(tok);
    } else if (isStandaloneKatexFallback && SAFE_KATEX_CLASS_RE.test(tok)) {
      allowed.push(tok);
    }
  }
  return allowed.join(" ");
}

export function sanitizeMarkdownDom(container, trustedKatexNodes = null) {
  if (!container) return;
  const ownerDoc = container.ownerDocument || document;

  container
    .querySelectorAll("script, style, iframe, object, embed, form, meta, base, link, textarea, button, select")
    .forEach((node) => node.remove());

  const nodes = Array.from(container.querySelectorAll("*"));
  for (const node of nodes) {
    if (!container.contains(node)) continue;
    const tag = node.tagName.toLowerCase();
    if (!ALLOWED_MARKDOWN_TAGS.has(tag)) {
      node.remove();
      continue;
    }

    // Only allow GFM task-list checkboxes `<input type="checkbox" disabled>` inside `<li>` (fixes NEW-M7)
    if (tag === "input") {
      const inputType = (node.getAttribute("type") || "").toLowerCase();
      const parentTag = node.parentElement ? node.parentElement.tagName.toLowerCase() : "";
      if (inputType !== "checkbox" || (parentTag !== "li" && parentTag !== "p")) {
        node.remove();
        continue;
      }
      const wasChecked = node.hasAttribute("checked");
      for (const attr of Array.from(node.attributes)) {
        node.removeAttribute(attr.name);
      }
      node.setAttribute("type", "checkbox");
      node.setAttribute("disabled", "");
      if (wasChecked) node.setAttribute("checked", "");
      const liAncestor = node.closest("li");
      if (liAncestor) liAncestor.classList.add("task-list-item");
      continue;
    }

    const isTrustedKatex = Boolean(trustedKatexNodes && trustedKatexNodes.has(node));
    const isStandaloneKatexFallback =
      trustedKatexNodes === null && Boolean(node.closest(".katex, .katex-display-block"));
    const insideKatex = isTrustedKatex || isStandaloneKatexFallback;

    for (const attr of Array.from(node.attributes)) {
      const name = attr.name.toLowerCase();
      if (name.startsWith("on") || !ALLOWED_MARKDOWN_ATTRS.has(name)) {
        node.removeAttribute(attr.name);
        continue;
      }
      if (name === "href") {
        if (!_isSafeUrl(attr.value, { allowMailtoAndHash: true, allowDataAndBlob: false })) {
          node.removeAttribute(attr.name);
        }
      } else if (name === "src") {
        if (!_isSafeUrl(attr.value, { allowMailtoAndHash: false, allowDataAndBlob: true })) {
          node.removeAttribute(attr.name);
        }
      } else if (name === "class") {
        const cleanCls = _sanitizeClassAttr(attr.value, {
          isTrustedKatex,
          isStandaloneKatexFallback,
        });
        if (cleanCls) {
          node.setAttribute("class", cleanCls);
        } else {
          node.removeAttribute("class");
        }
      } else if (name === "style") {
        const safeStyle = _sanitizeCssStyle(attr.value, { insideKatex });
        if (safeStyle) {
          node.setAttribute("style", safeStyle);
        } else {
          node.removeAttribute("style");
        }
      }
    }

    if (tag === "a" && node.hasAttribute("href")) {
      node.setAttribute("rel", "noopener noreferrer");
    }

    // Prevent tracking pixels from auto-firing on untrusted external image hosts (`C3`)
    if (tag === "img") {
      const srcVal = node.getAttribute("src") || "";
      if (!srcVal) {
        node.remove();
        continue;
      }
      if (_isTrustedAutoLoadImageUrl(srcVal)) {
        node.setAttribute("referrerpolicy", "no-referrer");
        node.setAttribute("loading", "lazy");
      } else {
        let hostLabel = "external host";
        try {
          hostLabel = new URL(srcVal).hostname || hostLabel;
        } catch (e) {}
        const altVal = node.getAttribute("alt") || "";
        const btn = ownerDoc.createElement("button");
        btn.setAttribute("type", "button");
        btn.setAttribute("class", "blocked-external-image");
        btn.setAttribute("data-external-src", srcVal);
        if (altVal) btn.setAttribute("data-external-alt", altVal);
        btn.setAttribute("title", srcVal);
        btn.innerHTML = `<i data-lucide="image" style="width: 13px; height: 13px;"></i><span>Load external image (${escapeHtml(hostLabel)})</span>`;
        node.replaceWith(btn);
      }
    }
  }
}

export function renderMarkdownWithEnhancements(markdownText, options = {}) {
  if (!markdownText || typeof markdownText !== "string") return "";
  const isStreaming = Boolean(options && options.isStreaming);
  const nonce = Math.random().toString(36).slice(2, 10);

  // 1. Protect fenced code blocks (including unclosed trailing fences during streaming) and inline code spans
  const codeTokens = [];
  let protectedText = markdownText.replace(/(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]+`)/g, (match) => {
    const idx = codeTokens.length;
    codeTokens.push(match);
    return `@@CIT_${nonce}_CODE_${idx}@@`;
  });

  // 2. Protect Markdown image tags, link destinations, HTML URL/alt/title attributes, and bare URLs
  // BEFORE KaTeX `$` extraction so `$` in query params or image alt text never leaks placeholders (`NEW-M7`).
  const linkTokens = [];
  const stashLinkPiece = (match) => {
    const idx = linkTokens.length;
    linkTokens.push(match);
    return `@@CIT_${nonce}_LINK_${idx}@@`;
  };
  // Full Markdown image syntax `![alt](url)`
  protectedText = protectedText.replace(/!\[(?:[^\[\]\\]|\\.)*\]\((?:[^()\n]|\\\(|\\\))*\)/g, stashLinkPiece);
  // Markdown link destination `](url)` (leaving `[label]` open so math inside link text still renders!)
  protectedText = protectedText.replace(/\]\((?:[^()\n]|\\\(|\\\))*\)/g, stashLinkPiece);
  // Angle-bracket autolinks & HTML attributes (`href="..."`, `src="..."`, `alt="..."`, `title="..."`)
  protectedText = protectedText.replace(/<https?:\/\/[^>\s]+>/gi, stashLinkPiece);
  protectedText = protectedText.replace(/\b(?:href|src|alt|title)\s*=\s*(?:"[^"]*"|'[^']*')/gi, stashLinkPiece);
  // Bare URLs containing `$` query parameters (`https://api.example.com/v1?$filter=a&$select=b`)
  protectedText = protectedText.replace(/https?:\/\/[^\s<>()]+/gi, stashLinkPiece);

  // 3. Extract and render KaTeX math into nonce-protected placeholders
  const mathTokens = [];
  const mathRawSources = [];
  if (typeof window !== "undefined" && window.katex) {
    const pushMathToken = (expr, displayMode, rawFallback) => {
      try {
        const rendered = window.katex.renderToString(expr.trim(), {
          displayMode: displayMode,
          throwOnError: false,
        });
        // Use `<span class="katex-display-block">` (`display: block` in CSS) so `<p>` never splits into empty `<p></p>` (`NEW-M7`)
        const htmlOut = displayMode ? `<span class="katex-display-block">${rendered}</span>` : rendered;
        const idx = mathTokens.length;
        mathTokens.push(htmlOut);
        mathRawSources.push(rawFallback);
        return `@@CIT_${nonce}_MATH_${idx}@@`;
      } catch (e) {
        return rawFallback;
      }
    };

    // Block math: $$ ... $$ or \[ ... \]
    protectedText = protectedText.replace(/\$\$([\s\S]+?)\$\$/g, (m, expr) => pushMathToken(expr, true, m));
    protectedText = protectedText.replace(/\\\[([\s\S]+?)\\\]/g, (m, expr) => pushMathToken(expr, true, m));

    // Inline math: \( ... \) or $ ... $ (closing `$` must NOT be followed by `$` or a digit so `$100–$200` & `$5/$10` stay currency!)
    protectedText = protectedText.replace(/\\\(([\s\S]+?)\\\)/g, (m, expr) => pushMathToken(expr, false, m));
    protectedText = protectedText.replace(/(^|[^\$\\])\$([^\$\n\r]+?)\$(?![\$0-9])/g, (m, prefix, expr) => {
      if (expr.trim() !== expr || /^\d+(?:[.,]\d+)*$/.test(expr)) {
        return m;
      }
      if (/^\d+(?:[.,]\d+)?\s*(?:[-–—/~]|to\b|and\b|or\b|per\b)\s*$/i.test(expr)) {
        return m;
      }
      return prefix + pushMathToken(expr, false, `$${expr}$`);
    });
  }

  // 4. Restore protected URLs/images and code blocks before running marked.parse
  if (linkTokens.length > 0) {
    const linkRestoreRe = new RegExp(`@@CIT_${nonce}_LINK_(\\d+)@@`, "g");
    protectedText = protectedText.replace(linkRestoreRe, (_, idx) => linkTokens[Number(idx)]);
  }
  if (codeTokens.length > 0) {
    const codeRestoreRe = new RegExp(`@@CIT_${nonce}_CODE_(\\d+)@@`, "g");
    protectedText = protectedText.replace(codeRestoreRe, (_, idx) => codeTokens[Number(idx)]);
  }

  let html = "";
  try {
    html = window.marked ? window.marked.parse(protectedText) : escapeHtml(protectedText);
  } catch (e) {
    return `<pre>${escapeHtml(markdownText)}</pre>`;
  }

  const parser = new DOMParser();
  const doc = parser.parseFromString(`<div>${html}</div>`, "text/html");
  const rootContainer = doc.body.firstElementChild;

  if (rootContainer) {
    const trustedKatexNodes = new WeakSet();

    // 5. Expand KaTeX placeholder tokens into DOM nodes BEFORE running sanitizeMarkdownDom
    if (mathTokens.length > 0) {
      const mathTokenPattern = new RegExp(`@@CIT_${nonce}_MATH_(\\d+)@@`, "g");

      // Restore raw `$...$` if any placeholder ever ended up inside an HTML attribute
      rootContainer.querySelectorAll("*").forEach((elNode) => {
        for (const attr of Array.from(elNode.attributes)) {
          if (attr.value && attr.value.includes(`@@CIT_${nonce}_MATH_`)) {
            elNode.setAttribute(
              attr.name,
              attr.value.replace(mathTokenPattern, (_, idx) => mathRawSources[Number(idx)] || "")
            );
          }
        }
      });

      if (typeof doc.createTreeWalker === "function") {
        const walker = doc.createTreeWalker(rootContainer, NodeFilter.SHOW_TEXT);
        const textNodesToReplace = [];
        let curNode = walker.nextNode();
        while (curNode) {
          if (curNode.nodeValue && curNode.nodeValue.includes(`@@CIT_${nonce}_MATH_`)) {
            textNodesToReplace.push(curNode);
          }
          curNode = walker.nextNode();
        }
        for (const textNode of textNodesToReplace) {
          const val = textNode.nodeValue;
          const frag = doc.createDocumentFragment();
          let lastIdx = 0;
          mathTokenPattern.lastIndex = 0;
          let m;
          while ((m = mathTokenPattern.exec(val)) !== null) {
            if (m.index > lastIdx) {
              frag.appendChild(doc.createTextNode(val.slice(lastIdx, m.index)));
            }
            const tokenHtml = mathTokens[Number(m[1])] || "";
            const mathDoc = parser.parseFromString(`<div>${tokenHtml}</div>`, "text/html");
            const mathWrapper = mathDoc.body.firstElementChild;
            if (mathWrapper) {
              while (mathWrapper.firstChild) {
                const adopted = doc.adoptNode(mathWrapper.firstChild);
                if (adopted.nodeType === 1) {
                  trustedKatexNodes.add(adopted);
                  adopted.querySelectorAll("*").forEach((desc) => trustedKatexNodes.add(desc));
                }
                frag.appendChild(adopted);
              }
            }
            lastIdx = mathTokenPattern.lastIndex;
          }
          if (lastIdx < val.length) {
            frag.appendChild(doc.createTextNode(val.slice(lastIdx)));
          }
          textNode.parentNode.replaceChild(frag, textNode);
        }
      }
    }

    // 6. Sanitize ALL Markdown + KaTeX nodes via strict allowlist
    sanitizeMarkdownDom(rootContainer, trustedKatexNodes);

    // 7. Wrap tables in horizontal scroll containers (`NEW-L2`)
    rootContainer.querySelectorAll("table").forEach((tbl) => {
      if (tbl.parentElement && tbl.parentElement.classList.contains("markdown-table-scroll")) return;
      const scrollWrap = doc.createElement("div");
      scrollWrap.className = "markdown-table-scroll";
      tbl.parentNode.insertBefore(scrollWrap, tbl);
      scrollWrap.appendChild(tbl);
    });

    // 8. Highlight fenced code blocks & attach copy header
    rootContainer.querySelectorAll("pre").forEach((pre) => {
      const code = pre.querySelector("code");
      let lang = "code";
      if (code && code.className) {
        const m = code.className.match(/language-([^\s]+)/);
        if (m) lang = m[1];
      }

      if (typeof window !== "undefined" && window.hljs && code) {
        const rawCode = code.textContent || "";
        try {
          if (lang && lang !== "code" && window.hljs.getLanguage(lang)) {
            code.innerHTML = window.hljs.highlight(rawCode, { language: lang, ignoreIllegals: true }).value;
            code.classList.add("hljs");
          } else if (!isStreaming) {
            code.innerHTML = window.hljs.highlightAuto(rawCode).value;
            code.classList.add("hljs");
          }
        } catch (e) {}
      }

      const wrapper = doc.createElement("div");
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

    return rootContainer.innerHTML;
  }

  return escapeHtml(markdownText);
}
