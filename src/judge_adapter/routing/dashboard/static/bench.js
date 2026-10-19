'use strict';

// The flow as a machine, drawn with the Anatomy kit (window.ISO).
//
// Claude Code is a bench plate with its nameplate standing at the back. A
// puck (the task) runs along engraved grooves through its stations: the
// question tray, the main agent, the Agent-call dispatcher and the
// PreToolUse diverter, then through one of three model bays (haiku,
// sonnet, opus: taller is stronger), back to the main agent and into the
// answer tray. Our parts are drawn in our colour: on the routed bench the
// diverter is the davi-route plugin, cabled to Davi, a gauge on its own
// base outside the plate; its needle reads the difficulty and the diverter
// turns to the bay it picks. On the baseline bench the diverter is Claude
// Code's own, pinned to opus. Every station carries a tag with its name,
// its time and, on the bays, Davi's probability for that model.

const BENCH = (() => {
  const K = window.ISO;
  const WIDTH = 760;
  const HEIGHT = 330;
  const PLATE = { x: -152, y: -64, w: 304, d: 138, r: 8 };
  const TOP_GROOVE = -6;
  const BOTTOM_GROOVE = 60;
  const BAYS = { haiku: 20, sonnet: 70, opus: 120 };
  const BAY_HEIGHT = { haiku: 12, sonnet: 18, opus: 26 };
  const STATION = {
    question: { x: -120, y: -31, top: 20 },
    main: { x: -60, y: -31, top: 17 },
    agent: { x: 0, y: -31, top: 10 },
    hook: { x: 60, y: -31, top: 9 },
    collect: { x: -40, y: 37, top: 17 },
    final: { x: -112, y: 37, top: 10 },
  };
  const DAVI = { x: 206, y: -26, top: 19 };
  const P = K.fitProjection(
    [
      ...K.boxCorners(PLATE, -16, 0),
      ...K.boxCorners({ x: 168, y: -58, w: 76, d: 62, r: 5 }, -10, 0),
      // room for the tags above the back row and below the plate
      [-130, -40, 60], [70, -40, 60], [DAVI.x, DAVI.y, 60],
      [-130, 74, -60], [130, 74, -60],
    ],
    WIDTH,
    HEIGHT,
    { pad: 8, azimuth: 80, elevation: 40 },
  );
  return {
    K, P, WIDTH, HEIGHT, PLATE, TOP_GROOVE, BOTTOM_GROOVE, BAYS, BAY_HEIGHT, STATION, DAVI,
  };
})();

const BAY_NAMES = { haiku: 'Haiku', sonnet: 'Sonnet', opus: 'Opus' };
const CUTOFFS = { low: 0.48, high: 0.78 };

function benchSvg(tag, attrs = {}, text) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text != null) node.textContent = text;
  return node;
}

// the same rule the plugin uses: Davi's average difficulty (0-1) against
// the two cut-offs
function cutoffs(data) {
  const low = Number.isFinite(data?.tier_low) ? data.tier_low : CUTOFFS.low;
  const high = Number.isFinite(data?.tier_high) ? data.tier_high : CUTOFFS.high;
  return { low, high };
}

function bayRange(model, { low, high }) {
  if (model === 'haiku') return `${low.toFixed(2)} 미만`;
  if (model === 'opus') return `${high.toFixed(2)} 이상`;
  return `${low.toFixed(2)}~${high.toFixed(2)}`;
}

function ruleText(level, model, { low, high }) {
  const value = level.toFixed(2);
  const name = BAY_NAMES[model] || model;
  if (level >= high) return `평균 ${value} ≥ ${high.toFixed(2)} → ${name}`;
  if (level < low) return `평균 ${value} < ${low.toFixed(2)} → ${name}`;
  return `${low.toFixed(2)} ≤ 평균 ${value} < ${high.toFixed(2)} → ${name}`;
}

function levelList(levels) {
  if (!levels || typeof levels !== 'object') return null;
  const list = [1, 2, 3, 4, 5].map((index) => Number(levels[String(index)]));
  return list.every(Number.isFinite) ? list : null;
}

class BenchArt {
  // static markup of one bench, as SVG strings
  constructor(routed) {
    this.routed = routed;
    const { K, P } = BENCH;
    this.at = (point) => K.iso(point, P);
    this.S = (paths, style) => K.solidSvg(paths, style);
    this.L = (d, style) => K.lineSvg(d, style);
    this.D = (points, style) => K.dotsSvg(points, style);
  }

  engrave(x, y, z, text, size = 3.4) {
    const { K, P } = BENCH;
    return K.faceTextSvg(K.topMatrix([x, y], z, P, 'x'), text, { size, tone: 'lo' });
  }

  keyed(svg, key) {
    return svg.replace('<g class="iso-solid', `<g data-key="${key}" class="iso-solid`);
  }

  // text in the plane of a vertical face that looks toward +y
  faceMatrix(x, y, z) {
    const origin = this.at([x, y, z]);
    const ux = this.at([x + 1, y, z]);
    const uz = this.at([x, y, z + 1]);
    const f = (value) => Math.round(value * 1000) / 1000;
    return `matrix(${f(ux[0] - origin[0])} ${f(ux[1] - origin[1])} ${f(origin[0] - uz[0])} ${f(origin[1] - uz[1])} ${f(origin[0])} ${f(origin[1])})`;
  }

