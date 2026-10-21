# Routing experiments (scenario 3) — 실험용, 브랜치 `feat/routing-verifier`

[README](../README.md) · [Verification](verification.md) · [한국어](ko.md)

질문: **"고성능 모델이 필요한 순간"을 누가 더 잘 판단하는가 (Davi / Jev / Claude)?** 기존 `davi` 플러그인(검색 결과
필터)은 그대로 두고, 판정자 인터페이스, Claude Code 개입 hook, 측정 하네스를 별도로 추가했습니다. 모든 기능은 기본
꺼짐이고 fail-open이며, 프롬프트와 임계값은 튜닝하지 않은 초안입니다. 품질 주장은 하지 않습니다.

| 구성 | 위치 | 하는 일 |
|---|---|---|
| Verifier | `src/judge_adapter/routing/verifiers.py` | `verify(task, candidate, rubric, kind) -> P(accept)`. 판정자 `davi`, `jev`, `claude`, `fake` |
| Policy (E) | `routing/policy.py` | P(accept) → accept / escalate (`RoutePolicy`), 난이도 → cheap / mid / strong (`TierPolicy`). **다음 모델을 문자열로** 반환 |
| Hooks A / E / B | `routing/hooks.py`, `plugins/davi-route/` | A: Agent 결과 검증, E: 다음 Agent 호출의 model 지정, B: Stop 시 완료 검증 |
| Harness C | `routing/harness.py`, `routing/analysis.py`, CLI `davi-route` | `claude -p`로 arm별 실행 → 판정(harness) → 오프라인 곡선, 기준선, 고정 임계값 결과(analysis) |
| 공통 | `routing/config.py`, `routing/questions.py`, `routing/grading.py` | 설정·키 읽기, 판정 질문 문구, 채점기 |
| Probes | `routing/probe.py`, `davi-route probe` | 실제 Claude Code에서 hook 메커니즘 확인 (아래 1~4) |

## 판정자

세 판정자 모두 같은 지시문을 받습니다. kind는 네 가지이고, `tier`만 선택지가 다섯 개입니다.

- `answer`: "이 답을 최종 답으로 써도 되는가, 강한 모델이 다시 풀어야 하는가"
- `completion`: "에이전트가 정말 요청을 끝냈는가, 계속해야 하는가"
- `pre`: 답이 나오기 **전에** 과제만 보고 "저비용 모델이 맞힐 것인가, 바로 강한 모델로 보낼 것인가" (사전 라우팅).
  `candidate` 필드 없이 `task`(와 `rubric`)만 보냅니다. 하네스에서는 `judge --kind pre`로 돌리고 결과는
  `<judge>:pre`(예: `davi:pre`)로 저장됩니다. 사전 라우팅에서 강한 모델로 보낸 과제는 저비용 실행 비용을 내지
  않으므로 `analyze`/`demo`의 비용도 판정 + 강한 모델만으로 계산합니다.

  `pre` 문구는 "작고 빠른 모델이 도구 없이 기억만으로 답하면 맞을까?"입니다. KLEAR 174문항(haiku 5.5 정답 여부)에서
  처음 문구("저비용 모델이 잘 풀까?", Davi AUROC 0.661)보다 이 형태가 haiku가 맞힐 문항과 틀릴 문항을 더 잘
  갈랐습니다. `DAVI_BASE_URL`의 Davi는 같은 질문을 여덟 가지로 바꿔 한 번의 호출로 묻고 logit 평균을 P로 씁니다
  (`PRE_ENSEMBLE`, 가중치 학습 없음, `davi_pre_ensemble=false`로 끔). 결과(2026-10-15, 1회 실행):

  | | AUROC | 예산 50% 정답률 | haiku로 보낸 것 중 정답 | 판정 지연 |
  |---|---|---|---|---|
  | Davi, 처음 문구 | 0.661 | 60.3% | 60.3% | 0.3초 |
  | Davi, 기억 문구 8개 평균 | 0.726 | 62.1% | 64.6% | 0.7초 |
  | Claude Haiku 5.5, 처음 문구 | 0.763 | 62.6% | 65.1% | 3.2초 |
  | Claude Haiku 5.5, 기억 문구 | 0.788 | 63.8% | 67.3% | 3.9초 |
  | (참고) Davi, 답을 보고 판정(`post`) | 0.825 | 65.5% | - | 0.4초 |

  문구는 같은 데이터에서 두 번 골랐으므로 수치는 약간 낙관적일 수 있습니다. 반으로 나눠 한쪽에서 고르고 다른
  쪽에서 잰 값은 0.71~0.72였습니다. 여러 질문을 로지스틱으로 합치는 것(교차검증 0.706)은 평균보다 낫지 않았습니다.
