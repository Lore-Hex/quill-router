// Repeat column labels visually on phones while retaining table semantics.
(() => {
  "use strict";
  document.querySelectorAll("[data-reference-table]").forEach((table) => {
    if (table.querySelector("[colspan], [rowspan], table")) return;
    const rows = [...table.tBodies].flatMap((body) => [...body.rows]);
    if (!rows.length) return;
    const headers = table.tHead?.rows.length === 1 ? [...table.tHead.rows[0].cells] : [];
    const columns = headers.length > 0 && headers.every((cell) => cell.tagName === "TH")
      && rows.every((row) => row.cells.length === headers.length);
    const rowHeaders = !table.tHead && rows.every((row) => row.cells.length === 2
      && row.cells[0].tagName === "TH" && row.cells[1].tagName === "TD");
    if (!columns && !rowHeaders) return;
    table.classList.add("reference-mobile-table");
    table.setAttribute("role", "table");
    [...table.tBodies, ...(table.tHead ? [table.tHead] : [])].forEach((group) => group.setAttribute("role", "rowgroup"));
    [...table.rows].forEach((row) => {
      row.setAttribute("role", "row");
      [...row.cells].forEach((cell) => cell.setAttribute("role", cell.tagName === "TH"
        ? (row.parentElement === table.tHead ? "columnheader" : "rowheader") : "cell"));
    });
    if (columns) {
      table.classList.add("reference-column-table");
      headers.forEach((header) => { header.scope = "col"; });
      rows.forEach((row) => [...row.cells].forEach((cell, index) => {
        if (index === 0) { cell.classList.add("reference-key"); return; }
        const label = document.createElement("span");
        label.className = "reference-cell-label";
        label.setAttribute("aria-hidden", "true");
        label.textContent = headers[index].textContent.trim();
        cell.prepend(label);
      }));
    } else {
      rows.forEach((row) => { row.cells[0].scope = "row"; });
    }
  });
  // Keep the desktop reference table intact; offer compact, native disclosures on phones.
  document.querySelectorAll("[data-reference-accordion]").forEach((table) => {
    if (table.classList.contains("has-reference-accordion")
      || table.querySelector("[colspan], [rowspan], table")) return;
    const rows = [...table.tBodies].flatMap((body) => [...body.rows]);
    if (!rows.length || !rows.every((row) => row.cells.length === 3
      && row.dataset.purpose?.trim() && row.cells[0].querySelector("code"))) return;
    const cloneContent = (source) => {
      const clone = source.cloneNode(true);
      clone.removeAttribute("id");
      clone.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
      clone.querySelectorAll(".reference-cell-label").forEach((node) => node.remove());
      return [...clone.childNodes];
    };
    const accordion = document.createElement("div");
    accordion.className = "content-reference-accordion";
    rows.forEach((row, index) => {
      const entry = document.createElement("details");
      entry.className = "content-reference-entry";
      entry.open = index === 0;
      const summary = document.createElement("summary");
      const field = document.createElement("code");
      field.append(...cloneContent(row.cells[0].querySelector("code")));
      const purpose = document.createElement("span");
      purpose.className = "content-reference-purpose";
      purpose.textContent = row.dataset.purpose.trim();
      summary.append(field, purpose);
      const body = document.createElement("div");
      body.className = "content-reference-body";
      body.append(...cloneContent(row.cells[1]));
      const values = document.createElement("div");
      values.className = "content-reference-values";
      const label = document.createElement("strong");
      label.textContent = "Values";
      values.append(label, document.createTextNode(" "), ...cloneContent(row.cells[2]));
      body.append(values);
      entry.append(summary, body);
      accordion.append(entry);
    });
    table.after(accordion);
    table.classList.add("has-reference-accordion");
  });
  // Arrow buttons for anything that scrolls sideways; hidden when it fits.
  const addScrollControls = (scroller, noun, placeControls) => {
    const controls = document.createElement("div");
    controls.className = "content-code-controls";
    controls.hidden = true;
    placeControls(controls);
    const buttons = [-1, 1].map((direction) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "content-code-scroll";
      button.setAttribute("aria-label", `Scroll ${noun} ${direction < 0 ? "left" : "right"}`);
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 20 20");
      svg.setAttribute("width", "20");
      svg.setAttribute("height", "20");
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", direction < 0 ? "M12 5 7 10l5 5" : "m8 5 5 5-5 5");
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "currentColor");
      path.setAttribute("stroke-width", "1.5");
      path.setAttribute("stroke-linecap", "round");
      path.setAttribute("stroke-linejoin", "round");
      svg.append(path);
      button.append(svg);
      button.addEventListener("click", () => {
        scroller.scrollBy({ left: direction * scroller.clientWidth * 0.75, behavior: "instant" });
        update();
      });
      controls.append(button);
      return button;
    });
    const label = noun.charAt(0).toUpperCase() + noun.slice(1);
    const update = () => {
      const maxScroll = scroller.scrollWidth - scroller.clientWidth;
      const overflow = maxScroll > 1;
      controls.hidden = !overflow;
      buttons[0].disabled = scroller.scrollLeft <= 1;
      buttons[1].disabled = scroller.scrollLeft >= maxScroll - 1;
      scroller.classList.toggle("has-overflow", overflow);
      scroller.classList.toggle("can-scroll-right", overflow && scroller.scrollLeft < maxScroll - 1);
      scroller.tabIndex = overflow ? 0 : -1;
      scroller.setAttribute("aria-label", overflow ? `${label}, scroll horizontally for more` : label);
    };
    scroller.addEventListener("scroll", update, { passive: true });
    new ResizeObserver(update).observe(scroller);
    document.fonts.ready.then(update);
    update();
  };
  // Code examples scroll with the shared code block's own arrow (below).
  // Comparison tables: arrows sit above the table so the scroll is seen first.
  document.querySelectorAll(".scroll-table").forEach((wrap) => {
    // Pinned names like "trustedrouter/trev-1.0" may wrap only after the slash.
    wrap.querySelectorAll("tbody td:first-child").forEach((cell) => {
      const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
      const nodes = [];
      for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node);
      nodes.forEach((node) => {
        const parts = node.textContent.split(/(?<=\/)/);
        if (parts.length < 2) return;
        node.replaceWith(...parts.flatMap((part, index) => (index ? [document.createElement("wbr"), part] : [part])));
      });
    });
    addScrollControls(wrap, "table", (controls) => {
      controls.classList.add("scroll-table-controls");
      wrap.before(controls);
    });
  });
})();