  plate() {
    const { K, P, PLATE } = BENCH;
    const out = [`<path class="iso-halo" d="${K.haloOf(PLATE, -16, 10, P)}"/>`];
    const feet = K.corners(PLATE, 22).sort((a, b) => K.depthOf([...a, 0], P) - K.depthOf([...b, 0], P));
    for (const [x, y] of feet) {
      out.push(this.S(K.cylinder(x, y, 6.4, -16, 1.6, P, 24), { tone: 'lo' }));
      out.push(this.S(K.cylinder(x, y, 5, -14.4, 4.4, P, 24), { tone: 'mid' }));
    }
    out.push(this.S(K.slabOf(PLATE, -10, 10, P, 8, 1.4), { tone: 'mid' }));
    out.push(this.L(K.planOutline(K.insetPlan(PLATE, 5), 0, P), { tone: 'lo' }));
    const screws = K.corners(PLATE, 10);
    out.push(this.L(screws.map(([x, y]) => K.ring(x, y, 2, 0, P, 14)).join(''), { tone: 'lo' }));
    out.push(this.D(screws.map(([x, y]) => this.at([x, y, 0])), { size: 0.5 }));
    // the nameplate along the front edge
    const strip = { x: -146, y: 64, w: 292, d: 8, r: 1.2 };
    out.push(this.S(K.slabOf(strip, 0, 0.8, P, 3), { tone: 'lo', crease: 'none', className: 'sign' }));
    out.push(K.faceTextSvg(K.topMatrix([strip.x + 5, strip.y + 6.2], 0.8, P, 'x'), 'CLAUDE CODE  ·  AGENT HARNESS', { size: 5.6, tone: 'hi' }));
    out.push(this.D([this.at([strip.x + 2, strip.y + 4, 0.8]), this.at([strip.x + strip.w - 2, strip.y + 4, 0.8])], { size: 0.5 }));
    return out.join('');
  }

  grooves() {
    const { K, P, TOP_GROOVE: T, BOTTOM_GROOVE: B, BAYS } = BENCH;
    const out = [];
    const groove = (points, key) => {
      const d = K.onTop(points, 0, P);
      out.push(`<path data-groove="${key}" class="iso-line" data-tone="faint" d="${d}" style="stroke-width:2.4;stroke-linecap:round"/>`);
      out.push(`<path data-groove-lit="${key}" class="iso-line bench-flow" data-tone="lit" d="${d}"/>`);
    };
    groove([[-120, T], [-60, T]], 'qm');
    groove([[-60, T], [0, T]], 'ma');
    groove([[0, T], [60, T]], 'ah');
    for (const [model, x] of Object.entries(BAYS)) {
      groove([[60, T], [x, T], [x, 20]], `h-${model}`);
      groove([[x, 50], [x, B], [-40, B]], `${model}-c`);
    }
    groove([[-40, B], [-112, B]], 'cf');
    return out.join('');
  }

  stationQuestion() {
    const { K, P, STATION } = BENCH;
    const { x, y } = STATION.question;
    const tray = { x: x - 14, y: y - 13, w: 28, d: 26, r: 2.4 };
    return [
      this.keyed(this.S(K.slabOf(tray, 0, 7, P, 3, 0.6), { tone: 'mid' }), 'body'),
      this.L(K.planOutline(K.insetPlan(tray, 3), 7, P, 3), { tone: 'faint' }),
      this.S(K.slabOf({ x: x - 8, y: y - 2, w: 16, d: 2.2, r: 0.4 }, 7, 13, P, 2), { tone: 'hi', crease: 'none' }),
      this.D([this.at([tray.x + 3, tray.y + 3, 7]), this.at([tray.x + tray.w - 3, tray.y + 3, 7])], { size: 0.45 }),
      this.engrave(tray.x + 4, tray.y + tray.d - 3, 7, 'PROMPT'),
    ].join('');
  }

  stationMain(key) {
    const { K, P, STATION } = BENCH;
    const { x, y } = STATION[key];
    const body = { x: x - 20, y: y - 16, w: 40, d: 32, r: 3 };
    const top = 14;
    const out = [
      this.keyed(this.S(K.slabOf(body, 0, top, P, 4, 0.8), { tone: 'mid' }), 'body'),
      this.L(K.planOutline(K.insetPlan(body, 2.6), top, P, 4), { tone: 'faint' }),
      this.S(K.cylinder(x + 3, y - 2, 9, top, 3, P, 40, 0.5), { tone: 'hi', crease: 'lo' }),
      this.L(K.ring(x + 3, y - 2, 7.6, top + 3, P, 40), { tone: 'lo' }),
    ];
    const ticks = K.radialTicks(x + 3, y - 2, 7.4, 24, 6, top + 3, [0.9, 1.8], P);
    out.push(this.L(ticks.minor, { tone: 'lo' }), this.L(ticks.major, { tone: 'mid' }));
    out.push(this.L(K.ring(x + 3, y - 2, 0.9, top + 3, P, 10), { tone: 'mid' }));
    const knob = [x - 12, y + 9];
    out.push(this.S(K.cylinder(knob[0], knob[1], 3.2, top, 3, P, 20), { tone: 'mid' }));
    out.push(this.L(K.knurl(knob[0], knob[1], 3.2, top, top + 3, 18, P), { tone: 'lo' }));
    out.push(this.D([this.at([body.x + 3, body.y + 3, top]), this.at([body.x + body.w - 3, body.y + 3, top])], { size: 0.45 }));
    out.push(this.engrave(body.x + 22, body.y + body.d - 2.6, top, 'MAIN'));
    return out.join('');
  }

