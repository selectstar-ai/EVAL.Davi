# Routing experiments (scenario 3) — 실험용, 브랜치 `feat/routing-verifier`

[README](../README.md) · [Verification](verification.md) · [한국어](ko.md)

질문: **"고성능 모델이 필요한 순간"을 누가 더 잘 판단하는가 (Davi / Jev / Claude)?** 기존 `davi` 플러그인(검색 결과
필터)은 그대로 두고, 판정자 인터페이스, Claude Code 개입 hook, 측정 하네스를 별도로 추가했습니다. 모든 기능은 기본
꺼짐이고 fail-open이며, 프롬프트와 임계값은 튜닝하지 않은 초안입니다. 품질 주장은 하지 않습니다.

| 구성 | 위치 | 하는 일 |
|---|---|---|
| Verifier | `src/judge_adapter/routing/verifiers.py` | `verify(task, candidate, rubric, kind) -> P(accept)`. 판정자 `davi`, `jev`, `claude`, `fake` |
| Policy (E) | `routing/policy.py` | P(accept) → accept / escalate, **다음 모델을 문자열로** 반환 (`Route.next_model`) |
| Hooks A / E / B | `routing/hooks.py`, `plugins/davi-route/` | A: Agent 결과 검증, E: 다음 Agent 호출의 model 지정, B: Stop 시 완료 검증 |
| Harness C | `routing/harness.py`, CLI `davi-route` | `claude -p`로 arm별 실행 → 판정 → 오프라인 곡선, 기준선, 고정 임계값 결과 |
| Probes | `routing/probe.py`, `davi-route probe` | 실제 Claude Code에서 hook 메커니즘 확인 (아래 1~4) |

## 판정자

세 판정자 모두 같은 지시문과 같은 두 선택지(`accept` / `escalate`)를 받습니다. kind는 두 가지입니다.

- `answer`: "이 답을 최종 답으로 써도 되는가, 강한 모델이 다시 풀어야 하는가"
- `completion`: "에이전트가 정말 요청을 끝냈는가, 계속해야 하는가"

| judge | 호출 | P의 출처 | 비용 추정 |
|---|---|---|---|
| `davi` | RunPod choice endpoint (기존 `ChoiceEndpointBackend`) | 반환된 확률 | executionTime × `usd_per_second` |
| `jev` | `POST https://api.typesafe.ai/v1/systemone`, Bearer | `answers.route.probabilities.accept` | usage × 단가 (**기본값 $0.042/MTok 입력은 가정**, 설정 권장) |
| `claude` | Anthropic Messages | **말로 답한 0~100 숫자** (API가 logprob을 주지 않음) | usage × 단가 (기본 Haiku 4.5 $1/$5) |
| `fake` | 프로세스 내 | `[[ACCEPT=p]]` 표식, 없으면 `fake_default` | 0 |

`claude` 판정자는 확률이 말로 나오기 때문에 다른 둘보다 거칠고 보정이 덜 됩니다. 비교할 때 이 차이를 함께 적어야
합니다.

단일 호출 확인:

```bash
export TYPESAFE_API_KEY=...            # 또는 DAVI_ROUTE_TYPESAFE_API_KEY
davi-route verify --judge jev --task "What is 17*23?" --candidate "It is 390."
```

## Claude Code 개입 (A / E / B)

플러그인은 `plugins/davi-route`에 있고, `--plugin-dir`로 불러옵니다 (marketplace에는 등록하지 않음).

```bash
claude --plugin-dir plugins/davi-route          # 대화형
claude -p "..." --plugin-dir plugins/davi-route  # headless
```

| hook | 이벤트 | 동작 |
|---|---|---|
| A `post` | PostToolUse, matcher `^(Agent\|Task)$` | `tool_input.prompt`와 서브에이전트 결과를 판정. P < `route_threshold`이면 "model `opus`로 다시 위임하라"를 `additionalContext`(또는 `route_post_mode=block`이면 `decision: block`)로 전달하고, 세션의 다음 모델을 `opus`로 저장 |
| E `pre` | PreToolUse, 같은 matcher | `updatedInput.model`을 설정. 저장된 다음 모델이 있으면 그것을 한 번 적용(에스컬레이션), 없으면 cheap 모델 고정(`route_pin_cheap`) |
| B `stop` | Stop (SubagentStop은 `stop_subagents=true`일 때) | transcript의 마지막 사용자 요청과 `last_assistant_message`로 완료 여부를 판정. P < `stop_threshold`이면 한 번 `decision: block` + 사유. 요청별 카운터와 연속 block 체인 카운터로 루프 방지 |

