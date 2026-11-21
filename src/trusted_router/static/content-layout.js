// Shared helpers for code blocks and sideways scrolling on public pages.
// window.TRCode: the kit's copy and check icons and one copy behaviour.
// window.TRScroll: one sideways-scroll hint, a soft edge fade that shows only
// while there is more to see. Page scripts load after this file, so they can
// call these; everything here also attaches on its own.
(() => {
  "use strict";
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

  // Copy: writes the text, swaps the icon for a check, says "Copied" in the
  // button's label and in a role=status line. Without clipboard access it
  // selects the text instead. Buttons that carry their own icon keep it.
  const wireCopy = (button, {
    text, source, status, done = "Copied.", fallback = "Code selected. Use your device's copy command.",
    resetAfter = 2000, onCopy, disableWhileCopying = false, keepLabel = false,
  } = {}) => {
    if (!button || "copyWired" in button.dataset) return;
    button.dataset.copyWired = "";
    if (button.classList.contains("tr-code-copy") && !button.querySelector("svg, img")) {
      button.replaceChildren(icon("tr-code-icon", copyPaths), icon("tr-code-done", donePaths));
    }
    // The resting label is read at each click: pages may rename the button.
    let resting = "";
    let restingTitle = "";
    let timer = 0;
    const statusEl = () => (typeof status === "function" ? status() : status);
    const announce = (message) => {
      const line = statusEl();
      if (!line) return;
      line.textContent = "";
      window.setTimeout(() => { line.textContent = message; }, 50);
    };
    button.addEventListener("click", async () => {
      const node = typeof source === "function" ? source() : source;
      const value = typeof text === "function" ? text() : (node ? node.textContent.trim() : "");
      if (!value && !node) return;
      if (!button.classList.contains("is-done")) {
        resting = button.getAttribute("aria-label") || "";
        restingTitle = button.title;
      }
      window.clearTimeout(timer);
      if (disableWhileCopying) button.disabled = true;
      try {
        await navigator.clipboard.writeText(value);
        button.classList.add("is-done");
        button.setAttribute("data-copied", "");
        if (!keepLabel) {
          button.setAttribute("aria-label", "Copied");
          if (restingTitle) button.title = "Copied";
        }
        announce(done);
        onCopy?.();
      } catch {
        const selection = window.getSelection();
        if (node && selection) {
          const range = document.createRange();
          range.selectNodeContents(node);
          selection.removeAllRanges();
          selection.addRange(range);
        }
        announce(fallback);
      } finally {
        if (disableWhileCopying) button.disabled = false;
      }
      timer = window.setTimeout(() => {
        button.classList.remove("is-done");
        button.removeAttribute("data-copied");
        if (!keepLabel) {
          if (resting) button.setAttribute("aria-label", resting);
          else button.removeAttribute("aria-label");
          if (restingTitle) button.title = restingTitle;
        }
      }, resetAfter);
    });
  };
  window.TRCode = { icon, copyPaths, donePaths, wireCopy };

  // Sideways scroll (NYTE sideways-scroll rule): a soft fade into the ground
  // colour at each edge that has more to see; nothing to press, and nothing
  // shows while the content fits.
  // - row: single-line rows (chips, link rows). A sticky edge inside the row
  //   that takes no width of its own.
  // - over: code and tables. The fade sits over the scroller's box inside its
  //   parent, and the scroller itself takes keyboard focus.
  const groundOf = (element) => {
    for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
      const colour = getComputedStyle(node).backgroundColor;
      const parts = colour.match(/[\d.]+/g);
      if (parts && (parts.length < 4 || Number(parts[3]) > 0.5)) return colour;
    }
    return "rgb(250, 248, 244)";
  };

  // Rows are measured together once per frame (all reads, then all writes),
  // and rows and code only start once they come near the screen: a catalog
  // page has well over a thousand rows.
  const queue = new Set();
  let frame = 0;
  const flush = () => {
    frame = 0;
    const items = [...queue];
    queue.clear();
    items.map((item) => [item, item.measure()]).forEach(([item, state]) => item.apply(state));
  };
  const schedule = (item) => {
    queue.add(item);
    if (!frame) frame = window.requestAnimationFrame(flush);
  };
  const attached = new WeakMap();
  const resized = new ResizeObserver((entries) => entries.forEach((entry) => {
    const item = attached.get(entry.target) || attached.get(entry.target.parentElement);
    if (item) schedule(item);
  }));
  const nearScreen = new IntersectionObserver((entries) => entries.forEach((entry) => {
    if (!entry.isIntersecting) return;
    nearScreen.unobserve(entry.target);
    attached.get(entry.target)?.start();
  }), { rootMargin: "400px 0px" });

  // attach(scroller, { place: "row" | "over", focusLabel })
  const attach = (scroller, { place = "row", focusLabel = "" } = {}) => {
    if (!scroller) return null;
    if (attached.has(scroller)) return attached.get(scroller);
    const over = place === "over";
    const host = over ? scroller.parentElement : null;
    const make = (side) => {
      const edge = document.createElement(!over && /^(UL|OL)$/.test(scroller.tagName) ? "li" : "span");
      edge.className = `tr-scroll-edge tr-scroll-edge-${side} is-${place}`;
      edge.setAttribute(edge.tagName === "LI" ? "role" : "aria-hidden", edge.tagName === "LI" ? "none" : "true");
      edge.hidden = true;
      return edge;
    };
    let prev = null;
    let next = null;
    const ownsFocus = Boolean(focusLabel) && !scroller.hasAttribute("tabindex");
    let ground = "";
    const item = {
      // Reads only.
      measure: () => {
        const style = getComputedStyle(scroller);
        const max = scroller.scrollWidth - scroller.clientWidth;
        const overflow = /auto|scroll/.test(style.overflowX) && max > 1;
        const state = { overflow, max, x: Math.abs(scroller.scrollLeft) };
        if (!overflow) return state;
        if (!ground) state.ground = groundOf(scroller);
        if (!over) {
          state.gap = parseFloat(style.columnGap) || 0;
          state.padStart = parseFloat(style.paddingLeft) || 0;
          state.padEnd = parseFloat(style.paddingRight) || 0;
        } else {
          // Over code: cover the code area's own box inside its panel.
          state.top = scroller.offsetTop;
          state.height = scroller.clientHeight;
          state.left = scroller.offsetLeft;
          state.right = host.clientWidth - scroller.offsetLeft - scroller.clientWidth;
        }
        return state;
      },
      // Writes only. The edges are added the first time there is more to
      // see; a row's edge takes no width, so adding it changes nothing.
      apply: (state) => {
        const { overflow, max, x } = state;
        if (!prev && overflow) {
          prev = make("prev");
          next = make("next");
          if (over) {
            host.classList.add("tr-scroll-host");
            host.append(prev, next);
          } else {
            scroller.prepend(prev);
            scroller.append(next);
          }
        }
        if (!prev) return;
        [prev, next].forEach((edge) => {
          edge.hidden = !overflow;
          if (state.ground) edge.style.setProperty("--tr-scroll-ground", state.ground);
          if (!overflow) return;
          if (over) {
            edge.style.top = `${state.top}px`;
            edge.style.height = `${state.height}px`;
          } else {
            edge.style.setProperty("--tr-scroll-gap", `${state.gap}px`);
            // A sticky edge stops at the row's padding; it covers that too.
            edge.style.setProperty("--tr-scroll-pad", `${edge === prev ? state.padStart : state.padEnd}px`);
          }
        });
        if (state.ground) ground = state.ground;
        if (over && overflow) {
          prev.style.left = `${state.left}px`;
          next.style.right = `${state.right}px`;
        }
        prev.classList.toggle("is-off", !(overflow && x > 1));
        next.classList.toggle("is-off", !(overflow && x < max - 1));
        if (ownsFocus) {
          if (overflow) {
            scroller.tabIndex = 0;
            scroller.setAttribute("role", "region");
            scroller.setAttribute("aria-label", `${focusLabel}, scroll sideways for more`);
          } else {
            scroller.removeAttribute("tabindex");
            scroller.removeAttribute("role");
            scroller.removeAttribute("aria-label");
          }
        }
      },
      started: false,
      start: () => {
        if (item.started) return;
        item.started = true;
        scroller.addEventListener("scroll", () => schedule(item), { passive: true });
        resized.observe(scroller);
        // Code: tabs and pickers change the content, not the box.
        if (over) [...scroller.children].forEach((child) => resized.observe(child));
        document.fonts.ready.then(() => schedule(item));
        schedule(item);
      },
      update: () => schedule(item),
    };
    attached.set(scroller, item);
    nearScreen.observe(scroller);
    return item;
  };

  // Code areas (.tr-code-body) in the quickstart box and homepage panel.
  const attachCode = (body) => attach(body, { place: "over", focusLabel: "Code" });
  // Rows: anything marked data-scroll-row, plus the existing row classes.
  const rows = [
    "[data-scroll-row]",
    ".model-explorer > .model-filter-row",
    ".model-result-card .catalog-privacy-summary",
    ".model-result-card .model-card-badges",
    ".model-result-card .model-card-providers",
    "[data-md-chips-track]",
    ".compare-chip-track",
  ].join(", ");
  const scan = (root) => {
    if (root.matches?.(rows)) attach(root);
    root.querySelectorAll?.(rows).forEach((row) => attach(row));
    if (root.matches?.(".tr-code-body")) attachCode(root);
    root.querySelectorAll?.(".tr-code-body").forEach(attachCode);
  };
  window.TRScroll = { attach, scan };
  scan(document);
  // Cards and rows drawn later (filters, pagination) get the same fade.
  new MutationObserver((records) => {
    records.forEach((record) => record.addedNodes.forEach((node) => {
      if (node.nodeType === 1 && !node.classList.contains("tr-scroll-edge")) scan(node);
    }));
  }).observe(document.body, { childList: true, subtree: true });
})();

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
  // Pinned names like "trustedrouter/trev-1.0" in comparison tables may wrap
  // only after the slash.
  document.querySelectorAll(".scroll-table tbody td:first-child").forEach((cell) => {
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    const nodes = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node);
    nodes.forEach((node) => {
      const parts = node.textContent.split(/(?<=\/)/);
      if (parts.length < 2) return;
      node.replaceWith(...parts.flatMap((part, index) => (index ? [document.createElement("wbr"), part] : [part])));
    });
  });
  // Wide tables: the same soft edge fade as code.
  document.querySelectorAll(".scroll-table, .model-table-wrap, .seo-evidence-table-wrap").forEach((wrap) => {
    window.TRScroll.attach(wrap, { place: "over", focusLabel: wrap.querySelector("caption")?.textContent.trim() || "Table" });
  });
})();