  stationAgent() {
    const { K, P, STATION } = BENCH;
    const { x, y } = STATION.agent;
    const body = { x: x - 18, y: y - 13, w: 36, d: 26, r: 2.4 };
    const top = 10;
    const out = [this.keyed(this.S(K.slabOf(body, 0, top, P, 3, 0.6), { tone: 'mid' }), 'body')];
    for (const dx of [-9, 0, 9]) {
      out.push(this.L(K.planOutline({ x: x + dx - 3, y: y - 9, w: 6, d: 13, r: 1 }, top, P, 2), { tone: 'lo' }));
    }
    out.push(this.D([this.at([body.x + 3, body.y + 3, top]), this.at([body.x + body.w - 3, body.y + 3, top])], { size: 0.45 }));
    out.push(this.engrave(body.x + 4, body.y + body.d - 3, top, 'AGENT'));
    return out.join('');
  }

  stationHook() {
    const { K, P, STATION } = BENCH;
    const { x, y } = STATION.hook;
    const ours = this.routed ? 'ours' : undefined;
    const out = [
      this.keyed(this.S(K.cylinder(x, y, 13, 0, 6, P, 48, 0.6), { tone: 'mid', className: ours }), 'body'),
      this.S(K.cylinder(x, y, 10, 6, 3, P, 44, 0.5), { tone: 'hi', crease: 'lo', className: ours }),
      this.L(K.radialTicks(x, y, 9.4, 12, 1, 9, [1.2, 1.2], P).major, { tone: 'lo' }),
    ];
    if (this.routed) {
      const port = { x: x + 11, y: y - 3, w: 6, d: 6, r: 1 };
      out.push(this.S(K.slabOf(port, 0, 5, P, 2, 0.3), { tone: 'mid', className: 'ours' }));
    } else {
      out.push(this.S(K.cylinder(x - 6, y + 5, 1.4, 9, 4, P, 12), { tone: 'hi' }));
    }
    const plate = { x: x - 11, y: y + 14, w: 22, d: 7, r: 1 };
    out.push(this.S(K.slabOf(plate, 0, 0.8, P, 2), { tone: 'lo', crease: 'none', className: ours }));
    out.push(this.engrave(plate.x + 2.4, plate.y + 5, 0.8, 'HOOK'));
    return out.join('');
  }

  stationBay(model) {
    const { K, P, BAYS, BAY_HEIGHT } = BENCH;
    const x = BAYS[model];
    const h = BAY_HEIGHT[model];
    const body = { x: x - 17, y: 20, w: 34, d: 30, r: 3 };
    const out = [
      this.keyed(this.S(K.slabOf(body, 0, h, P, 4, 0.7), { tone: 'mid', className: `bay bay-${model}` }), 'body'),
      this.L(K.planOutline(K.insetPlan(body, 2.6), h, P, 4), { tone: 'faint' }),
      this.keyed(this.S(K.cylinder(x + 10, 27, 2.6, h, 2, P, 16), { tone: 'mid', crease: 'none', className: `lamp lamp-${model}` }), 'lamp'),
      K.faceTextSvg(K.topMatrix([x - 12, 45], h, P, 'x'), model.toUpperCase(), { size: 4.2, tone: 'mid' }),
    ];
    const vents = [0, 1, 2].map((index) => K.planOutline({ x: x - 12 + index * 6, y: 25, w: 3, d: 12, r: 1.2 }, h, P, 2)).join('');
    out.push(this.L(vents, { tone: 'lo' }));
    out.push(this.D([this.at([body.x + 3, body.y + 3, h]), this.at([body.x + body.w - 3, body.y + 3, h])], { size: 0.45 }));
    return out.join('');
  }

  stationFinal() {
    const { K, P, STATION } = BENCH;
    const { x, y } = STATION.final;
    const tray = { x: x - 16, y: y - 13, w: 32, d: 26, r: 2.4 };
    return [
      this.keyed(this.S(K.slabOf(tray, 0, 6, P, 3, 0.6), { tone: 'mid' }), 'body'),
      this.L(K.planOutline(K.insetPlan(tray, 3), 6, P, 3), { tone: 'faint' }),
      `<g data-part="stamp" opacity="0">${this.S(K.slabOf({ x: x - 7, y: y - 6, w: 14, d: 12, r: 1 }, 6, 4, P, 2, 0.4), { tone: 'hi', lit: true })}</g>`,
      this.D([this.at([tray.x + 3, tray.y + 3, 6]), this.at([tray.x + tray.w - 3, tray.y + 3, 6])], { size: 0.45 }),
      this.engrave(tray.x + 4, tray.y + tray.d - 3, 6, 'ANSWER'),
    ].join('');
  }

