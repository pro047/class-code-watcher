# 핸드오프 — Class Code Watcher

- 기준 문서: `PRD.md` v1.1.1 (14절 MVP 단계별 개발 계획)
- 갱신 시점: 2026-08-28
- 기준 커밋: `652082b` (main)
- 한 줄 요약: **0·1단계 완료 — 테스트 119개, 게이트 3종 녹색. 실기기 확인 6건 중 4건 통과.
  다음은 2단계 "Diff"(0.5일).**

---

## 1. 이 프로젝트가 무엇인가

수업 중 코드 변경을 감시하다가 세션이 끝나면 diff를 만들고, OpenAI로 요약해서 Discord로 1회 전송하는 CLI 도구다.
읽는 사람은 코드를 보지 않는 팀원 5인이다 — 그래서 "요약이 이해되는가"가 최종 성공 지표다 (PRD 15절).

배포 형태는 PyInstaller 단일 exe + `.env`를 USB에 담아 학원 공용 PC에서 실행 (FR-054).

## 2. 개발 방식 — DMS 파이프라인

제품 코드를 사람이 직접 쓰지 않는다. `orchestrate.sh`가 단계별 에이전트를 돌린다.

```
design → judge → impl → verify → run_verify(셸 판정)
```

| 스크립트 | 역할 |
|---|---|
| `orchestrate.sh` | 오케스트레이터. 진행 결정권 독점 |
| `advisor.sh` | 상담역. 읽기 전용, 진행 권한 없음 |
| `approve.sh` | 사람이 게이트 버튼을 누르는 자리 |
| 사람 | 유일하게 게이트를 통과시키는 주체 |

실행:

```bash
PY=.venv/Scripts/python ./orchestrate.sh <feature-name>
# AUTO=1          사람 게이트 건너뜀 (무인)
# MAX_RETRY=3     재시도 횟수
# FRESH_DESIGN=1  DONE 상태인 DESIGN.md 를 무시하고 설계를 새로 뽑음
# GATE_TOOLS_OVERRIDE="Bash(...)"  게이트 명령 허용 목록 교체
```

주행 산출물은 `.pipeline/<feature>/` 아래에 남는다. **커밋되지 않는다** — 자세한 것은 2.1 절.

검증 게이트 3종: `pytest`, `ruff check .`, `mypy src`.
pytest는 수집 테스트가 0개면 exit 5로 실패한다 — 검증 단계가 테스트를 안 쓰고 넘어가는 걸 막으려는 의도적 설계다.

**게이트 판정권은 셸에 있다.** 에이전트가 "통과했다"고 쓴 문장은 읽지 않고 `run_verify` 가 직접 돌려 판정한다.

### 2.1 `.pipeline/` — 주행 산출물과 증거

`orchestrate.sh` 가 `.pipeline/<feature>/` 에 전부 쌓는다. **`.gitignore` 대상이라 커밋되지
않고, 돌린 PC 에만 있다.**

| 파일 | 성격 | 누가 쓰나 |
|---|---|---|
| `DESIGN.md` | 설계. 첫 줄이 `STATUS:` | design 단계 |
| `JUDGE.md` | 설계 주장 감사. 둘째 줄이 `UNVERIFIED: n REFUTED: n` | judge 단계 |
| `IMPL.md` | 구현 결과 + **다음 단계 인계 노트** | impl 단계 |
| `VERIFY.md` | 테스트 목록 + **사람 확인 체크리스트** | verify 단계 |
| `STATE.md` | 실시간 상태. 상담역·런처가 읽는 유일한 창구 | **셸이 자동 생성 — 사람이 편집하지 말 것** |
| `FAIL_LOG.md` | append-only 실패 기록. impl 이 "이전에 왜 실패했나"의 입력으로 읽는다 | 셸 |
| `MODEL_LOG.md` | 요청 모델 vs 실제 실행 모델 | 셸 |
| `<파일>.approved` | 승인 마커. 대상 파일의 sha256 을 담는다 | **사람만** (`approve.sh`) |
| `<단계>.stream.jsonl` | 에이전트 주행 원시 스트림 (NDJSON) | 셸 |
| `<단계>.result.json` | 스트림 마지막 result 이벤트. 비용·턴·사인·`permission_denials` | 셸 |

**증거 보관 규칙** — 재시도가 이전 증거를 덮지 않는다. 고정 이름은 유지하고 덮어쓰기 직전에
번호로 민다:

- `<단계>.attempt<n>.*` — 재시도로 밀려난 이전 주행
- `<단계>.ratelimit<n>.*` — 레이트 리밋으로 모델을 갈아타기 전의 주행 (결함 ① 수정이 만든다)
- `<산출물>.crashed` — 프로세스는 죽었지만 산출물이 `STATUS: DONE` 이라 파킹된 것.
  **사람이 `mv` 로 되살리는 행위 자체가 승인이다**

