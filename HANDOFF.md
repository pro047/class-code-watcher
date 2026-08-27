# 핸드오프 — Class Code Watcher

- 기준 문서: `PRD.md` v1.1.1 (14절 MVP 단계별 개발 계획)
- 작성 시점: 2026-08-27
- 기준 커밋: `c77e87c` (main) — 0단계 스냅샷 커밋
- 한 줄 요약: **파이프라인 인프라는 완성. 제품 코드는 0단계 "골격"에서 멈춰 있고, 테스트가 0개라 0단계가 아직 닫히지 않았다.**
- verify 사망 원인은 규명됐다 (레이트 리밋). 재실행 지점은 `impl` — 5절 참조.

---

## 1. 이 프로젝트가 무엇인가

수업 중 코드 변경을 감시하다가 세션이 끝나면 diff를 만들고, OpenAI로 요약해서 Discord로 1회 전송하는 CLI 도구다.
읽는 사람은 코드를 보지 않는 팀원 5인이다 — 그래서 "요약이 이해되는가"가 최종 성공 지표다 (PRD 15절).

배포 형태는 PyInstaller 단일 exe + `.env`를 USB에 담아 학원 공용 PC에서 실행 (FR-054).

## 2. 개발 방식 — DMS 파이프라인

제품 코드를 사람이 직접 쓰지 않는다. `orchestrate.sh`가 단계별 에이전트를 돌린다.

```
design → judge → impl → verify
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
# AUTO=1        사람 게이트 건너뜀 (무인)
# MAX_RETRY=3   재시도 횟수
# FRESH_DESIGN=1  DONE 상태인 DESIGN.md 를 무시하고 설계를 새로 뽑음
```

산출물은 `.pipeline/<feature>/` 아래 `DESIGN.md`, `JUDGE.md`, `IMPL.md`, `VERIFY.md`, `STATE.md`, `FAIL_LOG.md`, `MODEL_LOG.md` 로 남는다.
`STATE.md` 는 셸이 자동 생성한다 — **사람이 편집하지 말 것.**

검증 게이트 3종: `pytest`, `ruff check .`, `mypy src`.
pytest는 수집 테스트가 0개면 exit 5로 실패한다 — 검증 단계가 테스트를 안 쓰고 넘어가는 걸 막으려는 의도적 설계다.

## 3. 지금 어디까지 왔나

### 커밋 이력

| 커밋 | 내용 |
|---|---|
| `00017cf` | PRD docx 원본 |
| `bde763a` | DMS 파이프라인 포팅 — `orchestrate.sh`(795줄), 프롬프트 5종, `PRD.md`(747줄), `test/run-tests.sh`(737줄), `pyproject.toml` |
| `a4f5da6` | 셸 스크립트 실행 비트 |
| `f475c9a` | 스트림에 JSON 아닌 줄이 섞여도 단계가 안 죽게 수정 + PRD v1.1.1 |
| `c77e87c` | 0단계 골격 구현 스냅샷 (아래 참조) |

`src/class_watcher/__init__.py` 는 docstring + `__version__` 6줄뿐.

### 0단계 스냅샷 커밋 `c77e87c` — `bootstrap-cli` 주행이 생성한 코드 (562줄)

impl 재실행이 덮어쓰기 전에 남긴 스냅샷이다. 결과가 나빠지면 이 커밋으로 되돌린다.

| 파일 | 줄 | 내용 |
|---|---|---|
| `src/class_watcher/cli.py` | 248 | argparse(`watch` 서브커맨드 + PRD 10.2 옵션 12개), `build_config`, `run_preflight`, `bootstrap`, `main`, EXIT 상수 4종. **`run_watch` 는 스텁** |
| `src/class_watcher/config.py` | 139 | 기본값 상수, `WatchConfig`, `Secrets`(repr/str 차단), `load_secrets`, `merge_env`, `mask_secrets` |
| `src/class_watcher/selector.py` | 62 | `Selection`, `is_watched`(순수 판정), `scan_files`(os.walk 래퍼) |
| `src/class_watcher/session.py` | 107 | `SessionStatus` 6종, `generate_session_id`, `SessionPaths`, `create_session_dirs`, `write_session_json`(os.replace 원자적 교체), `initial_session_doc` |

