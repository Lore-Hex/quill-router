import * as fs from "node:fs";
import { feature } from "topojson-client";
import { geoEquirectangular, geoPath } from "d3-geo";

const topo = JSON.parse(fs.readFileSync("./node_modules/world-atlas/land-110m.json", "utf8"));
const land = feature(topo, topo.objects.land);

const W = 1000, H = 500;
// Equirectangular: maps lon -180..180 -> x 0..W, lat 90..-90 -> y 0..H.
// d3 default `geoEquirectangular` is centered at 0,0, so scale = W/(2π) and translate to center.
const projection = geoEquirectangular()
  .scale(W / (2 * Math.PI))
  .translate([W / 2, H / 2]);

const path = geoPath(projection);
const d = path(land);
console.error(`d-length=${d.length}`);

const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="World map">
  <defs>
    <linearGradient id="land" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#1f6f4a"/>
      <stop offset="1" stop-color="#0e3a26"/>
    </linearGradient>
    <linearGradient id="ocean" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#0c1e35"/>
      <stop offset="1" stop-color="#0a1726"/>
    </linearGradient>
  </defs>
  <rect width="${W}" height="${H}" fill="url(#ocean)"/>
  <g stroke="#1c3958" stroke-width="0.5" opacity="0.35" fill="none">
    <line x1="0" y1="${H/4}" x2="${W}" y2="${H/4}"/>
    <line x1="0" y1="${H/2}" x2="${W}" y2="${H/2}"/>
    <line x1="0" y1="${H*3/4}" x2="${W}" y2="${H*3/4}"/>
    <line x1="${W/6}" y1="0" x2="${W/6}" y2="${H}"/>
    <line x1="${W/3}" y1="0" x2="${W/3}" y2="${H}"/>
    <line x1="${W/2}" y1="0" x2="${W/2}" y2="${H}"/>
    <line x1="${W*2/3}" y1="0" x2="${W*2/3}" y2="${H}"/>
    <line x1="${W*5/6}" y1="0" x2="${W*5/6}" y2="${H}"/>
  </g>
  <path d="${d}" fill="url(#land)" stroke="#2d8a5b" stroke-width="0.4" stroke-linejoin="round"/>
</svg>
`;
fs.writeFileSync("./src/trusted_router/static/world-map.svg", svg);
console.error(`wrote ${svg.length} bytes to src/trusted_router/static/world-map.svg`);
