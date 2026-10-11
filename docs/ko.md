# 한국어 요약

[README (영문)](../README.md) · [상세 레퍼런스](reference.md) · [Claude Code plugin](plugin.md) · [검증 현황](verification.md)

아래는 0.4.0 기준 전체 한국어 요약이다. 영문 상세: 컨텍스트 필터·pairwise judge·설정과 한계는 [reference.md](reference.md), plugin 설치·설정·검색 전 옵션·기능 켜고 끄기는 [plugin.md](plugin.md), 실제 실행·fake 테스트·미검증 구분은 [verification.md](verification.md).

- **무엇인가:** 두 기능을 담은 패키지다. (1) **컨텍스트 필터**: 검색 문서마다 선택지 모델에 한 번 물어 P(keep)을
  받고 기준 이상만 넘겨 agent의 **입력 토큰을 줄인다**. (2) **pairwise judge 어댑터**: 질문 하나(1~2 turn)와 응답
  두 개를 넣으면 `A`/`B`/`tie`/명시 `error`와 호출 기록을 남긴다. JSONL CLI(`judge-adapter`), Python API(`import
  judge_adapter`), 선택 FastChat binding(`judge-adapter-fastchat`, extra `fastchat`), Claude Code plugin(`davi`)을
  제공한다. 배포 이름 `judge-adapter`, 라이선스 Apache-2.0.
- **설치:** `pip install "judge-adapter @ git+https://github.com/selectstar-ai/EVAL.Davi"`. 코어 의존성은 `httpx`
  하나다. FastChat은 `[fastchat]` extra이며 commit `587d5cfa`로 고정하고 torch 등 무거운 의존성을 함께 설치한다.
- **결과(단일 실행, 보장 아님):** HLE 영어 텍스트 90문항, 웹 검색을 쓰는 GPT-5.2 agent, 검색당 5문서, preset
  `v1-thr0.3-dev`. 세 설정이 모두 끝난 86문항에서 agent 입력 토큰은 top-3 대비 −31.4%, 5문서 전부 대비 −44.9%였다.
  이전 README의 "90문항 전체 합 기준 약 44% 감소(top-3 대비)"는 중단된 궤적 4개(그중 3개가 필터 설정)를 0토큰으로 센
  과대 수치였으므로 이 86문항 수치로 바로잡는다. 정답 수(Haiku가 참고답 기준 채점): HLE 90문항 필터 28 / top-3 27 /
  5문서 31, 이전 한국어 25문항 실행 필터 14 / top-3 10 / 전체 12. 두 실행 모두 필터가 top-3 이상(+1/90, +4/25)이었지만
  통계적으로 확립된 차이는 아니며, 5문서 전부와는 엇갈렸다(−3/90, +2/25). 필터 판단은 입력 전용이고, Jev 방식 정가
  입력 1M 토큰당 $0.042(가정; 이 패키지가 그 모델을 제공하지 않음) 기준 문항당 약 $0.0003이다. 총비용은 생성 출력에
  따라 달랐다: HLE 86문항에서 필터 설정은 top-3 대비 출력 토큰 +6.7%, 총비용 +4.5%였다. 총비용 절감은 확립되지
  않았다. 같은 HLE 실행에서 72회 검색 중 25회는 5문서가 모두 drop되었다(그 실행의 검색 단위 관찰이며 일반 drop률·실패율이
  아님). 용도는 opt-in 입력 축소 도구이며 품질 향상·총비용 절감을 주장하지 않는다.
