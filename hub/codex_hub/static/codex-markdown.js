(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.CodexMarkdown = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const SAFE_SCHEMES = new Set(["http:", "https:", "mailto:"]);
  const KEYWORDS = new Set(
    (
      "as async await break case catch class const continue def delete do else " +
      "elif except export extends false finally for from function if import in " +
      "instanceof let new none null of pass raise return static super switch this " +
      "throw true try typeof undefined var void while with yield"
    ).split(/\s+/)
  );

  function sanitizeUrl(raw, baseUrl) {
    const value = String(raw == null ? "" : raw).trim();
    if (!value || /[\u0000-\u001f\u007f]/.test(value)) return null;

    // Detect obfuscated schemes before URL normalisation. Relative links may
    // contain colons later in the path, but never before /, ?, or #.
    const schemeCandidate = value.match(/^([^/?#]*):/);
    if (schemeCandidate) {
      const scheme = schemeCandidate[1].replace(/\s+/g, "").toLowerCase() + ":";
      if (!SAFE_SCHEMES.has(scheme)) return null;
    }

    try {
      const fallback = baseUrl || "https://codex-hub.invalid/";
      const parsed = new URL(value, fallback);
      if (!SAFE_SCHEMES.has(parsed.protocol)) return null;
      return value;
    } catch (_error) {
      return null;
    }
  }

  function appendText(tokens, text) {
    if (!text) return;
    const last = tokens[tokens.length - 1];
    if (last && last.type === "text") last.text += text;
    else tokens.push({ type: "text", text: text });
  }

  function attachmentLink(raw, label, baseUrl) {
    try {
      const base = new URL(baseUrl);
      let value = raw;
      const project = base.pathname.match(/^(.*\/projects\/[^/]+)(?:\/|$)/);
      // Older results stored task-relative links. They must also work when
      // rendered on the project root rather than /tasks/id or /conversations/id.
      if (project && /^\.\.\/attachments\/\d+(?:[/?#]|$)/.test(value)) {
        value = project[1] + '/' + value.slice(3);
      }
      const parsed = new URL(value, base);
      if (parsed.origin !== base.origin || !/\/projects\/[^/]+\/attachments\/\d+(?:\/preview)?$/.test(parsed.pathname)) return null;
      const download = /下载|download|保存(?:原图|文件)/i.test(label);
      if (download) {
        parsed.pathname = parsed.pathname.replace(/\/preview$/, '');
        parsed.searchParams.delete('inline');
        parsed.searchParams.delete('size');
      }
      return {href: parsed.href, download};
    } catch (_error) {
      return null;
    }
  }

  function inlineText(tokens) {
    return (tokens || []).map(token => token.text == null ? inlineText(token.children) : token.text).join('');
  }

  function parseInline(source, baseUrl) {
    const text = String(source == null ? "" : source);
    const tokens = [];
    let index = 0;

    while (index < text.length) {
      const rest = text.slice(index);
      let match = rest.match(/^`([^`\n]+)`/);
      if (match) {
        tokens.push({ type: "code", text: match[1] });
        index += match[0].length;
        continue;
      }

      match = rest.match(/^\[([^\]\n]+)\]\(([^)\s]+)(?:\s+["']([^"']*)["'])?\)/);
      if (match) {
        const href = sanitizeUrl(match[2], baseUrl);
        if (href) {
          tokens.push({
            type: "link",
            href: href,
            title: match[3] || "",
            children: parseInline(match[1], baseUrl),
          });
        } else {
          appendText(tokens, match[1]);
        }
        index += match[0].length;
        continue;
      }

      match = rest.match(/^<(https?:\/\/[^>\s]+|mailto:[^>\s]+)>/i);
      if (match) {
        const href = sanitizeUrl(match[1], baseUrl);
        if (href) {
          tokens.push({
            type: "link",
            href: href,
            title: "",
            children: [{ type: "text", text: match[1] }],
          });
        } else appendText(tokens, match[0]);
        index += match[0].length;
        continue;
      }

      match = rest.match(/^\*\*([^*\n]+)\*\*/);
      if (match) {
        tokens.push({ type: "strong", children: parseInline(match[1], baseUrl) });
        index += match[0].length;
        continue;
      }

      match = rest.match(/^_([^_\n]+)_|^\*([^*\n]+)\*/);
      // Underscores inside identifiers and JSON keys are literal characters.
      // Unlike asterisks, underscore emphasis cannot open/close inside a word.
      const insideWord = match && match[1] && (
        /[\p{L}\p{N}_]/u.test(text[index - 1] || "") ||
        /[\p{L}\p{N}_]/u.test(text[index + match[0].length] || "")
      );
      if (match && !insideWord) {
        tokens.push({ type: "em", children: parseInline(match[1] || match[2], baseUrl) });
        index += match[0].length;
        continue;
      }

      match = rest.match(/^~~([^~\n]+)~~/);
      if (match) {
        tokens.push({ type: "del", children: parseInline(match[1], baseUrl) });
        index += match[0].length;
        continue;
      }

      appendText(tokens, text[index]);
      index += 1;
    }
    return tokens;
  }

  function splitTableRow(line) {
    let value = String(line).trim();
    if (value.startsWith("|")) value = value.slice(1);
    if (value.endsWith("|") && !value.endsWith("\\|")) value = value.slice(0, -1);
    const cells = [];
    let cell = "";
    let escaped = false;
    for (const char of value) {
      if (escaped) {
        cell += char;
        escaped = false;
      } else if (char === "\\") {
        escaped = true;
      } else if (char === "|") {
        cells.push(cell.trim());
        cell = "";
      } else cell += char;
    }
    if (escaped) cell += "\\";
    cells.push(cell.trim());
    return cells;
  }

  function tableAlignment(line) {
    const cells = splitTableRow(line);
    if (!cells.length || !cells.every((cell) => /^:?-{3,}:?$/.test(cell))) return null;
    return cells.map((cell) => {
      if (cell.startsWith(":") && cell.endsWith(":")) return "center";
      if (cell.endsWith(":")) return "right";
      if (cell.startsWith(":")) return "left";
      return null;
    });
  }

  function listMatch(line) {
    return String(line).match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
  }

  function fenceMatch(line) {
    return String(line).match(/^\s{0,3}(`{3,}|~{3,})\s*([\w+-]*)\s*$/);
  }

  function startsBlock(lines, index) {
    const line = lines[index] || "";
    if (!line.trim()) return true;
    if (fenceMatch(line)) return true;
    if (/^\s{0,3}#{1,6}\s+/.test(line)) return true;
    if (/^\s{0,3}>/.test(line)) return true;
    if (listMatch(line)) return true;
    if (/^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$/.test(line)) return true;
    return index + 1 < lines.length && line.includes("|") && Boolean(tableAlignment(lines[index + 1]));
  }

  function parseMarkdown(source, options) {
    const settings = options || {};
    const baseUrl = settings.baseUrl;
    const lines = String(source == null ? "" : source).replace(/\r\n?/g, "\n").split("\n");
    const blocks = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      const fence = fenceMatch(line);
      if (fence) {
        const marker = fence[1][0];
        const minimum = fence[1].length;
        const language = (fence[2] || "").toLowerCase();
        const code = [];
        index += 1;
        while (index < lines.length) {
          const close = lines[index].match(/^\s{0,3}(`{3,}|~{3,})\s*$/);
          if (close && close[1][0] === marker && close[1].length >= minimum) {
            index += 1;
            break;
          }
          code.push(lines[index]);
          index += 1;
        }
        blocks.push({ type: "code", language: language, text: code.join("\n") });
        continue;
      }

      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (heading) {
        blocks.push({
          type: "heading",
          level: heading[1].length,
          children: parseInline(heading[2], baseUrl),
        });
        index += 1;
        continue;
      }

      if (/^\s{0,3}>/.test(line)) {
        const quoted = [];
        while (index < lines.length && /^\s{0,3}>/.test(lines[index])) {
          quoted.push(lines[index].replace(/^\s{0,3}>\s?/, ""));
          index += 1;
        }
        blocks.push({ type: "blockquote", children: parseMarkdown(quoted.join("\n"), settings) });
        continue;
      }

      const item = listMatch(line);
      if (item) {
        const ordered = /^\d/.test(item[1]);
        const items = [];
        while (index < lines.length) {
          const next = listMatch(lines[index]);
          if (!next || /^\d/.test(next[1]) !== ordered) break;
          items.push(parseInline(next[2], baseUrl));
          index += 1;
        }
        blocks.push({ type: "list", ordered: ordered, items: items });
        continue;
      }

      const alignment = index + 1 < lines.length ? tableAlignment(lines[index + 1]) : null;
      if (line.includes("|") && alignment) {
        const header = splitTableRow(line).map((cell) => parseInline(cell, baseUrl));
        const rows = [];
        index += 2;
        while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
          rows.push(splitTableRow(lines[index]).map((cell) => parseInline(cell, baseUrl)));
          index += 1;
        }
        blocks.push({ type: "table", alignment: alignment, header: header, rows: rows });
        continue;
      }

      if (/^\s{0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$/.test(line)) {
        blocks.push({ type: "hr" });
        index += 1;
        continue;
      }

      const paragraph = [line.trim()];
      index += 1;
      while (index < lines.length && !startsBlock(lines, index)) {
        paragraph.push(lines[index].trim());
        index += 1;
      }
      blocks.push({ type: "paragraph", lines: paragraph.map((part) => parseInline(part, baseUrl)) });
    }
    return blocks;
  }

  function tokenizeCode(code, language) {
    const source = String(code == null ? "" : code);
    const lang = String(language || "").toLowerCase();
    const markup = new Set(["html", "xml", "svg"]);
    const pattern = markup.has(lang)
      ? /<!--[\s\S]*?-->|<\/?[A-Za-z][^>]*>|&(?:#\d+|#x[\da-f]+|\w+);/gi
      : /\/\*[\s\S]*?\*\/|\/\/[^\n]*|#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\b(?:\d+(?:\.\d+)?|[A-Za-z_$][\w$]*)\b/g;
    const tokens = [];
    let cursor = 0;
    let match;
    while ((match = pattern.exec(source))) {
      if (match.index > cursor) tokens.push({ type: "plain", text: source.slice(cursor, match.index) });
      const value = match[0];
      let type = "plain";
      if (markup.has(lang)) type = value.startsWith("<!--") ? "comment" : "tag";
      else if (/^(\/\*|\/\/|#)/.test(value)) type = "comment";
      else if (/^["']/.test(value)) type = "string";
      else if (/^\d/.test(value)) type = "number";
      else if (KEYWORDS.has(value.toLowerCase())) type = "keyword";
      tokens.push({ type: type, text: value });
      cursor = pattern.lastIndex;
    }
    if (cursor < source.length) tokens.push({ type: "plain", text: source.slice(cursor) });
    return tokens;
  }

  function appendInline(documentRef, parent, tokens) {
    tokens.forEach((token) => {
      if (token.type === "text") {
        parent.appendChild(documentRef.createTextNode(token.text));
        return;
      }
      const tag = { code: "code", strong: "strong", em: "em", del: "del", link: "a" }[token.type];
      if (!tag) return;
      const element = documentRef.createElement(tag);
      if (token.type === "link") {
        const attachment = attachmentLink(token.href, inlineText(token.children), documentRef.baseURI);
        element.setAttribute("href", attachment ? attachment.href : token.href);
        if (token.title) element.setAttribute("title", token.title);
        let external = false;
        try {
          external = new URL(token.href, documentRef.baseURI).origin !== new URL(documentRef.baseURI).origin;
        } catch (_error) {
          external = true;
        }
        if (attachment && attachment.download) {
          element.setAttribute('download', '');
          element.setAttribute('data-hub-download', 'true');
        } else if (external || attachment) {
          element.setAttribute("target", "_blank");
          element.setAttribute("rel", "noopener noreferrer");
        }
      }
      if (token.text != null) element.textContent = token.text;
      if (token.children) appendInline(documentRef, element, token.children);
      parent.appendChild(element);
    });
  }

  async function copyText(text, documentRef) {
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_error) {
      // Fall through to the selection-based compatibility path.
    }
    if (!documentRef || typeof documentRef.execCommand !== "function") return false;
    const textarea = documentRef.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    documentRef.body.appendChild(textarea);
    textarea.select();
    let copied = false;
    try {
      copied = documentRef.execCommand("copy");
    } finally {
      textarea.remove();
    }
    return copied;
  }

  function renderBlocks(documentRef, parent, blocks) {
    blocks.forEach((block) => {
      let element;
      if (block.type === "heading") {
        element = documentRef.createElement("h" + block.level);
        appendInline(documentRef, element, block.children);
      } else if (block.type === "paragraph") {
        element = documentRef.createElement("p");
        block.lines.forEach((line, lineIndex) => {
          if (lineIndex) element.appendChild(documentRef.createElement("br"));
          appendInline(documentRef, element, line);
        });
      } else if (block.type === "blockquote") {
        element = documentRef.createElement("blockquote");
        renderBlocks(documentRef, element, block.children);
      } else if (block.type === "list") {
        element = documentRef.createElement(block.ordered ? "ol" : "ul");
        block.items.forEach((item) => {
          const li = documentRef.createElement("li");
          appendInline(documentRef, li, item);
          element.appendChild(li);
        });
      } else if (block.type === "table") {
        const scroll = documentRef.createElement("div");
        scroll.className = "codex-markdown-table-scroll";
        element = documentRef.createElement("table");
        const thead = documentRef.createElement("thead");
        const headRow = documentRef.createElement("tr");
        block.header.forEach((cell, cellIndex) => {
          const th = documentRef.createElement("th");
          if (block.alignment[cellIndex]) th.style.textAlign = block.alignment[cellIndex];
          appendInline(documentRef, th, cell);
          headRow.appendChild(th);
        });
        thead.appendChild(headRow);
        element.appendChild(thead);
        const tbody = documentRef.createElement("tbody");
        block.rows.forEach((row) => {
          const tr = documentRef.createElement("tr");
          block.header.forEach((_header, cellIndex) => {
            const td = documentRef.createElement("td");
            if (block.alignment[cellIndex]) td.style.textAlign = block.alignment[cellIndex];
            appendInline(documentRef, td, row[cellIndex] || []);
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
        element.appendChild(tbody);
        scroll.appendChild(element);
        parent.appendChild(scroll);
        return;
      } else if (block.type === "code") {
        element = documentRef.createElement("div");
        element.className = "codex-code-block";
        const toolbar = documentRef.createElement("div");
        toolbar.className = "codex-code-toolbar";
        const language = documentRef.createElement("span");
        language.textContent = block.language || "text";
        const button = documentRef.createElement("button");
        button.type = "button";
        button.className = "codex-code-copy";
        button.textContent = "复制";
        button.setAttribute("aria-label", "复制代码");
        button.addEventListener("click", async () => {
          const copied = await copyText(block.text, documentRef);
          button.textContent = copied ? "已复制" : "复制失败";
          setTimeout(() => { button.textContent = "复制"; }, 1600);
        });
        toolbar.appendChild(language);
        toolbar.appendChild(button);
        const pre = documentRef.createElement("pre");
        const code = documentRef.createElement("code");
        if (block.language) code.className = "language-" + block.language.replace(/[^a-z0-9_-]/g, "");
        tokenizeCode(block.text, block.language).forEach((token) => {
          if (token.type === "plain") code.appendChild(documentRef.createTextNode(token.text));
          else {
            const span = documentRef.createElement("span");
            span.className = "token " + token.type;
            span.textContent = token.text;
            code.appendChild(span);
          }
        });
        pre.appendChild(code);
        element.appendChild(toolbar);
        element.appendChild(pre);
      } else if (block.type === "hr") element = documentRef.createElement("hr");
      if (element) parent.appendChild(element);
    });
  }

  function renderMarkdown(source, options) {
    const settings = options || {};
    const documentRef = settings.document || (typeof document !== "undefined" ? document : null);
    if (!documentRef) throw new Error("renderMarkdown requires a DOM document");
    const container = documentRef.createElement("div");
    container.className = "codex-markdown";
    container.setAttribute("data-markdown-rendered", "true");
    renderBlocks(documentRef, container, parseMarkdown(source, { baseUrl: documentRef.baseURI }));
    return container;
  }

  function candidateSources(rootNode) {
    const sources = [];
    if (!rootNode || !rootNode.querySelectorAll) return sources;
    if (rootNode.matches && rootNode.matches("[data-markdown-source]")) sources.push(rootNode);
    rootNode.querySelectorAll("[data-markdown-source]").forEach((node) => sources.push(node));
    rootNode.querySelectorAll(".message-card > pre").forEach((node) => {
      const label = node.previousElementSibling;
      if (label && label.tagName === "STRONG" && label.textContent.trim().toLowerCase() === "markdown") {
        sources.push(node);
      }
    });
    return Array.from(new Set(sources));
  }

  function enhanceMarkdown(rootNode, options) {
    const settings = options || {};
    const documentRef = settings.document || (rootNode && rootNode.ownerDocument) ||
      (typeof document !== "undefined" ? document : null);
    if (!documentRef) return [];
    const rendered = [];
    candidateSources(rootNode || documentRef).forEach((source) => {
      if (!source.isConnected || source.dataset.markdownEnhanced === "true") return;
      source.dataset.markdownEnhanced = "true";
      const result = renderMarkdown(source.textContent || "", { document: documentRef });
      source.replaceWith(result);
      rendered.push(result);
    });
    return rendered;
  }

  function init(options) {
    const settings = options || {};
    const documentRef = settings.document || (typeof document !== "undefined" ? document : null);
    if (!documentRef) return null;
    const run = (rootNode) => enhanceMarkdown(rootNode || documentRef, { document: documentRef });
    run(documentRef);
    if (typeof MutationObserver === "undefined") return null;
    const observer = new MutationObserver((records) => {
      records.forEach((record) => record.addedNodes.forEach((node) => {
        if (node.nodeType === 1) run(node);
      }));
    });
    observer.observe(documentRef.body, { childList: true, subtree: true });
    return observer;
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => init());
    else init();
  }

  return {
    attachmentLink,
    enhanceMarkdown,
    init,
    parseInline,
    parseMarkdown,
    renderMarkdown,
    sanitizeUrl,
    tokenizeCode,
  };
});
