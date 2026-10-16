// Builds static/figure.html: a gauge-block timing bench.
//
//   node src/judge_adapter/routing/dashboard/figure/build.mjs
//
// The page picks its theme at run time (?theme=, a {type:'theme'} message
// or prefers-color-scheme); ?embed trims it for the dashboard's iframe.
//
// Two lanes on a surface plate, one per Claude Code session. Each stage of
// a run is a gauge block as long as the seconds it took (one ruler tick is
// one second), wrung end to end against a stop. A carriage with a dial
// gauge travels along each lane at the run's clock and lays the blocks as
// it goes; its plunger reads the height of the block under it. Davi's
// judgment is a feeler leaf 0.15 s thick. The kit is vendored from the
// Anatomy skill (vendor/iso-kit.mjs, MIT).

import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as k from './vendor/iso-kit.mjs';
import * as M from './model.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = join(HERE, '..', 'static');
const strip = (file) => readFileSync(join(HERE, file), 'utf8').replace(/^export /gm, '');

const WIDTH = 640;
const HEIGHT = 290;

const U = 6.4;
const SPAN = 60;
const X0 = -196;
const X_END = X0 + SPAN * U;
const PLATE = { x: X0 - 30, y: -68, w: SPAN * U + 54, d: 136, r: 8 };
const PLATE_H = 12;
const FOOT_H = 5;
const LANE_Y = { opus: -28, routed: 18 };
const BED_H = 4;
const BED_HALF = 10;
const BLOCK_HALF = 8;
const LEAF_HALF = 9.5;
const RAIL = [-16, 13];
const RAIL_D = 3;
const RAIL_H = 3;
const BUSH = { w: 12, d: 7, z: RAIL_H, h: 5 };
const POST_R = 2;
const POST_Y = 14.5;
const BAR_Z = 38;
const BAR_H = 5;
const DIAL_R = 7;
const DIAL_Z = BAR_Z + BAR_H;
const DIAL_H = 4;
const DIAL_TOP = DIAL_Z + DIAL_H;
const TIP_R = 2;
const PER_TURN = 36;

const P = k.fitProjection(
  [
    ...k.boxCorners(PLATE, -PLATE_H - FOOT_H, 0),
    [X0, LANE_Y.opus, DIAL_TOP + 2],
    [X_END, LANE_Y.opus, DIAL_TOP + 2],
    [X0, LANE_Y.routed, DIAL_TOP + 2],
  ],
  WIDTH,
  HEIGHT,
  { pad: 26, azimuth: 64 },
);

const at = (point) => k.iso(point, P);
const S = (paths, style) => k.solidSvg(paths, style);
const L = (d, style) => k.lineSvg(d, style);
const D = (points, style) => k.dotsSvg(points, style);
const byDepth = (points) =>
  [...points].sort((a, b) => k.depthOf([a[0], a[1], 0], P) - k.depthOf([b[0], b[1], 0], P));

function plate() {
  const out = [`<path class="iso-halo" d="${k.haloOf(PLATE, -PLATE_H - FOOT_H, PLATE_H, P)}"/>`];
  for (const [x, y] of byDepth(k.corners(PLATE, 24))) {
    out.push(S(k.cylinder(x, y, 7, -PLATE_H - FOOT_H, 1.6, P, 28), { tone: 'lo' }));
    out.push(S(k.cylinder(x, y, 5.4, -PLATE_H - FOOT_H + 1.6, FOOT_H - 1.6, P, 28), { tone: 'mid' }));
  }
  out.push(S(k.slabOf(PLATE, -PLATE_H, PLATE_H, P, 8, 1.6), { tone: 'mid' }));
  out.push(L(k.planOutline(k.insetPlan(PLATE, 5), 0, P), { tone: 'lo' }));
  const screws = k.corners(PLATE, 11);
  out.push(L(screws.map(([x, y]) => k.ring(x, y, 2.2, 0, P, 16)).join(''), { tone: 'lo' }));
  out.push(D(screws.map(([x, y]) => at([x, y, 0])), { size: 0.5 }));
  return out.join('');
}

function ruler() {
  const strip = { x: X0 - 4, y: 50, w: SPAN * U + 8, d: 8, r: 1 };
  const out = [S(k.slabOf(strip, 0, 0.6, P, 3), { tone: 'lo', crease: 'none' })];
  out.push(L(k.lineOnTop([X0, strip.y + 0.8], [X0, strip.y + 5.6], 0.6, P), { tone: 'hi' }));
  out.push(D([at([strip.x + 2, strip.y + 4, 0.6]), at([strip.x + strip.w - 2, strip.y + 4, 0.6])], { size: 0.45 }));
  const label = { x: X_END - 52, y: -64, w: 54, d: 12, r: 1.5 };
  out.push(S(k.slabOf(label, 0, 0.8, P, 4), { tone: 'lo', crease: 'none' }));
  out.push(L(k.planOutline(k.insetPlan(label, 1.6), 0.8, P, 4), { tone: 'faint' }));
  out.push(k.faceTextSvg(k.topMatrix([label.x + 5, label.y + 5.6], 0.8, P, 'x'), 'DAVI-ROUTE', { size: 3, tone: 'lo' }));
  out.push(L(k.lineOnTop([label.x + 5, label.y + 8.4], [label.x + 34, label.y + 8.4], 0.8, P), { tone: 'faint', free: true }));
  out.push(D([at([label.x + 2.4, label.y + 6, 0.8]), at([label.x + label.w - 2.4, label.y + 6, 0.8])], { size: 0.4 }));
  return out.join('');
}

