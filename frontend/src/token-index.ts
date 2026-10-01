// /index: interactive NYTE charts.
//
// Progressive enhancement. The page ships fixed PNG charts, which stay as the no-JS fallback and
// the link-preview image. This script fetches the daily closes (static/token-index/series.json,
// written by the nytokenexchange renderer: one rounded value per series per UTC day, nothing
// else) and replaces each figure's images with an SVG chart: a hover/keyboard readout, date-range
// buttons and, for the grades, a log/linear switch and series toggles. Colours come from the
// theme variables in token-index.css, so the theme toggle restyles a drawn chart without a redraw.
(() => {
  type Key = 'ALL' | 'AAA' | 'A' | 'B' | 'C';
  type Point = number | null;
  interface SeriesFile {
    as_of: string;
    start: string;
    series: Record<Key, Point[]>;
  }
  interface Data {
    days: Date[];
    series: Record<Key, Point[]>;
  }
  interface Geometry {
    x: (i: number) => number;
    y: (v: number) => number;
    i0: number;
    i1: number;
    w: number;
    h: number;
    left: number;
    right: number;
    top: number;
    bottom: number;
  }

  const figures = Array.from(document.querySelectorAll<HTMLElement>('[data-ti-chart]'));
  const source = figures.length ? figures[0].dataset.series : undefined;
  if (!figures.length || !source) return;

  const NAMES: Record<Key, string> = { ALL: 'NYTE', AAA: 'Frontier', A: 'Advanced', B: 'Professional', C: 'Efficient' };
  const GRADES: Key[] = ['AAA', 'A', 'B', 'C'];
  const RANGES: Array<[string, number]> = [['1M', 31], ['3M', 92], ['6M', 183], ['All', 0]];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const DAY_MS = 86_400_000;

  const money = (v: number): string => '$' + (v >= 100 ? Math.round(v).toLocaleString('en-US') : v.toFixed(1));
  const tickMoney = (v: number): string => '$' + v.toLocaleString('en-US', { maximumFractionDigits: 1 });
  const longDate = (d: Date): string => `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}, ${d.getUTCFullYear()}`;
  const css = (k: Key): string => `ti-${k.toLowerCase()}`;

  const svgEl = <K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, string | number>, parent?: Element): SVGElementTagNameMap[K] => {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
    if (parent) parent.appendChild(node);
    return node;
  };
  const htmlEl = <K extends keyof HTMLElementTagNameMap>(tag: K, className: string, parent?: Element): HTMLElementTagNameMap[K] => {
    const node = document.createElement(tag);
    node.className = className;
    if (parent) parent.appendChild(node);
    return node;
  };

  // A step of 1, 2, 2.5 or 5 times a power of ten giving about `target` intervals over `span`.
  const niceStep = (span: number, target: number): number => {
    const raw = span / Math.max(1, target);
    const magnitude = 10 ** Math.floor(Math.log10(raw));
    const f = raw / magnitude;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * magnitude;
  };

  const parse = (file: SeriesFile): Data | null => {
    const start = Date.parse(`${file.start}T00:00:00Z`);
    const length = file.series && Array.isArray(file.series.ALL) ? file.series.ALL.length : 0;
    if (!Number.isFinite(start) || length < 2) return null;
    const keys: Key[] = ['ALL', ...GRADES];
    if (keys.some(k => !Array.isArray(file.series[k]) || file.series[k].length !== length)) return null;
    return { days: Array.from({ length }, (_, i) => new Date(start + i * DAY_MS)), series: file.series };
  };

  const mount = (figure: HTMLElement, data: Data): void => {
    const grades = figure.dataset.tiChart === 'grades';
    const keys: Key[] = grades ? [...GRADES, 'ALL'] : ['ALL'];
    const n = data.days.length;
    const hidden = new Set<Key>();
    let range = 0;            // days in view; 0 = the whole series
    let log = grades;         // grades span $10 to $3,000: log by default
    let hover = -1;
    let geom: Geometry | null = null;
    const label = figure.querySelector('img')?.getAttribute('alt') || '';

    // controls
    const toolbar = htmlEl('div', 'ti-toolbar');
    const group = (name: string): HTMLDivElement => {
      const g = htmlEl('div', 'ti-buttons', toolbar);
      g.setAttribute('role', 'group');
      g.setAttribute('aria-label', name);
      return g;
    };
    const button = (parent: Element, className: string, text: string, pressed: boolean, onClick: () => void): HTMLButtonElement => {
      const b = htmlEl('button', className, parent);
      b.type = 'button';
      b.textContent = text;
      b.setAttribute('aria-pressed', String(pressed));
      b.addEventListener('click', onClick);
      return b;
    };
    const rangeGroup = group('Date range');
    const rangeButtons = RANGES.map(([text, days]) => button(rangeGroup, 'ti-button', text, days === range, () => {
      range = days;
      rangeButtons.forEach((b, j) => b.setAttribute('aria-pressed', String(RANGES[j][1] === range)));
      draw();
    }));
    if (grades) {
      const scaleGroup = group('Scale');
      const scaleButtons = ['Log', 'Linear'].map(text => button(scaleGroup, 'ti-button', text, (text === 'Log') === log, () => {
        log = text === 'Log';
        scaleButtons.forEach(b => b.setAttribute('aria-pressed', String((b.textContent === 'Log') === log)));
        draw();
      }));
      const legend = htmlEl('div', 'ti-legend', toolbar);
      legend.setAttribute('role', 'group');
      legend.setAttribute('aria-label', 'Series');
      for (const k of keys) {
        const chip = button(legend, `ti-chip ${css(k)}`, NAMES[k], true, () => {
          if (!hidden.has(k) && keys.length - hidden.size <= 1) return;   // keep one series on
          if (hidden.has(k)) hidden.delete(k); else hidden.add(k);
          chip.setAttribute('aria-pressed', String(!hidden.has(k)));
          draw();
        });
      }
    }

    // plot
    const plot = htmlEl('div', 'ti-plot');
    const chart = svgEl('svg', { class: 'ti-svg', role: 'img', 'aria-label': label, tabindex: 0, focusable: 'true' }, plot);
    const tip = htmlEl('div', 'ti-tip', plot);
    tip.hidden = true;
    const live = htmlEl('p', 'ti-visually-hidden');
    live.setAttribute('aria-live', 'polite');
    figure.prepend(toolbar);
    figure.append(plot, live);
    figure.classList.add('is-live');
    let hoverLayer = svgEl('g', { class: 'ti-hover' }, chart);

    const draw = (): void => {
      const w = Math.max(280, Math.round(plot.clientWidth));
      const narrow = w < 640;
      const h = grades ? (narrow ? 320 : 440) : (narrow ? 240 : 340);
      const left = narrow ? 52 : 64;
      const right = grades ? (narrow ? 12 : 156) : (narrow ? 14 : 78);
      const top = 14, bottom = 42;
      const i1 = n - 1, i0 = range ? Math.max(0, n - range) : 0;
      const shown = keys.filter(k => !hidden.has(k));
      let lo = Infinity, hi = -Infinity;
      for (const k of shown) {
        for (let i = i0; i <= i1; i++) {
          const v = data.series[k][i];
          if (v !== null && v > 0) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
        }
      }
      if (!Number.isFinite(lo)) { lo = 1; hi = 10; }
      const plotH = h - top - bottom;
      let y: (v: number) => number;
      let ticks: number[];
      if (grades && log) {
        // the floor snaps down to a 1-3-10 step, so the lowest line always has a labelled gridline under it
        const steps = [1, 3, 10, 30, 100, 300, 1000, 3000, 10000, 30000];
        const floor = steps.filter(t => t <= lo / 1.15).pop() ?? 1;
        const a = Math.log10(floor), b = Math.log10(hi * 1.3);
        y = v => top + plotH * (1 - (Math.log10(Math.max(v, 1e-9)) - a) / (b - a));
        ticks = steps.filter(t => t >= floor && Math.log10(t) <= b);
      } else {
        const step = niceStep(hi * 1.08, narrow ? 4 : 5);
        const max = Math.ceil((hi * 1.08) / step) * step;
        y = v => top + plotH * (1 - v / max);
        ticks = [];
        for (let t = 0; t <= max + step / 2; t += step) ticks.push(Math.round(t * 1e6) / 1e6);
      }
      const x = (i: number): number => left + (i1 === i0 ? 0 : (i - i0) / (i1 - i0)) * (w - left - right);
      geom = { x, y, i0, i1, w, h, left, right, top, bottom };

      chart.setAttribute('viewBox', `0 0 ${w} ${h}`);
      chart.setAttribute('width', String(w));
      chart.setAttribute('height', String(h));
      chart.replaceChildren();

      const grid = svgEl('g', { class: 'ti-grid' }, chart);
      for (const t of ticks) {
        const ty = y(t);
        svgEl('line', { x1: left, x2: w - right, y1: ty, y2: ty }, grid);
        svgEl('text', { x: left - 8, y: ty, class: 'ti-ylabel' }, grid).textContent = tickMoney(t);
      }
      // x ticks: weekly in the one-month view, else month starts (thinned so labels never collide)
      const xticks: number[] = [];
      if (i1 - i0 <= 40) {
        for (let i = i1; i >= i0; i -= 7) xticks.unshift(i);
      } else {
        for (let i = i0; i <= i1; i++) if (data.days[i].getUTCDate() === 1) xticks.push(i);
        const room = Math.floor((w - left - right) / 46);
        const every = Math.max(1, Math.ceil(xticks.length / Math.max(1, room)));
        for (let j = xticks.length - 1; j >= 0; j--) if (j % every) xticks.splice(j, 1);
      }
      const axis = svgEl('g', { class: 'ti-axis' }, chart);
      svgEl('line', { x1: left, x2: w - right, y1: h - bottom, y2: h - bottom }, axis);
      xticks.forEach((i, j) => {
        const d = data.days[i];
        const text = svgEl('text', { x: x(i), y: h - bottom + 18, class: 'ti-xlabel' }, axis);
        text.textContent = i1 - i0 <= 40 ? `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}` : MONTHS[d.getUTCMonth()];
        if (i1 - i0 > 40 && (j === 0 || d.getUTCMonth() === 0)) {
          svgEl('tspan', { x: x(i), dy: 15 }, text).textContent = String(d.getUTCFullYear());
        }
      });

      // lines (a gap in a series breaks its line)
      const lines = svgEl('g', { class: 'ti-lines' }, chart);
      const pathOf = (k: Key): string => {
        let d = '', pen = false;
        for (let i = i0; i <= i1; i++) {
          const v = data.series[k][i];
          if (v === null || (log && grades && v <= 0)) { pen = false; continue; }
          d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)} ${y(v).toFixed(1)}`;
          pen = true;
        }
        return d;
      };
      if (!grades) {
        const line = pathOf('ALL');
        if (line) svgEl('path', { d: `${line}L${x(i1).toFixed(1)} ${y(0).toFixed(1)}L${x(i0).toFixed(1)} ${y(0).toFixed(1)}Z`, class: 'ti-area' }, lines);
      }
      for (const k of shown) {
        svgEl('path', { d: pathOf(k), class: `ti-line ${css(k)}${grades && k === 'ALL' ? ' ti-ref' : ''}` }, lines);
      }

      // end labels: the last value in view, nudged apart so they never overlap
      if (!narrow) {
        const ends = shown
          .map(k => ({ k, v: data.series[k][i1] }))
          .filter((e): e is { k: Key; v: number } => e.v !== null)
          .map(e => ({ ...e, ty: y(e.v) }))
          .sort((a, b) => b.ty - a.ty);
        for (let j = 1; j < ends.length; j++) if (ends[j - 1].ty - ends[j].ty < 17) ends[j].ty = ends[j - 1].ty - 17;
        const labels = svgEl('g', { class: 'ti-ends' }, chart);
        for (const e of ends) {
          if (!grades) svgEl('circle', { cx: x(i1), cy: y(e.v), r: 4, class: `ti-end-dot ${css(e.k)}` }, labels);
          const t = svgEl('text', { x: x(i1) + 10, y: e.ty, class: `ti-end ${css(e.k)}` }, labels);
          t.textContent = grades ? `${NAMES[e.k]} ${money(e.v)}` : money(e.v);
        }
      }

      hoverLayer = svgEl('g', { class: 'ti-hover' }, chart);
      if (hover >= 0) show(hover);
    };

    const show = (i: number): void => {
      if (!geom) return;
      hover = Math.max(geom.i0, Math.min(geom.i1, i));
      const g = geom;
      const hx = g.x(hover);
      hoverLayer.replaceChildren();
      svgEl('line', { x1: hx, x2: hx, y1: g.top, y2: g.h - g.bottom, class: 'ti-crosshair' }, hoverLayer);
      const rows: Array<[Key, number]> = [];
      for (const k of keys) {
        const v = data.series[k][hover];
        if (hidden.has(k) || v === null) continue;
        rows.push([k, v]);
        svgEl('circle', { cx: hx, cy: g.y(v), r: 4, class: `ti-dot ${css(k)}` }, hoverLayer);
      }
      rows.sort((a, b) => b[1] - a[1]);
      tip.replaceChildren();
      htmlEl('div', 'ti-tip-date', tip).textContent = longDate(data.days[hover]);
      for (const [k, v] of rows) {
        const row = htmlEl('div', `ti-tip-row ${css(k)}`, tip);
        htmlEl('span', 'ti-tip-name', row).textContent = grades ? NAMES[k] : 'NYTE Token Index';
        htmlEl('span', 'ti-tip-value', row).textContent = money(v);
      }
      tip.hidden = false;
      const scale = plot.clientWidth / g.w;
      const px = hx * scale;
      const width = tip.offsetWidth;
      tip.style.left = `${Math.max(0, px + 14 + width > plot.clientWidth ? px - 14 - width : px + 14)}px`;
      tip.style.top = `${g.top * scale}px`;
      live.textContent = `${longDate(data.days[hover])}: ${rows.map(([k, v]) => `${NAMES[k]} ${money(v)}`).join(', ')}`;
    };
    const hide = (): void => {
      hover = -1;
      hoverLayer.replaceChildren();
      tip.hidden = true;
    };

    const track = (event: PointerEvent): void => {
      if (!geom) return;
      const rect = chart.getBoundingClientRect();
      const px = ((event.clientX - rect.left) * geom.w) / Math.max(1, rect.width);
      const f = (px - geom.left) / Math.max(1, geom.w - geom.left - geom.right);
      show(Math.round(geom.i0 + f * (geom.i1 - geom.i0)));
    };
    chart.addEventListener('pointermove', track);
    chart.addEventListener('pointerdown', track);   // a tap shows the readout on touch screens
    chart.addEventListener('pointerleave', hide);
    chart.addEventListener('blur', hide);
    chart.addEventListener('keydown', event => {
      if (!geom) return;
      const step = event.shiftKey ? 7 : 1;
      const at = hover < 0 ? geom.i1 : hover;
      const moves: Record<string, number> = { ArrowLeft: at - step, ArrowRight: at + step, Home: geom.i0, End: geom.i1 };
      if (event.key in moves) { show(moves[event.key]); event.preventDefault(); }
      else if (event.key === 'Escape') hide();
    });
    new ResizeObserver(() => draw()).observe(plot);
    draw();
  };

  fetch(source, { credentials: 'same-origin' })
    .then(response => {
      if (!response.ok) throw new Error(`series ${response.status}`);
      return response.json() as Promise<SeriesFile>;
    })
    .then(file => {
      const data = parse(file);
      if (data) for (const figure of figures) mount(figure, data);
    })
    .catch(() => { /* the fixed images stay */ });
})();
