# 핸드오프 — Class Code Watcher

- 기준 문서: `PRD.md` v1.1.1 (14절 MVP 단계별 개발 계획)
- 갱신 시점: 2026-08-27
- 기준 커밋: `092594a` (main)
- 한 줄 요약: **0단계 "골격" 완료 — 게이트 3종 녹색. 다음은 1단계 "감시"(PRD 최대 단계, 2일).**

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

산출물은 `.pipeline/<feature>/` 아래 `DESIGN.md`, `JUDGE.md`, `IMPL.md`, `VERIFY.md`, `STATE.md`, `FAIL_LOG.md`, `MODEL_LOG.md` 로 남는다 (`.gitignore` 대상 — 커밋되지 않는다).
`STATE.md` 는 셸이 자동 생성한다 — **사람이 편집하지 말 것.**

검증 게이트 3종: `pytest`, `ruff check .`, `mypy src`.
pytest는 수집 테스트가 0개면 exit 5로 실패한다 — 검증 단계가 테스트를 안 쓰고 넘어가는 걸 막으려는 의도적 설계다.

**게이트 판정권은 셸에 있다.** 에이전트가 "통과했다"고 쓴 문장은 읽지 않고 `run_verify` 가 직접 돌려 판정한다.

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

### 0단계 산출물

소스 4모듈 562줄 + 테스트 4파일 695줄.

| 파일 | 줄 | 내용 |
|---|---|---|
| `src/class_watcher/cli.py` | 248 | argparse(`watch` + PRD 10.2 옵션 12개), `build_config`, `run_preflight`, `bootstrap`, `main`, EXIT 4종. **`run_watch` 는 스텁** |
| `src/class_watcher/config.py` | 140 | 기본값 상수, `WatchConfig`, `Secrets`(repr/str 차단), `load_secrets`, `merge_env`, `mask_secrets` |
| `src/class_watcher/selector.py` | 62 | `Selection`, `is_watched`(순수 판정), `scan_files` |
| `src/class_watcher/session.py` | 107 | `SessionStatus` 6종, `generate_session_id`, `SessionPaths`, `write_session_json`(원자적 교체) |
| `tests/test_cli.py` | 308 | 파서, `build_config`, `run_preflight`, `bootstrap`, 종료 코드, argparse SystemExit 환원 |
| `tests/test_config.py` | 135 | 기본 상수, Secrets 차단, `merge_env`, `mask_secrets` |
| `tests/test_selector.py` | 113 | `is_watched` 순수 판정, `scan_files` |
| `tests/test_session.py` | 139 | 상태 6종, id 생성, 원자적 교체 |

**게이트 실측 (2026-08-27):** `pytest` 61 passed / `ruff` All checks passed / `mypy` no issues.
PRD 14절 0단계 완료 기준("잘못된 입력·정상 시작의 자동 테스트") 충족.

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
레이트 리밋은 그 경로를 안 탄다. **아직 안 고쳤다. 개선 후보 1순위** (7절 참조).

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
| **1. 감시** | baseline(다중 파일), watchdog, debounce, flush+안정화, final | 저장·원자적 교체·삭제/재생성·신규 파일 시나리오 통과 | **2일** | ⬜ **다음 차례** |
| 2. Diff | difflib, 바이너리/대용량 제외, 파일별·합산 통계 | 다중 파일 fixture 결과 검증 | 0.5일 | ⬜ 미착수 |
| 3. 정제 | secret scanner, 경로 상대화, 환경정보 제거 | 키 패턴 fixture 전량 탐지, 마스킹 테스트 | 0.5일 | ⬜ 미착수 |
| 4. LLM | 프롬프트, strict schema, 1회 호출 + 1회 재시도, fallback | mock 기반 호출 횟수·스키마·timeout 테스트 | 1일 | ⬜ 미착수 |
| 5. Discord | 메시지 렌더링, 모바일 가독성, Webhook, 실패 보존 | 204/4xx/5xx mock 테스트 | 0.5일 | ⬜ 미착수 |
| 6. 통합 | 상태 전이, 종료 코드, 마스킹 E2E | E2E 10회 연속 성공, 오류별 산출물 검증 | 1일 | ⬜ 미착수 |
| 7. 배포 | PyInstaller 단일 exe, `.env` 템플릿, USB 실행 검증 | Python 없는 PC에서 실행 성공 | 0.5일 | ⬜ 미착수 |

합계 6.5일 중 0.5일 완료 — **약 8%**.

부수 체크리스트는 아직 전량 미완이다:
- PRD 14.1 MVP 테스트 체크리스트 **19항목** — 0단계 범위 밖 항목이 대부분이라 그대로다
- PRD 18절 DoD **7조건** 전부 미충족

## 5. 다음에 할 일 — 1단계 "감시"

PRD 14절에서 **가장 큰 단계(2일)**다. 여기서 `cli.run_watch` 스텁이 실물이 된다.

범위: baseline 다중 파일 스냅샷, watchdog 이벤트, debounce(기본 750ms), flush+안정화, final 스냅샷.
완료 기준: 저장·원자적 교체·삭제/재생성·신규 파일 시나리오 통과.

관련 FR: FR-014(저장 중 Ctrl+C 에도 안정 상태), FR-016(네트워크 드라이브 폴링 전환), FR-017(세션 중 신규 파일).

```bash
PY=.venv/Scripts/python ./orchestrate.sh watch-engine
```

**시작 전에 정할 것 두 가지:**

1. **폴백 체인이 레이트 리밋을 못 받는 문제를 먼저 고칠지** (7절). 2일짜리 단계를 리밋 창에 걸쳐 돌리면
   1차와 같은 방식으로 죽는다. 값싼 수정이고 남은 7단계 내내 효과가 있다.