// Opt-in section navigation shared by long-form content pages.
(() => {
  "use strict";
  function buildContents(layout, content, headings) {
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
    return { disclosure, entries };
  }

  function readContents(nav, content) {
    const links = new Map([...nav.querySelectorAll('a[href^="#"]')].map(link => [link.hash, link]));
    // The menu is grouped by task; scroll tracking follows document order.
    const entries = [...content.querySelectorAll("a.docs-topic-link")].flatMap(topic => {
      const link = links.get(topic.hash);
      const target = document.getElementById(topic.hash.slice(1));
      const heading = topic.parentElement;
      if (!link || !target) return [];
      heading.tabIndex = -1;
      return [{ heading, target, link }];
    });
    return { disclosure: nav.querySelector("details"), entries };
  }

  document.querySelectorAll("[data-content-toc]").forEach((layout) => {
    const content = layout.querySelector(".content-document");
    if (!content) return;
    const nav = layout.querySelector(".content-toc");
    if (nav && !nav.hasAttribute("data-docs-contents")) return;
    const headings = [...content.querySelectorAll("h2")];
    if (headings.length < 2) return;
    const { disclosure, entries } = nav ? readContents(nav, content) : buildContents(layout, content, headings);
    if (!entries.length) return;

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
// short label (language or file) on the left and an icon-only Copy button
// on the right. Wide code scrolls in the panel with the shared soft edge
// fade. Existing .copy-terminal, .green-code and .company-code panels
// become the panel and their head becomes the strip; an existing
// data-copy-target or data-copy-prompt-target still names what is copied.
// Tabbed quickstart boxes (.md-code) and the homepage migration panel use
// the same strip, copy and scroll parts from their own markup; chat output
// and the console keep their own code.
(() => {
  "use strict";
  if (document.body.classList.contains("console")) return;
  const skip = ".mcode, .md-code, .fusion-answer, .chat-msg-md, [data-code-plain]";
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
    let panel = pre.closest(".copy-terminal, .green-code, .company-code");
    if (panel && panel.querySelectorAll("pre").length !== 1) panel = null;
    const oldHead = panel?.querySelector(":scope > .code-head, :scope > .green-code-head, :scope > .company-code-head") || null;
    const oldButton = oldHead?.querySelector("button") || null;
    const promptTarget = oldButton?.dataset.copyPromptTarget || "";
    const targetId = oldButton?.dataset.copyTarget || promptTarget;
    const source = (targetId && document.getElementById(targetId)) || pre;
    // Prompt panels (copy-prompt.js) keep their status line and wording.
    const promptStatus = promptTarget ? document.getElementById(oldButton.dataset.copyPromptStatus || "") : null;

    // Head text: each child, and a <small> inside one names the language.
    const parts = oldHead
      ? [...oldHead.children].filter((el) => el.tagName !== "BUTTON")
        .flatMap((el) => {
          const small = el.querySelector("small");
          const own = small ? el.textContent.replace(small.textContent, "") : el.textContent;
          return [own.trim(), small?.textContent.trim() || ""];
        }).filter(Boolean)
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
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "tr-code-copy";
    const resting = oldButton?.getAttribute("aria-label") || `Copy ${name}`;
    copy.setAttribute("aria-label", resting);
    copy.title = resting;
    if (targetId && !promptTarget) copy.dataset.copyTarget = targetId;
    if (promptTarget) {
      copy.dataset.copyPromptTarget = promptTarget;
      copy.dataset.copyPromptStatus = oldButton.dataset.copyPromptStatus || "";
      copy.setAttribute("aria-controls", promptTarget);
    }
    tools.append(copy);
    head.append(labelText, tools);
    if (oldHead) oldHead.replaceWith(head);
    else panel.prepend(head);

    let status = promptStatus;
    // A prompt panel's own status line inside the panel is read out, not shown.
    if (status && panel.contains(status)) status.classList.add("tr-code-status");
    if (!status) {
      status = document.createElement("span");
      status.className = "tr-code-status";
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      status.setAttribute("aria-atomic", "true");
      panel.append(status);
    }

    highlight(pre, language || guessed);

    window.TRCode.wireCopy(copy, promptTarget
      ? { source, status, done: "Copied. Ready for your agent chat.", fallback: "Prompt selected. Use your device's copy command." }
      : { source, status });

    // Lines never wrap; wide code scrolls in the panel under the shared fade.
    const scroll = window.TRScroll.attach(pre, { place: "over", focusLabel: title || label });

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
      scroll.update();
    });
    pre.after(toggle);
    setCollapsed(true);
  });
})();

// Long inline code (URLs, model ids, field paths) wraps only after a / . ?
// & = or bracket, never mid-word. <wbr> adds a break point without changing
// the text that is copied.
(() => {
  "use strict";
  const breakAfter = /([/.?&=}\]])(?=[^/\s])/g;
  document.querySelectorAll(".public-wrap :not(pre) > code").forEach((code) => {
    if (code.textContent.length < 20) return;
    const walker = document.createTreeWalker(code, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      const parts = node.data.split(breakAfter);
      if (parts.length < 2) return;
      const fragment = document.createDocumentFragment();
      // split() with one capture group alternates text and break characters.
      for (let i = 0; i < parts.length; i += 2) {
        const text = parts[i] + (parts[i + 1] || "");
        if (text) fragment.append(text);
        if (i + 1 < parts.length) fragment.append(document.createElement("wbr"));
      }
      node.replaceWith(fragment);
    });
  });
})();