- **Claude Code plugin `davi`:** `claude plugin marketplace add selectstar-ai/EVAL.Davi` 후 `claude plugin install
  davi@judge-adapter`. 대상은 **Tavily MCP의 `tavily_search` 하나**다(`tavily-mcp` npm 0.2.22, 소스 commit `1c1d54c`에서
  출력 형식 확인). 서버 별칭과 상관없이 `^mcp__[A-Za-z0-9_-]+__tavily_search$`로만 매칭하고, 다른 도구(다른 Tavily 도구·
  다른 검색 MCP·읽기/쓰기 도구·내장 WebSearch)에는 전혀 개입하지 않는다(결과 필터 기준; 검색 전 선택 옵션은 아래 항목). **Claude Code 내장 WebSearch는 필터하지
  않는다**: 한 번의 실제 실행에서 결과마다 제목·URL만 있고 본문이 없었다(점수 매길 텍스트 없음). Tavily 서버 추가는 버전을
  고정한다: `claude mcp add --transport stdio --env TAVILY_API_KEY="$TAVILY_API_KEY" tavily -- npx -y tavily-mcp@0.2.22`
  (Tavily API 키는 본인 것, 검색 비용은 Tavily 크레딧 과금, 키가 없으면 keyless 모드로 검색은 동작). hook은 검색어
  (`tool_input.query`)를 질문으로 결과의 `Content` 스니펫마다 필터를 한 번씩 호출하고, drop된 결과 블록만 텍스트에서 잘라 낸
  **같은 모양·같은 형식**으로 `updatedToolOutput`을 돌려주며(`updatedMCPToolOutput`은 쓰지 않음) `context filter (...)
  kept k/n results`를 덧붙인다. 헤더·`Answer:` 요약·이미지 목록·다른 블록은 그대로 두고 점수에 넣지 않으며, 새 텍스트를
  만들지 않는다. 한계: `Answer:` 요약은 그대로 남으므로 drop된 문서의 정보가 거기 남을 수 있다. `isError: true`이거나
  `structuredContent`가 있으면 그대로 통과시킨다. 하나도 drop되지 않으면 아무것도 출력하지 않는다. 모두 drop되면 기본값
  (`keep_original`)은 원래 결과를 그대로 두고 0개를 남겼다는 한 줄을 덧붙인다(`drop_all`로 바꿀 수 있음). 알 수 없는
  형식(서버 오류 메시지 포함)·키/endpoint/단가 누락·설정 오류·예외·httpx 없음·호출당 시간 예산(기본 20초) 초과에서는
  아무것도 출력하지 않아 원래 결과가 그대로 쓰인다(fail-open). 설정은 `userConfig`(RunPod 키와 endpoint ID는 sensitive,
  단가, enable, preset, 동시성, 최대 문서 수, 시간 예산, 전부 drop 시 동작, `mcp_tools` 재정의(`name=tavily|off`), 로그
  파일)이며 hook에는 `CLAUDE_PLUGIN_OPTION_<KEY>`로 전달되고, 패키지 변수 `RUNPOD_API_KEY`·
  `JUDGE_ADAPTER_CHOICE_ENDPOINT_ID`와 `JUDGE_ADAPTER_HOOK_<KEY>`를 대체값으로 읽는다. 결과 하나당 필터 호출 1회라서
  검색마다 지연이 생긴다. Jev 호환 선택지 endpoint(예: RunPod serverless에 직접 띄운 모델)가 필요하다. 로그는 개수·
  bytes·상태·형식·응답 구조(키 이름과 형식)만 남기고 검색어·본문·제목·URL·키·endpoint ID는 남기지 않는다. 실제
  Tavily 검색 1회(결과 5개)에서 hook 실행과 필터 호출 5회·정상 응답 5회를 확인했으나 5개 모두 keep되어 결과는 바뀌지
  않았다. drop된 결과를 빼고 줄어든 결과가 모델에 전달되는 경로는 fake 테스트로만 확인했고 실제 실행으로는 미검증이다.
  Claude Code가 MCP 출력을 hook에 넘기는 모양은 문서화되어 있지 않다. 선택 사항으로, 본인 `settings.json`에서 내장
  WebSearch를 deny하면 웹 검색을 Tavily로 제한한 구성에서 지원 응답에 hook이 적용된다. 이는 WebSearch만 막을 뿐
  WebFetch·Bash·다른 MCP·미지원/오류 응답·fail-open 경우는 필터를 거치지 않으며, plugin이 권한을 자동으로 바꾸지 않는다.
