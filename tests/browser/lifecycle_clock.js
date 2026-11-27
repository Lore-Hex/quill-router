// The time the server's provider lifecycle runs at.
//
// CI runs this suite a second time with TR_LIFECYCLE_CLOCK_OVERRIDE pinned past
// every scheduled cutover (ci.yml, browser-post-cutover), and the app the
// suite starts reads the same variable. A spec that decides what the page
// should show from a scheduled date (a suspension, a retirement) must ask this
// clock, not Date.now(): otherwise it expects the near side of the cutover
// while the server is past it, and fails only on the day the cutover arrives
// (the Fortanix suspension pre-empting the homepage offer, 2026-11-27).
function lifecycleNow() {
  const override = process.env.TR_LIFECYCLE_CLOCK_OVERRIDE;
  if (!override) return Date.now();
  const pinned = Date.parse(override);
  if (Number.isNaN(pinned)) throw new Error(`TR_LIFECYCLE_CLOCK_OVERRIDE is not a time: ${override}`);
  return pinned;
}

module.exports = { lifecycleNow };