2. **watchdog 의존성.** `pyproject.toml` 에 있는지 확인하고, 없으면 추가해야 한다 —
   그런데 `pyproject.toml` 은 `PROTECTED` 지문 감시 대상이라 **에이전트가 못 건드린다.**
   사람이 미리 넣어 두지 않으면 impl 이 그 자리에서 막힌다.

**설계 단계에서 미리 못박아 둘 것**: DESIGN.md 의 "검증 기준"에 [테스트 가능] 항목을 충분히 넣어야 한다.
verify 는 그 목록을 pytest 로 옮기는 일만 한다. 0단계에서 24개 → 61 케이스가 나왔다.
watchdog 의 실제 파일시스템 이벤트는 OS·타이밍·백신에 의존해 불안정하므로,
`prompts/verify.md:30` 이 경고한 대로 **판정 함수를 순수하게 떼어내 테스트 가능하게** 설계해야 한다.

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

## 7. 아직 안 고친 것 — 다음 주행 전에 볼 것

**`FALLBACK_*` 이 레이트 리밋을 못 받는다.** `orchestrate.sh:66` 이 스스로 밝히듯 폴백은 529 가용성 전용이다.
1차 주행이 이것 때문에 fable-5 에서 죽었고, `FALLBACK_VERIFY` 에 opus-5·sonnet-5 가 있었는데도 안 탔다.

고칠 자리: `run_stage` 가 `result` 의 `terminal_reason == "api_error"` + 스트림의 `rate_limit_event.status == "rejected"` 를
감지하면 폴백 체인의 다음 모델로 자동 재시도. 지금은 그냥 죽고 사람이 `MODEL_VERIFY=` 로 손수 우회해야 한다.

1단계가 2일짜리라 리밋 창에 걸릴 확률이 높다. **여기 들어가기 전에 고치는 게 값싸다.**

## 8. 다음 세션이 놀라지 않으려면

- **정상 경로에서 `main(["watch", str(tmp)])` 는 `1` 을 반환한다.** `run_watch` 가 스텁이라 `session.json` 을
  `status="failed"`, `error="not_implemented"` 로 갱신하고 `EXIT_RUNTIME` 을 돌려준다. 버그가 아니라 0단계의 정의된 동작이다.
  **1단계가 이걸 바꾼다** — 관련 테스트도 같이 갱신해야 한다.
- 세션 디렉터리(`<session_dir>/<id>/` + `baseline/` + `final/` + `session.json`)는 생성되지만
  `events.jsonl`·`errors.jsonl` 은 **경로만 정의하고 파일을 만들지 않는다.**
- preflight 실패(경로 없음 / 파일 / 읽기 불가 / 파일 수 상한 초과)는 `EXIT_CONFIG=2`, 이때
  **세션 디렉터리는 만들지 않는다** (`create_session_dirs` 가 preflight 뒤에 있다).
- `pyproject.toml`·`.env*`·`.gitignore`·`PRD.md`·`requirements*.txt` 는 `orchestrate.sh` 의 `PROTECTED` 지문 감시 대상이다.
  에이전트가 건드리면 즉시 죽는다. **의존성 추가는 사람이 미리 해야 한다.**
- 판정 함수(`run_preflight` 등)에는 `print` 가 없다. 콘솔 출력은 `main` 에서만 한다.
- verify 는 `tests/test_*.py` 만 수정할 수 있다 (`prompts/verify.md:62`). **소스의 린트·타입 오류는 impl 만 고칠 수 있다.**
- 콘솔 한글이 cp949 로 깨진다 (`class-watcher --help`). 기능 문제는 아니지만 PRD 10.1 의 사용자 경험과 어긋난다 — 언젠가 볼 것.
- JUDGE 미확인 2건(Windows `os.replace` 원자성, `secrets.token_hex(2)` 가 소문자 hex 4자)은
  이제 61개 테스트가 실제로 돌면서 간접 확인됐다.

## 9. 오픈 이슈 (PRD 17.1)

1. **수신자가 실제로 읽는가** — 제품 관점 최대 리스크. 파일럿 5세션 후 채널에서 직접 확인.
2. **PyInstaller exe 백신 오탐** — 서명 없는 단일 exe 가 학원 PC 백신에 차단될 수 있다.
   **C-02(단일 exe 배포)의 근거가 여기 걸려 있으므로 파일럿 첫날 실제 학원 PC로 검증.** 차단 시 코드 서명 또는 `--onedir`.
3. **secret scanner 오탐률** — 기본 정책이 전송 중단이라 오탐이 잦으면 사용성이 무너진다.
   5세션에서 반복되면 "경고 후 확인"으로 완화 재검토.
4. **알림 빈도** — 하루 여러 세션이면 채널이 시끄러워진다. 그 시점에 주간 다이제스트(P2) 앞당김.

## 10. 환경

- Python 3.14.6 (`.venv/pyvenv.cfg`), `requires-python >=3.11`
- editable 설치됨 — `.venv/Scripts/class-watcher.exe` 존재, 진입점 `class_watcher.cli:main`
- 비밀값 2개: `OPENAI_API_KEY`, `DISCORD_WEBHOOK_URL` (`.env.example` 참조, `.env` 는 커밋 안 됨)
- 공용 PC 에서는 `.env` 를 PC 에 남기지 말고 exe 와 함께 USB 에 둔다 (FR-054)
- 모델 배치: design·judge·verify = `claude-fable-5`, impl = `claude-opus-5`
- 예산: design/judge/verify $5, impl $8. 턴: design/judge/verify 40, impl 80