- **선택 기능: 검색 전 옵션(실험적, 기본 꺼짐):** PreToolUse hook이 내장 `WebSearch`와 Tavily `tavily_search` 두 도구에만
  붙는다. 둘 다 꺼져 있으면 launcher가 stdin도 읽지 않고 바로 끝난다(키·네트워크·상태 파일·출력 없음; 판단을 위해 환경변수와
  작은 toggle 파일만 읽는다; 측정 시 빈 Python 실행 대비 약 0.3~0.5 ms 추가, Python 시작 자체 약 14 ms는 검색마다, 그리고
  이번 변경부터 사용자 프롬프트마다 든다). 켜면 검색마다 옵션당 endpoint 호출 1회가
  추가되고 검색은 그만큼 기다린다. endpoint로 보내는 것은 **검색어뿐**이다(domain preset은 사용자가 정한 preset 이름·
  설명·도메인 목록 포함). PreToolUse 입력에는 사용자 메시지 필드가 없고 transcript 파일은 읽지 않으므로
  `gate_use_last_user_message`는 제공하지 않는다. 프롬프트는 아래 UserPromptSubmit hook이 로컬에서 명시 요청 여부만
  확인하며 전송·저장·로그하지 않는다. (1) **search gate**(기본 꺼진 개발용 옵션; 실제 유용성과 사용자 의도 보존은 미검증, transcript 접근·프롬프트/임계값 튜닝 없음): needed / not needed / cannot tell 중 P(not needed)가
  `search_gate_threshold`(기본 0.85, 사전 고정·미보정) 이상일 때만 deny하고, Claude에게는 고정 안내문(검증된 판단이 아니며
  필요하면 같은 검색어로 다시 시도하라는 내용)만 보인다. 최신성·검증·특정 출처 키워드가 있는 검색어와 Tavily 날짜 필드·이미
  지정된 도메인 필드는 호출 없이 통과시키지만, 이는 키워드 휴리스틱일 뿐 사용자 의도를 보존하지 않는다. **검색어만으로는
  사용자의 명시적 검색 요청을 알 수 없으므로** 필요한 검색을 막을 수 있다. 실제로 2026-10-11 실제 smoke에서 gate를 켠 채
  "Using web search, confirm how many days there are in a week. Answer in one sentence."를 입력하자 WebSearch
  "how many days in a week"가 P(not needed)=0.999로 deny되었다. **사용자가 명시적으로 요청한 검색을 막은 의미상 실패이며
  릴리스 차단 사유다.** 그래서 **UserPromptSubmit hook**을 추가했다(입력에 `prompt`·`session_id`·`prompt_id`가 있음, hooks
  문서). gate가 켜져 있을 때만 프롬프트를 고정 패턴 목록(영어·한국어, 대소문자 무시: search 계열 전부, look up, google,
  online/internet, cite/source/reference/link, verify/confirm, latest/current/today/now/news/연도, 웹 검색·검색해(서/줘)·
  서치·구글·인터넷·찾아봐·찾아줘·출처·근거·링크·확인·검증·최신·최근·오늘·현재·지금·뉴스)과 로컬에서 대조하고, 세션별 작은
  상태 파일(시각·명시 여부·해시 접두만, 프롬프트 원문 없음, 0600, 원자적 교체)을 남긴다. stdout에는 아무것도 쓰지 않고
  (plain stdout은 Claude 문맥에 추가됨) 프롬프트를 막지 않으며 네트워크 호출이 없다. gate는 이 상태를 먼저 읽어 **명시
  요청이면 `explicit_request_keep`: endpoint 호출 0, 출력 없음**(원래 권한 흐름, 명시적 allow 아님). 30분 이내의 "명시 요청
  없음" 상태가 있을 때만 endpoint에 묻고, **상태 없음(예: 방금 켬)·읽기 오류·다른 세션·만료·마지막 toggle 변경 이전 상태·
  `prompt_id` 불일치에서는 호출 없이 검색을 유지**한다. 갱신 규칙(보수적): 신호가 있으면 플래그를 세운다. 유효한 플래그가
  있을 때 신호 없는 프롬프트는 길이와 상관없이 플래그를 만료(명시 프롬프트로부터 30분, 연장 없음)까지 유지한다("continue",
  "계속", "그걸로", "같은 방식으로 하루는 몇 시간인지도" 등). **새 작업이 분명할 때만**, 즉 고정 표지(`NEW_TASK_PREFIXES`:
  "new task", "new question", "different topic", 새 작업·새 질문·다른 질문·다른 주제·주제를 바꿔 등)로 시작할 때만 지운다.
  애매하면 유지한다. 세션별 파일 lock 아래에서 읽고-판단하고-쓰므로 동시 프롬프트가 보지 못한 플래그를 지우지 못하며,
  lock·쓰기 실패 시 상태를 지워(신호는 가능하면 기록) gate가 유지하게 한다. 공식 turn id가 PreToolUse에 따로 없으므로 양쪽에 `prompt_id`가 있을 때만 일치를 요구하고,
  아니면 세션+시간 창으로 맞춘다(정확한 turn 단위가 아님). **알려진 A1 수정과 일반 의도 보존을 구분한다**: 관찰된 A1
  표현 계열("using web search", "search for", "검색해서", "출처" 등 목록의 표현)은 보호되지만, 일반 의도 보존은 **해결되지
  않았다**. 목록에 없는 표현은 여전히 deny될 수 있고, 키워드 미검출은 검색 의도가 없다는 증거가 아니며, 알려진 실패 문구를
  고친 것은 일반 해결이 아니고 **기본 채택의 근거가 아니다**.
  **이 수정은 fake 테스트로만 확인했다.** gate는 계속 기본 꺼짐·실험 기능이다. 같은 세션·같은 도구·같은 정규화 검색어는 deny 후
  10분간 **아무 출력 없이** 통과시켜 완화한다(SHA-256 해시와 시각만 저장, 검색어 원문 저장 없음, 다른 세션은 영향 없음).
  최종 확인과 기록은 키별 lock 파일의 `fcntl.flock` 아래 짧은 프로세스 간 임계 구역에서 수행하며(endpoint 호출 중에는
  lock을 잡지 않음) 같은 키 deny는 최대 1회다. 만료 기록 정리도 같은 lock 안에서 다시 확인해 새 기록을 지우지 않는다.
  세션 id 없음·lock/IO 문제·`fcntl` 없음(Windows)에서는 deny하지 않는다. (2) **domain presets**: 사용자가 정한 고정 preset 목록(없으면 예시 `docs`·`academic`; 모든
  질의에 맞는 도메인이 아님) 중 하나를 고르며 `none`(제한 없음)은 항상 있다. 모델이 쓴 도메인은 실행하지 않는다.
  include → `allowed_domains`/`include_domains`, exclude → `blocked_domains`/`exclude_domains`로 그 필드 하나만 더한
  **원래 입력 전체**를 `updatedInput`으로 돌려준다. 이미 도메인 필드가 있거나(빈 목록 포함) `none`이 이기거나 최고 확률이
  `domain_min_confidence`(기본 0.6) 미만이면 바꾸지 않는다. hook은 `"allow"`를 절대 내지 않는다. allow 모드도 없다. 기본
  `domain_apply_mode=ask`는 문서화된 `"ask"`+`updatedInput`이라 사용자가 바뀐 검색을 확인한다. **headless(`-p`) 실행에서는
  답할 사람이 없어 `ask`가 검색을 막을 수 있으므로** 무인 실행에서는 domain preset을 끄는 것이 맞다. `input_only`는 결정
  없이 `updatedInput`만 주는 실험 모드이며 문서에 없는 조합이라 실제 효과는 미검증이다. 자동으로 `input_only`로 바꾸지
  않는다.
  순서는 gate 먼저, deny면 domain 생략이며 시간 예산·호출 상한·비용 추정 상한을 공유한다. 오류·시간 초과·설정 누락/오류·
  동률 답·예외에서는 아무것도 출력하지 않는다(fail-open). 로그는 상태·호출 수·시간·반올림 확률·고른 preset 이름만 남기고
  검색어·URL·도메인 목록·세션 id·키는 남기지 않는다. domain preset은 **fake 테스트로만 확인했고**, gate는 위 실제 smoke
  1회(실패) 외에는 fake 테스트뿐이다. **품질 주장 없음.**
