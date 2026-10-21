# 다른 컴퓨터에서 대시보드 데모 실행

대상 브랜치: `feat/route-dashboard`. 저장소 접근 권한, Git, Python 3.10 이상을 권장합니다. 아래 명령은 macOS/Linux 또는 Windows WSL 터미널 기준입니다. 실시간 실행도 이 환경을 기준으로 안내하며 Windows 네이티브 Claude 세션은 검증하지 않았습니다.

## 1. 설치

```bash
git clone --branch feat/route-dashboard --single-branch https://github.com/selectstar-ai/EVAL.Davi.git
cd EVAL.Davi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

이미 clone한 경우 해당 checkout의 변경을 먼저 보존한 뒤 브랜치를 선택하고 `git pull --ff-only`로 갱신하세요. 명령은 저장소 루트에서 실행합니다. 실시간 실행은 `plugins/davi-route`를 사용하므로 패키지만 설치한 디렉터리가 아니라 이 clone이 필요합니다.

이 프로젝트는 `setup.py` 대신 `pyproject.toml`과 Hatchling을 사용합니다. 오래된 pip는 이 형식의 editable 설치를 지원하지 않아 `setup.py`가 없다는 오류를 낼 수 있으므로, 위의 pip 업그레이드를 먼저 실행하세요. `setup.py`를 새로 만들 필요는 없습니다.

같은 오류가 계속되면 아래로 위치와 환경을 확인하세요.

```bash
python --version
python -m pip --version
python -c "from pathlib import Path; print('directory:', Path.cwd()); print('pyproject.toml:', Path('pyproject.toml').is_file())"
git branch --show-current
```

`pyproject.toml`이 `False`면 clone한 저장소 루트로 이동해야 합니다. 브랜치는 `feat/route-dashboard`여야 하며 pip 경로는 활성 가상환경을 가리켜야 합니다. 업그레이드가 실패했다면 설치를 계속하기 전에 그 오류를 확인하세요.

## 2. 키 없이 화면 확인 — 모의 녹화 재생

```bash
davi-route dashboard \
  --tasks examples/dashboard/tasks.jsonl \
  --record-dir examples/dashboard/recordings \
  --no-live --open
```

브라우저에서 `[모의 데이터 · 실측 아님] 라우팅 화면 예시` 녹화를 선택해 재생합니다. Claude Code, Davi 서버, API 키가 없어도 됩니다. 설치 이후 재생은 모델 호출 없이 동작합니다.

**포함한 녹화는 테스트용 fake runner로 만든 합성 데이터입니다.** 표시되는 모델·비용·시간·절감률은 화면 설명용이며 실제 측정·성능 주장이 아닙니다. 실제 사용자의 녹화·질문·답변을 복사한 파일이 아닙니다.

기본 주소는 `http://127.0.0.1:8765/`입니다. 사용 중이면 다음 빈 포트로 이동하므로 터미널의 주소를 확인하세요. 브라우저가 자동으로 열리지 않으면 그 주소를 직접 여세요. 종료는 `Ctrl+C`입니다.

## 3. 실제 모델로 실행

필요 조건:

- 이 컴퓨터에서 설치·로그인된 Claude Code. `claude --version`과 대화형 `claude` 실행으로 확인합니다.
- 이 컴퓨터에서 접근 가능한 Davi SystemOne 서버 URL. `localhost` 주소를 쓰면 이 컴퓨터에 서버가 있어야 합니다. 실행 담당자에게 실제 URL과 필요한 키를 전달받으세요.
- Python 환경은 위에서 설치한 환경을 사용합니다.

```bash
export DAVI_BASE_URL="https://YOUR-DAVI-SERVER"
# 인증이 필요한 서버만 설정:
# export DAVI_API_KEY="YOUR-KEY"

davi-route dashboard \
  --tasks examples/dashboard/tasks.jsonl \
  --judge davi --open
```

질문을 선택하거나 직접 입력한 뒤 실시간 실행을 누릅니다. 양쪽 메인 모델은 sonnet, 서브에이전트는 Davi 라우팅과 Opus 고정을 비교합니다. 샘플 질문은 설치·실행 확인용이며 라우팅 효과를 입증하도록 선별한 데이터가 아닙니다.

실시간 1회는 Claude Code 두 세션과 판정 호출을 실행하므로 사용량/비용이 발생합니다. 기본 세션 예산 `--budget-usd 1.0`은 전체 데모 비용 상한이 아니며 판정 비용은 별도입니다. 실제 기록은 기본 `dashboard-recordings/`에 저장됩니다. 모의 샘플 디렉터리를 실시간 기록 위치로 지정하지 마세요.

Jev 비교를 추가하려면 `JEV_API_KEY`를 설정하고 `--judge davi --judge jev`를 사용합니다. Claude 판정 비교는 `ANTHROPIC_API_KEY`와 `--judge claude`가 필요합니다. 해당 Anthropic 키는 비교 판정/LLM 채점용이며 Claude Code 두 세션에는 전달하지 않습니다. 키를 저장소에 커밋하지 마세요.

## 4. 실제 녹화 재생

```bash
davi-route dashboard --record-dir dashboard-recordings --no-live --open
```

다른 컴퓨터에 실제 녹화를 옮길 때는 내용 공개 가능 여부를 확인한 파일만 별도로 전달하세요. 질문·답변이 들어 있어 기본 녹화 폴더는 Git에서 제외돼 있습니다. 공유된 JSON 파일을 해당 컴퓨터의 `dashboard-recordings/`에 두면 됩니다.

## 문제가 생기면

- `dashboard` 명령이 없음: 브랜치가 맞는지 확인하고 활성 가상환경에서 `python -m pip install -e .`를 다시 실행합니다.
- 재생 목록이 비어 있음: 저장소 루트인지, 위의 `--record-dir` 경로가 존재하는지 확인합니다.
- 실시간 실행 비활성: `claude`가 PATH에 있는지, `DAVI_BASE_URL`이 설정됐는지 확인합니다.
- Davi 연결 실패: 현재 컴퓨터에서 서버로 연결 가능한지, 서버가 실행 중인지 확인합니다. 로컬 작업자의 URL·환경변수는 clone으로 전달되지 않습니다.
- 비용/시간 비교는 표시된 샘플이 합성 데이터인지 실제 녹화인지 먼저 확인합니다.

이 가이드는 각 컴퓨터에서 로컬 서버를 실행하는 방식입니다. 외부 공개 웹 서비스로 배포하지 않습니다.