  davi() {
    const { K, P, DAVI } = BENCH;
    const { x, y } = DAVI;
    const base = { x: x - 38, y: y - 32, w: 76, d: 62, r: 5 };
    const box = { x: x - 26, y: y - 21, w: 52, d: 40, r: 3.4 };
    const out = [`<path class="iso-halo" d="${K.haloOf(base, -10, 6, P)}"/>`];
    for (const [fx, fy] of K.corners(base, 10)) out.push(this.S(K.cylinder(fx, fy, 4, -10, 4, P, 18), { tone: 'lo' }));
    out.push(this.S(K.slabOf(base, -6, 6, P, 6, 1), { tone: 'mid', className: 'ours' }));
    out.push(this.L(K.planOutline(K.insetPlan(base, 3.6), 0, P, 5), { tone: 'lo' }));
    out.push(this.keyed(this.S(K.slabOf(box, 0, 16, P, 4, 0.8), { tone: 'hi', crease: 'lo', className: 'ours' }), 'body'));
    out.push(this.S(K.cylinder(x, y - 2, 15, 16, 2.4, P, 56, 0.5), { tone: 'mid', crease: 'lo', className: 'ours' }));
    const dial = K.radialTicks(x, y - 2, 13.6, 20, 5, 18.4, [1.2, 2.6], P, -Math.PI / 2, Math.PI);
    out.push(this.L(dial.minor, { tone: 'lo' }), this.L(dial.major, { tone: 'mid' }));
    out.push(this.L(K.ring(x, y - 2, 1.4, 18.4, P, 12), { tone: 'mid' }));
    out.push(K.faceTextSvg(K.topMatrix([box.x + 4, box.y + box.d - 4], 16, P, 'x'), 'DAVI', { size: 4.4, tone: 'hi' }));
    out.push(K.faceTextSvg(K.topMatrix([base.x + 4, base.y + base.d - 3], 0, P, 'x'), 'OUTSIDE CLAUDE CODE', { size: 3.2, tone: 'mid' }));
    out.push(this.D([this.at([base.x + 4, base.y + 4, 0]), this.at([base.x + base.w - 4, base.y + 4, 0])], { size: 0.45 }));
    return out.join('');
  }

  cable() {
    const { STATION, DAVI, PLATE } = BENCH;
    const from = this.at([STATION.hook.x + 17, STATION.hook.y, 3]);
    const edge = PLATE.x + PLATE.w;
    const via = [
      this.at([edge - 2, STATION.hook.y, 1]),
      this.at([edge + 4, STATION.hook.y, -3]),
      this.at([DAVI.x - 34, DAVI.y - 4, -1]),
    ];
    const to = this.at([DAVI.x - 26, DAVI.y - 4, 7]);
    const d = `M${[from, ...via, to].map(([sx, sy]) => `${sx.toFixed(1)} ${sy.toFixed(1)}`).join('L')}`;
    return `<g class="iso-wire ours-wire"><path class="iso-line" d="${d}" style="stroke-width:1.6"/>` +
      `<path data-part="cable-lit" class="iso-line bench-flow ours-flow" d="${d}"/>` +
      `<circle class="iso-wire-end" cx="${from[0].toFixed(1)}" cy="${from[1].toFixed(1)}" r="1.5"/>` +
      `<circle class="iso-wire-end" cx="${to[0].toFixed(1)}" cy="${to[1].toFixed(1)}" r="1.5"/></g>`;
  }
}