**E에 대하여**: hook은 메인 세션의 모델을 바꿀 수 없습니다(문서상 PreModelSwitch는 허용·차단만 가능). 대신 다음
모델을 문자열로 넘기는 방식이 두 곳에서 동작합니다.

1. Claude Code 안에서는 **다음 Agent 호출의 `model`**에 적용합니다 (PreToolUse `updatedInput`, 아래 probe 4로 확인).
2. 하네스에서는 다음 `claude -p --model <next_model>`에 적용합니다.

설정은 `DAVI_ROUTE_<KEY>` 환경변수가 userConfig(`CLAUDE_PLUGIN_OPTION_<KEY>`)보다 우선합니다. 하네스 arm은 이 방식으로
설정을 넘깁니다.

| 키 | 기본값 | 설명 |
|---|---|---|
| `route_enabled` | false | A + E |
| `route_judge` | davi | davi / jev / claude / fake |
| `route_threshold` | 0.5 | P(accept)가 이 값보다 작으면 escalate |
| `route_cheap_model` / `route_strong_model` | haiku / opus | 모델 문자열 |
| `route_pin_cheap` | always | always / if_unset / off |
| `route_post_mode` | context | context / block |
| `route_agent_types` | (전부) | 쉼표로 구분한 subagent_type 목록 |
| `stop_verify_enabled` | false | B |
| `stop_judge` | route_judge | |
| `stop_threshold` / `stop_max_blocks` | 0.5 / 1 | |
| `stop_subagents` | false | SubagentStop도 검증 |
| `on_error` | accept | 판정 실패 시 accept(fail-open) 또는 escalate |
| `wall_budget_seconds` | 25 | hook 1회당 시간 상한, 넘으면 아무것도 바꾸지 않음 |
| `log_file` | (없음) | hook 호출마다 JSON 한 줄 (확률, 결정, 모델, 지연, 판정 비용; 본문은 기록하지 않음) |
| `state_dir` | `${CLAUDE_PLUGIN_DATA}/route-state` | 다음 모델과 block 카운터 |

로그 요약: `davi-route hooklog runs/A_davi.hooks.jsonl`

## 메커니즘 확인 결과 (2026-10-14, Claude Code 2.1.299, `claude -p`, 1회씩)

`davi-route probe --plugin-dir plugins/davi-route --out runs/probe`. probe 모드는 판정자를 호출하지 않습니다.

| id | 확인 내용 | 결과 | 근거 |
|---|---|---|---|
| 1a | PostToolUse가 Agent 도구에서 실행됨 | pass | post 기록 1건, `tool_name=Agent` |
| 1b | PostToolUse의 `additionalContext`가 메인 에이전트에 전달됨 | pass | 최종 답에 `DAVI-PROBE-A-<nonce>` 포함 |
| 2 | Agent 호출별 `model: "haiku"`가 받아들여짐 | pass | `tool_response.resolvedModel = claude-haiku-5-5`, subagent 1개 |
| 3a | Stop hook이 사유와 함께 block | pass | `stop_hook_active` false → true, `last_assistant_message` 있음 |
| 3b | block 사유를 따름 | pass | 최종 답 `DAVI-PROBE-B-<nonce>` |
| 3c | 루프 방지: 다음 stop은 허용되고 정상 종료 | pass | allow 1건, exit 0 |
| 4 | PreToolUse `updatedInput`으로 서브에이전트 모델 변경 (opus 요청 → haiku) | pass | `resolvedModel = claude-haiku-5-5`, modelUsage에 opus 없음 |