- `tier`: 답이 나오기 전에 과제만 보고 "작은 모델이 도구 없이 기억만으로 풀기에 얼마나 어려운가"를 1~5로
  매깁니다(3단 라우팅용). 판정 결과의 `level`은 다섯 단계 확률로 낸 기대 단계를 0~1로 바꾼 값이고, `p_accept`는
  `1 - level`입니다. Davi(`/v1/systemone`)는 단계별 확률을, Claude는 정수 하나를 답합니다. RunPod choice endpoint는
  두 선택지만 받으므로 `tier`를 지원하지 않습니다(판정 실패로 처리). 하네스 결과는 `<judge>:tier`로 저장되며,
  두 모델용 `analyze`/`demo`에서는 빠집니다.

  KLEAR 174문항, haiku 5.5 / sonnet 5.5 / opus 5.5 각 1회(2026-10-15). sonnet만 쓰면 63.2%, 비용은 opus만의 60%.

  | 3단 배정 (예산은 opus만 대비) | 70% | 80% |
  |---|---|---|
  | Davi `tier`, 반으로 나눠 기준을 고르고 다른 쪽에서 잰 정답률 | 68.7% | 69.9% |
  | Claude Haiku 5.5 `tier`, 같은 방법 | 63.3% | 63.4% |
  | (참고) haiku → Davi 답 판정 → opus (`post`) | 65.6% | 66.9% |
  | (참고) opus만 | 71.8% | 71.8% |

  기본 기준값(`route_tier_low` 0.48, `route_tier_high` 0.78)에서 Davi는 정답률 71.3%, 비용 70%, 문항당 9.7초
  (opus만과 같은 속도)였습니다. 이 기준값은 이 데이터로 정했으므로 낙관적이고, 고난도 문항에 맞춘 값입니다. 쉬운
  질문("대한민국의 수도는?", "2 더하기 2는?")은 0.03~0.09로 나와 cheap 모델로 갑니다. 다른 작업에 쓸 때는
  기준값을 다시 맞추세요.

| judge | 호출 | P의 출처 | 비용 추정 |
|---|---|---|---|
| `davi` | `DAVI_BASE_URL`이 있으면 그 주소의 `POST /v1/systemone` (Jev와 같은 형식, 키는 선택 `DAVI_API_KEY`), 없으면 RunPod choice endpoint (기존 `ChoiceEndpointBackend`) | `answers.route.probabilities.accept` / 반환된 확률 | systemone: `davi_usd_in_per_mtok`를 넣기 전까지 알 수 없음(None) / RunPod: executionTime × `usd_per_second` |
| `jev` | `POST https://api.typesafe.ai/v1/systemone`, Bearer | `answers.route.probabilities.accept` | usage × 단가 (**기본값 $0.042/MTok 입력은 가정**, 설정 권장) |
| `claude` | Anthropic Messages | **말로 답한 0~100 숫자** (API가 logprob을 주지 않음) | usage × 단가 (기본 Haiku 5.5 $0.10/$0.50, effort low) |
| `fake` | 프로세스 내 | `[[ACCEPT=p]]` 표식, 없으면 `fake_default` | 0 |