// Opt-in section navigation shared by long-form content pages.
(() => {
  "use strict";
  document.querySelectorAll("[data-content-toc]").forEach((layout) => {
    const content = layout.querySelector(".content-document");
    if (!content || layout.querySelector(".content-toc")) return;
    const headings = [...content.querySelectorAll("h2")];
    if (headings.length < 2) return;
    const nav = document.createElement("nav");
    nav.className = "content-toc";
    nav.setAttribute("aria-label", "On this page");
    const title = document.createElement("p");
    title.className = "content-toc-title";
    title.textContent = "On this page";
    const disclosure = document.createElement("details");
    disclosure.className = "content-toc-disclosure";
    const summary = document.createElement("summary");
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 20 20");
    icon.setAttribute("width", "20");
    icon.setAttribute("height", "20");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("focusable", "false");
    const iconPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    iconPath.setAttribute("d", "M3 5h1m3 0h10M3 10h1m3 0h10M3 15h1m3 0h10");
    iconPath.setAttribute("fill", "none");
    iconPath.setAttribute("stroke", "currentColor");
    iconPath.setAttribute("stroke-width", "1.5");
    iconPath.setAttribute("stroke-linecap", "round");
    icon.append(iconPath);
    summary.append(icon, document.createTextNode("Contents"));
    const list = document.createElement("ol");
    const entries = headings.map((heading) => {
      const section = heading.closest(".content-section");
      const target = section && content.contains(section) && section.querySelector("h2") === heading
        ? section : heading;
      if (!target.id) {
        const base = heading.textContent.trim().toLowerCase().normalize("NFKD")
          .replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9]+/g, "-")
          .replace(/^-|-$/g, "") || "section";
        let id = base;
        let suffix = 2;
        while (document.getElementById(id)) id = `${base}-${suffix++}`;
        target.id = id;
      }
      heading.tabIndex = -1;
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = `#${encodeURIComponent(target.id)}`;
      const longLabel = document.createElement("span");
      longLabel.className = "content-toc-long";
      longLabel.textContent = heading.textContent.trim();
      const shortLabel = document.createElement("span");
      shortLabel.className = "content-toc-short";
      shortLabel.textContent = heading.dataset.tocLabel || longLabel.textContent;
      link.append(longLabel, shortLabel);
      item.append(link);
      list.append(item);
      return { heading, target, link };
    });
    disclosure.append(summary, list);
    nav.append(title, disclosure);
    layout.prepend(nav);
    layout.classList.add("has-content-toc");

    const desktop = window.matchMedia("(min-width: 1400px)");
    const syncDisclosure = () => { disclosure.open = desktop.matches; };
    desktop.addEventListener("change", syncDisclosure);
    syncDisclosure();

    let pending = false;
    const updateCurrent = () => {
      pending = false;
      let active = entries[0];
      for (const entry of entries) {
        if (entry.target.getBoundingClientRect().top <= 100) active = entry;
      }
      entries.forEach((entry) => {
        if (entry === active) entry.link.setAttribute("aria-current", "location");
        else entry.link.removeAttribute("aria-current");
      });
    };
    const scheduleCurrent = () => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(updateCurrent);
    };
    const navigate = (entry, focus) => {
      disclosure.open = desktop.matches;
      requestAnimationFrame(() => {
        entry.target.scrollIntoView({ behavior: "instant", block: "start" });
        if (focus) entry.heading.focus({ preventScroll: true });
        updateCurrent();
      });
    };
    entries.forEach((entry) => {
      entry.link.addEventListener("click", (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey
          || event.shiftKey || event.altKey) return;
        event.preventDefault();
        const hash = entry.link.hash;
        if (window.location.hash !== hash) window.history.pushState(null, "", hash);
        navigate(entry, true);
      });
    });
    const followHash = (focus = true) => {
      let id;
      try { id = decodeURIComponent(window.location.hash.slice(1)); }
      catch { return; }
      const entry = entries.find((candidate) => candidate.target.id === id);
      if (entry) navigate(entry, focus);
      else scheduleCurrent();
    };
    window.addEventListener("hashchange", () => followHash());
    window.addEventListener("popstate", () => followHash());
    window.addEventListener("scroll", scheduleCurrent, { passive: true });
    window.addEventListener("resize", scheduleCurrent, { passive: true });
    new ResizeObserver(scheduleCurrent).observe(content);
    document.fonts.ready.then(scheduleCurrent);
    updateCurrent();
    followHash(false);
  });
})();