class BenchPanel {
  constructor(container, arm, view) {
    this.arm = arm;
    this.view = view;
    this.routed = arm.key === 'routed';
    this.root = document.createElement('div');
    this.root.className = `flow-panel bench-panel flow-${arm.key}`;
    const head = document.createElement('div');
    head.className = 'flow-head';
    this.heading = document.createElement('h3');
    this.setHeading(window.DAVI_MAIN_MODEL);
    this.meta = document.createElement('span');
    this.meta.className = 'flow-meta';
    head.append(this.heading, this.meta);
    this.stage = document.createElement('div');
    this.stage.className = 'bench iso';
    this.stage.setAttribute('data-theme', document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
    this.root.append(head, this.stage);
    container.append(this.root);
    this.build();
  }

  setHeading(mainModel) {
    const main = BAY_NAMES[flowModel(mainModel)] || mainModel;
    const lead = main ? `메인 ${main} · ` : '';
    this.heading.textContent = this.routed
      ? `Davi · ${lead}서브에이전트는 작업마다 선택`
      : `기준 · ${lead}서브에이전트 언제나 Opus`;
  }

  build() {
    const { K, P, WIDTH, HEIGHT, BAYS, DAVI } = BENCH;
    const art = new BenchArt(this.routed);
    const bays = Object.keys(BAYS).map((model) => `<g data-station="${model}">${art.stationBay(model)}</g>`).join('');
    const body = [
      art.plate(),
      art.grooves(),
      `<g data-station="question">${art.stationQuestion()}</g>`,
      `<g data-station="main">${art.stationMain('main')}</g>`,
      `<g data-station="agent">${art.stationAgent()}</g><g data-part="calls"></g>`,
      `<g data-station="hook">${art.stationHook()}</g>`,
      `<path data-part="pointer" class="iso-line${this.routed ? ' ours-needle' : ''}" data-tone="lit" d="" style="stroke-width:1.4"/>`,
      '<g data-part="pucks-back"></g>',
      bays,
      `<g data-station="collect">${art.stationMain('collect')}</g>`,
      `<g data-station="final">${art.stationFinal()}</g>`,
      '<path data-part="main-needle" class="iso-line" data-tone="lit" d="" style="stroke-width:0.9"/>',
      '<path data-part="collect-needle" class="iso-line" data-tone="lit" d="" style="stroke-width:0.9"/>',
      '<g data-part="pucks-front"></g>',
    ];
    if (this.routed) {
      body.push(art.cable(), `<g data-station="davi">${art.davi()}</g>`,
        '<path data-part="davi-needle" class="iso-line ours-needle" d="" style="stroke-width:1.4"/>');
      const marks = [0.48, 0.78].map((level) => {
        const a = -Math.PI / 2 + level * Math.PI;
        return K.lineOnTop([DAVI.x + Math.sin(a) * 9.6, DAVI.y - 2 - Math.cos(a) * 9.6], [DAVI.x + Math.sin(a) * 13.6, DAVI.y - 2 - Math.cos(a) * 13.6], 18.4, P);
      }).join('');
      body.push(`<path class="iso-line" data-tone="hi" d="${marks}" style="stroke-width:1"/>`);
    }
    body.push('<g data-part="tags"></g>');
    const label = this.routed
      ? 'Claude Code 작업대와 바깥의 Davi 계기. 과제 퍽이 질문 → 메인 에이전트 → Agent 호출 → PreToolUse 분기기(davi-route 플러그인)를 지나고, 분기기가 Davi 판정에 따라 haiku, sonnet, opus 칸 중 하나로 보냅니다.'
      : 'Claude Code 작업대. 과제 퍽이 질문 → 메인 에이전트 → Agent 호출 → PreToolUse 분기기를 지나고, 분기기는 opus 칸에 고정되어 있습니다.';
    this.stage.innerHTML = K.figureSvg({ width: WIDTH, height: HEIGHT, label, body });
    this.svg = this.stage.querySelector('svg');
    this.svg.classList.add('flow-svg');
    const q = (selector) => this.svg.querySelector(selector);
    this.parts = {
      pointer: q('[data-part="pointer"]'),
      callsLayer: q('[data-part="calls"]'),
      back: q('[data-part="pucks-back"]'),
      front: q('[data-part="pucks-front"]'),
      mainNeedle: q('[data-part="main-needle"]'),
      collectNeedle: q('[data-part="collect-needle"]'),
      daviNeedle: q('[data-part="davi-needle"]'),
      stamp: q('[data-part="stamp"]'),
      cable: q('[data-part="cable-lit"]'),
      tags: q('[data-part="tags"]'),
    };
    this.calls = [];
    this.chain = Promise.resolve();
    this.generation = (this.generation || 0) + 1;
    this.started = null;
    this.startedAt = null;
    this.postAt = null;
    this.done = false;
    this.spin = { main: false, collect: false };
    this.needles = { main: 0, collect: 0, davi: -0.02 };
    this.daviTarget = null;
    this.pointerAngle = this.angleTo(this.routed ? 'sonnet' : 'opus');
    this.pointerTarget = this.pointerAngle;
    this.clocks = {};
    this.meta.textContent = '';
    this.buildTags();
    this.drawNeedles();
    this.drawPointer();
    this.loop();
  }

  idle() {
    this.svg.classList.add('bench-idle');
  }

  reset(task) {
    this.build();
    this.light('question', true);
    this.setTag('question', (task.title || '').slice(0, 8) || '입력', 'active');
  }

  // --- tags: name, time and range at each station --------------------------
  buildTags() {
    const { STATION, BAYS, DAVI } = BENCH;
    this.tags = {};
    const add = (key, point, side, title, value, lift) => {
      const anchor = BENCH.K.iso(point, BENCH.P);
      const group = benchSvg('g', { class: `bench-tag bench-tag-${key}`, 'data-state': 'idle' });
      const line = benchSvg('path', { class: 'bench-tag-line' });
      const box = benchSvg('rect', { class: 'bench-tag-box', rx: 5 });
      const name = benchSvg('text', { class: 'bench-tag-title' }, title);
      const text = benchSvg('text', { class: 'bench-tag-value' }, value);
      group.append(line, box, name, text);
      this.parts.tags.append(group);
      this.tags[key] = { group, line, box, name, text, anchor, side, lift };
    };
    const station = (key) => [STATION[key].x, STATION[key].y, STATION[key].top];
    const below = (x) => [x, 74, -10];
    add('question', station('question'), 'up', '질문', '대기', 8);
    add('main', station('main'), 'up', '메인 에이전트 · 계획', '-', 52);
    add('agent', station('agent'), 'up', 'Agent 호출', '-', 8);
    add('hook', station('hook'), 'up', this.routed ? 'PreToolUse 훅 · 우리 플러그인' : 'PreToolUse 훅 · opus 고정', '-', 58);
    for (const [model, x] of Object.entries(BAYS)) {
      const used = this.routed || model === 'opus';
      const value = this.routed ? `난이도 ${bayRange(model, CUTOFFS)}` : used ? '-' : '사용 안 함';
      add(model, below(x), 'down', `${BAY_NAMES[model]} 서브에이전트`, value, model === 'sonnet' ? 54 : 8);
    }
    add('collect', below(STATION.collect.x), 'down', '메인 에이전트 · 취합', '-', 56);
    add('final', below(STATION.final.x), 'down', '최종 답변', '-', 10);
    if (this.routed) {
      add('davi', [DAVI.x, DAVI.y, DAVI.top], 'up', 'Davi 판정 모델 · 우리 것', '대기', 22);
      this.addLevelBars(this.tags.davi);
    }
    const relayout = () => Object.keys(this.tags).forEach((key) => this.layoutTag(key));
    requestAnimationFrame(relayout);
    document.fonts?.ready.then(relayout);
  }

  // five small columns in the Davi tag: P(level 1) ... P(level 5)
  addLevelBars(tag) {
    tag.bars = [1, 2, 3, 4, 5].map((index) => {
      const bar = benchSvg('rect', { class: 'bench-level-bar', rx: 1.5 });
      const label = benchSvg('text', { class: 'bench-level-label' }, `${index}단계`);
      const share = benchSvg('text', { class: 'bench-level-share' });
      label.style.display = 'none';
      tag.group.append(bar, label, share);
      return { bar, label, share };
    });
    tag.levels = null;
  }

  setLevels(levels) {
    const tag = this.tags.davi;
    if (!tag?.bars) return;
    tag.levels = levelList(levels);
    this.layoutTag('davi');
  }

  layoutTag(key) {
    const tag = this.tags[key];
    if (!tag || !tag.group.isConnected) return;
    const [ax, ay] = tag.anchor;
    const lift = tag.lift;
    const pad = 7;
    let width = 80;
    try {
      width = Math.max(tag.name.getComputedTextLength(), tag.text.getComputedTextLength()) + pad * 2;
    } catch (error) {
      width = 80;
    }
    const bars = tag.bars && tag.levels ? tag.bars : null;
    const column = 40;
    const gap = 4;
    if (bars) width = Math.max(width, bars.length * column + (bars.length - 1) * gap + pad * 2);
    const height = bars ? 94 : 36;
    const top = tag.side === 'up' ? ay - lift - height : ay + lift;
    const left = Math.min(Math.max(ax - width / 2, 4), BENCH.WIDTH - width - 4);
    tag.box.setAttribute('x', left.toFixed(1));
    tag.box.setAttribute('y', top.toFixed(1));
    tag.box.setAttribute('width', width.toFixed(1));
    tag.box.setAttribute('height', height);
    tag.name.setAttribute('x', (left + pad).toFixed(1));
    tag.name.setAttribute('y', (top + 14).toFixed(1));
    tag.text.setAttribute('x', (left + pad).toFixed(1));
    tag.text.setAttribute('y', (top + 30).toFixed(1));
    for (const [index, { bar, label, share: percent }] of (tag.bars || []).entries()) {
      const share = bars ? tag.levels[index] : 0;
      const tall = bars ? Math.max(1, share * 24) : 0;
      const x = left + pad + index * (column + gap);
      bar.setAttribute('x', x.toFixed(1));
      bar.setAttribute('y', (top + 64 - tall).toFixed(1));
      bar.setAttribute('width', column);
      bar.setAttribute('height', tall.toFixed(1));
      label.setAttribute('x', (x + column / 2).toFixed(1));
      label.setAttribute('y', (top + 76).toFixed(1));
      label.style.display = bars ? '' : 'none';
      percent.setAttribute('x', (x + column / 2).toFixed(1));
      percent.setAttribute('y', (top + 88).toFixed(1));
      percent.textContent = bars ? `${Math.round(share * 100)}%` : '';
    }
    const edge = tag.side === 'up' ? top + height : top;
    tag.line.setAttribute('d', `M${ax.toFixed(1)} ${ay.toFixed(1)}L${ax.toFixed(1)} ${edge.toFixed(1)}`);
  }

  setTag(key, value, state, title) {
    const tag = this.tags[key];
    if (!tag) return;
    if (title != null) tag.name.textContent = title;
    if (value != null && tag.text.textContent !== value) tag.text.textContent = value;
    if (state) tag.group.dataset.state = state;
    this.layoutTag(key);
  }

  startClock(key, from = performance.now()) {
    this.clocks[key] = from;
  }

  stopClock(key) {
    delete this.clocks[key];
  }

  // --- small drawing helpers ------------------------------------------------
  station(key) {
    return this.svg.querySelector(`[data-station="${key}"]`);
  }

  light(key, on) {
    const node = this.station(key);
    if (!node) return;
    for (const solid of node.querySelectorAll('[data-key="body"]')) {
      if (on) solid.setAttribute('data-lit', '');
      else solid.removeAttribute('data-lit');
    }
  }

  lamp(model, on) {
    const solid = this.station(model)?.querySelector('[data-key="lamp"]');
    if (!solid) return;
    if (on) solid.setAttribute('data-lit', '');
    else solid.removeAttribute('data-lit');
  }

  groove(key, on) {
    const path = this.svg.querySelector(`[data-groove-lit="${key}"]`);
    if (path) path.classList.toggle('on', on);
  }

  angleTo(model) {
    const { BAYS, STATION } = BENCH;
    return Math.atan2(BAYS[model] - STATION.hook.x, -(35 - STATION.hook.y));
  }

  drawPointer() {
    const { K, P, STATION } = BENCH;
    const { x, y } = STATION.hook;
    const a = this.pointerAngle;
    this.parts.pointer.setAttribute('d', K.lineOnTop([x - Math.sin(a) * 2.4, y + Math.cos(a) * 2.4], [x + Math.sin(a) * 9, y - Math.cos(a) * 9], 9.2, P));
  }

  drawNeedles() {
    const { K, P, STATION, DAVI } = BENCH;
    const needle = (cx, cy, z, angle, length) =>
      K.lineOnTop([cx - Math.sin(angle) * 1.6, cy + Math.cos(angle) * 1.6], [cx + Math.sin(angle) * length, cy - Math.cos(angle) * length], z, P);
    const m = STATION.main;
    const c = STATION.collect;
    this.parts.mainNeedle.setAttribute('d', needle(m.x + 3, m.y - 2, 17.2, this.needles.main, 6.4));
    this.parts.collectNeedle.setAttribute('d', needle(c.x + 3, c.y - 2, 17.2, this.needles.collect, 6.4));
    if (this.parts.daviNeedle) {
      const a = -Math.PI / 2 + Math.max(0, Math.min(1, this.needles.davi)) * Math.PI;
      this.parts.daviNeedle.setAttribute('d', needle(DAVI.x, DAVI.y - 2, 18.6, a, 12));
    }
  }

  loop() {
    const generation = this.generation;
    let last = performance.now();
    const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const step = (now) => {
      if (generation !== this.generation) return;
      const dt = Math.min((now - last) / 1000, 1 / 30);
      last = now;
      if (this.spin.main) this.needles.main += dt * 4;
      if (this.spin.collect) this.needles.collect += dt * 4;
      if (this.daviTarget != null) {
        this.needles.davi += (this.daviTarget - this.needles.davi) * (still ? 1 : 1 - Math.exp(-dt / 0.18));
      }
      this.pointerAngle += (this.pointerTarget - this.pointerAngle) * (still ? 1 : 1 - Math.exp(-dt / 0.16));
      for (const [key, from] of Object.entries(this.clocks)) {
        const seconds = ((now - from) / 1000) * (this.view.speed || 1);
        this.setTag(key, `${secondsText(seconds)} · 진행 중`, 'active');
      }
      this.drawNeedles();
      this.drawPointer();
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  queue(step) {
    const generation = this.generation;
    this.chain = this.chain.then(() => (generation === this.generation ? step() : null)).catch(() => null);
  }

  duration(ms) {
    return ms / Math.min(Math.max(this.view.speed, 1), 4);
  }

  sessionSeconds(ts) {
    if (typeof ts === 'number' && typeof this.startedAt === 'number') return ts - this.startedAt;
    return null;
  }

  wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, this.duration(ms)));
  }