- **기능 켜고 끄기:** Claude Code 안에서 인자 없는 고정 명령 `/davi:status`, `/davi:filter-on`, `/davi:filter-off`,
  `/davi:gate-on`, `/davi:gate-off`, `/davi:domain-on`, `/davi:domain-off`, `/davi:domain-mode-ask`,
  `/davi:domain-mode-input-only`, 셸에서는 `python3 <plugin>/hooks/davi_toggle.py filter|gate|domain on|off`,
  `domain-mode ask|input_only`, `status`, `reset`(엄격한 인자 검사).
  세 기능은 서로 독립이다(filter off가 gate/domain을 끄지 않음). 파일은 `${CLAUDE_PLUGIN_DATA}/toggles.json` 하나(0600,
  마켓플레이스 설치는 `~/.claude/plugins/data/davi-judge-adapter/`, `--plugin-dir` 세션은 다른 디렉터리이므로
  `--data-dir` 지정)이며 **세션별이 아니라 설치 전체 공유**라서 이미 실행 중인 다른 세션에도 다음 hook 호출부터 적용된다.
  우선순위: (1) userConfig `enabled=false`는 세 기능을 모두 끄고 어떤 toggle도 이를 넘지 못한다, (2) 그 외에는 toggle
  파일이 정한 키, (3) 나머지는 userConfig/환경변수. 파일이 없으면 toggle 없음, 손상·읽기 불가면 hook은 무시하고 userConfig를
  쓰며 `status`가 경고하고 CLI는 `reset` 전까지 수정을 거부한다. 쓰기는 엄격한 인자 검사, `fcntl.flock` 아래 읽기-수정-쓰기
  (동시 변경 보존), 0600 임시 파일의 원자적 교체 후 재확인이며, 실패는 성공으로 보고하지 않는다(`FAILED`, 종료 코드 1).
  키·endpoint ID·단가는 toggle 파일에 넣지 않는다. `status`는 비밀값을 출력하지 않으며 키·endpoint는 환경변수에 보일 때만
  set/missing, 아니면 "not visible here"(secure storage에 있고 hook 프로세스에만 전달)로 표시하고, 단가 누락·잘못된
  preset·EXAMPLE preset 사용·httpx 없음 같은 실행 불가 상태를 경고한다. slash command는 plugin `commands/*.md`이며 문서화된
  `` !`command` `` 주입으로 plugin `bin/davi-toggle`을 실행한 뒤 **Claude가 그 출력을 전달하므로 명령마다 모델 turn 1회를
  쓴다**(내장 명령이 아니고 무료가 아님). `disable-model-invocation: true`, `allowed-tools: Bash(davi-toggle *)`로 권한을
  최소화했다. 사용자 문자열은 셸 코드에 절대 들어가지 않는다: 각 명령 파일의 주입 줄은 상수이고 유일한 확장은 Claude Code가
  치환하는 데이터 경로다(이전 초안의 `$ARGUMENTS`는 셸이 사용자 텍스트를 먼저 해석하는 문제가 있어 제거했고, 인용으로는
  해결되지 않는다). 악성 인자로 렌더링·실행해도 sentinel 파일이 생기지 않음을 로컬 테스트로 확인했다(같은 테스트가 이전
  형식에서는 sentinel을 만든다). 명령 출력과 hook 로그에 같은 `data dir id`(경로 해시)가 남는다. 재검증 smoke(2026-10-11,
  `--plugin-dir`, headless `claude -p`)에서 `status`·`gate-on`·`gate-off`·`domain-on`·`domain-off` 5개가 실제로 동작함을
  관측했다(토글 파일·data dir id 대조). 나머지 명령과 대화형 사용은 실행하지 않았다. 같은 smoke에서 A1 프롬프트는 gate가
  Davi 호출 0으로 유지했고(알려진 A1 표현 보호만, 일반 의도 보존은 미검증), gate·domain 모두 off면 hook이 개입하지
  않았다. domain `input_only` 적용은 그 smoke에서는 단 1회의 필터 요청이 15.3초 timeout(no_answer)으로 끝나 입력이 그대로
  유지되었다(timeout 원인은 확정하지 않았고 cold start는 가설일 뿐이다; 하네스가 이 hook 내부 timeout을 중단 조건으로
  잡지 못해 2세션이 더 실행된 편차가 있었다). 같은 고정 프롬프트로 1세션을 더 실행했을 때(사용자가 endpoint를 미리 데움)
  모델 입력에는 도메인이 없었고, 필터 1회가 `python_docs`(최고 확률 0.902)를 골라 hook이 `allowed_domains`
  `["docs.python.org"]`를 담은 `updatedInput`을 결정 없이 반환했으며, Claude Code debug 로그에 "modified tool input keys:
  [query, mode, allowed_domains]"가 기록되었다(1회 관측; 결과 hostname이 모두 docs.python.org인 것은 서버 적용의 간접
  증거). 기본값 `ask`는 여전히 미검증이다. 셸 CLI는 별도로 동작한다.