용량은 `.md` 문서가 157KB, `*.jsonl` 원시 스트림이 **6.6MB** 다. 스트림이 거의 전부다.

**진단할 때 여기를 본다.** 이번 프로젝트에서 하네스 결함 5건을 전부 이 파일들로 규명했다:

```bash
# 단계가 왜 죽었나 — 비용·턴·사인
jq -r '"turns=\(.num_turns) cost=\(.total_cost_usd) reason=\(.terminal_reason)"' <단계>.result.json

# 에이전트가 실행하려다 거부당한 것 (결함 ② 를 찾은 방법)
jq -r '.permission_denials[].tool_input.command' <단계>.result.json

# 레이트 리밋 신호 (결함 ① 판정 함수와 같은 조건)
jq -Rn '[inputs|fromjson?|select(.type?=="rate_limit_event" and .rate_limit_info?.status?=="rejected")]|length' <단계>.stream.jsonl

# 에이전트가 실제로 부른 도구 순서
jq -Rr 'fromjson? // empty | select(.type?=="assistant") | .message.content[]?
        | select(.type=="tool_use") | "\(.name) \(.input.file_path // .input.command // "")"' <단계>.stream.jsonl
```

**다른 PC 로 넘어갈 때 딸려오지 않는다.** 실무상 의미:

- 승인 마커가 없으므로 **그 PC 에서 게이트를 다시 통과시켜야 한다** (`approve.sh` 재실행).
  마커는 파일 해시에 묶여 있어 어차피 옮겨도 내용이 같아야만 유효하다
- `DESIGN.md`/`JUDGE.md` 재사용도 안 된다 — 같은 feature 를 이어서 돌리면 **설계부터 다시 뽑는다**
- **새 feature 로 시작하는 단계에는 영향이 없다.** 2단계는 `diff-engine` 이라 무관하다
- 지난 단계의 인계 노트를 다시 봐야 하면 그 PC 로 가거나, 필요한 `.md` 만 따로 옮겨라
  (`.md` 는 157KB 라 가볍다. `*.jsonl` 은 6.6MB 이고 진단용이라 옮길 이유가 거의 없다)

> **커밋 대상으로 바꾸지 마라.** 매 주행마다 수 MB 씩 늘고, 스트림에는 프롬프트 전문과
> 파일 내용이 그대로 들어 있다. `.gitignore` 첫 줄이 그래서 `.pipeline/` 이다.
> 특정 주행의 판단 근거를 남기고 싶으면 그 내용을 `HANDOFF.md` 나 커밋 메시지로 옮겨라 —
> 이 문서 3절의 주행 기록이 그 방식이다.

## 3. 지금 어디까지 왔나

### 커밋 이력

| 커밋 | 내용 |
|---|---|
| `00017cf` | PRD docx 원본 |
| `bde763a` | DMS 파이프라인 포팅 — `orchestrate.sh`, 프롬프트 5종, `PRD.md`, `test/run-tests.sh`, `pyproject.toml` |
| `a4f5da6` | 셸 스크립트 실행 비트 |
| `f475c9a` | 스트림에 JSON 아닌 줄이 섞여도 단계가 안 죽게 수정 + PRD v1.1.1 |
| `c77e87c` | 0단계 골격 구현 스냅샷 (562줄) |
| `4eb5a60` | 이 핸드오프 문서 |
| `1e7fe58` | **에이전트가 게이트 명령을 직접 실행할 수 있게 수정** (아래 참조) |
| `092594a` | **0단계 테스트 61 케이스 — 게이트 3종 통과. 0단계 완료** |
| `a1bc96f` | 핸드오프 갱신 |
| `bbcc316` | **레이트 리밋 거부를 셸이 감지해 폴백 체인으로 갈아탄다** (아래 참조) |
| `962f532` | 검증 명령 시간 상한 (결함 ④) |
| `bc702e7` | 낡은 테스트 교착 해소 — impl 인계 예외 (결함 ⑤) |
| `9f8c03c` | **1단계 감시 엔진 — 테스트 119개, 게이트 3종 통과. 1단계 완료** |

### 0·1단계 산출물 — 소스 11모듈 / 테스트 119개

| 소스 | 단계 | 역할 |
|---|---|---|
| `cli.py` | 0→1 | argparse(10.2 옵션 12개), preflight, bootstrap, **`run_watch` 실물** |
| `config.py` | 0→1 | 기본값, `WatchConfig`, `Secrets`, `mask_secrets`, FR-014 상수 3종 |
| `selector.py` | 0 | `is_watched`(순수), `scan_files` |
| `session.py` | 0→1 | `SessionStatus` 6종, 원자적 `write_session_json`, `history_dir` |
| `debounce.py` | 1 | 병합 판정 — **시계 주입 순수 상태 기계** |
| `stability.py` | 1 | FR-014 안정화 — stat·clock·sleep 주입 |
| `watchmode.py` | 1 | FR-016 native/polling 판별 — 순수 판정 + 어댑터 주입 |
| `eventlog.py` | 1 | FR-041 — 행 구성(순수) + append(부작용) 분리 |
| `snapshot.py` | 1 | baseline/final/history, SHA-256, meta |
| `watcher.py` | 1 | watchdog 배선·감시 루프·finalize (**부작용 층, 얇게**) |

