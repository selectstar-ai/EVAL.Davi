# Claude Code 서브에이전트 라우터로서의 Davi

[README (English)](../README.md) · [라우팅 실험 기록](routing.md) · [Reference](reference.md) ·
[GitHub](https://github.com/selectstar-ai/EVAL.Davi)

코딩 에이전트는 보통 모든 작업을 한 모델로 돌립니다. 작은 모델은 싸지만 어려운 작업을 놓치고, 강한 모델은 더 많이
풀지만 쉬운 작업에도 같은 값을 냅니다. 작업마다 맞는 모델로 보내면 둘의 장점을 모두 얻을 수 있지만, 라우터 자체가
싸고, 빠르고, 안정적일 때만 그렇습니다.

이 프로젝트는 결정 모델을 그 라우터로 시험합니다. `davi-route`는 Claude Code 플러그인입니다. 서브에이전트가 실행되기
전에 Davi가 작업의 난이도를 매기고, 플러그인이 그 호출 하나에 **haiku**, **sonnet**, **opus** 중 하나를 고릅니다.
같은 작업에서 Davi를 다른 두 판정자(TypeSafe Jev, Claude Haiku 5.5) 및 단일 모델 기준선과 비교해 정확도, 비용,
일관성을 측정했습니다.

> 실험 단계입니다. 모든 hook은 기본 꺼짐이고 fail-open입니다. hook 오류나 느린 판정은 호출을 바꾸지 않고, 판정
> 실패는 저비용 모델로 갑니다(`on_error`). hook은 자기 오류로 Claude Code를 막지 않습니다.

## davi-route는 무엇을 하나요?

hook은 메인 세션의 모델을 바꿀 수 없으므로, Claude Code가 호출마다 모델을 받는 곳에서 라우팅합니다. 바로 각 `Agent`
도구 호출의 `model` 필드입니다. 플러그인은 PreToolUse hook(`updatedInput`)에서 이 값을 바꾸며, 실제 Claude Code
실행에서 동작을 확인했습니다.

```
main agent ── Agent tool call (prompt, model?) ──▶ PreToolUse hook (davi-route)
                                                     │
                                                     ▼
                                    judge: "how hard is this task for a small
                                    model answering from memory?"  levels 1-5
                                                     │  expected level, 0-1
                         ┌───────────────────────────┼───────────────────────────┐
                    level < 0.48               0.48 ≤ level < 0.78           level ≥ 0.78
                         ▼                           ▼                           ▼
                   model = haiku               model = sonnet              model = opus
                         └──────────── updatedInput.model ──▶ subagent runs ─────┘

judge failed ──▶ cheap model (on_error=accept) or strong (on_error=escalate)
judge too slow (wall_budget_seconds) ──▶ the call is left as it was
```

판정자는 다섯 난이도 단계의 확률을 돌려줍니다. 그 기대값을 `0`–`1`로 바꿔 두 기준값과 비교합니다.

| 기대 난이도 | 모델 | 전형적인 작업 |
| --- | --- | --- |
| `0.48` 미만 | haiku | 작은 모델이 기억만으로 답할 수 있음 |
| `0.48` ~ `0.78` | sonnet | 더 많은 지식이나 추론이 필요함 |
| `0.78` 이상 | opus | 강한 모델이 아니면 어려움 |

## 왜 답이 아니라 작업을 판정하나요?

흔한 라우터는 저비용 모델을 먼저 돌리고, 답을 검사해, 틀려 보이면 강한 모델로 올립니다. 그러면 모든 작업에 저비용
실행 한 번, 올릴 때마다 실행 한 번이 더 들고, 검사는 지연이 이미 생긴 뒤에 이루어집니다.

`davi-route`는 답이 나오기 전에 **작업만** 판정합니다. 그래서 라우팅 비용은 판정 호출 한 번이고 모델 실행은 늘지
않습니다. Davi(`/v1/systemone`)는 다섯 단계를 한 번의 호출에 확률로 돌려주며, 보통 1초 이내입니다.

작업만 보는 판정자도 일관되게 틀릴 수 있으므로, 라우팅은 실제로 중요한 것으로 측정합니다. 배정된 모델이 맞혔는지,
그리고 비용이 얼마였는지입니다.

## 실험

사내 데이터셋 K-HLE(어려운 한국어 closed-book QA 174문항)를 썼고, 문항마다 haiku 5.5, sonnet 5.5, opus 5.5를 headless
(`claude -p`)로 돌렸습니다. 전체를 다섯 번 반복해 정책마다 870회 실행했고, 모든 답을 참고답으로 채점했습니다.

판정자마다 모든 작업의 난이도를 매기고, 위의 3단 정책이 그 값을 모델 선택으로 바꿨습니다.

| 판정자 | 난이도를 매기는 방식 |
| --- | --- |
| Davi | 다섯 단계의 선택지 확률, `/v1/systemone` 호출 한 번 |
| TypeSafe Jev | 다섯 단계의 선택지 확률, `/v1/systemone` 호출 한 번 |
| Claude Haiku 5.5 | 말로 답한 단계(logprob 없음), Anthropic Messages API |

라우팅 정책은 모든 작업을 한 모델로 보내는 세 기준선과 비교합니다.

## 결과

### 정확도와 비용

다섯 라운드 합계의 정답 수와 모델 실행 비용입니다.

| 정책 | 정답 (870 중) | 정확도 | 모델 실행 비용 |
| --- | ---: | ---: | ---: |
| haiku만 | 451 | 51.8% | $0.96 |
| sonnet만 | 574 | 66.0% | $9.15 |
| 3단, 판정자 Claude Haiku 5.5 | 587 | 67.5% | $10.18 |
| 3단, 판정자 TypeSafe Jev | 604 | 69.4% | $9.66 |
| **3단, 판정자 Davi** | **619** | **71.1%** | **$10.78** |
| opus만 | 612 | 70.3% | $14.99 |

opus만과 비교하면 Davi 라우팅은 7문항을 더 맞혔고(+0.8%p) **비용은 28% 낮았습니다**($4.21 절감). 5라운드 중 3라운드에서
opus와 같거나 앞섰고, 모든 라운드에서 비용이 더 낮았습니다(opus의 70–75%).

기본 기준값은 같은 데이터로 정했으므로 위 수치는 낙관적입니다. 더 공정하게 비교하려고 문항의 절반에서 기준값을
고르고(opus만 대비 예산 80%), 나머지 절반에서 측정했습니다.

| 판정자 | held-out 정확도 |
| --- | ---: |
| Davi | 69.9% |
| TypeSafe Jev | 66.9% |
| Claude Haiku 5.5 | 63.4% |

모든 라운드에서 Davi의 라우팅이 Jev와 Claude보다 앞섰습니다.

### 일관성

라우터는 같은 작업을 매번 같은 모델로 보내야 합니다. 다섯 라운드에서 tier가 바뀐 문항은 174개 중 Davi **2개**,
Jev 14개, Claude 40개였습니다.

| 판정자 | 라운드 간 tier가 바뀐 문항 |
| --- | ---: |
| Davi | 2 |
| TypeSafe Jev | 14 |
| Claude Haiku 5.5 | 40 |

### Claude Code 안에서

위 수치는 오프라인 재현(replay) 결과입니다. 플러그인 전체 흐름을 확인하려고 후보 43문항을 실제 플러그인(`Agent`
도구, `route_mode=tiers`, 판정자 Davi)으로 네 번씩 돌렸습니다. 24문항은 네 번 모두 같은 모델로 가서 맞혔습니다. 이
24문항은 결과를 보고 고른 것이므로 동작을 보여 줄 뿐 성능 추정치가 아닙니다.

### 한계

- **데이터셋 하나.** 언어 하나, 작업 유형 하나(closed-book QA)입니다. 코딩이나 긴 에이전트 작업은 측정하지
  않았습니다.
- **맞춘 기준값.** 기본값(`0.48` / `0.78`)은 원래 어렵게 만든 K-HLE에서 정했습니다. 자기 작업에 맞게 다시 맞추세요.
  공정한 비교는 반으로 나눈 수치입니다.
- **비용은 추정치.** Claude Code `total_cost_usd`의 정가 기준 모델 실행 추정치이며 청구액이 아닙니다. **판정 호출은
  포함하지 않았습니다.** Davi 가격은 정해지지 않았고, Jev의 $0.042/MTok은 가정입니다.

## 저렴한 라우팅이 바꾸는 것

세션의 서브에이전트 호출 대부분은 가장 강한 모델이 필요하지 않습니다. 작업당 1초 미만의 호출 한 번으로 라우팅할 수
있다면, 모든 곳에 강한 모델 값을 치르지 않아도 됩니다. 다만 절감은 정확도가 유지될 때만 의미가 있으므로, 기준값,
판정자, fail-open 기본값은 쓰기 전에 자기 작업으로 확인해야 합니다.

## 플러그인 실행

요구 사항:

- **Python 3.10 이상** (PATH에 `python3`)
- `Agent` 도구의 PreToolUse `updatedInput`을 지원하는 **Claude Code**. Claude Code 2.1.299와 2.1.300에서
  확인했습니다. `claude --version`으로 버전을 확인하세요.
- 판정자: Davi 서버, 또는 Jev용 TypeSafe API 키 ([판정자로 Jev 쓰기](#판정자로-jev-쓰기))

`datumo` 마켓플레이스(이 저장소)에서 CLI 또는 Claude 앱의 플러그인 설정으로 설치합니다.

```bash
claude plugin marketplace add selectstar-ai/EVAL.Davi
claude plugin install davi-route@datumo
```

그다음 Claude Code(터미널 또는 데스크톱 앱의 **Code 탭**)에서 Davi 서버를 지정합니다. 라우팅이 켜지고(`tiers`
모드) 서버 응답을 확인합니다.

```
/davi-route:setup http://<your-davi-host>:<port> --set log_file=/tmp/route.jsonl
```

`setup`은 skill이므로 말로 요청해도 됩니다("davi-route에 http://host:port의 Davi 서버를 써줘"). 요청이 오면
`python3 .../configure.py` 실행을 승인하세요. 확인이나 끄기도 같은 방식입니다(`--show`, `--off`).

그다음 서브에이전트를 쓰는 작업을 요청합니다("Agent 도구를 써서 ..."). `Agent` 호출마다 모델이 정해지고, hook 호출마다
JSON 한 줄이 기록됩니다(단계, tier, 모델, 지연. 작업 텍스트는 기록하지 않음).

```bash
davi-route hooklog /tmp/route.jsonl
```

플러그인은 hook이 실행되는 곳, 즉 Claude Code와 Cowork에서만 동작합니다. 일반 claude.ai 채팅에서는 hook과
서브에이전트가 실행되지 않으므로 라우팅되지 않습니다.

### 판정자로 Jev 쓰기

Davi는 우리 회사 모델입니다. Davi 서버가 없다면 같은 3단 라우팅을 TypeSafe Jev로 돌릴 수 있습니다. Jev는
`https://api.typesafe.ai/v1/systemone`으로 같은 다섯 단계 질문에 답합니다. 실험에서 Jev 라우팅은 Davi 다음이었고
(69.4% vs 71.1%) 모델 실행 비용은 더 낮았습니다.

1. [TypeSafe](https://docs.typesafe.ai/introduction)에서 API 키를 받아, 키가 채팅을 거치지 않도록 Claude Code를
   시작하기 전에 export합니다.

   ```bash
   export JEV_API_KEY=<your-typesafe-key>     # TYPESAFE_API_KEY도 됨
   claude
   ```

2. Jev로 라우팅을 켭니다. Davi URL이 없으면 `setup`이 라우팅을 스스로 켜지 않으므로 직접 켭니다.

   ```
   /davi-route:setup --judge jev --mode tiers --set route_enabled=true --set log_file=/tmp/route.jsonl
   ```

   skill 없이:

   ```bash
   export DAVI_ROUTE_ROUTE_ENABLED=true
   export DAVI_ROUTE_ROUTE_MODE=tiers
   export DAVI_ROUTE_ROUTE_JUDGE=jev
   ```

3. 판정자를 직접 확인합니다.

   ```bash
   davi-route verify --judge jev --kind tier --task "What is 17*23?" --candidate ""
   ```

기본 기준값(`0.48` / `0.78`)은 Davi에 맞춘 값입니다. Jev는 단계 분포가 다르므로
[자기 작업으로 측정하기](#자기-작업으로-측정하기)로 Jev에 맞게 다시 정하고 `route_tier_low` / `route_tier_high`를
설정하세요.

### setup skill 없이

claude.ai 플러그인 페이지에는 플러그인 옵션 입력란이 없으므로, skill은 옵션을 `~/.claude/davi-route.json`에
씁니다. hook은 호출마다 이 파일을 읽습니다. 같은 옵션을 환경 변수(셸 또는 `settings.json`의 `env`)로도 줄 수
있습니다.

```bash
export DAVI_ROUTE_DAVI_BASE_URL=http://<your-davi-host>:<port>   # POST /v1/systemone
export DAVI_ROUTE_ROUTE_ENABLED=true
export DAVI_ROUTE_ROUTE_MODE=tiers
export DAVI_ROUTE_LOG_FILE=$PWD/route.jsonl
claude --plugin-dir plugins/davi-route
```

옵션 우선순위: `DAVI_ROUTE_<KEY>` 환경 변수, 그다음 `~/.claude/davi-route.json`(`DAVI_ROUTE_CONFIG`로 위치 변경),
그다음 플러그인 옵션(`claude plugin configure`, `CLAUDE_PLUGIN_OPTION_<KEY>`로 읽음).

### 의존성

설치본은 자체 완결형입니다. 라우팅 코드는 `plugins/davi-route/lib`(`davi-route bundle`로 생성)에 들어 있고,
`python3`가 `httpx`를 import하지 못하면 세션 시작 시 `${CLAUDE_PLUGIN_DATA}/pydeps`에 pip로 설치합니다(처음 한 번
네트워크 필요, 옵션 `auto_install_deps`, 기본 켜짐). 실패하면 hook은 `${CLAUDE_PLUGIN_DATA}/venv`를 시도하고, 그것도
없으면 `dependency_missing`을 기록하고 아무것도 바꾸지 않습니다.

개발할 때는 체크아웃에서 플러그인을 불러옵니다(이때는 체크아웃의 `src`가 `lib`보다 우선).

```bash
git clone https://github.com/selectstar-ai/EVAL.Davi && cd EVAL.Davi
pip install -e ".[test,lint]"
claude --plugin-dir plugins/davi-route
```

`src/judge_adapter` 아래를 고친 뒤에는 `davi-route bundle`을 실행하세요(`lib`가 오래되면 테스트가 실패합니다).

### 설정

| 키 | 기본값 | 용도 |
| --- | --- | --- |
| `route_enabled` | `false` | 서브에이전트 라우팅 켜기 |
| `route_mode` | `post` | `tiers`(난이도별 3모델)를 쓰세요. Davi URL로 `setup`하면 설정됩니다. 다른 모드: [예정 기능](#예정-기능) |
| `route_judge` | `davi` | `davi`, `jev`, `claude`, `fake` |
| `davi_base_url` / `davi_api_key` | (없음) | Davi 서버(`POST /v1/systemone`)와 선택 키. `DAVI_BASE_URL` / `DAVI_API_KEY` 환경 변수보다 우선 |
| `typesafe_api_key` | (없음) | Jev 키. 환경 변수 `JEV_API_KEY` 또는 `TYPESAFE_API_KEY`도 됨 |
| `route_cheap_model` / `route_mid_model` / `route_strong_model` | `haiku` / `sonnet` / `opus` | 모델 문자열 |
| `route_tier_low` / `route_tier_high` | `0.48` / `0.78` | 난이도 기준값 (K-HLE에서 Davi로 맞춤) |
| `route_agent_types` | (전체) | 쉼표로 구분한 `subagent_type` 필터 |
| `on_error` | `accept` | 판정 실패 시: `accept`(저비용 모델, fail-open) 또는 `escalate` |
| `wall_budget_seconds` | `25` | hook 호출당 시간 제한. 넘으면 아무것도 바꾸지 않음 |
| `log_file` | (없음) | 모든 hook 호출의 JSONL 로그 |

모든 옵션, 효과, 판정 프롬프트: [routing.md](routing.md).

### 판정자

| 판정자 | 엔드포인트 | 확률 | 비용 추정 |
| --- | --- | --- | --- |
| `davi` | `DAVI_BASE_URL` + `/v1/systemone`(선택 키 `DAVI_API_KEY`). 없으면 RunPod choice endpoint | 다섯 단계의 선택지 확률 | `davi_usd_in_per_mtok`를 넣기 전까지 알 수 없음 |
| `jev` | `https://api.typesafe.ai/v1/systemone`, 키 `JEV_API_KEY` 또는 `TYPESAFE_API_KEY` | 다섯 단계의 선택지 확률 | 사용량 × $0.042/MTok (가정) |
| `claude` | Anthropic Messages, 키 `ANTHROPIC_API_KEY`, 기본 `claude-haiku-5-5`, effort low | 말로 답한 숫자(logprob 없음) | 사용량 × 정가 |
| `fake` | 프로세스 내. 텍스트의 `[[ACCEPT=p]]`가 P를 정함 | 고정 | 0 |

## 자기 작업으로 측정하기

`davi-route` CLI는 어떤 작업 묶음에서든 실험을 재현합니다. 작업을 headless로 돌리고, 판정하고, 라우팅 정책을
오프라인으로 재현합니다.

```bash
# 1) run every task once per model (spends money: needs --yes; --dry-run prints the commands)
davi-route generate --tasks tasks.jsonl --cache runs/x \
    --arm cheap=haiku --arm mid=sonnet --arm strong=opus --yes
# 2) rate each task's difficulty with one or more judges
davi-route judge --tasks tasks.jsonl --cache runs/x --source-arm cheap \
    --judge davi --judge jev --kind tier
# 3) curves, baselines and a frozen threshold on a held-out split
davi-route analyze --cache runs/x --cheap cheap --strong strong
```

`generate`는 환경에 `ANTHROPIC_API_KEY`가 없는 상태에서 실행하세요. 있으면 `claude -p`가 로그인한 플랜 대신 그 키로
과금합니다.

작업 한 줄에는 `id`, `prompt`와 함께 `answer` + `grader`(`contains`, `exact`, `regex`, `number`, `llm`) 또는
`check_cmd`가 들어갑니다. [examples/routing/](../examples/routing/)을 보세요. 다른 명령: `regrade`, `curate`, `demo`,
`hooklog`, `probe`. 자세한 내용: [routing.md](routing.md).

## 프로젝트 구조

- `plugins/davi-route/` — Claude Code 플러그인: hook, setup skill, 번들된 `lib`
- `src/judge_adapter/routing/` — 라우팅 코드 (PEP 8, 79열, 규칙은 [CLAUDE.md](../CLAUDE.md))
  - `config.py` — 환경에서 읽는 옵션과 비밀 값 (`CLAUDE_PLUGIN_OPTION_*`보다 `DAVI_ROUTE_*` 우선)
  - `questions.py` — kind별 판정 질문(`answer`, `completion`, `pre`, `tier`)과 Davi 문구 앙상블
  - `verifiers.py` — `Verifier`(추상) → `FakeVerifier`, `DaviRunPodVerifier`, `HttpVerifier` →
    `SystemOneVerifier`(`DaviVerifier`, `JevVerifier`), `ClaudeVerifier`
  - `policy.py` — `RoutePolicy`(accept / escalate)와 `TierPolicy`(cheap / mid / strong)
  - `hooks.py` — `Settings`, `State`, `RouteHook`(추상) → `AgentPreHook`, `AgentPostHook`, `StopHook`, `main`
  - `grading.py` — `Grader`(추상) → exact, contains, regex, number, command, LLM 채점기
  - `harness.py` — 작업, arm, `RunCache`, `claude -p` 실행, 판정, 재채점
  - `analysis.py` — `ReplayRow`, 기준선, 스윕, 고정 임계값, 시연 표
  - `cli.py`, `probe.py` — `davi-route` 명령과 hook 메커니즘 확인
- `src/judge_adapter/decision/` — 공통 판정 모듈: `ChoiceQuestion` / `NoulQuestion`과 `/v1/systemone` client
  (`DaviClient`, `JevClient`), 엄격한 응답 검증과 다섯 가지 오류 코드
- `src/judge_adapter/reading/` — `davi-read`: 블록 나누기, 페이지 받기 제한, 2단계 판정, 고르기, 명령
- `plugins/davi-route/skills/davi-read/`, `plugins/davi-route/hooks/davi_read.py` — `davi-read` skill과 플러그인의
  실행 파일
- `examples/routing/` — 예시 작업·arm 파일
- `docs/` — 라우팅 실험 기록(한국어), 레거시 플러그인과 레퍼런스 문서
- `tests/test_routing.py`, `tests/test_decision.py`, `tests/test_reading.py` — 테스트

```bash
ruff check && ruff format --check
PYTHONPATH=src pytest -q
```

## 참고 자료

- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
- [Claude Code plugins](https://code.claude.com/docs/en/plugins)
- [TypeSafe introduction (Jev)](https://docs.typesafe.ai/introduction)
- [Jev as a judge for agent evals](https://github.com/danielgshea/jev-as-a-judge)

## 예정 기능

아래 기능은 코드에 있지만 위에서 측정한 3단 라우팅에는 포함되지 않습니다. 실험 단계이고, 공개된 결과가 아직 없으며,
바뀔 수 있습니다.

- **답 검사 모드** (`route_mode`):
  - `post`(현재 기본값): haiku가 먼저 실행되고, 판정자가 그 답을 읽어 틀렸을 가능성이 높으면 opus로 다시 실행합니다.
  - `pre`: 두 모델, 작업에 대한 예/아니오 판정으로 미리 배정합니다.
  - `both`: `pre`와 `post`를 함께 씁니다.
- **tier 답 검사** (`route_tier_check`): `tiers` 모드에서 저비용 모델로 간 답도 판정합니다.
- **Stop 시 완료 검사** (`stop_verify_enabled`): 세션이 끝나기 전에 판정자가 작업이 끝났는지 확인하고, 최대
  `stop_max_blocks`번까지 종료를 막습니다.

| 키 | 기본값 | 용도 |
| --- | --- | --- |
| `route_tier_check` | `false` | `tiers`: 저비용 모델로 간 답도 판정 |
| `route_threshold` / `route_pre_threshold` | `0.5` / `0.5` | P(accept)에 대한 `post` / `pre` 임계값 |
| `route_pin_cheap` | `always` | `post`: `Agent` 호출에 저비용 모델을 언제 지정할지 |
| `stop_verify_enabled` | `false` | Stop 시 완료 검사 (최대 `stop_max_blocks`번 막음) |

`setup`으로 켭니다. 예: `/davi-route:setup --mode post` 또는 `--set stop_verify_enabled=true`. 답 검사 모드를
오프라인으로 측정하려면 `davi-route judge ... --kind answer`로 저비용 모델의 답을 판정하세요.

## 실험 기능: `davi-read`로 페이지 읽기

`davi-read`는 웹 페이지(또는 로컬 HTML·Markdown·텍스트 파일)에서 질문에 답하는 부분만 제목 경로와 함께 원문 그대로
출력합니다. 에이전트가 요약된 글을 돌려주는 WebFetch 대신 부를 수 있습니다. 아직 측정 결과가 없는 새 기능입니다. 답이 더
정확해진다는 근거는 없고, 원문을 그대로 돌려준다는 점만 보장합니다.

`davi-route` 플러그인에 들어 있습니다. 플러그인을 설치하고 Davi 서버 주소를 한 번 알려 주면, Claude Code가 페이지에서
특정 내용을 찾아야 할 때 `davi-read` skill을 씁니다. httpx는 처음 쓸 때 플러그인용으로 설치됩니다.

```text
/plugin install davi-route@datumo
/davi-route:setup http://<davi-host>:<port> --off     # --off: davi-read만 쓰고 라우팅은 끈 채로 둠
```

저장소를 받아 터미널에서 직접 쓸 수도 있습니다.

```bash
pip install -e .            # davi-read 명령 설치
davi-read https://docs.python.org/ko/3/library/functools.html "lru_cache의 maxsize를 None으로 하면 어떻게 되나요?"
davi-read --file docs/routing.md "route_mode는 무엇을 바꾸나요?"
```

- **동작([jevfetch](https://github.com/a2sys-ai/jevfetch)와 같은 방식, 우리 코드):** 페이지를 블록(문단, 목록
  항목, 코드, 표 행)으로 나눕니다. 문서의 주의·참고 상자는 남기고, ":"로 끝나는 문장은 뒤의 목록과 묶습니다. 약
  3,000토큰 단위 창마다 요청 하나로 블록별 `noul` 질문을 보내고, 각 창의 가능성 있는 블록은 앞뒤 글과 함께 하나씩 다시
  묻습니다. 남기는 것은 남김 경계값 이상인 블록, 같은 제목 아래 이웃 중 자기 점수로 통과한 블록, 첫 블록, 남긴 표 행의
  머리글 행입니다. 약 2,000토큰 이하 페이지는 그대로 돌려줍니다. 방식이 같으므로 jevfetch와의 차이는 판정 모델뿐입니다.
- **판정자:** 기본은 Davi이며, 라우팅 훅과 같은 설정(`DAVI_BASE_URL`, `/davi-route:setup`)을 읽습니다. Davi는 자체
  서버에서 돌기 때문에 내부 문서가 밖으로 나가지 않습니다. `--judge jev`를 쓰면 TypeSafe Jev로 판정하고 페이지 글이
  그 API로 전송됩니다.
- **실패해도 원문 전달:** 판정 요청이 실패하면 최대 3번 다시 시도합니다. 그래도 판정하지 못한 부분은 판정 없이
  포함하고, 판정 전체가 실패할 때만 판정 없이 페이지를 출력합니다(상태 `judge_failed`).
- **제한:** 사설 주소로 연결되는 호스트는 허용하지 않으면 거부합니다(`--allow-host`, 사내 위키는 `read_allow_hosts`
  옵션). 5 MB 넘는 페이지는 거부합니다. PDF 등 파일은 저장하고 경로를 알려 줍니다(`# saved:`). 정적 HTML에 글이 거의
  없는 페이지는 headless Chrome이 설치돼 있으면 다시 읽습니다(`--no-render`로 끔). 경계값(0.6 / 0.8 / 0.6)은
  jevfetch의 값이며 Davi에 맞춰 정하지 않았습니다.
- **에이전트 설정:** 플러그인의 [davi-read skill](../plugins/davi-route/skills/davi-read/SKILL.md)이 Claude에게 언제
  쓰고 결과를 어떻게 읽는지 알려 줍니다. 실행되는 코드는 플러그인에 들어 있는 복사본(`plugins/davi-route/lib`)입니다.

Python `functools` 문서(2026-10-27, Davi, 각 1회)에서 영어는 143블록 중 6개(32,464자 중 1,327자), 한국어는
143블록 중 7개(27,358자 중 1,410자)를 남겼고, 둘 다 질문에 답하는 문장을 포함했습니다. 약 6초 걸렸습니다.

## 레거시: `davi` 검색 결과 필터

이 저장소는 `judge-adapter`로 시작했습니다. pairwise LLM 판정 어댑터와, 에이전트가 읽기 전에 Tavily 검색 결과를
선택지 모델로 거르는 Claude Code 플러그인(`davi`)입니다. 바뀐 것 없이 예전처럼 설치됩니다
(`claude plugin install davi@datumo`). [plugin.md](plugin.md), [reference.md](reference.md),
[verification.md](verification.md), [한국어 요약](ko.md)을 보세요.

## 라이선스

Apache-2.0. `LICENSE`와 `NOTICE`를 보세요.