`tests/` 에는 `.gitkeep` 하나뿐. **테스트 파일 0개.**

### 파이프라인 상태: `DIED` — 사인 규명됨 (2026-08-27)

design ✅ → judge ✅ → impl ✅ → **verify 사망 (attempt 1/3)**. `VERIFY.md` 는 생성 안 됨.

**진짜 사인: 레이트 리밋. 스크립트 버그가 아니다.**

`verify.stream.jsonl` 은 6줄이 전부고 실제 작업은 0턴이다 — 시작하자마자 죽었다.

| 줄 | 내용 |
|---|---|
| 1 | `rate_limit_event` — `seven_day` utilization **0.98**, `allowed_warning` |
| 2 | `system/init` — model `claude-fable-5` |
| 3 | `rate_limit_event` — `seven_day_overage_included` utilization **1.0**, status **`rejected`**, `overageDisabledReason: "org_level_disabled"` |
| 4 | 합성 assistant 메시지 — "You've reached your Fable 5 limit." (`is_api_error_message: true`) |
| 5 | `result` — `is_error: true`, `terminal_reason: "api_error"`, `num_turns: 1`, $0.0029 |
| 6 | **JSON 아닌 줄** — `Client.listTools() called but server does not advertise tools capability` |

`modelUsage` 에는 haiku-4-5 의 2845/14 토큰만 있다. 부트스트랩 몇 토큰 쓰고 첫 요청에서 거부당했다.

**`STATE.md` 에 적힌 사인("스트림에 result 이벤트가 없다")은 오진이다.**
result 이벤트는 line 5 에 멀쩡히 있다. 당시 jq 가 line 6 의 비-JSON 줄에서 죽어
`verify.result.json` 이 0바이트가 됐고, 그래서 "없다"고 판정했다.
이 결함은 **이미 닫혔다** — 커밋 `f475c9a`(`orchestrate.sh:382`, `fromjson?`)가 고쳤는데
타이밍이 아슬아슬했을 뿐이다:

```
16:10:16  verify 사망
16:22:16  f475c9a 스트림 관용 파싱 수정   ← 12분 늦었다
```

현재 코드로 같은 스트림을 파싱하면 result 가 정상 추출된다 (실측 확인).

**남은 결함 — 폴백 체인이 레이트 리밋을 못 받는다.**
`FALLBACK_VERIFY="claude-opus-5,claude-sonnet-5"` 가 설정돼 있는데도 fable-5 에서 그냥 죽었다.
`orchestrate.sh:66` 주석이 이유를 이미 적어놨다 — *"FALLBACK_* 은 가용성 폴백(529 과부하 등) 전용"*.
레이트 리밋은 그 경로를 안 탄다. 설계대로 동작한 것이지만, 앞으로 7단계를 더 돌리는 동안
같은 자리에서 또 죽는다. **개선 후보 1순위.**

**리밋은 풀렸다** — `seven_day` resetsAt = 2026-08-27 06:00 KST.

### 게이트 실측 (2026-08-27 직접 실행)

| 게이트 | 결과 |
|---|---|
| `mypy src` | ✅ 통과 — 5 files, no issues |
| `ruff check .` | ❌ **1건** — `src/class_watcher/config.py:12` E501 (103 > 100, 한글 주석) |
| `pytest` | ❌ **exit 5** — no tests ran |
| `class-watcher --help` | ✅ 동작 (콘솔 한글이 cp949로 깨지는 건 별개 이슈) |

## 4. 마일스톤 (PRD 14절) 대비 진척