테스트 10파일 1968줄 / **119 케이스**. `test_watcher.py` 가 518줄로 가장 크다 —
Observer 를 띄우지 않고 이벤트 정규화·finalize 판정만 검사한다.

**게이트 실측 (2026-08-27):** `pytest` 119 passed / `ruff` All checks passed / `mypy` 11 files.

### 파이프라인 주행 기록 — 두 번 죽고 세 번째에 닫혔다

두 번의 실패가 서로 다른 원인이었고, 둘 다 규명·수정됐다. 같은 실수를 반복하지 않도록 남긴다.

**1차 (08-26 16:10) — 레이트 리밋으로 verify 사망**

`verify.stream.jsonl` 은 6줄이 전부고 실제 작업은 0턴이었다.

| 줄 | 내용 |
|---|---|
| 1 | `rate_limit_event` — `seven_day` utilization **0.98**, `allowed_warning` |
| 3 | `rate_limit_event` — `seven_day_overage_included` utilization **1.0**, **`rejected`**, `overageDisabledReason: "org_level_disabled"` |
| 4 | 합성 assistant 메시지 — "You've reached your Fable 5 limit." |
| 5 | `result` — `is_error: true`, `terminal_reason: "api_error"`, `num_turns: 1` |
| 6 | **JSON 아닌 줄** — `Client.listTools() called but server does not advertise tools capability` |

`STATE.md` 가 적은 사인("스트림에 result 이벤트가 없다")은 **오진이었다.** result 는 line 5에 멀쩡히 있었고,
당시 jq 가 line 6의 비-JSON 줄에서 죽어 `verify.result.json` 이 0바이트가 됐을 뿐이다.
커밋 `f475c9a`(`fromjson?`)가 이미 고쳤는데 타이밍이 12분 늦었다 (사망 16:10:16, 수정 16:22:16).

**남은 결함 — `FALLBACK_*` 이 레이트 리밋을 못 받는다.**
`FALLBACK_VERIFY="claude-opus-5,claude-sonnet-5"` 가 있는데도 fable-5 에서 그냥 죽었다.
`orchestrate.sh:66` 주석이 이유를 적어놨다 — *"FALLBACK_* 은 가용성 폴백(529 과부하 등) 전용"*.
레이트 리밋은 그 경로를 안 탄다. **고쳤다 (`bbcc316`)** — 7절 참조.

**2차 (08-27 09:10) — 권한 거부로 impl 이 눈을 가린 채 돌았다**

impl `$1.32`/22턴 DONE, verify `$4.77`/20턴 DONE, 테스트 61개 작성·전부 통과.
그런데 `run_verify` 가 ruff E501 1건(`config.py:12`, 103자 한글 주석)에서 실패했다 — **1차와 똑같은 자리**.

원인: **에이전트들이 파이썬을 한 번도 실행하지 못했다.** `*.result.json` 의 `permission_denials` 실측:

| 단계 | 거부된 호출 |
|---|---|
| judge | 파이썬 실행 전량 거부 (JUDGE.md 가 "미확인 2건"을 남긴 이유) |
| impl | `ruff check` ×3, `mypy src` ×1, `ruff.exe check` ×1 — **5건 전부** |
| verify | `pytest -q` ×3 — **전부** |

`orchestrate.sh:363` 의 `--permission-mode acceptEdits` 는 Write/Edit 만 자동 승인하고
Bash·PowerShell 은 승인을 요구하는데, `-p` 는 비대화형이라 승인할 사람이 없었다.
impl 은 자기가 뭘 어겼는지 볼 수 없었다 — 눈을 가린 채 린트를 통과시키라고 시킨 셈이다.

**수정 (`1e7fe58`)**: `--allowedTools` 로 `pytest`·`ruff`·`mypy` 세 모듈만 승인 없이 연다.
`$PY *` 처럼 열면 임의 파이썬 실행이 되므로 그렇게 하지 않았다. 최종 판정은 여전히 셸이 한다.

**닫는 방식**: E501 한 줄은 손으로 고쳤다 (`092594a`). 재시도 2를 돌리면 impl($8)+verify($5) 재실행인데,
이미 판정 통과한 소스와 61개 테스트를 ~$13 들여 다시 뽑는 거래가 나빴다. 근본 원인은 직전 커밋에서 고쳤다.