function laneBase(lane) {
  const c = LANE_Y[lane];
  const out = [];
  const bed = { x: X0, y: c - BED_HALF, w: SPAN * U + 4, d: BED_HALF * 2, r: 1.5 };
  for (const dy of RAIL) {
    const rail = { x: X0, y: c + dy, w: SPAN * U + 4, d: RAIL_D, r: 0.8 };
    out.push(S(k.slabOf(rail, 0, RAIL_H, P, 3, 0.4), { tone: 'mid' }));
    out.push(D([X0 + 20, X0 + 140, X0 + 260, X_END - 6].map((x) => at([x, c + dy + RAIL_D / 2, RAIL_H])), { size: 0.4 }));
  }
  out.push(S(k.slabOf(bed, 0, BED_H, P, 3, 0.5), { tone: 'lo', crease: 'faint' }));
  out.push(L(k.lineOnTop([X0 + 1, c], [X_END + 3, c], BED_H, P), { tone: 'faint' }));
  const stop = { x: X0 - 14, y: c - 19, w: 14, d: 38, r: 2 };
  out.push(S(k.slabOf(stop, 0, 12, P, 4, 0.6), { tone: 'mid' }));
  out.push(L(k.planOutline(k.insetPlan(stop, 2.4), 12, P, 4), { tone: 'faint' }));
  out.push(D([at([stop.x + 4, stop.y + 4, 12]), at([stop.x + 4, stop.y + stop.d - 4, 12])], { size: 0.45 }));
  const knob = [stop.x + 8, c + 12];
  out.push(S(k.cylinder(knob[0], knob[1], 3.4, 12, 4, P, 24), { tone: 'mid' }));
  out.push(L(k.knurl(knob[0], knob[1], 3.4, 12, 16, 24, P), { tone: 'lo' }));
  out.push(D([at([knob[0], knob[1], 16])], { size: 0.5, tone: 'hi' }));
  const end = { x: X_END + 6, y: c - 19, w: 8, d: 38, r: 1.5 };
  out.push(S(k.slabOf(end, 0, 7, P, 3, 0.5), { tone: 'mid' }));
  out.push(D([at([end.x + 4, end.y + 4, 7]), at([end.x + 4, end.y + end.d - 4, 7])], { size: 0.45 }));
  return out.join('');
}

// carriage parts, built at x = 0 and moved with translateAlong
function carriageBack(lane) {
  const c = LANE_Y[lane];
  const out = [];
  const bush = { x: -BUSH.w / 2, y: c + RAIL[0] - 2, w: BUSH.w, d: BUSH.d, r: 1.5 };
  out.push(S(k.slabOf(bush, BUSH.z, BUSH.h, P, 3, 0.5), { tone: 'mid' }));
  out.push(D([at([bush.x + 2.4, bush.y + 3.5, BUSH.z + BUSH.h]), at([bush.x + bush.w - 2.4, bush.y + 3.5, BUSH.z + BUSH.h])], { size: 0.4 }));
  out.push(S(k.cylinder(0, c - POST_Y, POST_R, BUSH.z + BUSH.h, BAR_Z - BUSH.z - BUSH.h, P, 20), { tone: 'mid', crease: 'none' }));
  return out.join('');
}

