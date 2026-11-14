/* NYTE theme only: tag <html> with the page family so the page takes its lead
   colour (NYTE brand book COL-01), and measure where the page hero starts so
   on phones the hero fills the first screen with its Band at the bottom.
   Loaded in <head>, before first paint. */
(function () {
  var p = location.pathname;
  var families = [
    ["price", /^\/(pricing|credits|index|bedrock-group-buy|token-exchange|green-tokens|console\/credits)(\/|$)/],
    ["trust", /^\/(trust|security|privacy|status|verified|badge|encrypted-ai|confidential-computing-llm|private-llm-api|no-log-llm-api|llm-zero-data-retention|hipaa-llm-api|gdpr-compliant-llm-api|llm-data-residency|eu-ai-act-llm-compliance|claude-api-privacy|deepseek-api-privacy|trustedos|legal|terms)(\/|$)/],
    ["knowledge", /^\/(docs|blog|resources|support|quickstart|benchmarks|llm-provider-latency-benchmarks|model-precision|prompt-caching|careers|customers|media-kit|contact)(\/|$)/]
  ];
  var family = "market";
  for (var i = 0; i < families.length; i++) {
    if (families[i][1].test(p)) { family = families[i][0]; break; }
  }
  document.documentElement.setAttribute("data-page", family);
})();

(function () {
  var root = document.documentElement;
  var heroes = ["public-hero", "models-page-hero", "md-hero", "compare-hero", "routing-feature-hero",
    "company-intro", "status-hero", "ti-hero", "lb-hero", "green-hero", "tm-hero", "ts-heading",
    "tx-heading", "bgb-hero", "cc-hero", "media-intro"];
  var selector = heroes.map(function (name) { return "main > ." + name; }).join(", ");
  var queued = false;
  function measure() {
    queued = false;
    var hero = document.querySelector(selector);
    if (!hero) return;
    var top = Math.round(hero.getBoundingClientRect().top + window.scrollY);
    root.style.setProperty("--nyte-above-hero", top + "px");
  }
  function queue() {
    if (queued) return;
    queued = true;
    window.requestAnimationFrame(measure);
  }
  document.addEventListener("DOMContentLoaded", measure);
  window.addEventListener("resize", queue);
})();