| 단계 | 범위 | 완료 기준 | 예상 | 상태 |
|---|---|---|---|---|
| **0. 골격** | CLI, 설정, 세션 디렉터리, 로깅 | 잘못된 입력/정상 시작의 자동 테스트 | 0.5일 | 🟡 **코드 완료 / 테스트 0개 → 미완** |
| 1. 감시 | baseline(다중 파일), watchdog, debounce, flush+안정화, final | 저장·원자적 교체·삭제/재생성·신규 파일 시나리오 통과 | **2일** | ⬜ 미착수 |
| 2. Diff | difflib, 바이너리/대용량 제외, 파일별·합산 통계 | 다중 파일 fixture 결과 검증 | 0.5일 | ⬜ 미착수 |
| 3. 정제 | secret scanner, 경로 상대화, 환경정보 제거 | 키 패턴 fixture 전량 탐지, 마스킹 테스트 | 0.5일 | ⬜ 미착수 |
| 4. LLM | 프롬프트, strict schema, 1회 호출 + 1회 재시도, fallback | mock 기반 호출 횟수·스키마·timeout 테스트 | 1일 | ⬜ 미착수 |
| 5. Discord | 메시지 렌더링, 모바일 가독성, Webhook, 실패 보존 | 204/4xx/5xx mock 테스트 | 0.5일 | ⬜ 미착수 |
| 6. 통합 | 상태 전이, 종료 코드, 마스킹 E2E | E2E 10회 연속 성공, 오류별 산출물 검증 | 1일 | ⬜ 미착수 |
| 7. 배포 | PyInstaller 단일 exe, `.env` 템플릿, USB 실행 검증 | Python 없는 PC에서 실행 성공 | 0.5일 | ⬜ 미착수 |

합계 6.5일 중 **0.5일치가 90% 진행** — 실질 진척률 약 7%.

부수 체크리스트도 전량 미완이다:
- PRD 14.1 MVP 테스트 체크리스트 **19항목 전부 미체크**
- PRD 18절 DoD **7조건 전부 미충족**

## 5. 다음에 할 일 — 재실행 지점은 `impl`

0단계 완료 기준은 "잘못된 입력 / 정상 시작의 자동 테스트"다. 코드는 있고 테스트가 없다.
필요한 것: `tests/test_cli.py`, `tests/test_config.py`, `tests/test_selector.py`, `tests/test_session.py`
(설계가 지정한 4파일) + ruff E501 1건 수정.

### 재실행하면 어디로 들어가는가

`./orchestrate.sh bootstrap-cli` 를 그대로 돌리면 이렇게 흘러간다 — **스크립트를 손댈 필요 없다.**

| 단계 | 동작 | 근거 |
|---|---|---|
| design | **재사용** | `DESIGN.md` STATUS: DONE (`orchestrate.sh:666`) |
| 설계 게이트 | **자동 통과** | `DESIGN.md.approved` 해시 유효 (검증 완료) |
| judge | **재사용** | `JUDGE.md`(15:50)가 `DESIGN.md`(15:45)보다 최신 (`:678`) |
| judge 게이트 (force) | **자동 통과** | `JUDGE.md.approved` 해시 유효 — force 게이트를 넘는 유일한 무인 경로 |
| **impl** | **재실행** | 스킵 로직이 design/judge 에만 있다. 재시도 루프가 `run_stage impl` 부터 시작 (`:752`) |
| verify | 재실행 | (`:758`) |

### impl 재실행은 낭비가 아니라 필수다

ruff E501 은 `src/class_watcher/config.py:12` — **소스 파일**이다.
`prompts/verify.md:62` 가 verify 에게 *"소스 코드를 수정하지 않는다. 고칠 수 있는 것은
`tests/test_*.py` 뿐이다"* 라고 못박고 있다. verify 를 아무리 돌려도 그 1건은 안 고쳐지고,
`run_verify` 가 ruff 에서 실패해 루프가 어차피 impl 로 돌아온다.

### 실행

```bash
PY=.venv/Scripts/python ./orchestrate.sh bootstrap-cli
```

주의: **impl 이 현재 소스 4파일을 덮어쓴다.** 그래서 재실행 전에 스냅샷 커밋을 남겼다
(`c77e87c`). 결과가 나빠지면 그 커밋으로 되돌리면 된다.

예산: impl $8 / opus-5 / 80턴, verify $5 / fable-5 / 40턴.
fable-5 주간 창이 또 차 있으면 `MODEL_VERIFY=claude-opus-5` 로 우회할 수 있다.

## 6. 이미 내려진 결정 — 다시 논쟁하지 말 것

