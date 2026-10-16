'use strict';

const MODEL_KINDS = { haiku: '가벼운 모델', sonnet: '중간 모델', opus: '고성능 모델' };
const ARM_TEXT = {
  routed: { sub: '서브에이전트는 Davi가 고름', judge: '판정 중…' },
  opus: { sub: '서브에이전트는 언제나 opus', judge: '판정 없음' },
};

const app = {
  mode: 'live',
  info: null,
  selected: null,
  speed: 1,
  source: null,
  run: null,
  history: [],
};

const $ = (id) => document.getElementById(id);

const flow = new FlowView($('flow-panels'), $('flow-caption'), $('flow-footer'));

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child != null) node.append(child);
  }
  return node;
}

function modelKey(name) {
  const text = String(name || '').toLowerCase();
  return ['haiku', 'sonnet', 'opus'].find((key) => text.includes(key)) || null;
}

function money(value) {
  if (typeof value !== 'number') return '-';
  return value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(3)}`;
}

function seconds(value) {
  return typeof value === 'number' ? `${value.toFixed(1)}초` : '-';
}

function judgeSeconds(value) {
  return typeof value === 'number' ? `${value.toFixed(2)}초` : '-';
}

function percent(value) {
  return typeof value === 'number' ? `${Math.round(value * 100)}%` : '-';
}

function verdictMark(correct) {
  if (correct === true) return '정답';
  if (correct === false) return '오답';
  return '채점 없음';
}

// --- page state -------------------------------------------------------------
async function loadInfo() {
  const response = await fetch('/api/state');
  app.info = await response.json();
  renderTasks();
  renderRecordings();
  renderSpeeds();
  updateRunButton();
}

function setMode(mode) {
  app.mode = mode;
  for (const tab of document.querySelectorAll('.tab')) {
    tab.classList.toggle('active', tab.dataset.mode === mode);
  }
  $('live-panel').hidden = mode !== 'live';
  $('replay-panel').hidden = mode !== 'replay';
}

function renderTasks() {
  const list = $('task-list');
  list.replaceChildren();
  for (const task of app.info.tasks) {
    const button = el('button', {
      class: 'task-item' + (app.selected === task.id ? ' selected' : ''),
      onclick: () => {
        app.selected = app.selected === task.id ? null : task.id;
        $('free-question').value = '';
        renderTasks();
        updateRunButton();
      },
    },
    el('span', { class: 'task-title', text: task.title || task.id }),
    el('span', { class: 'task-meta', text: task.note || task.question.slice(0, 60) }));
    list.append(el('li', {}, button));
  }
  const live = app.info.live;
  $('live-status').textContent = live.enabled
    ? (live.busy ? '다른 시연이 진행 중입니다.' : '')
    : live.problem;
}

function renderRecordings() {
  const list = $('recording-list');
  list.replaceChildren();
  const items = app.info.recordings;
  $('replay-empty').hidden = items.length > 0;
  for (const item of items) {
    const when = item.recorded_at
      ? new Date(item.recorded_at * 1000).toLocaleString('ko-KR')
      : '';
    const facts = [
      item.routed_model ? `${item.routed_model} 배정` : null,
      typeof item.saving_pct === 'number' ? `${percent(item.saving_pct)} 절약` : null,
      when,
    ].filter(Boolean).join(' · ');
    const button = el('button', {
      class: 'task-item',
      onclick: () => startReplay(item.id),
    },
    el('span', { class: 'task-title', text: item.title || item.task_id }),
    el('span', { class: 'task-meta', text: facts }));
    list.append(el('li', {}, button));
  }
}

function renderSpeeds() {
  const box = $('speeds');
  box.replaceChildren();
  for (const speed of app.info.replay_speeds) {
    box.append(el('button', {
      class: 'speed' + (speed === app.speed ? ' active' : ''),
      text: `×${speed}`,
      onclick: () => { app.speed = speed; renderSpeeds(); },
    }));
  }
}

function updateRunButton() {
  const live = app.info ? app.info.live : { enabled: false };
  const question = $('free-question').value.trim();
  const running = app.run && !app.run.done;
  $('run-button').disabled = !live.enabled || live.busy || running ||
    !(question || app.selected);
}

// --- starting runs ----------------------------------------------------------
async function post(body) {
  const response = await fetch('/api/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data.run_id;
}

async function startLive() {
  const question = $('free-question').value.trim();
  const body = question
    ? { mode: 'live', question }
    : { mode: 'live', task_id: app.selected };
  try {
    watch(await post(body));
  } catch (error) {
    alert(error.message);
  }
}

async function startReplay(recordingId) {
  try {
    watch(await post({ mode: 'replay', recording_id: recordingId, speed: app.speed }));
  } catch (error) {
    alert(error.message);
  }
}

function watch(runId) {
  if (app.source) app.source.close();
  app.run = {
    id: runId, done: false, speed: 1, lanes: {}, judges: {}, events: [],
    judgeTimes: {}, lastT: 0,
  };
  updateRunButton();
  const source = new EventSource(`/api/runs/${runId}/events`);
  app.source = source;
  source.onmessage = (message) => handle(JSON.parse(message.data));
}

// --- drawing one run --------------------------------------------------------
function resetRun(data) {
  const run = app.run;
  run.mode = data.mode;
  run.speed = data.speed || 1;
  run.tiers = data.tiers;
  $('intro').hidden = true;
  $('run').hidden = false;
  if (drawerOpen()) setDrawer(false);
  $('banner').hidden = true;
  $('timing').hidden = false;
  $('timing-rows').replaceChildren();
  $('timing-callout').textContent = '';
  $('event-log').textContent = '';
  $('usage').replaceChildren();

  const chip = $('mode-chip');
  chip.className = 'chip' + (data.mode === 'live' ? ' live' : '');
  chip.textContent = data.mode === 'live' ? '● 실시간' : `녹화 재생 ×${run.speed}`;
  $('question-title').textContent = data.task.title || '질문';
  $('question-text').textContent = data.task.question;

  drawGauge(data.tiers);
  for (const arm of data.arms) drawLane(arm);
  drawJudges(data.judges, data.tiers);
  flow.reset(data);
}

function drawGauge(tiers) {
  const widths = {
    cheap: tiers.low,
    mid: tiers.high - tiers.low,
    strong: 1 - tiers.high,
  };
  for (const [tier, width] of Object.entries(widths)) {
    const zone = $(`zone-${tier}`);
    const model = tiers.models[tier];
    zone.style.width = `${width * 100}%`;
    zone.classList.remove('picked');
    zone.querySelector('span').textContent =
      `${MODEL_KINDS[modelKey(model)] || tier}\n${model}`;
    zone.querySelector('span').style.whiteSpace = 'pre';
  }
  $('gauge').className = 'gauge thinking';
  $('gauge-marker').hidden = true;
  $('gauge-verdict').textContent = 'Davi가 난이도를 판단하는 중…';
}

function drawLane(arm) {
  const lane = $(`lane-${arm.key}`);
  lane.replaceChildren($('lane-template').content.cloneNode(true));
  lane.querySelector('.lane-title').textContent = arm.label;
  lane.querySelector('.lane-sub').textContent = (ARM_TEXT[arm.key] || {}).sub || '';
  lane.querySelector('.fact-judge').textContent = (ARM_TEXT[arm.key] || {}).judge || '-';
  app.run.lanes[arm.key] = {
    node: lane, label: arm.label, started: null, done: false, main: null, sub: null,
    timing: {},
  };
  setLaneStatus(arm.key, '시작 대기', false);
}

function setLaneStatus(key, text, running) {
  const status = app.run.lanes[key].node.querySelector('.lane-status');
  status.textContent = text;
  status.classList.toggle('running', running);
}

function setFlow(key) {
  const lane = app.run.lanes[key];
  const sub = lane.sub || (key === 'routed' ? 'Davi가 고름' : 'opus');
  lane.node.querySelector('.lane-sub').textContent =
    `메인 ${lane.main || '-'} → 서브에이전트 ${sub}`;
}

function tokens(value) {
  return typeof value === 'number' ? `${value.toLocaleString('ko-KR')} 토큰` : '-';
}

function setBadge(key, model) {
  const badge = app.run.lanes[key].node.querySelector('.model-badge');
  const name = modelKey(model);
  badge.dataset.model = name || '';
  badge.querySelector('.model-name').textContent = name || model || '대기 중';
  badge.querySelector('.model-kind').textContent = MODEL_KINDS[name] || '';
  badge.classList.remove('pop');
  void badge.offsetWidth;
  badge.classList.add('pop');
}

function drawJudges(judges, tiers) {
  const box = $('judges');
  box.replaceChildren();
  for (const judge of judges) {
    const dot = el('span', { class: 'mini-dot', hidden: '' });
    const gauge = el('div', { class: 'mini-gauge' }, dot);
    gauge.style.setProperty('--low', `${tiers.low * 100}%`);
    gauge.style.setProperty('--high', `${tiers.high * 100}%`);
    const pick = el('span', { class: 'judge-pick', text: '판단 중…' });
    const time = el('span', { class: 'judge-time' });
    const row = el('div', { class: 'judge-row' + (judge.judge === 'davi' ? ' ours' : '') },
      el('span', { class: 'judge-name', text: judge.label }), gauge, pick, time);
    app.run.judges[judge.judge] = { dot, pick, time };
    box.append(row);
  }
}

function onJudge(data) {
  const row = app.run.judges[data.judge];
  if (!row) return;
  if (data.ok) {
    row.dot.hidden = false;
    row.dot.style.left = `${data.level * 100}%`;
    row.pick.dataset.model = modelKey(data.model) || '';
    row.pick.textContent = `${data.model} (${MODEL_KINDS[modelKey(data.model)] || ''})`;
  } else {
    row.pick.textContent = data.outcome === 'local_config' ? '사용 불가 (키 없음)' : '판정 실패';
  }
  row.time.textContent = judgeSeconds(data.latency_s);
  if (data.ok) app.run.judgeTimes[data.judge] = data.latency_s;
}

function onRouting(data) {
  flow.routing(data);
  const lane = app.run.lanes[data.arm];
  if (lane) {
    lane.sub = modelKey(data.model_set || data.model_requested) || lane.sub;
    setFlow(data.arm);
    if (!lane.timing.routing) {
      lane.timing.routing = {
        ts: data.ts,
        t: app.run.lastT,
        seconds: data.seconds,
        judge: data.arm === 'routed' && typeof data.latency_s === 'number' ? data.latency_s : 0,
      };
    }
  }
  if (data.arm === 'opus') {
    setBadge('opus', data.model_set || 'opus');
    setLaneStatus('opus', `${data.model_set || 'opus'}로 풀이 중`, true);
    return;
  }
  const model = data.model_set || data.model_requested;
  if (typeof data.level === 'number') {
    const marker = $('gauge-marker');
    marker.hidden = false;
    marker.style.left = '0%';
    requestAnimationFrame(() => { marker.style.left = `${data.level * 100}%`; });
    $('gauge-value').textContent = `난이도 ${data.level.toFixed(2)}`;
    $('gauge').className = 'gauge decided';
    if (data.tier) $(`zone-${data.tier}`).classList.add('picked');
    $('gauge-verdict').textContent =
      `${data.tier_label || ''} → ${model}로 배정 (판단 ${judgeSeconds(data.latency_s)})`;
  } else {
    $('gauge').className = 'gauge';
    $('gauge-verdict').textContent = `판단 실패 → 기본값 ${model || '모델'} 사용`;
  }
  setBadge('routed', model);
  lane.node.querySelector('.fact-judge').textContent =
    typeof data.latency_s === 'number' ? judgeSeconds(data.latency_s) : '실패';
  setLaneStatus('routed', `${model}로 풀이 중`, true);
}

function onSubagent(data) {
  flow.subagent(data);
  const lane = app.run.lanes[data.arm];
  if (!lane) return;
  const node = lane.node;
  if (!lane.timing.post) {
    lane.timing.post = { ts: data.ts, t: app.run.lastT, ms: data.subagent_ms };
  }
  if (data.resolved_model) {
    node.querySelector('.fact-model').textContent = `${data.resolved_model} ✓`;
  }
  if (typeof data.subagent_ms === 'number') {
    node.querySelector('.fact-sub-time').textContent = seconds(data.subagent_ms / 1000);
  }
  const output = data.subagent_output_tokens ?? data.subagent_tokens;
  if (typeof output === 'number') {
    node.querySelector('.fact-sub-tokens').textContent = tokens(output);
    lane.subTokens = true;
  }
}

function onSessionStarted(data) {
  const lane = app.run.lanes[data.arm];
  if (!lane) return;
  lane.started = performance.now();
  lane.main = modelKey(data.main_model) || data.main_model;
  lane.timing.startedAt = data.started_at;
  lane.timing.t0 = app.run.lastT;
  flow.sessionStarted(data);
  setFlow(data.arm);
  setLaneStatus(data.arm, '질문을 읽고 서브에이전트에게 맡기는 중', true);
}

function onSessionDone(data) {
  const lane = app.run.lanes[data.arm];
  if (!lane) return;
  lane.done = true;
  const node = lane.node;
  node.querySelector('.fact-time').textContent = seconds(data.wall_s);
  const cost = node.querySelector('.fact-cost');
  cost.textContent = money(data.cost_usd);
  cost.classList.add('big');
  const answer = node.querySelector('.answer');
  answer.classList.toggle('correct', data.correct === true);
  answer.classList.toggle('wrong', data.correct === false);
  node.querySelector('.answer-mark').textContent = verdictMark(data.correct);
  node.querySelector('.answer-text').textContent = data.answer || '(답 없음)';
  if (data.is_error) {
    setLaneStatus(data.arm, `실행 실패${data.error ? `: ${data.error}` : ''}`, false);
  } else {
    setLaneStatus(data.arm, '완료', false);
  }
  drawUsage(data);
  lane.timing.wall = data.wall_s;
  drawTiming();
  flow.sessionDone(data);
  if (!lane.subTokens && lane.sub && lane.sub !== lane.main) {
    const entry = Object.entries(data.model_usage || {})
      .find(([model]) => modelKey(model) === lane.sub);
    if (entry) node.querySelector('.fact-sub-tokens').textContent = tokens(entry[1].outputTokens);
  }
}

function drawUsage(data) {
  const rows = Object.entries(data.model_usage || {}).map(([model, usage]) =>
    el('tr', {},
      el('td', { text: data.label }),
      el('td', { text: model }),
      el('td', { text: String(usage.inputTokens ?? '-') }),
      el('td', { text: String(usage.outputTokens ?? '-') }),
      el('td', { text: String(usage.cacheReadInputTokens ?? '-') }),
      el('td', { text: money(usage.costUSD) })));
  let table = $('usage').querySelector('table');
  if (!table) {
    table = el('table', {},
      el('thead', {}, el('tr', {},
        ...['세션', '모델', '입력 토큰', '출력 토큰', '캐시 읽기', '비용']
          .map((text) => el('th', { text })))),
      el('tbody'));
    $('usage').append(table);
  }
  table.querySelector('tbody').append(...rows);
}

// --- where the time went --------------------------------------------------
function timeline(lane) {
  const tm = lane.timing;
  if (typeof tm.wall !== 'number') return null;
  const rel = (ts, t) => {
    if (typeof ts === 'number' && typeof tm.startedAt === 'number') return ts - tm.startedAt;
    return typeof t === 'number' ? t - (tm.t0 || 0) : null;
  };
  const wall = tm.wall;
  const routing = tm.routing;
  if (!routing) return { total: wall, parts: { prep: wall }, judge: 0, noAgent: true };
  const hookTime = typeof routing.seconds === 'number' ? routing.seconds : routing.judge;
  // a log line without ts is written when the hook ends
  const hookStart = Math.max(0, typeof routing.ts === 'number'
    ? rel(routing.ts, null)
    : rel(null, routing.t) - hookTime);
  const hookEnd = hookStart + hookTime;
  const post = tm.post;
  const postAt = post ? rel(post.ts, post.t) : null;
  let sub = 0;
  if (post && typeof post.ms === 'number') sub = post.ms / 1000;
  else if (postAt != null) sub = Math.max(0, postAt - hookEnd);
  const handoff = postAt != null ? Math.max(0, postAt - sub - hookEnd) : 0;
  const finished = postAt != null ? postAt : hookEnd + sub;
  const parts = {
    prep: hookStart,
    judge: routing.judge,
    pass: Math.max(0, hookTime - routing.judge) + handoff,
    sub,
    wrap: Math.max(0, wall - finished),
  };
  const total = Object.values(parts).reduce((a, b) => a + b, 0);
  return { total, parts, judge: routing.judge, wall };
}

const SEGMENTS = [
  ['prep', '메인 에이전트 준비'],
  ['judge', 'Davi 판정'],
  ['pass', '전달'],
  ['sub', '서브에이전트 풀이'],
  ['wrap', '메인 에이전트 마무리'],
];

function drawTiming() {
  const lanes = Object.entries(app.run.lanes)
    .map(([key, lane]) => [key, lane, timeline(lane)])
    .filter(([, , line]) => line);
  if (!lanes.length) return;
  const scale = Math.max(...lanes.map(([, , line]) => line.total));
  const rows = $('timing-rows');
  rows.replaceChildren();
  for (const [key, lane, line] of lanes) {
    const bar = el('div', { class: 'timing-bar' });
    bar.style.width = `${(line.total / scale) * 100}%`;
    for (const [part, label] of SEGMENTS) {
      const value = line.parts[part] || 0;
      if (value <= 0) continue;
      const seg = el('div', {
        class: `seg seg-${part}`,
        title: `${label} ${seconds(value)}`,
      });
      seg.style.width = `${(value / line.total) * 100}%`;
      if (part === 'sub') seg.dataset.model = lane.sub || '';
      if (part === 'judge') {
        seg.append(el('span', { class: 'judge-flag', text: `Davi 판정 ${value.toFixed(2)}초` }));
      } else if (value / scale > 0.09) {
        seg.textContent = seconds(value);
      }
      bar.append(seg);
    }
    const name = el('div', { class: 'timing-name', text: lane.label },
      el('small', { text: `메인 ${lane.main || '-'} → 서브 ${lane.sub || '-'}` }));
    rows.append(el('div', { class: 'timing-row' + (line.judge > 0 ? ' has-flag' : '') },
      name,
      el('div', { class: 'timing-track' }, bar),
      el('div', { class: 'timing-total', text: seconds(line.total) })));
  }
  $('timing').hidden = false;
  drawTimingCallout(lanes);
}

function drawTimingCallout(lanes) {
  const routed = lanes.find(([key]) => key === 'routed');
  const box = $('timing-callout');
  if (!routed || routed[2].noAgent) {
    box.textContent = '';
    return;
  }
  const line = routed[2];
  const share = line.total ? (line.judge / line.total) * 100 : 0;
  const rest = line.total - line.judge;
  box.replaceChildren(
    'Davi 판정은 ',
    el('strong', { text: `${line.judge.toFixed(2)}초` }),
    `로, 전체 ${seconds(line.total)}의 ${share < 1 ? share.toFixed(1) : Math.round(share)}%입니다. ` +
      `나머지 ${seconds(rest)}는 메인 에이전트 준비 ${seconds(line.parts.prep)}, ` +
      `서브에이전트(${routed[1].sub || '-'}) 풀이 ${seconds(line.parts.sub)}, ` +
      `마무리 ${seconds(line.parts.wrap)} 등 Claude Code가 일하는 시간입니다.`);
  const others = ['claude', 'jev']
    .filter((name) => typeof app.run.judgeTimes[name] === 'number')
    .map((name) => `${name === 'claude' ? 'Claude' : 'Jev'}는 ${judgeSeconds(app.run.judgeTimes[name])}`);
  if (others.length) {
    box.append(` 같은 판정에 ${others.join(', ')}가 걸렸습니다.`);
  }
}

function bannerText(summary) {
  const rc = summary.routed_correct;
  const oc = summary.opus_correct;
  let head;
  if (rc === true && oc === true) head = '두 쪽 모두 정답';
  else if (rc === true && oc === false) head = 'Davi 라우팅만 정답';
  else if (rc === false && oc === true) head = '이번에는 opus만 정답';
  else if (rc === false && oc === false) head = '두 쪽 모두 오답';
  else head = '채점 없는 질문';
  let cost = '';
  if (typeof summary.saving_usd === 'number') {
    cost = summary.saving_usd >= 0
      ? `비용 ${percent(summary.saving_pct)} 절약 (${money(summary.routed_cost_usd)} vs ${money(summary.opus_cost_usd)})`
      : `이번에는 라우팅 쪽이 ${money(-summary.saving_usd)} 더 들었습니다`;
  }
  return { head, cost, good: rc !== false && (summary.saving_usd ?? 0) >= 0 };
}

function onSummary(summary) {
  const banner = $('banner');
  const text = bannerText(summary);
  banner.className = 'banner' + (text.good ? '' : ' neutral');
  banner.replaceChildren(
    document.createTextNode([text.head, text.cost].filter(Boolean).join(', ')),
    el('small', {
      text: `Davi가 고른 모델: ${summary.routed_model || '-'}` +
        (summary.routed_resolved_model ? ` · 실제 실행: ${summary.routed_resolved_model}` : ''),
    }));
  banner.hidden = false;
  flow.summary(summary, [text.head, text.cost].filter(Boolean).join(', '));
  addHistory(summary);
}

function addHistory(summary) {
  const replayed = app.run.mode === 'replay';
  app.history.push({
    title: $('question-title').textContent + (replayed ? ' (녹화)' : ''),
    ...summary,
  });
  $('history').hidden = false;
  const mark = (value) => el('span', {
    class: value === true ? 'good' : value === false ? 'bad' : '',
    text: verdictMark(value),
  });
  const item = app.history[app.history.length - 1];
  $('history-body').prepend(el('tr', {},
    el('td', { text: item.title }),
    el('td', { text: item.routed_model || '-' }),
    el('td', { text: money(item.routed_cost_usd) }),
    el('td', { text: money(item.opus_cost_usd) }),
    el('td', { text: percent(item.saving_pct) }),
    el('td', {}, mark(item.routed_correct), ' / ', mark(item.opus_correct))));
  updateTally();
}

function updateTally() {
  const priced = app.history.filter((item) =>
    typeof item.routed_cost_usd === 'number' && typeof item.opus_cost_usd === 'number');
  if (!priced.length) return;
  const routed = priced.reduce((sum, item) => sum + item.routed_cost_usd, 0);
  const opus = priced.reduce((sum, item) => sum + item.opus_cost_usd, 0);
  const saved = opus - routed;
  $('tally').hidden = false;
  $('tally-value').textContent =
    `${money(saved)} 절약 (${percent(opus ? saved / opus : null)}) · ${priced.length}문항`;
}

function onError(data) {
  const banner = $('banner');
  banner.className = 'banner neutral';
  banner.textContent = `문제가 생겼습니다${data.arm ? ` (${data.arm})` : ''}: ${data.message}`;
  banner.hidden = false;
}

function handle(event) {
  const run = app.run;
  if (!run) return;
  run.events.push(event);
  run.lastT = event.t;
  $('event-log').textContent +=
    `${event.t.toFixed(1).padStart(6)}s  ${event.type}  ${JSON.stringify(event.data).slice(0, 220)}\n`;
  const data = event.data;
  switch (event.type) {
    case 'run_started': resetRun(data); break;
    case 'judge': onJudge(data); break;
    case 'session_started': onSessionStarted(data); break;
    case 'routing': onRouting(data); break;
    case 'subagent': onSubagent(data); break;
    case 'session_done': onSessionDone(data); break;
    case 'summary': onSummary(data); break;
    case 'error': onError(data); break;
    case 'run_done':
      run.done = true;
      if (app.source) app.source.close();
      loadInfo();
      break;
    default: break;
  }
}

function tick() {
  const run = app.run;
  if (!run) return;
  for (const lane of Object.values(run.lanes)) {
    if (lane.started == null || lane.done) continue;
    const elapsed = ((performance.now() - lane.started) / 1000) * (run.speed || 1);
    lane.node.querySelector('.fact-time').textContent = seconds(elapsed);
  }
}

// --- the question drawer ----------------------------------------------------
function setDrawer(open) {
  $('layout').classList.toggle('collapsed', !open);
  $('drawer-toggle').setAttribute('aria-expanded', String(open));
  try { localStorage.setItem('davi-drawer', open ? 'open' : 'closed'); } catch (error) { /* private mode */ }
}

function drawerOpen() {
  return !$('layout').classList.contains('collapsed');
}

// --- wiring -----------------------------------------------------------------
$('drawer-toggle').addEventListener('click', () => setDrawer(!drawerOpen()));
try { if (localStorage.getItem('davi-drawer') === 'closed') setDrawer(false); } catch (error) { /* private mode */ }
for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => setMode(tab.dataset.mode));
}
$('run-button').addEventListener('click', startLive);
$('free-question').addEventListener('input', () => {
  if ($('free-question').value.trim() && app.selected) {
    app.selected = null;
    renderTasks();
  }
  updateRunButton();
});
setInterval(tick, 100);
loadInfo();