  puck(layer) {
    const { K, P } = BENCH;
    const group = benchSvg('g', { class: 'bench-puck' });
    group.innerHTML = K.solidSvg(K.cylinder(0, 0, 4.6, 0, 3, P, 24, 0.4), { tone: 'hi', lit: true });
    this.parts[layer].append(group);
    return group;
  }

  async roll(puck, points, ms, grooves = []) {
    const { K, P } = BENCH;
    for (const key of grooves) this.groove(key, true);
    const lengths = [0];
    for (let i = 1; i < points.length; i += 1) {
      lengths.push(lengths[i - 1] + Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]));
    }
    const total = lengths[lengths.length - 1] || 1;
    const time = this.duration(ms);
    const start = performance.now();
    await new Promise((resolve) => {
      const frame = (now) => {
        const share = Math.min(1, (now - start) / time);
        const eased = share < 0.5 ? 2 * share * share : 1 - (-2 * share + 2) ** 2 / 2;
        const at = eased * total;
        let i = 1;
        while (i < lengths.length - 1 && lengths[i] < at) i += 1;
        const local = (at - lengths[i - 1]) / ((lengths[i] - lengths[i - 1]) || 1);
        const x = points[i - 1][0] + (points[i][0] - points[i - 1][0]) * local;
        const y = points[i - 1][1] + (points[i][1] - points[i - 1][1]) * local;
        puck.setAttribute('transform', K.translateAlong(P, [x, y, 0]));
        if (share < 1) requestAnimationFrame(frame);
        else resolve();
      };
      requestAnimationFrame(frame);
    });
    for (const key of grooves) this.groove(key, false);
  }

  async pulse(ms) {
    const cable = this.parts.cable;
    if (!cable) return;
    cable.classList.add('on');
    await this.wait(ms);
    cable.classList.remove('on');
  }

  // --- events ---------------------------------------------------------------
  sessionStarted(data, now) {
    const { TOP_GROOVE: T, STATION } = BENCH;
    this.started = now;
    this.startedAt = typeof data.started_at === 'number' ? data.started_at : null;
    if (data.main_model) this.setHeading(data.main_model);
    this.queue(async () => {
      this.light('question', true);
      const puck = this.puck('back');
      this.current = puck;
      await this.roll(puck, [[STATION.question.x, T], [STATION.main.x, T]], 700, ['qm']);
      this.light('question', false);
      this.setTag('question', '전달됨', 'done');
      this.light('main', true);
      this.spin.main = true;
      this.startClock('main', this.started);
    });
  }

  routing(data) {
    const { TOP_GROOVE: T, STATION, BAYS } = BENCH;
    const call = { index: this.calls.length + 1 };
    this.calls.push(call);
    const model = flowModel(data.model_set || data.model_requested) || 'opus';
    call.model = model;
    const level = typeof data.level === 'number' ? data.level : null;
    const hookStart = this.sessionSeconds(data.ts);
    const range = cutoffs(data);
    this.queue(async () => {
      const puck = this.current || this.puck('back');
      this.current = null;
      call.puck = puck;
      this.spin.main = false;
      this.light('main', false);
      if (call.index === 1) {
        const plan = hookStart != null ? hookStart : ((performance.now() - this.started) / 1000) * (this.view.speed || 1);
        this.stopClock('main');
        this.setTag('main', secondsText(Math.max(0, plan)), 'done');
      }
      this.light('agent', true);
      this.setTag('agent', `${call.index}번째 호출`, 'active');
      this.addCallDot(call.index);
      await this.roll(puck, [[STATION.main.x, T], [STATION.agent.x, T]], 500, ['ma']);
      await this.roll(puck, [[STATION.agent.x, T], [STATION.hook.x, T]], 450, ['ah']);
      this.light('agent', false);
      this.setTag('agent', `${this.calls.length}회`, 'done');
      this.light('hook', true);
      this.setTag('hook', '모델 지정 중', 'active');
      if (this.routed) {
        this.setTag('davi', '난이도 판정 중', 'active');
        await this.pulse(420);
        this.light('davi', true);
        this.daviTarget = level == null ? 0 : level;
        await this.wait(650);
        const verdict = level == null ? '판정 실패 → 기본 모델' : ruleText(level, model, range);
        this.setTag('davi', verdict, 'done', `Davi 판정 모델 · ${secondsText(data.latency_s)}`);
        this.setLevels(data.level_probabilities);
        for (const name of Object.keys(BAYS)) {
          const value = `${name === model ? '선택됨 · ' : ''}난이도 ${bayRange(name, range)}`;
          this.setTag(name, value, name === model ? 'chosen' : 'idle');
        }
        await this.pulse(420);
        this.light('davi', false);
      }
      this.pointerTarget = this.angleTo(model);
      await this.wait(350);
      this.light('hook', false);
      const hookTime = typeof data.seconds === 'number' ? `${secondsText(data.seconds)} → ` : '→ ';
      this.setTag('hook', `${hookTime}${BAY_NAMES[model]}`, 'done');
      const x = BAYS[model];
      await this.roll(puck, [[STATION.hook.x, T], [x, T], [x, 22]], 650, [`h-${model}`]);
      puck.style.opacity = '0';
      this.light(model, true);
      this.lamp(model, true);
      this.startClock(model);
    });
    return { model, level };
  }

  addCallDot(index) {
    const { K, P, STATION } = BENCH;
    const { x, y } = STATION.agent;
    const slot = Math.min(index, 3) - 2;
    const group = benchSvg('g');
    group.innerHTML = K.solidSvg(K.cylinder(x + slot * 9, y - 2.5, 1.6, 10, 1.4, P, 12), { tone: 'hi', lit: true });
    this.parts.callsLayer.append(group);
  }

  subagent(data) {
    const { BOTTOM_GROOVE: B, STATION, BAYS } = BENCH;
    const call = this.calls.find((item) => !item.finished) || this.calls[this.calls.length - 1];
    if (!call) return null;
    call.finished = true;
    this.postAt = this.sessionSeconds(data.ts);
    this.queue(async () => {
      const model = call.model;
      const x = BAYS[model];
      this.stopClock(model);
      const time = typeof data.subagent_ms === 'number' ? ` · ${secondsText(data.subagent_ms / 1000)}` : '';
      this.setTag(model, `${this.routed ? '선택됨' : '완료'}${time}`, 'chosen');
      this.light(model, false);
      this.lamp(model, false);
      const puck = this.puck('front');
      call.puck?.remove();
      this.light('collect', true);
      await this.roll(puck, [[x, 50], [x, B], [STATION.collect.x, B]], 700, [`${model}-c`]);
      this.spin.collect = true;
      this.startClock('collect');
      this.current = puck;
    });
    return call;
  }

  sessionDone(data) {
    const { BOTTOM_GROOVE: B, STATION } = BENCH;
    this.done = true;
    const wrap = typeof data.wall_s === 'number' && typeof this.postAt === 'number' ? data.wall_s - this.postAt : null;
    this.queue(async () => {
      this.spin.collect = false;
      this.stopClock('collect');
      this.setTag('collect', wrap != null && wrap >= 0 ? secondsText(wrap) : '완료', 'done');
      const puck = this.current || this.puck('front');
      await this.roll(puck, [[STATION.collect.x, B], [STATION.final.x, B]], 600, ['cf']);
      this.light('collect', false);
      puck.remove();
      this.light('final', true);
      this.parts.stamp.setAttribute('opacity', '1');
      const mark = data.correct === true ? '정답' : data.correct === false ? '오답' : '채점 없음';
      this.setTag('final', `${mark} · ${secondsText(data.wall_s)} · ${moneyText(data.cost_usd)}`, data.correct === false ? 'wrong' : 'done');
      this.svg.dataset.result = data.correct === true ? 'correct' : data.correct === false ? 'wrong' : 'none';
    });
    this.meta.textContent = `${secondsText(data.wall_s)} · ${moneyText(data.cost_usd)}`;
  }

  tick(now) {
    if (this.started == null || this.done) return;
    const elapsed = ((now - this.started) / 1000) * (this.view.speed || 1);
    this.meta.textContent = `${secondsText(elapsed)} · 실행 중`;
  }
}