| # | 결정 | 근거 |
|---|---|---|
| C-15 | `--subject`/과목 옵션은 **삭제됐다. 누락이 아니다.** | JUDGE 반박 #8 이 FR-004 본문("과목")과 PRD 10.2 옵션표의 불일치를 잡아냈고, 사람이 PRD 를 고쳐 해소했다. **옵션의 정본 목록은 10.2 다** — FR 본문과 어긋나면 10.2 를 따르고 FR 을 고친다 |
| — | 패키지 내부 import 는 **상대**(`from .x`) | ruff isort 가 `class_watcher` 를 first-party 로 분류 못 하면 I001 이 난다. 상대 import 는 분류와 무관하게 안전 |
| — | `DEFAULT_INCLUDE` 에서 `*.json`·`*.md` 제외 | PRD 는 "언어별 기본 allowlist" 라고만 쓰고 열거하지 않는다. lock 파일·문서 노이즈 때문에 뺐고 `--include` 로 덮어쓴다 |
| — | 종료 코드 4종 | `EXIT_OK=0`, `EXIT_RUNTIME=1`, `EXIT_CONFIG=2`, `EXIT_ABORTED=130` (PRD 10.3, C-10 으로 7종→4종) |
| — | symlink 디렉터리는 **전부** 미하강 | PRD 13.3 은 "루트 밖으로 나가는 symlink" 한정이지만 구현이 더 엄격한 쪽이라 수용 기준 위반이 아니다 |

## 7. 다음 세션이 놀라지 않으려면

- **정상 경로에서 `main(["watch", str(tmp)])` 는 `1` 을 반환한다.** `run_watch` 가 스텁이라 `session.json` 을 `status="failed"`, `error="not_implemented"` 로 갱신하고 `EXIT_RUNTIME` 을 돌려준다. 버그가 아니라 0단계의 정의된 동작이다.
- 세션 디렉터리(`<session_dir>/<id>/` + `baseline/` + `final/` + `session.json`)는 생성되지만 `events.jsonl`·`errors.jsonl` 은 **경로만 정의하고 파일을 만들지 않는다.**
- preflight 실패(경로 없음 / 파일 / 읽기 불가 / 파일 수 상한 초과)는 `EXIT_CONFIG=2`, 이때 **세션 디렉터리는 만들지 않는다** (`create_session_dirs` 가 preflight 뒤에 있다).
- `pyproject.toml`·`.env*`·`.gitignore`·`PRD.md` 는 `orchestrate.sh:708` 의 `PROTECTED` 지문 감시 대상이다. 에이전트가 건드리면 잡힌다.
- JUDGE 미확인 2건이 남아 있다 — Windows `os.replace` 의 원자성, `secrets.token_hex(2)` 가 소문자 hex 4자인지. 둘 다 표준 동작이라 위험은 낮고, 0단계 테스트가 실행되면 간접 확인된다.
- 판정 함수(`run_preflight` 등)에는 `print` 가 없다. 콘솔 출력은 `main` 에서만 한다.

## 8. 오픈 이슈 (PRD 17.1)

1. **수신자가 실제로 읽는가** — 제품 관점 최대 리스크. 파일럿 5세션 후 채널에서 직접 확인.
2. **PyInstaller exe 백신 오탐** — 서명 없는 단일 exe 가 학원 PC 백신에 차단될 수 있다. **C-02(단일 exe 배포)의 근거가 여기 걸려 있으므로 파일럿 첫날 실제 학원 PC로 검증.** 차단 시 코드 서명 또는 `--onedir`.
3. **secret scanner 오탐률** — 기본 정책이 전송 중단이라 오탐이 잦으면 사용성이 무너진다. 5세션에서 반복되면 "경고 후 확인"으로 완화 재검토.
4. **알림 빈도** — 하루 여러 세션이면 채널이 시끄러워진다. 그 시점에 주간 다이제스트(P2) 앞당김.

## 9. 환경

- Python 3.14.6 (`.venv/pyvenv.cfg`), `requires-python >=3.11`
- editable 설치됨 — `.venv/Scripts/class-watcher.exe` 존재, 진입점 `class_watcher.cli:main`
- 비밀값 2개: `OPENAI_API_KEY`, `DISCORD_WEBHOOK_URL` (`.env.example` 참조, `.env` 는 커밋 안 됨)
- 공용 PC 에서는 `.env` 를 PC 에 남기지 말고 exe 와 함께 USB 에 둔다 (FR-054)
