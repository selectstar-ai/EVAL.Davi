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
  app.run = { id: runId, done: false, speed: 1, lanes: {}, judges: {}, events: [] };
  updateRunButton();
  const source = new EventSource(`/api/runs/${runId}/events`);
  app.source = source;
  source.onmessage = (message) => handle(JSON.parse(message.data));
}

// --- drawing one run --------------------------------------------------------
function setStep(step) {
  for (const item of document.querySelectorAll('#steps li')) {
    const n = Number(item.dataset.step);
    item.classList.toggle('done', n < step);
    item.classList.toggle('current', n === step);
  }
}

function resetRun(data) {
  const run = app.run;
  run.mode = data.mode;
  run.speed = data.speed || 1;
  run.tiers = data.tiers;
  $('intro').hidden = true;
  $('run').hidden = false;
  $('banner').hidden = true;
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
  setStep(2);
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
  app.run.lanes[arm.key] = { node: lane, started: null, done: false, main: null, sub: null };
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
  row.time.textContent = seconds(data.latency_s);
}

function onRouting(data) {
  const lane = app.run.lanes[data.arm];
  if (lane) {
    lane.sub = modelKey(data.model_set || data.model_requested) || lane.sub;
    setFlow(data.arm);
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
      `${data.tier_label || ''} → ${model}로 배정 (판단 ${seconds(data.latency_s)})`;
  } else {
    $('gauge').className = 'gauge';
    $('gauge-verdict').textContent = `판단 실패 → 기본값 ${model || '모델'} 사용`;
  }
  setBadge('routed', model);
  lane.node.querySelector('.fact-judge').textContent =
    typeof data.latency_s === 'number' ? seconds(data.latency_s) : '실패';
  setLaneStatus('routed', `${model}로 풀이 중`, true);
  setStep(4);
}

function onSubagent(data) {
  const lane = app.run.lanes[data.arm];
  if (!lane) return;
  const node = lane.node;
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
  if (!lane.subTokens && lane.sub && lane.sub !== lane.main) {
    const entry = Object.entries(data.model_usage || {})
      .find(([model]) => modelKey(model) === lane.sub);
    if (entry) node.querySelector('.fact-sub-tokens').textContent = tokens(entry[1].outputTokens);
  }
  if (Object.values(app.run.lanes).every((item) => item.done)) setStep(5);
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
  setStep(6);
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

// --- wiring -----------------------------------------------------------------
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