- **유지하는 동작:** 기본 fake·real은 명시 opt-in(`--allow-real` + `--max-submissions` + `--budget-cap-usd`),
  재시도 0, 실행 전체 제출·비용 상한, 비용 불명은 `null`, 전송 전 오류는 제출 0(`local_config`), 저장 trace·CLI
  출력·오류 메시지·log 키 제거(응답 파싱은 원문 body로 하고 메모리 결과의 provider 값은 바꾸지 않음),
  결과 덮어쓰기 거부, rubric/reference 전달과 버전·sha256 기록.
- **키·endpoint:** 키는 환경변수(`ANTHROPIC_API_KEY`, `RUNPOD_API_KEY`) 또는 명시한 env 파일에서만 읽는다. 기본
  파일 경로는 없다. `choice_endpoint` backend(사용자가 RunPod serverless에 직접 띄운 선택지 judge worker)의
  endpoint ID는 설정·`--choice-endpoint-id`·`JUDGE_ADAPTER_CHOICE_ENDPOINT_ID`로만, 초당 단가는
  `--choice-usd-per-second`로만 받는다(기본값 없음). 둘 중 하나라도 없으면 real 호출은 전송 0인 `local_config`이고,
  CLI는 단가 없는 real 실행을 시작 전에 거부한다. 기대 모델 이름은 선택 설정이며, 주면 불일치가 contract 실패다.
  선택지 endpoint 계약(요청·응답 형식)만 문서화했고 모델·단가·하드웨어 정보는 넣지 않았다.
