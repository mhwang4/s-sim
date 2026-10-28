# KRIHS S-Sim MCP 서버

S-Sim 플러그인의 분석 기능을 MCP(Model Context Protocol) 서버로 노출하여, Claude Desktop·Claude Code에서 대화로 공간분석을 실행하는 도구다.

- 모두 **내 PC에서 실행**된다. QGIS 엔진이 화면 없이(headless) 돌고, 데이터와 연산은 PC 밖으로 나가지 않는다.
- 플러그인과 같은 알고리즘, 같은 데이터팩, 같은 결과 폴더 구성을 쓰므로 플러그인 화면에서 돌린 결과와 같다.

## 제공 기능 (MCP tool)

| tool | 기능 |
|---|---|
| `check_environment` | QGIS·플러그인 버전, 알고리즘 수, 데이터팩 위치·지역, mcp 설치 여부 점검 |
| `list_regions` | 데이터팩의 지역 목록, 지역별 입력 파일 경로·존재 여부, 이미 만든 분석 결과 |
| `list_algorithms` | 알고리즘 17종 목록 (id·이름·그룹) |
| `describe_algorithm` | 알고리즘 1개의 파라미터 명세와 설명 |
| `run_algorithm` | 알고리즘 1개 실행 (17종 전부). 입력 사전 검사, 진행률 보고, 결과 요약, 실행 이력. OD 알고리즘(격자→중심지·중심지 간)은 전체·성공·실패 쌍 수(`od_pairs`)를 함께 돌려주고, 거리가 계산되지 않은 쌍(도달불가)이 있으면 경고 |
| `run_pipeline` | **플러그인 전체 흐름을 메뉴 순서대로 실행** — 장래인구 배분 → 장래 중심지 식별 → 중심지·생활권 ①~⑤ → KPI 일괄 산출·리포트 → 3D. 단계를 골라 실행할 수 있고, 최소 밀도·도시위상·시설 기준을 인자로 바꾼다 |
| `run_kpi` | KPI만 산출(일괄 또는 단계) + 종합 리포트 |
| `job_status` / `cancel_job` | `run_pipeline`·`run_kpi` 작업의 진행 상황·결과 확인 / 중지 |
| `guide` | 도움말 — 분석 흐름, 중심지·생활권 기준(표1~표5), KPI 지표와 해석(표6~표8), 입력자료·결과 폴더 |

`run_pipeline`과 `run_kpi`는 플러그인 화면 코드를 그대로 화면 없이 실행하는 `pipeline_runner.py`를 별도 프로세스로 돌린다. 그래서 기본값, 단계 사이 자동 연결, 결과 폴더가 플러그인과 같다. 전체 흐름은 15분 안팎 걸리므로 바로 작업 id를 돌려주고, 진행은 `job_status`로 확인한다. 한 번에 작업 하나만 실행한다.

