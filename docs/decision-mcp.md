# davi-decision-mcp: one judgment per tool call

[한국어](#한국어) · [English](#english)

`davi-decision-mcp` is a local stdio MCP server with one tool, `decide`. Each call asks the judge one question:
`choice` (pick one of 2–32 options, with a probability per option) or `noul` (true/false, with `p_true`). The judge
is Davi or TypeSafe Jev, chosen when the server starts. Experimental: no measured result yet shows that it saves
tokens or time or improves answers.

## 한국어

### 설치

MCP SDK(`mcp>=2.3,<3`)는 Python 3.10 이상에서만 설치됩니다. 기본 설치(`pip install -e .`)는 계속 Python 3.9를
지원하며 SDK 없이 설치·실행됩니다. MCP 서버를 쓸 때만 `mcp` extra를 설치합니다.

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[mcp]"          # davi-decision-mcp 명령 설치
davi-decision-mcp --help
```

### 서버 시작 설정

판정자·모델·주소·키·timeout·noul 방식은 서버를 띄울 때 정하며, 도구 입력으로는 바꿀 수 없습니다. 명령 인자가 먼저이고,
그다음 `DAVI_ROUTE_<KEY>`, `CLAUDE_PLUGIN_OPTION_<KEY>`, `~/.claude/davi-route.json`(`/davi-route:setup`이 저장)
순서입니다. 라우팅 훅·`davi-read`와 같은 설정을 읽습니다.

| 항목 | 명령 인자 | 옵션 키 / 환경변수 | 기본값 |
| --- | --- | --- | --- |
| 판정자 | `--judge davi\|jev` | `decision_judge` | `davi` |
| Davi 주소 | | `DAVI_BASE_URL` (또는 `DAVI_ROUTE_DAVI_BASE_URL`) | 없음(필수) |
| Davi 키 | | `DAVI_API_KEY` | 없음(키 없는 서버면 생략) |
| Davi 모델 | | `davi_model` | `davi` |
| Jev 키 | | `TYPESAFE_API_KEY` 또는 `JEV_API_KEY` | 없음(필수) |
| Jev 모델·주소 | | `jev_model`, `jev_base_url` | `jev-latest`, `https://api.typesafe.ai` |
| timeout(초) | `--timeout` | `decision_timeout_seconds` | `60` |
| noul 방식 | `--noul-mode choice\|native` | `noul_mode` | `choice` |
| 파일 읽기 범위 | `--allowed-root DIR`(반복 가능) | `decision_allowed_roots`(`:`로 구분) | 서버 작업 폴더 |

키는 첫 호출 때 읽으며, 로그·오류·결과에 남기지 않습니다. 재시도는 없습니다(retries 0). 모델 이름 옵션은 요청에 그대로
실리며, Davi 서버는 이 값으로 모델을 고르지 않고 되돌려 줄 수 있습니다.

### Claude Code에 등록

```bash
# Davi (프로젝트 폴더에서 실행; 명령이 PATH에 없으면 .venv/bin/davi-decision-mcp의 절대 경로를 씁니다)
claude mcp add davi-decision -e DAVI_BASE_URL=http://<davi-host>:<port> \
  -- davi-decision-mcp --judge davi --allowed-root "$PWD"

# Jev (호출마다 비용이 듭니다)
claude mcp add jev-decision -e TYPESAFE_API_KEY=<key> -- davi-decision-mcp --judge jev --allowed-root "$PWD"
```

`claude mcp list`로 연결을 확인하고, Claude에게 "decide 도구로 이 두 파일 중 질의에 답하는 쪽을 골라 줘"처럼 명시적으로
요청합니다.

### 도구 입력과 결과

```json
{"type": "choice", "instructions": "질의에 답하는 문서를 고르세요.",
 "criteria": {"a": "문서 1", "b": "문서 2"}, "state": "질의: ...", "state_files": ["docs/a.md", "docs/b.md"]}
```

```json
{"type": "choice", "value": "a", "probabilities": {"a": 0.96, "b": 0.04},
 "meta": {"provider": "davi", "model": "davi", "latency_ms": 444.2}}
```

- `noul`은 `criteria`가 정확히 `{"true": ..., "false": ...}`이고 `threshold`(기본 0.5, [0, 1])를 받습니다. 결과는
  `value`(`p_true >= threshold`), `p_true`, `threshold`, `meta.noul_mode`입니다. `choice`에 `threshold`를 주면
  오류입니다.
- `state_files`: 허용 폴더 안의 UTF-8 텍스트 파일 최대 16개. 서버가 파일을 읽어 `[파일 N: 상대 경로]` 머리글과 함께
  `state` 뒤에 붙입니다. **파일 내용은 설정된 판단 서버(Davi 또는 Jev)로 전송됩니다.** 폴더 밖·`..`·심볼릭 링크로
  벗어나는 경로, `.env*`·`*.pem`·`*.key`·`id_*`·`.git/` 아래 파일, 없는 파일, 바이너리, 256 KiB 초과(파일 하나 또는
  합친 state)는 거부합니다. 조용히 자르지 않습니다.
- 한도: 도구 인자 64 KiB, choice 후보 32개. 결과에는 파일 내용이 없습니다.

### 오류와 해결

오류는 `isError` 결과로 오며 `{"error": {"code": ..., "message": ...}}` 형태입니다. 메시지에는 state·파일 내용·전체
경로·키가 들어가지 않습니다. 실패를 false나 낮은 확률로 바꾸지 않습니다.

| code | 뜻 |
| --- | --- |
| `invalid_request` | 잘못된 입력(스키마, 경로, 한도) 또는 서버의 422(`InputTooLong` 등) |
| `credential_missing` | 키가 없거나 서버가 거부(401/403) |
| `timeout` | timeout 안에 응답 없음 |
| `provider_error` | 연결 실패, 서버 5xx |
| `format_error` | 응답에 확률이 없거나 잘못됨 |

예: Jev 서버를 키 없이 등록하면 첫 호출이 `credential_missing: none of DAVI_ROUTE_TYPESAFE_API_KEY, ... is set`을
돌려줍니다. `claude mcp remove jev-decision` 뒤 `-e TYPESAFE_API_KEY=<key>`를 넣어 다시 등록합니다.
`state_files[1]: outside the allowed roots`는 그 파일 폴더를 `--allowed-root`로 추가하라는 뜻입니다. Python 3.9에서
명령을 실행하면 `needs Python 3.10 or newer`로 끝나므로 3.10 이상 가상환경에 `.[mcp]`를 설치합니다.

## English

### Install

The MCP SDK (`mcp>=2.3,<3`) needs Python 3.10 or newer. The base install (`pip install -e .`) still supports
Python 3.9 and installs and runs without the SDK; install the `mcp` extra only for this server.

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[mcp]"          # provides davi-decision-mcp
davi-decision-mcp --help
```

### Server-start settings

The judge, model, URL, key, timeout and noul mode are fixed when the server starts; tool arguments cannot change
them. Command arguments come first, then `DAVI_ROUTE_<KEY>`, `CLAUDE_PLUGIN_OPTION_<KEY>` and
`~/.claude/davi-route.json` (saved by `/davi-route:setup`), the same options the routing hooks and `davi-read` read.

| Setting | Argument | Option key / variable | Default |
| --- | --- | --- | --- |
| Judge | `--judge davi\|jev` | `decision_judge` | `davi` |
| Davi URL | | `DAVI_BASE_URL` (or `DAVI_ROUTE_DAVI_BASE_URL`) | none (required) |
| Davi key | | `DAVI_API_KEY` | none (omit for a keyless server) |
| Davi model | | `davi_model` | `davi` |
| Jev key | | `TYPESAFE_API_KEY` or `JEV_API_KEY` | none (required) |
| Jev model, URL | | `jev_model`, `jev_base_url` | `jev-latest`, `https://api.typesafe.ai` |
| Timeout (s) | `--timeout` | `decision_timeout_seconds` | `60` |
| Noul mode | `--noul-mode choice\|native` | `noul_mode` | `choice` |
| Readable folders | `--allowed-root DIR` (repeatable) | `decision_allowed_roots` (`:`-separated) | the working directory |

Keys are read at the first call and never logged or returned. There are no retries. The model option is sent as is;
the Davi server may echo it back without using it to pick a model.

### Register it in Claude Code

```bash
# Davi (run in the project folder; use the absolute path of .venv/bin/davi-decision-mcp if it is not on PATH)
claude mcp add davi-decision -e DAVI_BASE_URL=http://<davi-host>:<port> \
  -- davi-decision-mcp --judge davi --allowed-root "$PWD"

# Jev (each call costs money)
claude mcp add jev-decision -e TYPESAFE_API_KEY=<key> -- davi-decision-mcp --judge jev --allowed-root "$PWD"
```

Check the connection with `claude mcp list`, then ask Claude explicitly, e.g. "use the decide tool to pick which of
these two files answers the query".

### Tool input and result

Input and result are as in the Korean examples above. `noul` takes `criteria` with exactly `true` and `false` and an
optional `threshold` (default 0.5, in [0, 1]); it returns `value` (`p_true >= threshold`), `p_true`, `threshold` and
`meta.noul_mode`. A `threshold` on a `choice` is an error. `state_files` takes up to 16 UTF-8 text files inside the
allowed folders; the server appends each after `state` under a `[파일 N: relative path]` header. **File contents are
sent to the configured judge server (Davi or Jev).** Paths that escape the folders (absolute, `..`, symbolic links),
`.env*`, `*.pem`, `*.key`, `id_*`, anything under `.git/`, missing or binary files and more than 256 KiB (one file or
the combined state) are refused, never cut. Tool arguments are limited to 64 KiB and a choice to 32 options. Results
never contain file contents.

### Errors

Errors are `isError` results shaped `{"error": {"code": ..., "message": ...}}`, with no state, file content, full
path or key in the message, and never turned into a false or low answer. Codes: `invalid_request` (bad input, or the
server's 422 such as `InputTooLong`), `credential_missing` (no key, or 401/403), `timeout`, `provider_error`
(connection failure, 5xx), `format_error` (missing or bad probabilities).

Example: a Jev server registered without a key answers its first call with `credential_missing: none of
DAVI_ROUTE_TYPESAFE_API_KEY, ... is set`; remove it (`claude mcp remove jev-decision`) and add it again with
`-e TYPESAFE_API_KEY=<key>`. `state_files[1]: outside the allowed roots` means the file's folder needs an
`--allowed-root`. On Python 3.9 the command stops with `needs Python 3.10 or newer`: install `.[mcp]` in a 3.10+
environment.