function carriageFront(lane) {
  const c = LANE_Y[lane];
  const out = [];
  const bush = { x: -BUSH.w / 2, y: c + RAIL[1] - 2, w: BUSH.w, d: BUSH.d, r: 1.5 };
  out.push(S(k.slabOf(bush, BUSH.z, BUSH.h, P, 3, 0.5), { tone: 'mid' }));
  out.push(D([at([bush.x + 2.4, bush.y + 3.5, BUSH.z + BUSH.h]), at([bush.x + bush.w - 2.4, bush.y + 3.5, BUSH.z + BUSH.h])], { size: 0.4 }));
  out.push(S(k.cylinder(0, c + POST_Y, POST_R, BUSH.z + BUSH.h, BAR_Z - BUSH.z - BUSH.h, P, 20), { tone: 'mid', crease: 'none' }));
  out.push(L(k.sideArc(0, c + POST_Y, POST_R, 24, P, 10), { tone: 'faint' }));
  const bar = { x: -4, y: c - POST_Y - 3, w: 8, d: POST_Y * 2 + 6, r: 1.5 };
  out.push(S(k.slabOf(bar, BAR_Z, BAR_H, P, 3, 0.5), { tone: 'mid' }));
  out.push(D([at([0, bar.y + 2.2, BAR_Z + BAR_H]), at([0, bar.y + bar.d - 2.2, BAR_Z + BAR_H])], { size: 0.45 }));
  out.push(S(k.cylinder(0, c, DIAL_R, DIAL_Z, DIAL_H, P, 48, 0.6), { tone: 'hi', crease: 'lo' }));
  out.push(L(k.knurl(0, c, DIAL_R, DIAL_TOP - 1.6, DIAL_TOP, 40, P, 0.3), { tone: 'lo' }));
  out.push(L(k.ring(0, c, DIAL_R - 1.2, DIAL_TOP, P, 48), { tone: 'lo' }));
  const ticks = k.radialTicks(0, c, DIAL_R - 1.6, 30, 5, DIAL_TOP, [0.8, 1.6], P);
  out.push(L(ticks.minor, { tone: 'lo' }), L(ticks.major, { tone: 'mid' }));
  out.push(S(k.slabOf({ x: DIAL_R - 1, y: c - 1.4, w: 3.2, d: 2.8, r: 0.8 }, DIAL_Z + 0.8, 2.4, P, 2), { tone: 'mid', crease: 'none' }));
  out.push(L(k.ring(0, c, 0.9, DIAL_TOP, P, 12), { tone: 'mid' }));
  return out.join('');
}

const figureBody = [plate(), ruler()];
for (const lane of M.LANES) {
  const c = LANE_Y[lane];
  figureBody.push(
    `<g data-lane="${lane}">`,
    laneBase(lane),
    `<g data-part="back">${carriageBack(lane)}</g>`,
    '<g data-part="blocks"></g>',
    '<g data-part="plunger"></g>',
    `<g data-part="front">${carriageFront(lane)}</g>`,
    `<path data-part="needle" class="iso-line" data-tone="lit" data-free="start" d="" style="stroke-width:0.9"/>`,
    '</g>',
  );
  void c;
}

const svg = k.figureSvg({
  width: WIDTH,
  height: HEIGHT,
  label:
    'A gauge-block timing bench. Two lanes, one per Claude Code session, each a row of gauge blocks as long as the seconds each stage took: the main agent preparing, Davi judging, the hand-off, the subagent working and the main agent wrapping up. A carriage with a dial gauge lays the blocks along each lane at the run\'s clock. Davi\'s judgment is a feeler leaf 0.15 seconds thick.',
  body: figureBody,
});

const CONSTANTS = {
  P, U, SPAN, X0, LANE_Y, BED_H, BLOCK_HALF, LEAF_HALF, BAR_Z, DIAL_TOP, DIAL_R, TIP_R, PER_TURN,
};

const live = `
${strip('model.mjs')}
${strip('live.js')}
start(${JSON.stringify(CONSTANTS)});
`;

const embedCss = `<style>
.iso-page{margin:12px auto 16px}
body[data-embed] .iso-page{width:100%;margin:0}
body[data-embed] .iso-caption{display:none}
body[data-embed]{background:transparent;overflow:hidden}
html:has(body[data-embed]){overflow:hidden}
.dr-stage{outline:none}
.dr-stage:focus-visible{outline:1px solid var(--anatomy-muted);outline-offset:6px}
</style>`;

const body = embedCss + k.plateHtml({
  fig: 'Fig 1',
  title: '시간은 어디에 쓰였나',
  hint: '올려서 시간 따라가기',
  readout: '대기 중',
  keys: [
    { mark: 'lit', label: 'Davi 판정: 0.15초 두께의 필러 게이지 한 장' },
    { mark: 'raised', label: '서브에이전트 블록: 높이가 모델 (haiku < sonnet < opus)' },
    { mark: 'flat', label: '메인 에이전트 준비 · 전달 · 마무리 블록' },
  ],
  caption:
    '위 레인은 항상 opus, 아래 레인은 Davi 라우팅 세션입니다. 블록 길이는 실제 걸린 시간이고 눈금 한 칸이 1초입니다. 캐리지가 시간에 맞춰 블록을 깔며 지나갑니다. Davi 판정은 거의 보이지 않을 만큼 얇아서, 시간은 판정이 아니라 에이전트들이 주고받고 푸는 데 쓰인다는 게 보입니다.',
  body: `<div class="iso-stage dr-stage" tabindex="0" role="slider" aria-label="시간 막대 작업대: 화살표 키로 구간을 옮겨 봅니다" aria-valuemin="0" aria-valuemax="60" aria-valuenow="0">${svg}</div><span class="dr-live" aria-live="polite" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)"></span>`,
});

const html = k
  .pageHtml({ title: 'Davi 라우팅 시간 작업대', body, script: strip('vendor/iso-kit.mjs') + live })
  .replace('<html lang="en">', '<html lang="ko">');
writeFileSync(join(OUT, 'figure.html'), html);
console.log('wrote static/figure.html', Math.round(html.length / 1024), 'KB');