MCP가 만드는 결과는 모두 `mcp_server\분석결과\` 한 곳에 모인다. 이 폴더는 처음 실행할 때 생긴다.

```
mcp_server\분석결과\
├── S-Sim_분석결과(지역명)\   run_pipeline·run_kpi 결과 (플러그인과 같은 구성)
└── MCP_실행결과\             run_algorithm 개별 출력, run_log.jsonl(실행 이력), 작업기록\
```

## 설치 (Windows, 처음 한 번)

1. QGIS(LTR)와 Claude Desktop이 설치되어 있어야 한다.
2. 배포 묶음 맨 위의 `설치.bat`으로 플러그인을 먼저 설치한다.
3. **`mcp_server\설치_mcp.bat` 더블클릭** — 세 단계를 자동으로 한다.
   1. QGIS 찾기
   2. QGIS 파이썬에 `mcp` 패키지 설치 (인터넷 필요)
   3. Claude Desktop 설정(`%APPDATA%\Claude\claude_desktop_config.json`)에 `krihs-s-sim` 등록 (기존 설정은 `.bak`으로 백업)
4. Claude Desktop을 트레이 아이콘까지 완전히 종료한 뒤 다시 연다. 도구 목록에 `krihs-s-sim`이 보이면 된다.

Claude Code에서 쓰려면 등록 명령 한 줄을 더 실행한다.

```
claude mcp add -s user krihs-s-sim -- cmd /c "(배포 묶음 경로)\mcp_server\run_server.bat"
```

QGIS가 기본 경로(`C:\Program Files\QGIS x.y.z`)에 없으면 환경변수 `QGIS_PYTHON_BAT`에 `...\bin\python-qgis-ltr.bat` 전체 경로를 지정한다.

## 위치 규약

| 대상 | 기본 위치 | 바꾸는 방법 |
|---|---|---|
| 플러그인 | 배포 묶음의 `KRIHS_S_Sim\` (없으면 QGIS에 설치된 플러그인) | — |
| 데이터팩 | 배포 묶음의 `S-Sim_데이터팩\` | 환경변수 `S_SIM_DATAPACK` |
| 결과 저장 | `mcp_server\분석결과\S-Sim_분석결과(지역명)\` | 환경변수 `S_SIM_RESULTS` 또는 `run_pipeline`·`run_kpi`의 `out_dir` |
| 개별 실행 출력·실행 이력·작업 기록 | `mcp_server\분석결과\MCP_실행결과\` | 환경변수 `S_SIM_RESULTS` |

`run_algorithm`에서 입력 파일은 절대경로 또는 파일명만 써도 된다(데이터팩·결과 폴더에서 자동 탐색). 출력 경로를 생략하면 `MCP_실행결과\`에 저장된다.

## 설치 확인 (선택)

명령 프롬프트에서:

```
mcp_server\run_server.bat --selftest
```

환경 점검, 지역 목록, 알고리즘 목록·명세, 도움말, 입력 오류 안내를 확인한다(분석은 실행하지 않음). `selftest OK — 17 algorithms, 4 guide topics`가 나오면 정상이다. `--selftest --full`은 KPI 일괄 산출까지 실행한다(수 분 소요).

## 사용 예 (Claude 대화창)

> 전남광주 데이터팩 입력 파일이 다 있는지 확인해 줘
> 전남광주 처음부터 끝까지 다 돌려 줘 (끝나면 KPI 요약해 줘)
> 최소 주거밀도 200, 강소도시 기준으로 중심지·생활권만 다시 돌려 줘
> 중심지 후보 추출 알고리즘 파라미터 알려 줘
> 전남광주 KPI 일괄 산출 돌리고 계획권역 결과 요약해 줘
> 거점시설 기준을 반경 5km, 최소 배후인구 1만 명으로 바꿔서 생활편리성만 다시 산출해 줘
> 가이드라인에서 중심지 접근 형평성은 어떻게 해석하라고 했지?

## 파일 구성

| 파일 | 역할 |
|---|---|
| `server.py` | MCP 서버 본체 (헤드리스 QGIS + S-Sim 알고리즘·지역·흐름 실행·도움말 tool) |
| `pipeline_runner.py` | 플러그인 화면 코드를 헤드리스로 띄워 메뉴 순서대로 실행 (`run_pipeline`·`run_kpi`가 호출) |
| `run_server.bat` | 서버 실행 (QGIS 자동 탐색) — Claude Desktop이 이 파일을 실행 |
| `설치_mcp.bat` | 원클릭 설치 (mcp 설치 + Claude Desktop 등록) |
| `register_claude.ps1` | Claude Desktop 설정 JSON 등록 (설치 스크립트가 호출) |
| `guide\*.md` | `guide` tool이 돌려주는 도움말 문서 |

## 문제 해결

- **서버가 목록에 안 보임** — Claude Desktop을 트레이 아이콘까지 완전히 종료한 뒤 재시작
- **QGIS를 찾지 못함** — 환경변수 `QGIS_PYTHON_BAT` 지정
- **pip install 오류** — 인터넷 연결 또는 사내망 프록시 설정 확인
- **플러그인을 찾지 못함** — 배포 묶음의 `설치.bat`을 먼저 실행