> `.pipeline/bootstrap-cli/STATE.md` 는 `AWAITING_APPROVAL`("재시도 2 진행?")에 멈춘 채로 남아 있다.
> 0단계는 그 게이트 밖에서 닫혔으므로 이 상태는 무시해도 된다. 1단계는 새 feature 이름으로 시작한다.

## 4. 마일스톤 (PRD 14절) 대비 진척

| 단계 | 범위 | 완료 기준 | 예상 | 상태 |
|---|---|---|---|---|
| **0. 골격** | CLI, 설정, 세션 디렉터리, 로깅 | 잘못된 입력/정상 시작의 자동 테스트 | 0.5일 | ✅ **완료** (`092594a`) |
| **1. 감시** | baseline(다중 파일), watchdog, debounce, flush+안정화, final | 저장·원자적 교체·삭제/재생성·신규 파일 시나리오 통과 | **2일** | ✅ **완료** (`9f8c03c`) |
| **2. Diff** | difflib, 바이너리/대용량 제외, 파일별·합산 통계 | 다중 파일 fixture 결과 검증 | 0.5일 | ⬜ **다음 차례** |
| 3. 정제 | secret scanner, 경로 상대화, 환경정보 제거 | 키 패턴 fixture 전량 탐지, 마스킹 테스트 | 0.5일 | ⬜ 미착수 |
| 4. LLM | 프롬프트, strict schema, 1회 호출 + 1회 재시도, fallback | mock 기반 호출 횟수·스키마·timeout 테스트 | 1일 | ⬜ 미착수 |
| 5. Discord | 메시지 렌더링, 모바일 가독성, Webhook, 실패 보존 | 204/4xx/5xx mock 테스트 | 0.5일 | ⬜ 미착수 |
| 6. 통합 | 상태 전이, 종료 코드, 마스킹 E2E | E2E 10회 연속 성공, 오류별 산출물 검증 | 1일 | ⬜ 미착수 |
| 7. 배포 | PyInstaller 단일 exe, `.env` 템플릿, USB 실행 검증 | Python 없는 PC에서 실행 성공 | 0.5일 | ⬜ 미착수 |

합계 6.5일 중 2.5일 완료 — **약 38%**.

1단계는 자동 테스트뿐 아니라 **실기기 확인 6건 중 4건까지 통과**했다 (5절 가).
남은 2건(폴링 전환·자원 사용)은 2단계 진행을 막지 않는다.

부수 체크리스트는 아직 전량 미완이다:
- PRD 14.1 MVP 테스트 체크리스트 **19항목** — 0단계 범위 밖 항목이 대부분이라 그대로다
- PRD 18절 DoD **7조건** 전부 미충족

## 5. 다음에 할 일

### (가) 사람만 할 수 있는 것 — 1단계 실기기 확인, 6건 중 4건 통과 (2026-08-28)

`VERIFY.md` 5절의 체크리스트다. watchdog 실이벤트·신호·실기기 의존이라 테스트로 못 덮는다.
**1·2·3·5번을 실기기로 통과시켰다 — 감시 엔진이 실제로 돈다는 것이 확인됐다.**

측정 환경: 로컬 SSD(`C:`), `watch_mode: native`(WindowsApiObserver), VS Code + 이클립스 병용,
감시 대상 3~4개. 재현용 프로젝트는 `C:\Users\ksmart\watcher-save-test` (리포 밖, 커밋 대상 아님).

1. ✅ **실제 IDE 저장 감지** — 두 IDE 모두 **저장 1회 = 논리 1건**.
   근거는 `events.jsonl` 의 `count`(하나로 병합된 raw 이벤트 수)다.

   | 조작 | raw `count` | 논리 이벤트 |
   |---|---|---|
   | VS Code 저장 1회 | 2 | 1 |
   | VS Code `Ctrl+S` 연타 | **17** | **1** |
   | 이클립스 저장 1회 | 2 | 1 |
   | 이클립스 `New > Class` | 4 | 1 |

   **예상이 빗나갔다 — 좋은 쪽으로.** 이클립스의 임시파일→rename 이 `.tmp` 누출이나
   한 저장의 `deleted`+`modified` 분리를 낼 것으로 봤는데 VS Code 와 수치가 같았다.
   이 환경에서는 IDE 차이가 감시 엔진에 보이지 않는다.
   이클립스 자동 빌드가 만든 `bin/*.class` 도 allowlist 밖이라 로그에 한 줄도 안 샜다.

2. ✅ **Ctrl+C 안전 종료** — `final/` 의 4개 파일이 실제 파일과 **바이트 동일**,
   `final/.meta.json` 의 `unstable: false`, 종료 코드 **1**.
   `final` 해시가 마지막 이벤트 로그 해시와 일치했다 — 감지와 종료 사이에 놓친 쓰기가 없다.
   세션 중 새로 만든 `New.java` 가 `added` 로 잡혔다 — 8절의 `state.observed` 경로 실증.

