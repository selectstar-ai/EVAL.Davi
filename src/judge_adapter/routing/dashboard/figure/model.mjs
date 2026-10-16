// Where the time goes in one Claude Code run with davi-route.
//
// A run is a row of stages laid end to end on a time axis, like gauge
// blocks wrung into one stack: the main agent prepares, Davi judges,
// Claude Code hands the task over, the subagent works, the main agent
// wraps up. Every number here comes from a recorded dashboard run.

export const STAGES = ['prep', 'judge', 'handoff', 'subagent', 'wrapup'];

export const STAGE_LABELS = {
  prep: '메인 에이전트 준비',
  judge: 'Davi 판정',
  handoff: '전달',
  subagent: '서브에이전트 풀이',
  wrapup: '메인 에이전트 마무리',
};

// block heights above the bed, in world units: the subagent block shows
// the model it ran on, the leaf is Davi's judgment
export const HEIGHTS = {
  prep: 8,
  judge: 30,
  handoff: 5,
  wrapup: 8,
  haiku: 12,
  sonnet: 18,
  opus: 24,
};

export const LANES = ['opus', 'routed'];
export const LANE_LABELS = { routed: 'Davi 라우팅', opus: '항상 opus' };

// recorded runs (dashboard recordings, 2026-10-16, opus main agent)
export const RUNS = {
  go: {
    title: '9로 바둑 사활',
    routed: { prep: 7.2, judge: 0.15, handoff: 0.23, subagent: 10.9, wrapup: 10.1, model: 'sonnet' },
    opus: { prep: 8.1, judge: 0, handoff: 0.12, subagent: 33.3, wrapup: 10.0, model: 'opus' },
  },
  oldboy: {
    title: '영화 올드보이 오프닝',
    routed: { prep: 6.2, judge: 0.15, handoff: 0.17, subagent: 3.7, wrapup: 6.0, model: 'sonnet' },
    opus: { prep: 5.8, judge: 0, handoff: 0.2, subagent: 9.1, wrapup: 7.5, model: 'opus' },
  },
};

export const JUDGE_TIMES = { davi: 0.15, jev: 0.26, claude: 3.05 };

const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);

// a finished lane: {prep, judge, handoff, subagent, wrapup, model} ->
// segments [{stage, from, to, model}]
export function segmentsOf(run) {
  const out = [];
  let at = 0;
  for (const stage of STAGES) {
    const seconds = isNumber(run[stage]) ? Math.max(0, run[stage]) : 0;
    if (stage === 'judge' && seconds === 0) continue;
    out.push({ stage, from: at, to: at + seconds, model: run.model });
    at += seconds;
  }
  return out;
}

export const totalOf = (run) =>
  STAGES.reduce((sum, stage) => sum + (isNumber(run[stage]) ? run[stage] : 0), 0);

export function heightOf(segment) {
  if (segment.stage === 'subagent') return HEIGHTS[segment.model] || HEIGHTS.sonnet;
  return HEIGHTS[segment.stage];
}

// the segment under time t, or null past the end
export function segmentAt(segments, t) {
  for (const segment of segments) {
    if (t >= segment.from && t < segment.to) return segment;
  }
  return null;
}

export function share(run) {
  const total = totalOf(run);
  return total ? (run.judge || 0) / total : 0;
}