A + E 전체 흐름(판정자 `fake`, P=0.2 고정) 1회: pin haiku → post escalate(`next_model=opus`) → 메인 에이전트가 재위임 →
pre가 opus 적용 → `resolvedModel = claude-opus-5-5` → `already_strong`으로 accept (루프 없음). 비용 $0.23 (Sonnet 메인).

첫 probe에서 1b가 fail로 나왔는데, 원인은 probe 설계였습니다(agent 실행에서도 Stop probe가 최종 답을 덮어씀). Stop
probe를 agent/pre 실행에서 끈 뒤 pass했습니다.

## 측정 (C, 그리고 A/B를 end-to-end arm으로)

```bash
# 1) 실행 (비용 발생: --yes 필요. --dry-run으로 명령만 확인)
davi-route generate --tasks examples/routing/tasks.example.jsonl --cache runs/x \
    --arm cheap=haiku --arm strong=opus --workers 2 --budget-usd 1 --yes
# A/B arm까지: --arms-file examples/routing/arms.example.json

# 2) cheap 답 판정
davi-route judge --tasks examples/routing/tasks.example.jsonl --cache runs/x --source-arm cheap \
    --judge davi --judge jev --judge claude

# 3) 오프라인 분석 → runs/x/report/{report.md, report.json, curve.csv}
davi-route analyze --cache runs/x --cheap cheap --strong strong --calib-frac 0.3 --tolerance 0.02
```

과제 JSONL: `id`, `prompt`, 그리고 `answer` + `grader`(contains / exact / regex / number / llm) 또는 `check_cmd`(stdin으로
답을 받아 exit 0이면 정답). 선택 필드: `rubric`, `cwd`, `max_turns`. `llm` grader는 이전 HLE 실험처럼 Claude로 채점합니다.

`analyze`가 내는 것:

- **기준선**: always-cheap, always-strong, oracle(cheap이 틀린 것만 escalate)
- **판정자별**:
  - AUROC (P(accept)로 cheap 정답 여부를 가려내는 능력)
  - 임계값 sweep 곡선 (정확도와 과제당 비용, 판정 비용 포함)
  - **같은 escalation 비율의 무작위 escalate** 대비 정확도
- **고정 임계값**: calibration split에서 "always-strong 대비 정확도 손실 ≤ tolerance"를 만족하는 가장 싼 임계값을
  고르고, held-out split에서 보고 (cost vs strong, gap closed, random 대비 차이)
- **arm 요약**: arm별 정확도, 과제당 비용, 성공 1건당 비용(실패 시도 포함), 서브에이전트 수, 사용 모델. A/B arm의
  end-to-end 비교에 씁니다.

오프라인 replay에서 escalate된 과제의 비용은 cheap 실행 + 판정 + 새 strong 실행입니다. 실시간 루프에서
`--model <next_model>`로 다시 돌리는 것과 같은 비용이며, 임계값마다 다시 돌릴 필요가 없습니다.

## 비용 회계와 한계

- 비용은 Claude Code `total_cost_usd`(정가 기준 추정)와 판정자 추정치입니다. 청구액이 아닙니다.
- A/B arm의 비용에는 **메인(호스트) 모델 비용이 포함**됩니다. 판정 효과만 보려면 cheap/strong arm과 `judge`/`analyze`의
  replay를 기준으로 삼으세요.
- Davi(RunPod)는 cold start가 길 수 있습니다(이전 smoke에서 15.3초 타임아웃). hook의 `wall_budget_seconds`를 넘으면
  fail-open으로 아무것도 바꾸지 않으니, 측정할 때는 워커를 warm 상태로 유지하세요.
- `modelUsage`에 haiku가 있는 것만으로는 약한 근거입니다(부가 작업에 작은 모델이 쓰일 수 있음). `resolvedModel`과
  "opus를 요청했는데 opus가 없음"(probe 4)을 근거로 삼았습니다.
- 사용자 수준 설정의 다른 플러그인이 같이 돌지 않도록 arm에 `--setting-sources project,local`을 권장합니다. `--bare`는
  hook과 플러그인을 모두 끄므로 A/B arm에는 쓸 수 없습니다.
- probe와 E2E는 각 1회 관찰입니다. 빈도나 성공률이 아닙니다.
