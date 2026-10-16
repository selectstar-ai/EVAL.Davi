'use strict';

// The data-flow view: two Claude Code harnesses side by side, drawn from
// the run's real events. Inside each harness: the question, the main
// agent, every Agent call it makes, the PreToolUse hook, the subagents and
// the final answer. On the routed side the hook reaches out of the harness
// to Davi, an external decision model, and brings the model choice back.

const FLOW = {
  width: 520,
  height: 566,
  harness: { x: 12, y: 112, w: 496, h: 444 },
  question: 146,
  main: 214,
  agents: 282,
  hook: 348,
  subs: 416,
  collect: 488,
  final: 534,
  davi: { x: 260, y: 56, w: 212, h: 46 },
  spread: [92, 428],
};
const SVG_NS = 'http://www.w3.org/2000/svg';
const MODEL_NAMES = { haiku: 'Haiku', sonnet: 'Sonnet', opus: 'Opus' };

function svgNode(tag, attrs = {}, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text != null) node.textContent = text;
  return node;
}

function flowModel(name) {
  const text = String(name || '').toLowerCase();
  return ['haiku', 'sonnet', 'opus'].find((key) => text.includes(key)) || null;
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const secondsText = (value) => (typeof value === 'number' ? `${value < 1 ? value.toFixed(2) : value.toFixed(1)}초` : '-');
const moneyText = (value) => (typeof value === 'number' ? `$${value.toFixed(3)}` : '-');

class FlowNode {
  constructor(layer, { shape = 'box', w = 140, h = 38, title = '', sub = '', kind = '' }) {
    this.shape = shape;
    this.w = w;
    this.h = h;
    this.x = FLOW.width / 2;
    this.y = 0;
    this.group = svgNode('g', { class: `flow-node ${kind}`, 'data-state': 'hidden' });
    this.body = shape === 'ellipse'
      ? svgNode('ellipse', { class: 'flow-body', rx: w / 2, ry: h / 2 })
      : svgNode('rect', { class: 'flow-body', x: -w / 2, y: -h / 2, width: w, height: h, rx: 7 });
    this.title = svgNode('text', { class: 'fn-title', y: sub ? -3 : 4 }, title);
    this.sub = svgNode('text', { class: 'fn-sub', y: 11 }, sub);
    this.group.append(this.body, this.title, this.sub);
    layer.append(this.group);
  }

  resize(w) {
    this.w = w;
    if (this.shape === 'ellipse') this.body.setAttribute('rx', w / 2);
    else {
      this.body.setAttribute('x', -w / 2);
      this.body.setAttribute('width', w);
    }
    return this;
  }

  place(x, y) {
    this.x = x;
    this.y = y;
    this.group.setAttribute('transform', `translate(${x} ${y})`);
    return this;
  }

  text(title, sub) {
    if (title != null) this.title.textContent = title;
    if (sub != null) {
      this.sub.textContent = sub;
      this.title.setAttribute('y', sub ? -3 : 4);
    }
    return this;
  }

  state(value) {
    this.group.dataset.state = value;
    return this;
  }

  set model(name) {
    if (name) this.group.dataset.model = name;
  }

  get top() {
    return [this.x, this.y - this.h / 2];
  }

  get bottom() {
    return [this.x, this.y + this.h / 2];
  }

  get right() {
    return [this.x + this.w / 2, this.y];
  }
}

class FlowEdge {
  constructor(layer, from, to, { route = null } = {}) {
    this.from = from;
    this.to = to;
    this.route = route;
    this.path = svgNode('path', { class: 'flow-edge', 'data-state': 'hidden' });
    layer.append(this.path);
    this.update();
  }

  update() {
    if (this.route) {
      this.path.setAttribute('d', this.route());
      return;
    }
    const [x1, y1] = this.from.bottom;
    const [x2, y2] = this.to.top;
    const mid = (y1 + y2) / 2;
    this.path.setAttribute('d', `M${x1} ${y1} C${x1} ${mid} ${x2} ${mid} ${x2} ${y2}`);
  }

  state(value) {
    this.path.dataset.state = value;
    return this;
  }
}

class FlowPanel {
  constructor(container, arm, view) {
    this.arm = arm;
    this.view = view;
    this.routed = arm.key === 'routed';
    this.root = document.createElement('div');
    this.root.className = `flow-panel flow-${arm.key}`;
    const head = document.createElement('div');
    head.className = 'flow-head';
    this.heading = document.createElement('h3');
    this.heading.textContent = this.routed ? 'Davi · 작업별 모델 선택' : '기준 · 모든 서브에이전트 Opus';
    this.meta = document.createElement('span');
    this.meta.className = 'flow-meta';
    head.append(this.heading, this.meta);
    this.svg = svgNode('svg', {
      class: 'flow-svg',
      viewBox: `0 0 ${FLOW.width} ${FLOW.height}`,
      role: 'img',
      'aria-label': `${arm.label}: Claude Code 안의 데이터 흐름`,
    });
    this.root.append(head, this.svg);
    container.append(this.root);
    this.build();
  }

  build() {
    const svg = this.svg;
    svg.replaceChildren();
    const box = FLOW.harness;
    svg.append(
      svgNode('rect', { class: 'flow-harness', x: box.x, y: box.y, width: box.w, height: box.h, rx: 14 }),
      svgNode('text', { class: 'flow-harness-label', x: box.x + 16, y: box.y + 22 }, 'Claude Code'),
      svgNode('text', { class: 'flow-harness-tag', x: box.x + box.w - 32, y: box.y + 22, 'text-anchor': 'end' }, 'AGENT HARNESS'),
    );
    this.edgeLayer = svgNode('g');
    this.nodeLayer = svgNode('g');
    this.packetLayer = svgNode('g');
    svg.append(this.edgeLayer, this.nodeLayer, this.packetLayer);

    const D = FLOW.davi;
    if (this.routed) {
      svg.insertBefore(svgNode('text', { class: 'flow-ext-tag', x: D.x, y: 18, 'text-anchor': 'middle' }, 'SELECTSTAR · EXTERNAL PLUGIN'), this.edgeLayer);
      this.davi = new FlowNode(this.nodeLayer, { w: D.w, h: D.h, title: 'DAVI · 외부 의사결정 모델', sub: 'davi-route 플러그인', kind: 'flow-davi' }).place(D.x, D.y).state('idle');
      this.daviStatus = svgNode('text', { class: 'flow-ext-status', x: D.x, y: D.y + D.h / 2 + 16, 'text-anchor': 'middle' }, '연결됨 · 호출 대기');
      svg.insertBefore(this.daviStatus, this.edgeLayer);
    } else {
      svg.insertBefore(svgNode('text', { class: 'flow-ext-none', x: D.x, y: D.y + 4, 'text-anchor': 'middle' }, '외부 플러그인 없음'), this.edgeLayer);
    }

    this.question = new FlowNode(this.nodeLayer, { w: 176, h: 40, title: '사용자 질문', sub: '' }).place(260, FLOW.question);
    this.main = new FlowNode(this.nodeLayer, { shape: 'ellipse', w: 168, h: 42, title: '메인 에이전트', sub: '작업 계획', kind: 'flow-main' }).place(260, FLOW.main);
    this.hook = new FlowNode(this.nodeLayer, {
      w: 210, h: 38,
      title: this.routed ? 'PreToolUse · 모델 지정' : 'PreToolUse · 모델 고정',
      sub: this.routed ? 'Agent.model을 Davi 판정으로' : '모든 Agent 호출 → opus',
      kind: 'flow-hook',
    }).place(260, FLOW.hook);
    this.collect = new FlowNode(this.nodeLayer, { shape: 'ellipse', w: 184, h: 42, title: '메인 에이전트', sub: '결과 취합', kind: 'flow-main' }).place(260, FLOW.collect);
    this.final = new FlowNode(this.nodeLayer, { w: 196, h: 38, title: '최종 답변', sub: '', kind: 'flow-final' }).place(260, FLOW.final);
    this.edgeQM = new FlowEdge(this.edgeLayer, this.question, this.main);
    this.edgeCF = new FlowEdge(this.edgeLayer, this.collect, this.final);
    if (this.routed) {
      const hook = this.hook;
      this.edgeDavi = new FlowEdge(this.edgeLayer, hook, this.davi, {
        route: () => {
          const [x, y] = hook.right;
          const right = FLOW.harness.x + FLOW.harness.w - 18;
          return `M${x} ${y} H${right - 10} Q${right} ${y} ${right} ${y - 10} V${FLOW.davi.y + 10} Q${right} ${FLOW.davi.y} ${right - 10} ${FLOW.davi.y} H${FLOW.davi.x + FLOW.davi.w / 2}`;
        },
      });
      this.edgeDavi.state('idle');
    }
    this.calls = [];
    this.chain = Promise.resolve();
    this.generation = (this.generation || 0) + 1;
    this.started = null;
    this.done = false;
    this.meta.textContent = '';
  }

  reset(task) {
    this.build();
    this.question.text('사용자 질문', (task.title || '').slice(0, 18)).state('active');
  }

  queue(step) {
    const generation = this.generation;
    this.chain = this.chain.then(() => (generation === this.generation ? step() : null)).catch(() => null);
  }

  duration(ms) {
    return ms / Math.min(Math.max(this.view.speed, 1), 4);
  }

  async travel(edge, ms, color) {
    edge.state('active');
    const length = edge.path.getTotalLength();
    const dot = svgNode('circle', { class: 'flow-packet', r: 4.5 });
    if (color) dot.dataset.model = color;
    this.packetLayer.append(dot);
    const time = this.duration(ms);
    const start = performance.now();
    await new Promise((resolve) => {
      const step = (now) => {
        const share = Math.min(1, (now - start) / time);
        const eased = share < 0.5 ? 2 * share * share : 1 - (-2 * share + 2) ** 2 / 2;
        const point = edge.path.getPointAtLength(length * (edge.reverse ? 1 - eased : eased));
        dot.setAttribute('cx', point.x);
        dot.setAttribute('cy', point.y);
        if (share < 1) requestAnimationFrame(step);
        else resolve();
      };
      requestAnimationFrame(step);
    });
    dot.remove();
    edge.state('done');
  }

  layoutCalls() {
    const count = this.calls.length;
    const [left, right] = FLOW.spread;
    const width = count > 1 ? Math.min(124, (right - left) / count + 20) : 124;
    this.calls.forEach((call, index) => {
      const x = count === 1 ? 260 : left + ((right - left) * index) / (count - 1);
      call.agent.resize(width).place(x, FLOW.agents);
      call.sub.resize(width).place(x, FLOW.subs);
      call.tools.setAttribute('x', x);
      call.tools.setAttribute('y', FLOW.subs + 34);
    });
    for (const call of this.calls) for (const edge of call.edges) edge.update();
  }

  addCall() {
    const index = this.calls.length + 1;
    const agent = new FlowNode(this.nodeLayer, { w: 124, h: 36, title: `Agent 호출 ${index}`, sub: '메인이 만든 작업' }).state('hidden');
    const sub = new FlowNode(this.nodeLayer, { shape: 'ellipse', w: 124, h: 42, title: '서브에이전트', sub: `${index}`, kind: 'flow-sub' }).state('hidden');
    const tools = svgNode('text', { class: 'flow-tools', 'text-anchor': 'middle' }, '');
    this.nodeLayer.append(tools);
    const call = { index, agent, sub, tools, edges: [] };
    call.edges = [
      new FlowEdge(this.edgeLayer, this.main, agent),
      new FlowEdge(this.edgeLayer, agent, this.hook),
      new FlowEdge(this.edgeLayer, this.hook, sub),
      new FlowEdge(this.edgeLayer, sub, this.collect),
    ];
    this.calls.push(call);
    this.layoutCalls();
    return call;
  }

  sessionStarted(data, now) {
    this.started = now;
    this.main.text('메인 에이전트', `${flowModel(data.main_model) || data.main_model} · 작업 계획`);
    this.collect.text('메인 에이전트', `${flowModel(data.main_model) || data.main_model} · 결과 취합`);
    this.queue(async () => {
      this.main.state('waiting');
      await this.travel(this.edgeQM, 500);
      this.question.state('done');
      this.main.state('active');
    });
  }

  routing(data) {
    const call = this.addCall();
    const model = flowModel(data.model_set || data.model_requested);
    const level = typeof data.level === 'number' ? data.level : null;
    this.queue(async () => {
      call.agent.state('active');
      await this.travel(call.edges[0], 450);
      this.main.state('done');
      call.agent.state('done');
      this.hook.state('active');
      await this.travel(call.edges[1], 380);
      if (this.routed) {
        this.daviStatus.textContent = '과제 수신 · 외부 난이도 판정';
        this.davi.state('active');
        this.edgeDavi.reverse = false;
        await this.travel(this.edgeDavi, 520);
        const verdict = level == null
          ? '판정 실패 → 기본 모델'
          : `판정 ${secondsText(data.latency_s)} · 난이도 ${level.toFixed(2)} → ${MODEL_NAMES[model] || model}`;
        this.daviStatus.textContent = verdict;
        this.edgeDavi.reverse = true;
        await this.travel(this.edgeDavi, 520, model);
        this.davi.state('done');
        this.hook.text('PreToolUse · 모델 지정', `Agent.model = ${model || '-'}`);
      }
      this.hook.state('done');
      call.sub.model = model;
      call.sub.text(MODEL_NAMES[model] || model || '서브에이전트', `서브에이전트 ${call.index} · 실행 중`).state('active');
      call.sub.started = performance.now();
      await this.travel(call.edges[2], 400, model);
    });
    return { model, level };
  }

  subagent(data) {
    const call = this.calls.find((item) => !item.finished) || this.calls[this.calls.length - 1];
    if (!call) return null;
    call.finished = true;
    this.queue(async () => {
      const parts = [];
      if (typeof data.subagent_ms === 'number') parts.push(secondsText(data.subagent_ms / 1000));
      const tokens = data.subagent_output_tokens ?? data.subagent_tokens;
      if (typeof tokens === 'number') parts.push(`${tokens.toLocaleString('ko-KR')}토큰`);
      call.sub.text(null, parts.join(' · ') || `서브에이전트 ${call.index} · 완료`).state('done');
      if (typeof data.subagent_tool_uses === 'number') {
        call.tools.textContent = data.subagent_tool_uses ? `도구 ${data.subagent_tool_uses}회 사용` : '도구 없이 답변';
      }
      this.collect.state('active');
      await this.travel(call.edges[3], 450, flowModel(data.resolved_model));
    });
    return call;
  }

  sessionDone(data) {
    this.done = true;
    this.queue(async () => {
      this.collect.state('done');
      this.final.state('active');
      await this.travel(this.edgeCF, 420);
      const mark = data.correct === true ? '정답 ✓' : data.correct === false ? '오답 ✗' : '채점 없음';
      this.final.text('최종 답변', `${mark} · ${moneyText(data.cost_usd)}`);
      this.final.state(data.correct === false ? 'wrong' : 'done');
    });
    this.meta.textContent = `${secondsText(data.wall_s)} · ${moneyText(data.cost_usd)}`;
  }

  tick(now) {
    if (this.started == null || this.done) return;
    const elapsed = ((now - this.started) / 1000) * (this.view.speed || 1);
    this.meta.textContent = `${secondsText(elapsed)} · 실행 중`;
  }
}

class FlowView {
  constructor(container, caption, footer) {
    this.container = container;
    this.caption = caption;
    this.footer = footer;
    this.panels = {};
    this.speed = 1;
    const loop = (now) => {
      for (const panel of Object.values(this.panels)) panel.tick(now);
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  reset(data) {
    this.speed = data.speed || 1;
    this.container.replaceChildren();
    this.panels = {};
    const order = ['opus', 'routed'];
    const arms = [...data.arms].sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key));
    for (const arm of arms) {
      const panel = new FlowPanel(this.container, arm, this);
      panel.reset(data.task);
      this.panels[arm.key] = panel;
    }
    this.say(`요청 · "${(data.task.question || '').split('\n')[0].slice(0, 80)}"`);
    this.footer.textContent = '비용과 시간은 이 실행의 실제 Claude Code 기록입니다.';
  }

  say(text) {
    this.caption.textContent = text;
  }

  sessionStarted(data) {
    const panel = this.panels[data.arm];
    if (!panel) return;
    panel.sessionStarted(data, performance.now());
    if (data.arm === 'routed') {
      this.say(`메인 에이전트(${flowModel(data.main_model) || data.main_model})가 질문을 읽고, 서브에이전트에게 맡길 작업을 계획합니다.`);
    }
  }

  routing(data) {
    const panel = this.panels[data.arm];
    if (!panel) return;
    const { model, level } = panel.routing(data);
    if (data.arm !== 'routed') return;
    const index = panel.calls.length;
    if (level == null) {
      this.say(`Agent 호출 ${index}: Davi 판정이 실패해 기본 모델(${model || '-'})로 진행합니다.`);
      return;
    }
    this.say(
      `메인 에이전트가 Agent 호출 ${index}을 만들었습니다(작업을 나누는 건 Claude Code 자신입니다). ` +
      `PreToolUse 훅이 이 과제를 밖의 Davi로 보냈고, Davi가 ${secondsText(data.latency_s)} 만에 ` +
      `난이도 ${level.toFixed(2)}로 판정해 ${MODEL_NAMES[model] || model}을 돌려줬습니다.`,
    );
  }

  subagent(data) {
    const panel = this.panels[data.arm];
    if (!panel) return;
    panel.subagent(data);
    const model = MODEL_NAMES[flowModel(data.resolved_model)] || data.resolved_model || '서브에이전트';
    const time = typeof data.subagent_ms === 'number' ? ` ${secondsText(data.subagent_ms / 1000)} 동안` : '';
    this.say(`${panel.arm.label}: ${model} 서브에이전트가${time} 작업을 마쳤고, 메인 에이전트가 결과를 취합합니다.`);
  }

  sessionDone(data) {
    const panel = this.panels[data.arm];
    if (!panel) return;
    panel.sessionDone(data);
    const other = Object.values(this.panels).find((item) => item !== panel);
    const tail = other && !other.done ? ` ${other.arm.label} 쪽은 아직 작업 중입니다.` : '';
    this.say(`${panel.arm.label}: 최종 답변 도착 (${secondsText(data.wall_s)}, ${moneyText(data.cost_usd)}).${tail}`);
  }

  summary(summary, text) {
    this.say(`양쪽 완료 · ${text}`);
    if (typeof summary.opus_cost_usd === 'number' && typeof summary.routed_cost_usd === 'number') {
      const saved = summary.opus_cost_usd - summary.routed_cost_usd;
      const share = summary.opus_cost_usd ? Math.round((saved / summary.opus_cost_usd) * 100) : 0;
      this.footer.textContent =
        `실제 비용 · 기준 ${moneyText(summary.opus_cost_usd)} → Davi ${moneyText(summary.routed_cost_usd)} · ` +
        `${saved >= 0 ? `${share}% 절감` : `${-share}% 증가`} · Claude Code total_cost_usd 기준(정가 추정)`;
    }
  }
}