3. ✅ **두 번째 Ctrl+C** — `[ABORTED]`, `status: failed` + `error: aborted_by_user`.
   `final/` 은 만들어지지 않고 `baseline/`·`events.jsonl` 만 남는다 (설계대로).
   **종료 코드 130 은 아직 눈으로 확인 안 했다** — 다음에 돌릴 때 `$LASTEXITCODE` 만 보면 된다.
   **그리고 `watched_files` 가 낡은 값으로 굳는 결함을 여기서 발견했다 — (라) 참조.**

4. ⬜ **폴링 자동 전환** (judge #23·#24 미확인 해소) — 네트워크 드라이브(`Z:` 매핑)와
   OneDrive 폴더에서 `watch_mode` 가 `polling` 이 되는지. `GetDriveTypeW` 가 4 를 반환하는지,
   개인+업무 OneDrive 병용 머신에서 `Get-ChildItem env:OneDrive*` 로 변수명 3종 실재 확인.
   **로컬 디스크에서 `native` 가 나오는 것은 확인됐다** — 판정 함수의 절반은 검증된 셈이다

5. ✅ **Ctrl+C 반응 시간** (judge #25) — 즉시. `[FINALIZING]` 이 체감 지연 없이 떴다

6. ⬜ **자원 사용** — 200파일 1시간 감시에 CPU 2% 이하 / 메모리 150MB 이하

### (나) 2단계 "Diff" (0.5일)

difflib, 바이너리/대용량 제외, 파일별·합산 통계. 완료 기준은 다중 파일 fixture 결과 검증.

```bash
BUDGET_JUDGE=8 BUDGET_VERIFY=10 PY=.venv/Scripts/python ./orchestrate.sh diff-engine
```

**예산 근거**: 기본값 `BUDGET_JUDGE=5`·`BUDGET_VERIFY=5` 는 0단계 실측으로 정한 값이고
1단계에서 둘 다 그 상한에서 죽었다. 1단계 실적은 judge $5.61 / verify $3.88 이다.
2단계는 1단계보다 작으므로 위 값이면 충분하다.

**과도기 매핑 주의**: 변경 있음 세션은 지금 `partial` + `summary_pipeline_not_implemented`
+ 코드 1 이다. 2~5단계가 구현되면 `test_main_changed_session_ends_partial`,
`test_changed_session_is_partial_and_preserves_artifacts`,
`test_status_transitions_end_partial_when_changed` 의 단언을 새 매핑으로 갱신해야 한다.

**`_drain_queue` 의존**: `test_cli.py` 의 통합 3건이 `watcher._drain_queue(sink, debouncer)`
패치에 의존한다. 감시 루프 구조를 바꾸면 깨지고, 그때는 `KeyboardInterrupt` 주입 지점만
옮기면 된다.

### (다) 별건 — cp949 콘솔에서 죽는 결함 (0단계 코드)

`src/class_watcher/cli.py:131` (`run_preflight` 의 `--max-files` 초과 안내)에 em dash `—` 가
있다. 한국어 Windows 콘솔에 실제로 출력되면 `UnicodeEncodeError: 'cp949' codec` 로 **죽는다**.
impl 이 이 세션에서 같은 예외를 재현했고, 이 문서를 쓰던 스크립트도 같은 이유로 한 번 죽었다.

기존 테스트가 못 잡는다 — `capsys` 는 실제 콘솔 인코딩을 타지 않는다.
**FR-006 은 P0 이고 그 안내 경로가 실제 사용 환경에서 죽는다.** 설계 범위 밖이라 impl 이
손대지 않았다. 2단계에 끼워 넣든 별도로 처리하든 사람이 정해야 한다.

**2026-08-28 실측으로 조건이 좁혀졌다.** 실기기 확인 4세션 동안 콘솔 한글은 한 번도 안 깨졌다 —
진짜 콘솔에서는 파이썬이 `WriteConsoleW` 로 쓰므로 cp949 를 안 탄다. **죽는 건 stdout 이
파이프/파일로 리다이렉트될 때**다 (`sys.stdout.encoding == 'cp949'` 가 된다). 그러면
`class-watcher watch ... > log.txt` 나 CI 로그 수집 같은 평범한 사용에서 터진다.

소스 전체를 훑어 cp949 로 인코딩 불가능한 문자열 리터럴 **8개**를 찾았는데, 그중 **출력되는
것은 `cli.py:131` 하나뿐**이고 나머지 7개는 docstring·주석이라 무해하다.
`[FINALIZING] 감시 중지 · debounce flush` 의 가운뎃점(`·`)은 cp949 에 있어서 안전하다.
**즉 `cli.py:131` 의 `—` 를 `-` 로 바꾸는 한 글자 수정이면 끝난다.**


### (라) 별건 — 중단된 세션의 `session.json` 이 "변경 없음"이라고 거짓말한다 (1단계 코드)

위 (가) 3번에서 나왔다. `[ABORTED]` 로 닫힌 세션인데 `watched_files` 가 네 파일 전부
`unchanged` 로 남았다. 같은 세션의 `events.jsonl` 은 `New.java` 변경을 기록했고
해시도 실제로 다르다 (baseline `3c756514…` 146B vs event `97ccd12f…` 166B).

원인은 갱신 경로가 하나뿐인 것이다. `watched_files` 는 세션 시작 때 `session.py:113` 이
전부 `unchanged` 로 한 번 쓰고, 갱신은 `watcher.py:342` 의 **정상 종료 경로에만** 있다.
abort 분기(`watcher.py:325`)는 `write_status(FAILED, ...)` 만 부르고 지나가므로 시작 시점
값이 그대로 굳는다. `no_change` 는 abort 경로에서 아예 쓰이지 않으므로 문제없다 —
**거짓말하는 필드는 `watched_files` 하나다.**

지금은 아무도 안 읽어서 무해하다. 하지만 **2단계부터 `session.json` 을 소비하기 시작하면
중단된 세션을 "변경 없음"으로 오독한다.**

권고: abort 시 `watched_files` 를 **빼거나 `"unknown"` 으로 표시한다** — 계산하지 않았다는
사실을 그대로 남기는 쪽이다. baseline↔현재 해시를 다시 비교해 채우는 방법도 있지만, abort 는
"빨리 끝내라"는 요청이라 파일 스캔을 한 번 더 도는 것은 그 의도와 충돌한다.

## 6. 이미 내려진 결정 — 다시 논쟁하지 말 것

| # | 결정 | 근거 |
|---|---|---|
| C-15 | `--subject`/과목 옵션은 **삭제됐다. 누락이 아니다.** | JUDGE 반박 #8 이 FR-004 본문("과목")과 PRD 10.2 옵션표의 불일치를 잡아냈고 사람이 PRD 를 고쳐 해소했다. **옵션의 정본 목록은 10.2 다** — FR 본문과 어긋나면 10.2 를 따르고 FR 을 고친다 |
| — | 패키지 내부 import 는 **상대**(`from .x`) | ruff isort 가 `class_watcher` 를 first-party 로 분류 못 하면 I001 이 난다. 상대 import 는 분류와 무관하게 안전 |
| — | `DEFAULT_INCLUDE` 에서 `*.json`·`*.md` 제외 | PRD 는 "언어별 기본 allowlist" 라고만 쓰고 열거하지 않는다. lock 파일·문서 노이즈 때문에 뺐고 `--include` 로 덮어쓴다 |
| — | 종료 코드 4종 | `EXIT_OK=0`, `EXIT_RUNTIME=1`, `EXIT_CONFIG=2`, `EXIT_ABORTED=130` (PRD 10.3, C-10 으로 7종→4종) |
| — | symlink 디렉터리는 **전부** 미하강 | PRD 13.3 은 "루트 밖으로 나가는 symlink" 한정이지만 구현이 더 엄격한 쪽이라 수용 기준 위반이 아니다 |
| — | 게이트 명령(`pytest`·`ruff`·`mypy`)은 에이전트가 **승인 없이 실행**한다 | 안 열면 에이전트가 자기 산출물을 검증할 수 없다. 세 모듈로만 못박았고 판정권은 여전히 셸에 있다 (`1e7fe58`) |
| — | 0단계의 ruff E501 은 **파이프라인 밖에서** 고쳤다 | 재시도에 ~$13 이 드는데 고칠 대상이 주석 한 줄이었다. 근본 원인(권한)은 같은 주행에서 고쳤다. **1단계부터는 파이프라인 안에서 닫는다** |

## 7. 하네스 결함 5건 — 전부 이 저장소에서만 고쳐졌다

`orchestrate.sh` 는 DMS 프로젝트에서 포팅한 것이고, **이 머신에 사본이 3개 있다.**

```
/c/Users/ksmart/class-code-watcher/orchestrate.sh      ← 여기 (고쳐진 유일한 사본)
/c/Users/ksmart/dms-auto-classify/orchestrate.sh       756줄
/c/Users/ksmart/Nyangmeong_care_dms/orchestrate.sh     756줄  (위와 완전히 동일)
```

| # | 결함 | 이 저장소 | 다른 두 사본 |
|---|---|---|---|
| ① | `FALLBACK_*` 이 레이트 리밋을 못 받아 단계가 그냥 죽는다 | ✅ `bbcc316` | ❌ 있음 |
| ② | 에이전트가 pytest·ruff·mypy 를 실행하지 못한다 | ✅ `1e7fe58` | ❌ 있음 |
| ③ | 스트림의 비-JSON 줄 하나에 단계가 통째로 죽는다 | ✅ `f475c9a` | ❌ 있음 |
| ④ | 검증 명령이 안 돌아오면 파이프라인이 조용히 매달린다 | ✅ `962f532` | ❌ 있음 |
| ⑤ | 낡은 테스트가 impl 을 막으면 verify 가 시작조차 못 하는 교착 | ✅ `bc702e7` | ❌ 있음 |

**셋 다 CLI 버그가 아니라 하네스 결함이다.** ①만 봐도 `--fallback-model` 의 문서화된 범위는
"overloaded or not available" 인데, 하네스가 그걸 유일한 안전망으로 삼았다.

**④는 1단계에서 실제로 밟았다.** impl 이 `run_watch` 를 진짜 감시 루프로 바꾸자
0단계가 남긴 테스트 3개가 Ctrl+C 를 영원히 기다리게 됐고 `pytest` 전체가 멈췄다.
`run_verify` 에 상한이 없어 셸이 거기서 무한정 서 있을 수 있었다. **죽는 것보다 나쁘다** —
죽으면 `FAIL_LOG` 라도 남는데 매달리면 아무것도 안 남는다. 감시 루프·네트워크 대기를
다루는 단계부터는 예외가 아니라 기본값이다.

**⑤도 1단계에서 실제로 밟았다.** impl 은 테스트를 못 고치고(프롬프트 금지), 고칠 수 있는
것은 verify 다. 그런데 루프가 `impl → verify` 순서라 impl 이 `BLOCKED` 를 올리면
**verify 가 시작조차 못 한다** — 아무도 고칠 수 없는 교착이다. 소스는 완결됐고
`ruff check src`·`mypy src` 가 통과하는데도 파이프라인이 멈췄다.
`prompts/impl.md` 에 "낡은 테스트" 예외를, `prompts/verify.md` 에 할 일 0번을 넣어 끊었다.

**②가 가장 조용하고 비싸다.** ①③은 죽어서 눈에 보이지만 ②는 에이전트가 자기 산출물을
검증하지 못한 채 "통과할 것"이라고 추측만 하게 만든다. 셸의 `run_verify` 가 최종 판정을
하니 틀린 코드가 통과하지는 않지만, **재시도가 늘어나고 그게 전부 돈이다** — 이 저장소에서
impl 이 주석 한 줄을 두 주행 내내 못 고친 것이 그 증거다.

**5건 전부 실전에서 작동을 확인했다** (1단계 주행 로그):
①은 `fable-5 → opus-5 → sonnet-5` 두 번 갈아탐, ②는 impl 이 게이트를 직접 돌려 조건 판정,
③은 `스트림에 JSON 아닌 줄 1개 — 무시하고 진행`, ④는 impl 의 pytest 가 300초에 끊겨 진단 가능,
⑤는 `BLOCKED` 대신 `DONE` + 인계.

**다른 두 사본은 아직 안 고쳤다.** 별도 세션에 맡길 프롬프트가 `docs/HARNESS_SYNC_PROMPT.md` 에 있다.

> **근본 문제**: 같은 하네스가 세 번 복사됐고 고침이 역류할 경로가 없다.
> 언젠가 별도 리포로 뽑아야 한다. 지금 할 일은 아니지만 네 번째 복사가 생기기 전에는 해야 한다.

## 8. 다음 세션이 놀라지 않으려면

- **`run_watch` 는 이제 스텁이 아니다.** 종료 코드 매핑은 `aborted`→130, `no_change`→0,
  변경 있음→1, 감시 중 `OSError`→1(`error="watch_io_error"`) 이다. 변경 있음 세션의
  `partial` + `error="summary_pipeline_not_implemented"` 은 **과도기 매핑**이고
  2~5단계가 구현되면 바뀐다 (5절 참조).
- **테스트에서 `cli.main(["watch", <디렉터리>])` 를 그냥 부르면 안 돌아온다.** 진짜 감시
  루프라 Ctrl+C 를 기다린다. `watcher._drain_queue` 를 패치해 종료를 주입하는 방식이
  `test_cli.py` 에 이미 있으니 그걸 따라라.
- 세션 디렉터리(`<session_dir>/<id>/` + `baseline/` + `final/` + `session.json`)는 생성되지만
  `events.jsonl`·`errors.jsonl` 은 **경로만 정의하고 파일을 만들지 않는다.**
- preflight 실패(경로 없음 / 파일 / 읽기 불가 / 파일 수 상한 초과)는 `EXIT_CONFIG=2`, 이때
  **세션 디렉터리는 만들지 않는다** (`create_session_dirs` 가 preflight 뒤에 있다).
- `pyproject.toml`·`.env*`·`.gitignore`·`PRD.md`·`requirements*.txt` 는 `orchestrate.sh` 의 `PROTECTED` 지문 감시 대상이다.
  에이전트가 건드리면 즉시 죽는다. **의존성 추가는 사람이 미리 해야 한다.**
- **세션 중 생긴 파일의 이벤트는 계속 `created` 로 찍힌다.** `debounce.py:41` 이 "known_paths 에
  없는 경로의 변경은 신규 편입이라 created 로 승격한다 (FR-017)" 라고 의도를 명시했다. baseline
  기준으로는 계속 신규가 맞고 최종 status 도 `added` 로 정확하다. **2단계 diff 엔진이
  `events.jsonl` 을 읽을 때 "한 파일이 `created` 로 여러 번 나온다"를 전제해야 한다** —
  실측으로 확인했다 (5절 가 1번).
- **중단된 세션의 `session.json` 은 `watched_files` 를 믿으면 안 된다** — 5절 (라).
- 판정 함수(`run_preflight` 등)에는 `print` 가 없다. 콘솔 출력은 `main` 에서만 한다.
- verify 는 `tests/test_*.py` 만 수정할 수 있다 (`prompts/verify.md:62`). **소스의 린트·타입 오류는 impl 만 고칠 수 있다.**
- 0단계 JUDGE 미확인 2건(Windows `os.replace` 원자성, `token_hex(2)`)은 119개 테스트가
  실제로 돌면서 간접 확인됐다. 1단계 미확인 3건 중 **#25(Ctrl+C 반응 시간)는 2026-08-28
  실기기로 해소**됐고, **#23·#24(폴링 전환)는 아직 열려 있다** — 5절 (가) 4번이 푸는 절차다.
- 콘솔 한글이 cp949 로 깨지는 정도가 아니라 **죽는 경로가 하나 있다** — 5절 (다).

## 9. 오픈 이슈 (PRD 17.1)

1. **수신자가 실제로 읽는가** — 제품 관점 최대 리스크. 파일럿 5세션 후 채널에서 직접 확인.
2. **PyInstaller exe 백신 오탐** — 서명 없는 단일 exe 가 학원 PC 백신에 차단될 수 있다.
   **C-02(단일 exe 배포)의 근거가 여기 걸려 있으므로 파일럿 첫날 실제 학원 PC로 검증.** 차단 시 코드 서명 또는 `--onedir`.
3. **secret scanner 오탐률** — 기본 정책이 전송 중단이라 오탐이 잦으면 사용성이 무너진다.
   5세션에서 반복되면 "경고 후 확인"으로 완화 재검토.
4. **알림 빈도** — 하루 여러 세션이면 채널이 시끄러워진다. 그 시점에 주간 다이제스트(P2) 앞당김.

## 10. 환경 — 다른 PC 에서 이어받을 때

`.venv/` 는 커밋되지 않는다 (`.gitignore`). 새 PC 에서는 이렇게 시작한다:

```powershell
git clone https://github.com/pro047/class-code-watcher.git
cd class-code-watcher
py -3.14 -m venv .venv                    # 3.11 이상이면 됨
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q       # 119 passed 나와야 정상
```

`.env` 도 커밋되지 않는다. 1~3단계는 외부 호출이 없어 없어도 되고, 4단계(LLM)부터
`.env.example` 을 복사해 채워야 한다.

**파이프라인은 bash 가 필요하다.** PowerShell 에서는 이렇게 부른다:

```powershell
& "C:\Program Files\Git\bin\bash.exe" ./orchestrate.sh <feature>
& "C:\Program Files\Git\bin\bash.exe" ./approve.sh <feature> DESIGN.md
```

`.pipeline/` 도 커밋되지 않는다. 1단계 산출물(`.pipeline/watch-engine/`)과 승인 마커는
학원 PC 에만 있다 — **2단계는 새 feature 이므로 문제되지 않는다.** 자세한 것은 2.1 절.

### 그 밖의 환경 사실

- Python 3.14.6 (학원 PC), `requires-python >=3.11`
- 의존성은 `pyproject.toml` 에 전부 선언돼 있다 — watchdog 6.0.0, openai, httpx,
  python-dotenv, (dev) pytest·ruff·mypy·pyinstaller. **`PROTECTED` 라 에이전트가 못 건드린다**
- 이 환경에서 `Observer` 는 `WindowsApiObserver`(ReadDirectoryChangesW)로 해석된다
- 비밀값 2개: `OPENAI_API_KEY`, `DISCORD_WEBHOOK_URL`. 공용 PC 에서는 `.env` 를 PC 에
  남기지 말고 exe 와 함께 USB 에 둔다 (FR-054)
- 모델 배치: design·judge·verify = `claude-fable-5`, impl = `claude-opus-5`
- 예산 실적: 0단계 impl $1.3~5.2 / verify $4.8. 1단계 judge $5.6 / impl $2.5 / verify $3.9