// Code blocks: one shared component for every <pre> on public pages (the
// NYTE code block standard). An Ink panel with a header strip inside it: a
// short label (language or file) on the left; on the right a scroll arrow
// while code is hidden to the right, and an icon-only Copy button. Existing
// .copy-terminal and .green-code panels become the panel and their head
// becomes the strip; an existing data-copy-target still names what is
// copied. Tabbed quickstart boxes (.md-code), the homepage, chat output and
// the console keep their own code.
(() => {
  "use strict";
  if (document.body.classList.contains("console")) return;
  const skip = ".mcode, .md-code, .fusion-answer, .chat-msg-md, [data-code-plain]";
  const svgNS = "http://www.w3.org/2000/svg";
  const icon = (className, paths) => {
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    if (className) svg.setAttribute("class", className);
    paths.forEach((d) => {
      const path = document.createElementNS(svgNS, "path");
      path.setAttribute("d", d);
      svg.append(path);
    });
    return svg;
  };
  // The kit's copy and check shapes, as on the NYTE API page.
  const copyPaths = ["M9 9 L20 9 L20 20 L9 20 L9 9", "M15 7.25 L15 4 L4 4 L4 15 L7.25 15"];
  const donePaths = ["M4.5 12.5 L9.5 17.5 L19.5 7"];
  const nextPaths = ["m9 5 7 7-7 7"];
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const collapseAfter = 10;

  // Head labels name the language, a file, or describe the example. The strip
  // shows the language (or file); a description names the Copy button.
  const languages = new Map([
    ["python", "Python"], ["typescript", "TypeScript"], ["javascript", "JavaScript"],
    ["json", "JSON"], ["application/json", "JSON"], ["html", "HTML"], ["swift", "Swift"],
    ["yaml", "YAML"], ["http", "HTTP"], ["prompt", "Prompt"], ["curl", "Shell"],
    ["curl + jq", "Shell"], ["bash", "Shell"], ["shell", "Shell"], ["uvx", "Shell"],
    ["environment", "Shell"],
  ]);
  const isFile = (text) => /^[\w.-]+\.[a-z]{1,5}$/i.test(text);
  const guess = (code) => {
    const text = code.trim();
    // The first line that is not a comment says the most.
    const first = text.split("\n").find((line) => line.trim() && !/^\s*(#(?!!)|\/\/)/.test(line)) || "";
    if (/^[[{]/.test(first) || /^\s*"[\w.-]+"\s*:/.test(first)) return "JSON";
    if (/^</.test(first)) return "HTML";
    if (/^(GET|POST|PUT|PATCH|DELETE) \S/.test(first)) return "HTTP";
    if (/^(\$ |#!|curl\b|export |pip3? |npm |npx |uvx? |git |docker |brew |claude |codex |openssl |wget |echo |jq |python3? )/.test(first)) return "Shell";
    if (/^(from [\w.]+ import |import [\w.]+\s*$|import \w+ as |def |print\(|\w+ = OpenAI\(|\w+ = \w+(\.\w+)+\()/m.test(text)) return "Python";
    if (/^(import .+ from ["']|const |let |await |export )/m.test(text)) return "JavaScript";
    return "";
  };

  // Quiet highlighting: strings and values in lime, comments muted. Only
  // plain text nodes are touched, so markup inside the code stays as it is.
  const highlighters = {
    Python: /("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|((?:^|(?<=\s))#[^\n]*)/g,
    Shell: /("(?:[^"\\\n]|\\.)*"|'[^'\n]*')|((?:^|(?<=\s))#[^\n]*)/g,
    JSON: /("(?:[^"\\\n]|\\.)*")()/g,
    JavaScript: /("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`[^`]*`)|((?:^|(?<=\s))\/\/[^\n]*)/g,
    TypeScript: /("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`[^`]*`)|((?:^|(?<=\s))\/\/[^\n]*)/g,
    HTTP: /("(?:[^"\\\n]|\\.)*")()/g,
    HTML: /("[^"\n]*")|(<!--[^]*?-->)/g,
  };
  const highlight = (pre, language) => {
    const pattern = highlighters[language];
    if (!pattern) return;
    const walker = document.createTreeWalker(pre, NodeFilter.SHOW_TEXT);
    const nodes = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node);
    nodes.forEach((node) => {
      const text = node.textContent;
      const parts = [];
      let last = 0;
      for (const match of text.matchAll(pattern)) {
        if (!match[0]) continue;
        if (match.index > last) parts.push(text.slice(last, match.index));
        const span = document.createElement("span");
        span.className = match[1] ? "tr-code-s" : "tr-code-c";
        span.textContent = match[0];
        parts.push(span);
        last = match.index + match[0].length;
      }
      if (!parts.length) return;
      if (last < text.length) parts.push(text.slice(last));
      node.replaceWith(...parts);
    });
  };

  document.querySelectorAll("pre").forEach((pre) => {
    if (pre.closest(skip) || pre.closest(".tr-code")) return;
    let panel = pre.closest(".copy-terminal, .green-code");
    if (panel && panel.querySelectorAll("pre").length !== 1) panel = null;
    const oldHead = panel?.querySelector(":scope > .code-head, :scope > .green-code-head") || null;
    const oldButton = oldHead?.querySelector("button") || null;
    const targetId = oldButton?.dataset.copyTarget || "";
    const source = (targetId && document.getElementById(targetId)) || pre;

    const parts = oldHead
      ? [...oldHead.children].filter((el) => el.tagName !== "BUTTON")
        .map((el) => el.textContent.trim()).filter(Boolean)
      : [];
    const language = parts.map((part) => languages.get(part.toLowerCase())).find(Boolean) || "";
    const file = parts.find(isFile) || "";
    const guessed = guess(pre.textContent);
    const label = language || file || guessed || parts[0] || "Text";
    const title = parts[0] && parts[0] !== label && !languages.has(parts[0].toLowerCase()) ? parts[0] : "";
    const plain = label === "Text" || label === "Prompt";
    const knownLanguage = [...languages.values(), "JavaScript"].includes(label);
    const name = title || (plain ? label.toLowerCase() : knownLanguage ? `${label} code` : label);

    if (!panel) {
      panel = document.createElement("div");
      pre.before(panel);
      panel.append(pre);
    }
    panel.classList.add("tr-code");

    const head = document.createElement("div");
    head.className = "tr-code-head";
    const labelText = document.createElement("span");
    labelText.className = label === file ? "tr-code-label is-file" : "tr-code-label";
    labelText.textContent = label;
    const tools = document.createElement("span");
    tools.className = "tr-code-tools";
    const next = document.createElement("button");
    next.type = "button";
    next.className = "tr-code-next";
    next.setAttribute("aria-label", "Scroll code right");
    next.title = "Scroll code right";
    next.hidden = true;
    next.append(icon("", nextPaths));
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "tr-code-copy";
    const resting = `Copy ${name}`;
    copy.setAttribute("aria-label", resting);
    copy.title = resting;
    if (targetId) copy.dataset.copyTarget = targetId;
    copy.append(icon("tr-code-icon", copyPaths), icon("tr-code-done", donePaths));
    tools.append(next, copy);
    head.append(labelText, tools);
    if (oldHead) oldHead.replaceWith(head);
    else panel.prepend(head);

    const status = document.createElement("span");
    status.className = "tr-code-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    status.setAttribute("aria-atomic", "true");
    panel.append(status);

    highlight(pre, language || guessed);

    let timer = 0;
    const announce = (message) => {
      status.textContent = "";
      window.setTimeout(() => { status.textContent = message; }, 50);
    };
    copy.addEventListener("click", async () => {
      window.clearTimeout(timer);
      try {
        await navigator.clipboard.writeText(source.textContent.trim());
        copy.classList.add("is-done");
        copy.setAttribute("aria-label", "Copied");
        copy.title = "Copied";
        announce("Copied.");
      } catch {
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(source);
        selection?.removeAllRanges();
        selection?.addRange(range);
        announce("Code selected. Use your device's copy command.");
      }
      timer = window.setTimeout(() => {
        copy.classList.remove("is-done");
        copy.setAttribute("aria-label", resting);
        copy.title = resting;
      }, 2000);
    });

    // Lines never wrap; wide code scrolls inside the panel.
    const ownsFocus = !pre.hasAttribute("tabindex");
    const update = () => {
      const max = pre.scrollWidth - pre.clientWidth;
      next.hidden = !(max > 1 && pre.scrollLeft < max - 1);
      if (!ownsFocus) return;
      if (max > 1) {
        pre.tabIndex = 0;
        pre.setAttribute("aria-label", `${title || label}, scroll sideways for more`);
      } else {
        pre.removeAttribute("tabindex");
        pre.removeAttribute("aria-label");
      }
    };
    next.addEventListener("click", () => {
      pre.scrollBy({ left: pre.clientWidth * 0.75, behavior: reduceMotion.matches ? "instant" : "smooth" });
    });
    pre.addEventListener("scroll", update, { passive: true });
    new ResizeObserver(update).observe(pre);
    document.fonts.ready.then(update);
    update();

    // Docs pages: long examples show their first lines on phones; copying
    // still copies all of it.
    if (!document.body.hasAttribute("data-docs-code")) return;
    const lines = pre.textContent.trim().split("\n").length;
    if (lines <= collapseAfter + 2) return;
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "tr-code-more";
    const setCollapsed = (collapsed) => {
      pre.classList.toggle("is-collapsed", collapsed);
      toggle.textContent = collapsed ? `Show all ${lines} lines` : "Show less";
      toggle.setAttribute("aria-expanded", String(!collapsed));
    };
    toggle.addEventListener("click", () => {
      setCollapsed(!pre.classList.contains("is-collapsed"));
      update();
    });
    pre.after(toggle);
    setCollapsed(true);
  });
})();