`claude` 판정자는 확률이 말로 나오기 때문에 다른 둘보다 거칠고 보정이 덜 됩니다. 비교할 때 이 차이를 함께 적어야
합니다. 모델은 `claude_judge_model`(기본 `claude-haiku-5-5`), 생각 깊이는 `claude_judge_effort`(기본 `low`)로
바꿉니다. 5세대 모델은 `temperature`를 받지 않으므로 effort로 보내고, 답의 **마지막** 숫자를 확률로 읽습니다(앞에
짧은 풀이를 쓰는 경우가 있음). Haiku 4.5는 맞는 답과 틀린 답 모두에 약 2%를 답해 판정자로 쓰지 않습니다
(2026-10-15 확인). `llm` grader는 기본 `claude-sonnet-5-5`(`grader_model`로 변경)입니다.

단일 호출 확인:

```bash
export TYPESAFE_API_KEY=...            # 또는 DAVI_ROUTE_TYPESAFE_API_KEY
davi-route verify --judge jev --task "What is 17*23?" --candidate "It is 390."
```

## Claude Code 개입 (A / E / B)

플러그인은 `plugins/davi-route`에 있습니다. `datumo` marketplace에서 `davi-route@datumo`로 설치하거나(CLI, Claude
앱 모두), 체크아웃에서 `--plugin-dir`로 불러옵니다. 설치 후 옵션 화면에서 켜야 동작합니다(전부 기본 꺼짐).

설치본에는 `plugins/davi-route`만 복사되므로 플러그인이 스스로 필요한 것을 갖고 있습니다.

- 라우팅 코드: `plugins/davi-route/lib`에 hook이 쓰는 `judge_adapter` 모듈의 사본이 들어 있습니다(`davi-route bundle`로
  생성, 직접 고치지 않음, 낡으면 테스트 실패). hook은 `DAVI_ROUTE_SRC` → 위쪽 체크아웃의 `src`(개발용) → `lib` →
  marketplace 레포 사본 순서로 찾습니다.
- `httpx`: 세션 시작(SessionStart) 때 기능이 켜져 있고 `python3`가 `httpx`를 못 불러오면
  `pip install --target ${CLAUDE_PLUGIN_DATA}/pydeps "httpx>=0.24,<1"`을 한 번 실행합니다(네트워크 필요). 옵션
  `auto_install_deps`(기본 켜짐)로 끕니다. 설치에 실패하면 hook은 아무것도 바꾸지 않고 통과합니다.

```bash
claude --plugin-dir plugins/davi-route          # 대화형
claude -p "..." --plugin-dir plugins/davi-route  # headless
```

| hook | 이벤트 | 동작 |
|---|---|---|
| A `post` | PostToolUse, matcher `^(Agent\|Task)$` | (`route_mode`가 `post`/`both`일 때) `tool_input.prompt`와 서브에이전트 결과를 판정. 이미 강한 모델로 실행된 결과는 판정하지 않음. P < `route_threshold`이면 "model `opus`로 다시 위임하라"를 `additionalContext`(또는 `route_post_mode=block`이면 `decision: block`)로 전달하고, 세션의 다음 모델을 `opus`로 저장 |
| E `pre` | PreToolUse, 같은 matcher | `updatedInput.model`을 설정. 저장된 다음 모델이 있으면 그것을 한 번 적용(에스컬레이션). 없으면 `route_mode`가 `pre`/`both`일 때 과제만 판정(kind `pre`)해 P < `route_pre_threshold`이면 strong, 아니면 cheap 모델. `tiers`일 때는 난이도(kind `tier`)로 cheap / mid / strong 중 하나. `post`일 때는 cheap 모델 고정(`route_pin_cheap`) |
| B `stop` | Stop (SubagentStop은 `stop_subagents=true`일 때) | transcript의 마지막 사용자 요청과 `last_assistant_message`로 완료 여부를 판정. P < `stop_threshold`이면 한 번 `decision: block` + 사유. 요청별 카운터와 연속 block 체인 카운터로 루프 방지 |

**E에 대하여**: hook은 메인 세션의 모델을 바꿀 수 없습니다(문서상 PreModelSwitch는 허용·차단만 가능). 대신 다음
모델을 문자열로 넘기는 방식이 두 곳에서 동작합니다.