- **FastChat 참고답:** `--reference-file` 또는 `--reference-model`로 명시 매핑한다. judge 이름으로 추정하지 않고
  자동으로 채우지 않는다. 누락·turn 불일치 등은 호출 전에 거부한다. 임시 작업 폴더는 symlink를 쓰며, symlink가
  없는 환경(일부 Windows)은 확인하지 않았다. host single 모드 참고답 채점은 계속 차단한다. host 템플릿은 재현이
  아니라 교체한다. FastChat·MT-Bench 데이터는 패키지에 포함하지 않는다.
- **컨텍스트 필터:** `judge_adapter.context_filter`는 질문 하나와 문서들(id·title·url·text)을 받아 문서마다
  `choice_endpoint`에 한 번 묻고, 입력 순서대로 문서별 P(keep)·상태(ok / error 사유 / not_scored)·근거와
  `selected_ids`를 돌려준다. 정책은 명시해야 한다(`threshold(t)`, `top_k(k)`, `disabled()` 또는 preset; 기본 정책 없음,
  `transport`와 같은 관례). 실패·미전송 문서는 지우지 않고 keep(fail-open)하며 상태로 이유를 보인다. 선택이 없으면 빈
  목록이다. 입력 오류·크기 초과는 호출 전에 거부한다(자르지 않음; 문서 크기 초과는 `oversize='flag'`로 미채점 keep
  가능). fail-open은 문서 단위다: 그 문서의 호출 오류·쓸 수 없는 답은 그 문서만 keep하고 나머지는 정상 판정한다.
  입력 오류(`FilterInputError`, 호출 전)·생성자 인자 오류·호출자가 넘긴 tokenizer/logger 예외만 호출 전체를
  실패시킨다. threshold 정책에서 모든 문서가 drop되면 `selected_ids == []`(오류 아님)이며, 대체 문서·최소 keep·재검색은
  없고 처리 방법은 호출자가 정한다. 비용 상한·제출 상한은 인스턴스 전체에 걸치며, 비용 불명·미정산 job·계약/설정 실패·상한에서 추가 제출을
  멈춘다. 동시성 기본 1, 상한 있는 동시성은 opt-in이며 fake barrier로만 검사했다(지연 개선 주장 없음). 원문 보관·복구는
  호출자 책임이다(`select_originals`). 로그는 문서 수·텍스트 bytes(토크나이저를 주면 토큰 수와 이름·버전)만 남기는
  로컬 추정이며 과금 usage나 절감액이 아니다. 질문·원문·id·키는 기본 로그에 없다. 필터 호출 단가는 사용자가 설정한
  가정이며 실제로 띄운 모델과 별개다.
