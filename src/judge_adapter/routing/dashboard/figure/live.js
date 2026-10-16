// The live part of the timing bench. Inlined after the kit and model.mjs.
//
// Messages (window.postMessage from the dashboard, same origin):
//   {type:'reset'}                                  clear both lanes
//   {type:'replay', runs:{routed, opus}, speed?}     lay both runs again
//   {type:'stage', lane, stage, at, model?}          live: a stage starts
//                                                    `at` s into the run
//   {type:'set', lane, run, open?}                   exact segments for a
//                                                    lane (open: still
//                                                    growing)
//   {type:'done', lane, at}                          live: the run ended
//   rate (on stage, set and done): run seconds per real second (replays)
// It posts {type:'figure-size', height} to its parent when its size changes.
//   {type:'theme', theme:'light'|'dark'}
//   {type:'demo'}                                    loop the recordings
// With no message the bench loops the recorded runs.

function start(C) {
  const { P, U, SPAN, X0, LANE_Y, BED_H, BLOCK_HALF, LEAF_HALF, BAR_Z, DIAL_TOP, DIAL_R, TIP_R, PER_TURN } = C;
  const stage = document.querySelector('.iso-stage');
  const svg = stage.querySelector('svg');
  const readout = document.querySelector('[data-readout]');
  const announcer = document.querySelector('.dr-live');
  const params = new URLSearchParams(location.search);
  const still = matchMedia('(prefers-reduced-motion: reduce)');
  const prefersLight = matchMedia('(prefers-color-scheme: light)');
  let themeSet = params.has('theme');
  const setTheme = (theme) => document.body.setAttribute('data-theme', theme === 'light' ? 'light' : 'dark');
  setTheme(params.get('theme') || (prefersLight.matches ? 'light' : 'dark'));
  prefersLight.addEventListener('change', (event) => { if (!themeSet) setTheme(event.matches ? 'light' : 'dark'); });
  if (params.has('embed')) document.body.setAttribute('data-embed', '');

  const BLOCK_TONES = { prep: 'mid', judge: 'hi', handoff: 'lo', subagent: 'hi', wrapup: 'mid' };
  const fmt = (seconds) => (seconds < 1 ? seconds.toFixed(2) : seconds.toFixed(1));
  const lanes = {};
  for (const name of LANES) {
    const g = svg.querySelector('[data-lane="' + name + '"]');
    lanes[name] = {
      name,
      c: LANE_Y[name],
      back: g.querySelector('[data-part="back"]'),
      blocks: g.querySelector('[data-part="blocks"]'),
      plunger: g.querySelector('[data-part="plunger"]'),
      front: g.querySelector('[data-part="front"]'),
      needle: g.querySelector('[data-part="needle"]'),
      segments: [],
      model: null,
      done: false,
      base: 0,
      origin: 0,
      rate: 1,
      t: 0,
      tip: { at: BED_H, rate: 0 },
      drawn: '',
    };
  }

  let mode = 'demo';
  let clock = 0;
  let speed = 4;
  let hold = 0;
  let demoIndex = 0;
  let scrub = null;
  let scrubTarget = null;
  let frame = 0;
  let last = 0;
  let visible = false;
  let leafFlash = 0;

  const extent = (lane) => {
    let end = 0;
    for (const segment of lane.segments) end = Math.max(end, segment.to == null ? lane.t : segment.to);
    return end;
  };
  const totalTime = () => Math.max(...LANES.map((name) => extent(lanes[name])), 0.01);
  const scaleNow = () => U * Math.min(1, SPAN / Math.max(totalTime(), 1));

  function setRun(lane, run, open) {
    lane.segments = segmentsOf(run);
    lane.model = run.model || null;
    if (open && lane.segments.length) lane.segments[lane.segments.length - 1].to = null;
    lane.done = !open;
  }

  function playRuns(runs, rate) {
    for (const name of LANES) {
      const lane = lanes[name];
      if (runs[name]) setRun(lane, runs[name], false);
      else lane.segments = [];
      lane.t = 0;
    }
    const longest = Math.max(...LANES.map((name) => (runs[name] ? totalOf(runs[name]) : 0)), 1);
    speed = rate || Math.max(1, longest / 12);
    clock = 0;
    hold = 0;
    scrub = scrubTarget = null;
    leafFlash = 0;
  }

  function nextDemo() {
    const keys = Object.keys(RUNS);
    const run = RUNS[keys[demoIndex % keys.length]];
    demoIndex += 1;
    playRuns(run, 0);
  }

  // --- drawing --------------------------------------------------------------
  const ticksGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  svg.querySelector('[data-lane]').before(ticksGroup);
  let tickScale = 0;
  function drawTicks(scale) {
    if (Math.abs(scale - tickScale) < 1e-6) return;
    tickScale = scale;
    const step = scale >= 3.2 ? 1 : 5;
    const span = SPAN * U;
    const ticks = topTicks(X0, X0 + Math.floor(span / (scale * step)) * scale * step, scale * step, step === 1 ? 10 : 2, 50.8, 0.6, [2, 3.8], P);
    ticksGroup.innerHTML = lineSvg(ticks.minor, { tone: 'lo' }) + lineSvg(ticks.major, { tone: 'mid' });
  }

  function blockSvg(segment, a, b, c, closed) {
    const w = b - a;
    if (w < 0.05) return '';
    const height = heightOf(segment);
    if (segment.stage === 'judge') {
      const plan = { x: a, y: c - LEAF_HALF, w: Math.max(w, 0.6), d: LEAF_HALF * 2, r: 0.2 };
      return solidSvg(slabOf(plan, BED_H, height, P, 2), { tone: 'hi', lit: true, crease: 'none' });
    }
    const plan = { x: a, y: c - BLOCK_HALF, w, d: BLOCK_HALF * 2, r: Math.min(1, w / 2) };
    let out = solidSvg(slabOf(plan, BED_H, height, P, 3, w > 4 ? 0.4 : 0), { tone: BLOCK_TONES[segment.stage], crease: segment.stage === 'subagent' ? 'lo' : 'faint' });
    if (w > 7) out += lineSvg(planOutline(insetPlan(plan, 1.8), BED_H + height, P, 3), { tone: 'faint' });
    if (closed && w > 26) {
      const label = fmt(segment.to - segment.from) + ' s';
      out += faceTextSvg(topMatrix([a + 3.4, c + 1.3], BED_H + height, P, 'x'), label, { size: 3.2, tone: segment.stage === 'subagent' ? 'mid' : 'lo' });
    }
    return out;
  }

  function heightUnder(lane, v, laid) {
    let top = BED_H;
    const reach = TIP_R / U;
    for (const segment of lane.segments) {
      const to = Math.min(segment.to == null ? laid : segment.to, laid);
      if (to > v - reach && segment.from < v + reach && to > segment.from) top = Math.max(top, BED_H + heightOf(segment));
    }
    return top;
  }

  function drawLane(lane, v, laid, scale) {
    const c = lane.c;
    const x = X0 + v * scale;
    const before = [];
    const ahead = [];
    for (const segment of lane.segments) {
      if (segment.from >= laid) continue;
      const to = Math.min(segment.to == null ? laid : segment.to, laid);
      const piece = blockSvg(segment, X0 + segment.from * scale, X0 + to * scale, c, segment.to != null && segment.to <= laid);
      (segment.from <= v + 1e-6 ? before : ahead).push(piece);
    }
    const blocks = before.join('') + '<g data-part="plunger-slot"></g>' + ahead.join('');
    const tip = lane.tip.at;
    const plunger =
      solidSvg(cylinder(x, c, TIP_R, tip, 1.6, P, 18), { tone: 'hi', crease: 'lo' }) +
      solidSvg(cylinder(x, c, 0.9, tip + 1.6, BAR_Z - tip - 1.6, P, 14), { tone: 'hi', crease: 'none' });
    const key = blocks.length + ':' + blocks.slice(-160);
    if (key !== lane.drawn) {
      lane.blocks.innerHTML = blocks;
      lane.drawn = key;
    }
    lane.blocks.querySelector('[data-part="plunger-slot"]').innerHTML = plunger;
    const move = translateAlong(P, [x, 0, 0]);
    lane.back.setAttribute('transform', move);
    lane.front.setAttribute('transform', move);
    const angle = ((tip - BED_H) / PER_TURN) * Math.PI * 2;
    const r = DIAL_R - 2.2;
    lane.needle.setAttribute('d', lineOnTop([x - Math.sin(angle) * 1.4, c + Math.cos(angle) * 1.4], [x + Math.sin(angle) * r, c - Math.cos(angle) * r], DIAL_TOP + 0.2, P));
  }

  // --- the readout ----------------------------------------------------------
  function stageAt(lane, v) {
    const segment = segmentAt(lane.segments.map((s) => (s.to == null ? { ...s, to: lane.t } : s)), v);
    return segment ? STAGE_LABELS[segment.stage] + (segment.stage === 'subagent' && segment.model ? ' (' + segment.model + ')' : '') : '';
  }

  function judgeShare() {
    const lane = lanes.routed;
    const judge = lane.segments.find((s) => s.stage === 'judge');
    if (!judge || judge.to == null) return null;
    const total = extent(lane);
    return { seconds: judge.to - judge.from, total, share: total ? (judge.to - judge.from) / total : 0 };
  }

  let said = '';
  let phase = '';
  function say(text, phaseText) {
    if (text !== said) {
      readout.textContent = text;
      said = text;
    }
    if (phaseText && phaseText !== phase) {
      phase = phaseText;
      announcer.textContent = phaseText;
    }
  }

  function readoutFor(views) {
    const routed = lanes.routed;
    const opus = lanes.opus;
    if (scrub != null) {
      const parts = LANES.slice().reverse().map((name) => LANE_LABELS[name] + ': ' + (stageAt(lanes[name], scrub) || '끝'));
      return fmt(scrub) + '초 · ' + parts.join(' · ');
    }
    if (leafFlash > 0) {
      const judge = routed.segments.find((s) => s.stage === 'judge');
      if (judge) return 'Davi 판정 ' + fmt((judge.to ?? routed.t) - judge.from) + '초 → ' + (routed.model || '') + ' 배정';
    }
    const finished = LANES.every((name) => !lanes[name].segments.length || views[name] >= extent(lanes[name]) - 1e-3) && (mode !== 'live' || LANES.every((name) => lanes[name].done || !lanes[name].segments.length));
    const shareInfo = judgeShare();
    if (finished && shareInfo) {
      return 'Davi 판정 ' + fmt(shareInfo.seconds) + '초 = 라우팅 전체 ' + fmt(shareInfo.total) + '초의 ' + (shareInfo.share * 100).toFixed(1) + '%';
    }
    const parts = [];
    if (routed.segments.length) parts.push('라우팅 ' + fmt(views.routed) + '초');
    if (opus.segments.length) parts.push('opus ' + fmt(views.opus) + '초');
    return parts.length ? parts.join(' · ') : '대기 중';
  }

  // --- the loop -------------------------------------------------------------
  function tick(now) {
    const dt = last ? Math.min((now - last) / 1000, 1 / 30) : 1 / 60;
    last = now;
    const calm = still.matches;
    const views = {};
    const laidOf = {};
    if (mode === 'live') {
      for (const name of LANES) {
        const lane = lanes[name];
        if (!lane.done && lane.segments.length) lane.t = lane.base + ((now - lane.origin) / 1000) * lane.rate;
        views[name] = lane.t;
        laidOf[name] = lane.t;
      }
    } else {
      if (scrub == null) {
        clock += dt * speed;
        if (calm) clock = 1e9;
      }
      const end = totalTime();
      for (const name of LANES) {
        const lane = lanes[name];
        lane.t = Math.min(clock, extent(lane));
        views[name] = lane.t;
        laidOf[name] = lane.t;
      }
      if (clock >= end) {
        hold += dt;
        if (mode === 'demo' && hold > 3.6 && scrub == null && !calm) nextDemo();
      }
    }
    if (scrubTarget != null) {
      scrub = scrub == null ? scrubTarget : calm ? scrubTarget : scrub + (scrubTarget - scrub) * (1 - Math.exp(-dt / 0.2));
      for (const name of LANES) {
        views[name] = Math.min(scrub, extent(lanes[name]));
        laidOf[name] = extent(lanes[name]);
      }
    }
    const judge = lanes.routed.segments.find((s) => s.stage === 'judge');
    if (judge && views.routed >= judge.from && views.routed < judge.from + 0.6) leafFlash = 1.4;
    leafFlash = Math.max(0, leafFlash - dt);
    const scale = scaleNow();
    drawTicks(scale);
    let moving = mode === 'live' || leafFlash > 0;
    for (const name of LANES) {
      const lane = lanes[name];
      const want = heightUnder(lane, views[name], laidOf[name]);
      const stiff = 520;
      const drag = calm ? 2 * Math.sqrt(stiff) : 30;
      for (let i = 0; i < 4; i += 1) {
        lane.tip.rate += ((want - lane.tip.at) * stiff - lane.tip.rate * drag) * dt / 4;
        lane.tip.at += lane.tip.rate * dt / 4;
      }
      if (calm || (Math.abs(want - lane.tip.at) < 0.005 && Math.abs(lane.tip.rate) < 0.005)) {
        lane.tip.at = want;
        lane.tip.rate = 0;
      } else {
        moving = true;
      }
      drawLane(lane, views[name], laidOf[name], scale);
    }
    const text = readoutFor(views);
    say(text, scrub == null && text.startsWith('Davi 판정') ? text : '');
    stage.setAttribute('aria-valuenow', String(Math.round(views.routed || 0)));
    const running = mode === 'live' || (scrub == null && (clock < totalTime() + 4 || mode === 'demo'));
    const scrubbing = scrubTarget != null && Math.abs((scrub ?? 0) - scrubTarget) > 1e-3;
    frame = visible && (running || moving || scrubbing) ? requestAnimationFrame(tick) : 0;
  }
  const run = () => {
    if (!frame && visible) {
      last = 0;
      frame = requestAnimationFrame(tick);
    }
  };
  new IntersectionObserver((entries) => {
    visible = entries[entries.length - 1].isIntersecting;
    if (visible) run();
  }, { rootMargin: '120px 0px' }).observe(stage);

  // --- input ----------------------------------------------------------------
  let quiet = 0;
  const finishedRun = () => mode !== 'live' || LANES.every((name) => lanes[name].done || !lanes[name].segments.length);
  function steer(t) {
    if (!finishedRun()) return;
    scrubTarget = Math.max(0, Math.min(totalTime(), t));
    clearTimeout(quiet);
    quiet = setTimeout(release, 3600);
    run();
  }
  function release() {
    scrubTarget = null;
    scrub = null;
    hold = 0;
    run();
  }
  stage.addEventListener('pointermove', (event) => {
    const box = svg.getBoundingClientRect();
    const a = iso([X0, 0, 0], P);
    const b = iso([X0 + SPAN * U, 0, 0], P);
    const vb = svg.viewBox.baseVal;
    const sx = ((event.clientX - box.left) / box.width) * vb.width + vb.x;
    const fraction = (sx - a[0]) / (b[0] - a[0]);
    steer((fraction * SPAN * U) / scaleNow());
  });
  stage.addEventListener('pointerleave', () => {
    clearTimeout(quiet);
    quiet = setTimeout(release, 600);
  });
  stage.addEventListener('keydown', (event) => {
    const lane = lanes.routed.segments.length ? lanes.routed : lanes.opus;
    const marks = [0, ...lane.segments.map((s) => (s.from + (s.to ?? lane.t)) / 2), extent(lane)];
    const here = scrubTarget ?? extent(lane);
    let to = null;
    if (event.key === 'ArrowRight' || event.key === 'ArrowUp') to = marks.find((m) => m > here + 1e-3) ?? marks[marks.length - 1];
    if (event.key === 'ArrowLeft' || event.key === 'ArrowDown') to = [...marks].reverse().find((m) => m < here - 1e-3) ?? 0;
    if (event.key === 'Home') to = 0;
    if (event.key === 'End') to = extent(lane);
    if (event.key === 'Escape') { release(); return; }
    if (to == null) return;
    event.preventDefault();
    steer(to);
  });

  // --- messages -------------------------------------------------------------
  addEventListener('message', (event) => {
    if (event.origin !== location.origin) return;
    const m = event.data;
    if (!m || typeof m !== 'object') return;
    const now = performance.now();
    const lane = lanes[m.lane];
    if (lane && Number(m.rate) > 0) lane.rate = Number(m.rate);
    if (m.type === 'theme') {
      themeSet = true;
      setTheme(m.theme);
      return;
    }
    if (m.type === 'demo') {
      mode = 'demo';
      nextDemo();
    } else if (m.type === 'reset') {
      mode = 'live';
      for (const name of LANES) Object.assign(lanes[name], { segments: [], done: false, t: 0, base: 0, origin: now, model: null });
      scrub = scrubTarget = null;
    } else if (m.type === 'replay' && m.runs) {
      mode = 'replay';
      playRuns(m.runs, m.speed);
    } else if (m.type === 'stage' && lane && STAGES.includes(m.stage)) {
      if (mode !== 'live') {
        mode = 'live';
        for (const name of LANES) Object.assign(lanes[name], { segments: [], done: false, t: 0 });
      }
      const at = Number(m.at) || 0;
      const open = lane.segments.find((s) => s.to == null);
      if (open) open.to = Math.max(open.from, at);
      if (m.model) lane.model = m.model;
      lane.segments.push({ stage: m.stage, from: at, to: null, model: lane.model });
      lane.base = at;
      lane.origin = now;
      lane.t = at;
      lane.done = false;
    } else if (m.type === 'set' && lane && m.run) {
      if (mode !== 'live') {
        mode = 'live';
        for (const name of LANES) Object.assign(lanes[name], { segments: [], done: false, t: 0 });
      }
      setRun(lane, m.run, Boolean(m.open));
      const end = totalOf(m.run);
      lane.base = end;
      lane.origin = now;
      lane.t = end;
    } else if (m.type === 'done' && lane) {
      const at = Number(m.at) || lane.t;
      const open = lane.segments.find((s) => s.to == null);
      if (open) open.to = Math.max(open.from, at);
      lane.t = at;
      lane.done = true;
    } else {
      return;
    }
    run();
  });

  if (parent !== window) {
    const sendSize = () => parent.postMessage({ type: 'figure-size', height: Math.ceil(document.documentElement.getBoundingClientRect().height) }, location.origin);
    new ResizeObserver(sendSize).observe(document.body);
  }

  nextDemo();
  drawTicks(scaleNow());
  run();
}
