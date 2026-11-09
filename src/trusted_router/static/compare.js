"use strict";

// Sideways chip rows: the fade and arrow show only while more chips sit
// off to the right. The arrow scrolls the row for mouse users.
document.querySelectorAll(".compare-chips").forEach((row) => {
  const track = row.querySelector(".compare-chip-track");
  const nudge = row.querySelector(".compare-chip-nudge");
  if (!track) return;
  const update = () => {
    const more = track.scrollLeft + track.clientWidth < track.scrollWidth - 2;
    row.toggleAttribute("data-more", more);
  };
  row.setAttribute("data-ready", "");
  update();
  track.addEventListener("scroll", update, { passive: true });
  window.addEventListener("resize", update);
  nudge?.addEventListener("click", () => {
    track.scrollBy({ left: track.clientWidth * 0.8, behavior: "smooth" });
  });
});