- **preset `v1-thr0.3-dev`:** 영어 V1 지시문·선택지, 실험 때의 문서 템플릿, `threshold(0.3)`, 시험한 크기 범위를 묶은
  opt-in 이름이다. 25문항에서 한 번 검증한 개발 후보이며 품질 보장이 아니다. 근거 자료는 비공개 연구 문서에 있고
  패키지에 넣지 않았다. 기본값이 아니며 결과는 사용자가 띄운 모델에 따라 달라진다.
- **한계:** 품질 우위·절감 보장·범용 host 정책 보존을 주장하지 않는다. 이 패키지의 검사는 fake transport만
  쓴다. 과거 16회 실제 smoke는 실험 버전에 대한 결과이며 이 패키지의 실제 실행 결과가 아니다. fake에서 두 방향
  결과가 같다는 것은 실제 모델의 순서 불변성 증거가 아니다. 비용은 클라이언트 추정이며 청구액이 아니다. plugin은
  `claude plugin validate --strict`와 서버 formatter로 재구성한 fixture 검사를 통과했다. 실제 실행은 내장 WebSearch 1회
  (통과만 확인)와 Tavily 1회(필터 호출 5회, 전부 keep), 그리고 search gate 1회(명시 요청 검색을 deny한 실패; 이후 수정은
  fake 테스트만)다. context filter는 commit `331081c`에서 실제 endpoint로 30회
  smoke(합성 문서, 계약·예산·순서·동시성 확인, 품질 주장 없음)를 했고, pairwise judge는 이 패키지로 실제 실행한 적이 없다.
