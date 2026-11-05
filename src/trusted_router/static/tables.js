// Wide data tables (.tbl-scroll): while columns are hidden to the right the edge
// fades and an arrow nudges that way; the arrow stays hidden after the first scroll.
(() => {
  "use strict";
  const svg = "http://www.w3.org/2000/svg";
  document.querySelectorAll(".tbl-scroll").forEach((scroller) => {
    if (scroller.parentElement?.classList.contains("tbl-c-frame")) return;
    const frame = document.createElement("div");
    frame.className = "tbl-c-frame";
    scroller.before(frame);
    frame.append(scroller);

    const arrow = document.createElement("span");
    arrow.className = "tbl-arrow";
    arrow.setAttribute("aria-hidden", "true");
    const icon = document.createElementNS(svg, "svg");
    icon.setAttribute("viewBox", "0 0 16 16");
    icon.setAttribute("width", "16");
    icon.setAttribute("height", "16");
    icon.setAttribute("focusable", "false");
    const path = document.createElementNS(svg, "path");
    path.setAttribute("d", "M3 8h10M9 4l4 4-4 4");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.6");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    icon.append(path);
    arrow.append(icon);
    frame.append(arrow);

    const update = () => {
      const max = scroller.scrollWidth - scroller.clientWidth;
      const right = scroller.scrollLeft < max - 1;
      frame.classList.toggle("can-scroll-right", right);
      scroller.classList.toggle("can-scroll-right", right);
      if (max > 1) {
        scroller.tabIndex = 0;
        scroller.setAttribute("role", "region");
        scroller.setAttribute("aria-label", scroller.querySelector("caption")?.textContent.trim() || "Table");
      } else {
        scroller.removeAttribute("tabindex");
        scroller.removeAttribute("role");
        scroller.removeAttribute("aria-label");
      }
    };
    scroller.addEventListener("scroll", () => {
      if (scroller.scrollLeft > 1) frame.classList.add("tbl-scrolled");
      update();
    }, { passive: true });
    scroller.addEventListener("keydown", (event) => {
      if (event.target !== scroller || event.altKey || event.ctrlKey || event.metaKey
        || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      scroller.scrollBy({ left: event.key === "ArrowRight" ? 40 : -40, behavior: "instant" });
    });
    const observer = new ResizeObserver(update);
    observer.observe(scroller);
    const table = scroller.querySelector("table");
    if (table) observer.observe(table);
    document.fonts?.ready.then(update);
    update();
  });
})();