1. Claude Code 안에서는 **다음 Agent 호출의 `model`**에 적용합니다 (PreToolUse `updatedInput`, 아래 probe 4로 확인).
2. 하네스에서는 다음 `claude -p --model <next_model>`에 적용합니다.

설정은 `DAVI_ROUTE_<KEY>` 환경변수가 userConfig(`CLAUDE_PLUGIN_OPTION_<KEY>`)보다 우선합니다. 하네스 arm은 이 방식으로
설정을 넘깁니다.

| 키 | 기본값 | 설명 |
|---|---|---|
| `route_enabled` | false | A + E 켜기/끄기 |
| `route_mode` | post | `post`: cheap 먼저, 답을 판정해 필요하면 strong으로 재실행 / `pre`: 실행 전 과제만 판정해 cheap 또는 strong으로 바로 보냄(재실행 없음, 답을 보지 못함) / `both`: `pre`로 보낸 뒤 cheap으로 간 것만 답을 다시 판정 / `tiers`: 실행 전 난이도로 cheap / mid / strong 중 하나로 보냄 |
| `route_mid_model` | sonnet | (`tiers`) 중간 난이도에 쓸 모델 |
| `route_tier_low` / `route_tier_high` | 0.48 / 0.78 | (`tiers`) 난이도가 low 미만이면 cheap, high 이상이면 strong, 그 사이는 mid. KLEAR로 정한 값 |
| `route_tier_check` | false | (`tiers`) cheap 모델로 간 답도 판정해 `route_threshold` 미만이면 strong으로 재실행 |
| `route_judge` | davi | davi / jev / claude / fake |
| `davi_base_url` / `davi_api_key` | (없음) | Davi 서버 주소(`POST /v1/systemone`)와 선택 키. Claude 앱에서는 플러그인 옵션 화면에서 입력하고, 넣으면 `DAVI_BASE_URL` / `DAVI_API_KEY` 환경변수보다 우선합니다 |
| `route_threshold` | 0.5 | (`post`/`both`) 답의 P(accept)가 이 값보다 작으면 escalate |
| `route_pre_threshold` | 0.5 | (`pre`/`both`) 과제만 본 P(저비용 모델 성공)가 이 값보다 작으면 strong 모델로 바로 보냄 |
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

채점: regex / contains / exact / number는 마크다운 강조(`**B**`)를 지운 뒤 비교하고, number는 한국어 단위 금액
(`21억 3,000만`)도 읽습니다. grader를 고쳤거나 llm grader 키를 나중에 넣었으면 `davi-route regrade --tasks T.jsonl
--cache runs/x`로 캐시된 답을 다시 채점합니다.

## 시연용 (고정 임계값, 선별)

```bash
# 판정자 결과에서 "우리 판정자는 맞는 경로, 다른 판정자는 틀린 경로"인 과제를 고름 → 과제 JSONL + 선별 기준 JSON
davi-route curate --tasks T.jsonl --cache runs/x --ours davi --n 20 --accept-frac 0.5 --threshold 0.5 \
    --out runs/x/demo.tasks.jsonl
# 과제별로 판정자마다 P(accept) → 경로 → 최종 정답 여부 → 비용, 마지막에 정확도 / 고성능 호출 비율 / 총비용 요약
davi-route demo --cache runs/x --tasks runs/x/demo.tasks.jsonl --threshold 0.5
```

- 맞는 경로는 사후 기준입니다: cheap이 맞았으면 accept, cheap이 틀리고 strong이 맞았으면 escalate. 둘 다 틀린 과제는
  어느 경로든 결과가 같아 선별에서 뺍니다.
- `--require all`(기본)은 다른 판정자가 **모두** 틀린 과제만, `any`는 하나라도 틀린 과제를 고릅니다. `--min-margin`은
  우리 판정자의 P가 임계값에서 그만큼 떨어진 과제만 남깁니다(재실행 시 뒤집힐 가능성을 줄임).
- `curate`는 **결과를 보고 고른 것**입니다. 그 숫자는 선별된 과제에 대한 것이지 일반 성능 추정이 아니므로, 시연에서는
  "선별 사례"로 표시하세요. 선별 기준은 `*.selection.json`에 남습니다.

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
