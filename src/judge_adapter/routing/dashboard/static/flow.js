'use strict';

// The data-flow view: two Claude Code benches (bench.js) driven by the
// run's real events, with a caption that narrates each step.
const IDLE_ARMS = [
  { key: 'opus', label: '서브에이전트 언제나 Opus' },
  { key: 'routed', label: 'Davi 라우팅' },
];
const MODEL_NAMES = { haiku: 'Haiku', sonnet: 'Sonnet', opus: 'Opus' };

function flowModel(name) {
  const text = String(name || '').toLowerCase();
  return ['haiku', 'sonnet', 'opus'].find((key) => text.includes(key)) || null;
}

const secondsText = (value) => (typeof value === 'number' ? `${value < 1 ? value.toFixed(2) : value.toFixed(1)}초` : '-');
const moneyText = (value) => (typeof value === 'number' ? `$${value.toFixed(3)}` : '-');

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
    this.draw(IDLE_ARMS, null);
  }

  draw(arms, task) {
    this.container.replaceChildren();
    this.panels = {};
    const order = ['opus', 'routed'];
    for (const arm of [...arms].sort((a, b) => order.indexOf(a.key) - order.indexOf(b.key))) {
      const panel = new BenchPanel(this.container, arm, this);
      if (task) panel.reset(task);
      else panel.idle();
      this.panels[arm.key] = panel;
    }
  }

  reset(data) {
    this.speed = data.speed || 1;
    this.draw(data.arms, data.task);
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
