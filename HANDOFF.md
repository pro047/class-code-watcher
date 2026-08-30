# 핸드오프 — Class Code Watcher

- 기준 문서: `PRD.md` v1.1.2 (14절 MVP 단계별 개발 계획)
- 갱신 시점: 2026-08-28
- 기준 커밋: `55ecc33` (main) + **미커밋 워킹트리에 4단계 구현**
- 갱신 시점: 2026-08-30
- 한 줄 요약: **0~4단계 코드 완료 — 테스트 268개, 게이트 3종 녹색.
  파이프라인이 baseline→감시→diff→정제→요약(`summary.json`)까지 이어졌다.
  단 실 OpenAI 호출은 아직 0회이고 3단계 실기기 확인도 0/2 다 — 5단계로 가기 전에
  사람이 닫아야 할 것이 `VERIFY.md` 3절에 있다. 다음은 5단계 "Discord"(0.5일).**

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
- **새 feature 로 시작하는 단계에는 영향이 없다.** 3단계는 `redactor` 라 무관하다
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
| `cd27f34`·`652082b` | 핸드오프 갱신 — 다른 PC 인계 절차, `.pipeline/` 절 |
| `9b997c5`·`c42ded2` | 1단계 실기기 확인 4건 반영 + 거기서 나온 결함 2건 수정 |
| `cd730af` | 2단계 작업 내용을 핸드오프에 반영 |
| `3f2b543` | **2단계 diff 엔진 — 테스트 147개, 게이트 3종 통과. 2단계 완료** |
| `86a149b`·`23f1d98` | 핸드오프 갱신 — 2단계 실기기 확인 3건, `session.json` 사용자명 누출 기록 |
| `e0b4837` | **3단계 정제 — `redact.py` 신규, 테스트 192개, 게이트 3종 통과. 3단계 완료** |
| `70c00d1` | 드라이브 판정 테스트의 실행 OS 종속 수정 (macOS 에서 드러남) |
| `96263db` | **보호 파일 지문 감시에 `PRD.md` 추가 + design·judge 까지 확장** |
| `55ecc33` | 핸드오프 갱신 — 3단계 완료 반영, 4단계 인계 |
| *(미커밋)* | **4단계 LLM — `summarize.py`·`openai_client.py` 신규, 테스트 268개** + 승인 범위 배선 |

### 0~4단계 산출물 — 소스 15모듈 / 테스트 268개

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
| `watcher.py` | 1→2 | watchdog 배선·감시 루프·finalize (**부작용 층, 얇게**) + diff 호출 1지점 |
| `diffgen.py` | 2 | FR-020·022·023·024 — 순수 함수 9개 + 부작용 3개. git 호출 0건 |
| `redact.py` | 3 | FR-036·037·038 — 규칙 11종 + `.env` 백스톱, 스캔·마스킹·환경정제. **매치 원문을 담는 자료구조가 없다** |
| `summarize.py` | 4 | FR-030·031·032·039 — 순수 계층. 스키마·프롬프트 조립·예산 절단·수신 검증·fallback. **openai 를 import 하지 않는다** |
| `openai_client.py` | 4 | 부작용 어댑터. SDK 호출 1함수. **외부 API `추정` 전부가 이 파일에만 있다** |

`watcher.py` 는 단계마다 호출 1지점씩 늘었다 — 3단계 정제(diff 성공 직후), 4단계 요약(정제 통과 직후).
538 → **760줄**. `session.py` 는 4단계에서 `summary_json`·`prompt_json` 두 경로가 늘었다.

테스트 14파일 4308줄 / **268 케이스**. `test_watcher.py`(59) · `test_redact.py`(36) ·
`test_summarize.py`(35) · `test_cli.py`(26) 넷이 대부분이다. 전부 실물을 안 띄운다 —
**4단계 테스트는 가짜 `CallFn` 을 주입해서 돌아 openai SDK 를 import 조차 하지 않는다.**

**게이트 실측 (2026-08-30, macOS / Python 3.12.11):**
`pytest` **268 passed** (9.6s) / `ruff` All checks passed / `mypy` **15 files**.
파일별 케이스 수: cli 26 / config 13 / debounce 14 / diffgen 23 / eventlog 3 /
**openai_client 12** / redact 36 / selector 17 / session 9 / snapshot 5 / stability 6 /
**summarize 35** / watcher 59 / watchmode 10.

### 0단계 주행 기록 — 두 번 죽고 세 번째에 닫혔다

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

### 2단계 주행 (08-28) — 재시도 0회, 하네스가 온전한 상태의 첫 주행

| 단계 | 결과 | 비용 | 턴 |
|---|---|---|---|
| design | DONE | $2.91 | 15 |
| judge | DONE — **미확인 0 / 반박 0** | $5.54 | 40 |
| impl | DONE | $2.34 | 31 |
| verify | DONE | $5.58 | 27 |

합계 **$16.4**. 0·1단계와 달리 죽은 단계가 없다. 결함 5건이 전부 고쳐진 뒤 도는 첫
주행이라 예상한 대로였고, 세 가지가 실제로 관측됐다:

- **결함 ② 수정이 judge 를 바꿨다.** judge 가 처음으로 자기 손으로 실측했다 — 허용 명령이
  `pytest|ruff|mypy` 로만 열려 있어서 프로브를 **pytest 파일로 만들어** 8케이스를 돌리고
  지웠다. `python -c` 직접 실행은 여전히 거부됐고 `judge.result.json` 의
  `permission_denials` 에 남아 있다. 0단계 judge 가 "미확인 2건"을 남긴 자리다
- **결함 ⑤ 예외 경로가 설계대로 돌았다.** 예고했던 `test_watcher.py:255` 의 낡은
  `change_stats` 단언을 impl 이 `BLOCKED` 대신 `DONE` + 인계로 넘겼고 verify 가 고쳤다
- **judge 의 비고 4건을 impl 이 전부 지켰다** — diff 호출이 `KeyboardInterrupt` try
  **안**에 있고, 그 앞에 `is_no_change` 가드가 있고, `errors.jsonl` 의 첫 사용처가 여기
  생겼다. 설계 문장이 참이 되는 조건을 judge 가 미리 못박은 것이 그대로 작동했다

**파이프라인 밖에서 결함 1건을 같이 닫았다** — 6절 관례. 5절 (마) 참조.

### 3단계 주행 (08-28) — 커밋 `e0b4837`

**주의: 이 절은 코드·커밋만으로 재구성했다.** 주행은 다른 PC 에서 돌았고 `.pipeline/redactor/`
는 이 리포에 없다 (2.1 절 — 커밋 대상이 아니라 돌린 PC 에만 있다). 따라서 **단계별 비용·턴 수·
재시도 횟수는 여기 없다.** 그 PC 에서 `jq -r '.total_cost_usd' .pipeline/redactor/*.result.json`
으로 뽑아 이 표를 채워라 — 4단계 예산 산정의 유일한 근거다.

산출물로 확인된 것: `redact.py` 373줄 신규, `watcher.py` +84줄, `cli.py` +7줄,
`session.py` +3줄, 테스트 3파일 +725줄. 소스와 테스트가 한 커밋에 같이 들어왔고
게이트 3종이 녹색이다.

#### judge 판정 6건이 실제로 어떻게 풀렸나 (전부 `코드 확인`)

5절 (나) 가 "judge 가 물고 늘어질 지점"으로 예고한 6건이다. **4번과 6번은 예고와 다르게
풀렸다** — 후속 단계가 문서만 믿으면 틀린다.

| # | 쟁점 | 결론 | 근거 |
|---|---|---|---|
| 1 | `redaction.json` 스키마 | `schema_version: "1.1"`. **탐지 원문을 담을 필드가 없다** — `SecretFinding` 이 `(rule_id, rel_path, line_no)` 뿐 | `redact.py:87` |
| 2 | 스캔 범위 | diff 본문 **+ 파일 경로 목록** 둘 다 스캔. 다만 **마스킹은 본문에만** — "경로를 가리면 요약이 무슨 파일인지 말할 수 없다" | `redact.py:282,296` |
| 3 | 사용자명 하한 | `MIN_BARE_USERNAME_LENGTH = 6`. 미만이면 맨몸 치환을 건너뛰고 `bare_username_skipped` 로 **건너뛴 사실을 기록** | `redact.py:30` |
| 4 | 종료 코드 충돌 | **`error` 뿐 아니라 `status` 까지 갈랐다.** 탐지-중단 = `failed` + `secrets_detected`, 과도기 = `partial` + `summary_pipeline_not_implemented`. 종료 코드는 **둘 다 1** | `watcher.py:509,512` |
| 5 | 오탐 정책 | 완화 정책을 만들지 않고 `by_rule`(규칙별 건수)을 `redaction.json`·`session.json` 양쪽에 남긴다 — 파일럿 데이터를 모으는 쪽 | `redact.py:315` |
| 6 | 정제본을 어디에 두나 | **어디에도 안 둔다.** `RedactionResult.text` 는 메모리에만 있고 `run_session` 이 끝나면 사라진다 — `WatchOutcome` 이 나르는 것은 `secrets_blocked: bool` 하나다 | `watcher.py:79` |

**6번이 4단계 설계의 첫 입력이다.** `final.diff` 는 원본 그대로 남고 `redaction.json` 에도
본문이 없으므로, 4단계는 정제본을 **넘겨받는 게 아니라 다시 만들어야 한다.** `redact_diff` 를
4단계가 직접 부르든 `WatchOutcome` 의 반환 경로를 넓히든, 어느 쪽인지 설계가 정해야 한다.

#### 스캐너 규칙 — 11종 + 백스톱 1

`private_key_header` / `openai_api_key` / `aws_access_key_id` / `github_token` / `slack_token` /
`jwt` / `bearer_token` / `discord_webhook` / `url_credentials` / `jdbc_credentials` /
`assignment_secret`, 그리고 **`known_secret`** — `.env` 의 실제 값을 정확 일치로 거는
백스톱이다(8자 이상만). 벤더 키 형식이 바뀌어 정규식이 헛나가도 내 키는 형식과 무관하게
잡힌다. 규칙 표는 `redact.py:50` 한 곳이고 한 행이 한 규칙이다.

스캔 순서가 설계 결정이다 — **원문에 먼저 스캔을 돌리고 그다음에 마스킹·환경정제**를 한다.
환경정제가 먼저 돌면 커넥션 문자열 속 사용자명이 치환되면서 비밀값 패턴이 깨져 검사를
빠져나갈 수 있다 (`redact.py:278` 이 이유를 적어놨다).

### 4단계 주행 (08-28~30) — 세 번 죽고 닫혔다. 사망 원인이 전부 달랐다

| 단계 | 결과 | 비용 | 턴 |
|---|---|---|---|
| design | 예산 소진 사망 → **산출물 온전, 사람이 되살림** | $5.05 | 16 |
| judge | DONE — 미확인 3 / 반박 1 | $5.53 | 50 |
| impl 1차 | **레이트 리밋 사망** (계정 세션 한도) | $2.60 | 20 |
| impl 2차 | DONE | $5.42 | 57 |
| verify | **예산 소진 사망** → 테스트는 착지, `VERIFY.md` 유실 | $10.05 | 37 |

합계 **$28.65** — 2단계($16.4)의 1.7배. 사망 3건의 원인이 전부 달랐고 셋 다 하네스가
설계대로 대응했다.

**① design 예산 소진 — 입력이 커졌다.** `BUDGET_DESIGN` 기본값이 $5 인데 $5.05 에서
죽었다. 2단계 design 은 $2.91 이었다. 원인은 입력 증가다 — `PRD.md`(58KB)를 2회,
`HANDOFF.md`를 2회 읽었고 **그 HANDOFF 가 45KB → 57KB 로 26% 커진 뒤였다**
(3단계 반영·4단계 인계를 쓰면서 늘렸다). 토큰 실측: cache_read 544k / cache_creation 114k /
output 29.9k(thinking 12.9k).
→ **다음 단계는 `BUDGET_DESIGN=8` 을 명시해라.** 이 문서는 앞으로도 계속 커진다.

**② `.crashed` 파킹 경로가 처음 돌았다.** design 은 죽기 전에 `DESIGN.md` 를
`STATUS: DONE` 으로 다 썼고, 셸이 `DESIGN.md.crashed` 로 파킹한 뒤 사람 게이트에서 멈췄다.
사람이 내용을 검토하고 `mv` 로 되살렸다 — **$5.05 를 버리지 않았고, 재실행에서 재사용
로직이 집어 design 을 다시 돌리지 않았다.**

**③ impl 레이트 리밋 — 폴백은 돌았지만 통하지 않았다.** `bbcc316` 이 만든 감지 경로가
정상 작동해 opus-5 → sonnet-5 로 갈아탔고 증거를 `impl.ratelimit1.*` 로 보존했다.
그런데 **한도가 모델별이 아니라 계정 세션 단위**라 sonnet 도 1턴에 같은 벽을 만났다.
폴백의 결함이 아니라 폴백으로 풀 수 없는 종류다 — 창이 리셋될 때까지 기다리는 수밖에 없다.
1차가 남긴 763줄(`summarize.py`·`openai_client.py`)은 워킹트리에 그대로 뒀고,
2차 impl 이 그것을 읽고 이어서 배선했다 (`IMPL.md` 1절이 그 사실을 적었다).

**④ verify 예산 소진 — 작업은 끝났는데 문서만 잃었다.** 테스트 4파일이 전부 디스크에
착지한 뒤 `VERIFY.md` 를 `Write` 하는 도중 $10 상한에 걸렸다. `.crashed` 파킹도 못 탔다
(파일이 아예 안 생겼다). **셸의 `run_verify` 는 돌지 못했다** — 사람이 같은 명령 3종을
직접 돌려 판정했고, 에이전트가 죽기 직전 주장한 "268개 통과"가 사실이었다.
`VERIFY.md` 는 사람이 재구성했고 **5절에 "이 문서가 못 가진 것"을 명시했다.**

#### 이 주행이 남긴 하네스 개선 2건

- **보호 파일 감시 확장** (`96263db`) — `PRD.md` 가 `PROTECTED` 밖이었고, 더 큰 문제로
  지문 기준선이 design·judge **뒤에** 찍혀 두 단계가 통째로 검사 밖이었다. 8절 참조.
- **승인 범위 배선** (미커밋) — `PIPELINE_APPROVED_SCOPE`. 아래 참조.

#### 훅 오탐 사고 — `session.py` 를 못 고쳤다

impl 이 `src/class_watcher/session.py` 편집을 두 번 거부당했다. 전역 PreToolUse 훅
(`~/.claude/hooks/sensitive-path-guard.py`)이 **파일명으로** 잡은 것이다:

```
인증·권한·세션 코드(src/class_watcher/session.py)를 수정하려 합니다.
```

이 리포의 `session.py` 는 인증이 아니라 세션 산출물 경로 모듈이다. 무인 주행이라 승인자가
없어 impl 은 `BLOCKED` 대신 경로 소유권을 `summarize.py` 로 임시 이전하고 인계로 남겼다.
**판단은 옳았다** — `BLOCKED` 를 올렸으면 나머지 설계 전부가 미구현으로 남았다.

**푼 방법 (C안)**: 훅에는 이 상황을 위한 탈출구가 이미 있었다 — `PIPELINE_APPROVED_SCOPE`
가 가리키는 파일에 적힌 경로는 통과시킨다 (훅 docstring 이 2026-08-24 alembic 사고를
근거로 기록해 둔 것). **메커니즘은 있는데 `orchestrate.sh` 가 배선을 안 했을 뿐이었다.**
DESIGN 게이트 **뒤**에서 `DESIGN.md` 의 "변경 대상 파일" 표 첫 열을 파싱해
`APPROVED_SCOPE.txt` 로 쓰고 export 한다. 승인의 출처가 사람이 통과시킨 설계 문서다.

훅 매처를 좁히는 안은 버렸다 — 전역 방어가 약해지고, 5단계(`webhook`·`credentials`)·
7단계(`.env` 템플릿)에서 또 막히는 두더지 잡기가 된다.

**실측 (2026-08-30)** — 훅에 직접 JSON 을 먹여 판정을 받았다:

| 상황 | 대상 | 결과 |
|---|---|---|
| 배선 전 | `session.py` | **차단** (사고 재현) |
| 배선 후 | `session.py`·`watcher.py` (목록 안) | 통과 |
| 배선 후 | `auth.py`·`.env`·`migrations/x.py` (목록 밖) | **차단** |

**방어선이 약해진 게 아니라 좁아졌다.** 파싱이 실패하면 목록이 비고 훅은 원래대로
동작한다 (fail-safe — 최악이 "배선 전과 같음").

이탈 자체는 **사람이 손으로 닫았다** (2026-08-30): `SessionPaths` 에 `summary_json`·
`prompt_json` 을 넣고, `summarize.py` 의 임시 상수·헬퍼를 지우고, `watcher.py` 두 줄과
테스트 9곳을 새 표면으로 옮겼다. 268 passed 유지.

## 4. 마일스톤 (PRD 14절) 대비 진척

| 단계 | 범위 | 완료 기준 | 예상 | 상태 |
|---|---|---|---|---|
| **0. 골격** | CLI, 설정, 세션 디렉터리, 로깅 | 잘못된 입력/정상 시작의 자동 테스트 | 0.5일 | ✅ **완료** (`092594a`) |
| **1. 감시** | baseline(다중 파일), watchdog, debounce, flush+안정화, final | 저장·원자적 교체·삭제/재생성·신규 파일 시나리오 통과 | **2일** | ✅ **완료** (`9f8c03c`) |
| **2. Diff** | difflib, 바이너리/대용량 제외, 파일별·합산 통계 | 다중 파일 fixture 결과 검증 | 0.5일 | ✅ **완료** (`3f2b543`) |
| **3. 정제** | secret scanner, 경로 상대화, 환경정보 제거 | 키 패턴 fixture 전량 탐지, 마스킹 테스트 | 0.5일 | ✅ **완료** (`e0b4837`) |
| **4. LLM** | 프롬프트, strict schema, 1회 호출 + 1회 재시도, fallback | mock 기반 호출 횟수·스키마·timeout 테스트 | 1일 | ✅ **코드 완료** (미커밋) — **실 API 호출 0회** |
| **5. Discord** | 메시지 렌더링, 모바일 가독성, Webhook, 실패 보존 | 204/4xx/5xx mock 테스트 | 0.5일 | ⬜ **다음 차례** — 범위·판정 지점은 5절 (나) |
| 6. 통합 | 상태 전이, 종료 코드, 마스킹 E2E | E2E 10회 연속 성공, 오류별 산출물 검증 | 1일 | ⬜ 미착수 |
| 7. 배포 | PyInstaller 단일 exe, `.env` 템플릿, USB 실행 검증 | Python 없는 PC에서 실행 성공 | 0.5일 | ⬜ 미착수 |

합계 6.5일 중 4.5일 완료 — **약 69%**. 다만 **코드 기준**이다 (아래 참조).

1단계는 자동 테스트뿐 아니라 **실기기 확인 6건 중 4건까지 통과**했다 (5절 가).
남은 2건(폴링 전환·자원 사용)은 진행을 막지 않는다.
2단계도 **실기기 확인 5건 중 3건까지 통과**했다 (5절 가) — diff 가독성·통계 정확도·
경로 누출 없음이 실물 세션으로 확인됐다. 남은 2건(git 미설치·cp949 리다이렉트)은
4단계 진행을 막지 않는다.
**3단계는 실기기 확인이 아직 0건이다** (5절 가) — 자동 테스트 36케이스는 전부 문자열
단위라, 실제 키를 심은 세션이 정말 차단되는지는 사람만 확인할 수 있다.
**4단계도 실 API 호출이 0회다** — 268개 테스트가 전부 가짜 `CallFn` 주입으로 돈다
(설계 의도: 네트워크 없는 PC 에서도 게이트가 돌아야 한다). `추정` 3건이 그대로 열려 있다.

> **"69% 완료"를 그대로 믿지 마라.** 이 수치는 PRD 14절의 일수 배분에 코드 완료를 곱한
> 것이고, **사람만 닫을 수 있는 항목은 세지 않는다.** 3단계 실기기 2건과 4단계 실 API
> 5건이 열린 채다 — 5단계로 넘어가기 전에 `VERIFY.md` 3절을 먼저 볼 것.

부수 체크리스트:
- PRD 14.1 MVP 테스트 체크리스트 19항목 중 **3항목이 자동 테스트 수준에서 닫혔다** —
  FR-036(키 fixture 전량 탐지·전송 차단), FR-037(절대 경로·사용자명 미포함),
  **FR-030(호출 1회 / 변경없음 0회 / 스키마 실패 최대 2회)**.
  **실기기·실 API 확인은 아직이다** — 위 문단 참조
- PRD 18절 DoD **7조건** 전부 미충족

## 5. 다음에 할 일

### (가) 사람만 할 수 있는 것 — 실기기 확인 (1단계 4/6, 2단계 3/5, **3단계 0/2, 4단계 0/6**)

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

**2단계 실기기 확인 5건 중 3건 통과 (2026-08-28).** `VERIFY.md` 5절이 남긴 것이다.
자동 테스트가 스냅샷 바이트만으로 돌아 실기기를 한 번도 안 탔던 부분이다.

측정 세션: `sessions/20260828-132747-3057` — 이클립스·VS Code 로 자바 2파일에 주석
한 줄씩 추가, `watch_mode: native`, 감시 대상 4개(자바 2 / JS 1 / HTML 1).

1. ⬜ **git 미설치 환경 실증** (FR-020, PRD 14.1) — `$env:Path` 에서 git 을 뺀 새 셸에서
   세션을 열고 파일 하나를 고친 뒤 Ctrl+C. `final.diff` 에 `--- a/…` 형식이 나와야 한다.
   **테스트로는 못 닫는다** — CI·개발 PC 에 git 이 있어서 "없는 환경"을 만들 수가 없다
2. ✅ **실제 IDE 저장 세션의 diff 가독성** — `[DIFF] 2개 파일 변경 (+2 / -0)` 이 PRD 10.1
   형식대로 떴고 `final.diff` 가 읽힌다. **세 가지가 같이 확인됐다:**

   - `final.diff`·`stats.json` 에 `ksmart` 도 `C:\` 도 없다 (grep 0건) — 설계 5.7 의
     `a/`·`b/` 상대 경로가 실제로 작동했다
   - **한글 주석이 그대로 살아 있다** — UTF-8 경로의 실기기 첫 검증
   - **탭과 스페이스가 섞여도 깨끗하다.** `New.java` 는 탭(이클립스), `Hello.java` 는
     스페이스(VS Code) 인데 개행 정규화가 들여쓰기를 건드리지 않는다
3. ⬜ **1MB 근처 파일 체감** — 종료 지연이 느껴지는지, `# skipped: … (too_large)` 로 남는지
4. ⬜ **cp949 콘솔의 `[DIFF]` 줄** — 한글이 안 깨지는지. (다)에서 좁힌 조건대로
   **`> log.txt` 로 리다이렉트했을 때**가 진짜 위험 구간이다. 위 세션의 콘솔 출력은
   안 깨졌지만 리다이렉트는 아직 안 걸어봤다
5. ✅ **`stats.json` 육안 대조** — impl 이 "본문을 눈으로 확인하지 못했다"고 남긴 자리다.
   파일별 `+1` 씩, 합계 `added_lines: 2 / deleted_lines: 0`, 인코딩 둘 다 `utf-8` —
   **실제 편집과 정확히 일치했다.** `unchanged` 2파일은 `stats.json` 의 `files` 에서
   아예 빠졌다(설계대로). 덤으로 `final/` 스냅샷이 실물과 바이트 동일, `unstable: false`,
   종료 코드 1(과도기 매핑)도 같이 확인됐다

**3단계 실기기 확인 2건 — 아직 0건 통과.** 자동 테스트 36케이스는 전부 문자열 단위라
"실제로 막히는가"를 한 번도 안 봤다. 4단계가 붙기 전에 여기서 걸러야 하는 이유는,
4단계부터는 검증에 실패하면 **실제로 외부로 나간다**는 것이다.

1. ⬜ **심은 키가 실제 세션에서 차단되는가** (FR-036, PRD 14.1) — 감시 대상 파일에
   `sk-` 로 시작하는 가짜 키 한 줄을 넣고 저장한 뒤 Ctrl+C. 기대값:
   콘솔에 `[SCAN] … 외부 전송을 중단합니다`, `session.json` 이 `status: failed` +
   `error: "secrets_detected"`, 종료 코드 **1**, 그리고 **`final.diff`·`baseline/`·`final/`
   이 전부 남아 있을 것** (FR-036 은 전송만 막고 로컬 산출물은 보존한다).
   `redaction.json` 에 **키 원문이 없는지**도 같이 눈으로 본다 — 없어야 정상이다
2. ⬜ **`--allow-secrets` 로 마스킹 후 진행** (FR-038) — 같은 파일로 `--allow-secrets` 를
   붙여 재실행. 기대값: `[SCAN] … 마스킹 후 진행합니다`, `session.json` 의
   `redaction.secrets_found` 가 1 이상, `by_rule` 에 `openai_api_key` 가 잡힘.
   **주의: 이 경로에서 정제본이 디스크에 안 남는다** — 3절 판정 6번. 마스킹이 실제로
   먹었는지는 지금 눈으로 확인할 방법이 없고, 4단계가 프롬프트를 만들 때 처음 보인다

**4단계 실 API 확인 6건 — 아직 0건 통과.** 전문은 `.pipeline/summarizer/VERIFY.md` 3절이
정본이다 (사람이 재구성한 문서 — 4단계 주행 기록 ④ 참조). 요약하면:

> **순서 주의: 위의 3단계 2건을 먼저 통과시켜라.** 4단계부터는 검증 실패가 곧 실제
> 외부 전송이다. 비밀값 차단이 실기기에서 한 번도 안 돈 상태로 실키를 꽂는 것은
> 순서가 뒤집힌 것이다.

1. ⬜ **실 OpenAI 호출 1회가 성공한다** — 어댑터의 파라미터명·`response_format` 형태·
   `resp.id` 접근이 전부 `추정`이다. judge 가 SDK 소스로 **타입 수준까지는** 확인했지만
   (JUDGE #20·#22·#23), 200 이 오는지는 호출해야 안다
2. ⬜ **`gpt-4o-mini` 서빙·strict json_schema 지원** (JUDGE 미확인 #25) — 틀리면 400/404 로
   즉시 드러난다. 복구는 `.env` 에 `OPENAI_MODEL=<다른모델>` 한 줄
3. ⬜ **strict 가 maxItems/maxLength 를 강제하는가** (JUDGE 미확인 #26) — 어느 쪽이든
   로컬 clamp 가 같은 결과를 낸다. `summary.json` 배열이 6개로 왔는지만 보면 안다
4. ⬜ **입력 20,000자 ≈ 8k 토큰인가** (JUDGE 미확인 #27) — **한글 주석 비중이 큰 diff 는
   밀도가 더 높다.** 복구는 `PROMPT_DIFF_BUDGET_CHARS` 상수 1곳
5. ⬜ **timeout 15초 실동작** — SDK 기본이 **600초**다(judge 실측). 명시가 안 먹으면
   세션 종료가 10분 멈춘다
6. ⬜ **요약을 코드 안 본 사람이 이해하는가** (FR-032, PRD 14.1 마지막) —
   **이 제품의 진짜 합격선이다.** 수신자 5인이 읽는다는 존재 이유가 여기 걸려 있다.
   프롬프트 문안의 품질은 테스트가 못 잰다 — 팀원에게 실제로 보여주는 것이 유일한 검사다

### (나) 5단계 "Discord" (0.5일) — 다음 차례

4단계가 만든 `summary.json` 을 사람이 읽는 메시지로 렌더링해 Webhook 으로 보낸다.
**이 메시지가 수신자 5인과의 유일한 접점이다** (PRD 11.4). 지금까지 만든 전부가
여기서 사람 눈에 닿거나, 안 닿는다.

**범위 — PRD 12절**

| FR | 내용 | 우선 | 수용 기준 |
|---|---|---|---|
| FR-033 | Webhook 메시지 제한에 맞게 렌더링 | **P0** | 본문이 길면 안전하게 분할하거나 항목 수를 줄여 축소 |
| FR-034 | Discord 오류 시 요약을 로컬 보존 | **P0** | `partial` + **HTTP 상태 코드가 기록**된다 |
| FR-050 | **모바일에서 스크롤 없이 핵심이 읽힌다** | **P0** | 제목·요약·주요 변경 상위 2건이 첫 화면. 세부는 그 아래 |
| FR-051 | **코드 원문·diff 전문 미포함** | **P0** | payload 에 `+`/`-` 로 시작하는 diff 라인이 **없다** |
| FR-052 | 알림 예산 | P1 | 변경 없음 세션 전송 0회(FR-035, 이미 섬). 재전송은 명시 요청 시에만 |
| FR-053 | 공용 PC 정리 안내 | P1 | 산출물 위치 + `.env`·환경변수 잔존 확인 체크리스트를 마지막에 출력 |

**입력은 이미 다 있다** — `summary.json`(4단계 산출), `stats.json`, `session.json`.
의존성도 선언돼 있다 (`httpx>=0.27`). **`pyproject.toml` 은 `PROTECTED` 라 에이전트가
못 건드린다** — 추가가 필요하면 사람이 먼저 넣어야 한다 (8절).

**메시지 형태의 정본은 PRD 11.4 의 예시 블록이다.** 설계가 그것을 그대로 인용해야 한다.

#### 설계 단계에서 판정이 필요한 것 — judge 가 물고 늘어질 지점

1. **`--no-discord` 세션의 status 가 이미 확정돼 있다** — 4단계가 `completed`/0 으로
   정했다(설계 6.6, `resolve_session_end`). 5단계는 **그 행을 바꾸면 안 된다.**
   바꿔야 하는 것은 `discord_pipeline_not_implemented` 행 하나뿐이다
2. **FR-051 을 무엇으로 보장하는가** — "diff 라인이 없다"는 사후 검사가 아니라 구조여야
   한다. 4단계가 `PromptInput` 타입으로 FR-037 을 막은 것과 같은 수법이 쓸 만하다.
   `summary.json` 의 `summary` 블록만 렌더러에 넘기면 diff 가 닿을 경로 자체가 없다
3. **Webhook URL 이 로그·예외에 안 나오는가** — `httpx` 예외 메시지에는 **요청 URL 이
   들어간다.** 4단계가 `openai_client.py` 에서 `from None` 으로 SDK 예외 메시지를 버린 것과
   같은 처리가 필요하다. `mask_secrets`(`config.py:139`)가 이미 있지만 **그건 값이
   문자열에 그대로 있을 때만 듣는다** — URL 이 잘려 들어가면 못 잡는다
4. **분할(FR-033)의 단위** — Discord 임베드/본문 길이 제한이 몇 자인지 **저장소 안에
   정본이 없다** (`추정`). 4단계처럼 어댑터 한 파일에 격리하고, 틀려도 상수 1곳으로
   복구되게 짜야 한다. 실 전송 1회가 확정해 준다
5. **재전송(FR-052)을 어떻게 막는가** — 같은 세션을 두 번 돌리면 두 번 간다.
   `session.json` 에 전송 기록을 남기는 것이 자연스럽지만, **재전송 CLI 는 PRD 16절
   확장안(P1)이라 이 단계 범위가 아니다.** 기록만 남기고 차단은 안 하는 선이 맞다
6. **FR-053 의 출력 위치** — 전송 성공/실패와 무관하게 마지막에 나와야 한다.
   `cli.run_watch` 의 종료 경로가 이미 6갈래다(4단계 6.6 표) — 그 전부에 붙는가,
   아니면 정상 종료에만 붙는가
7. **4xx 와 5xx 를 구분하는가** — PRD 12절 표는 둘 다 `partial` 이지만, 4xx 는 **URL 이
   잘못된 것**(재시도 무의미)이고 5xx 는 일시 장애다. `session.json` 의 `error` 값이
   달라야 6단계 E2E 가 판별한다 — 4단계가 `openai_auth`/`openai_http_<status>` 로
   가른 전례가 있다

#### 14.1 체크리스트에서 닫히는 것

- [ ] **Discord payload 에 diff 라인이 포함되지 않는다 (FR-051)**
- [ ] 변경 없음 세션은 Discord 전송이 0회다 (FR-035) — 경로는 이미 서 있고 단언만 추가
- [ ] (부분) Discord 실패에도 baseline·final·diff·`session.json` 이 남는다
- [ ] (사람만) 모바일 화면에서 스크롤 없이 읽히는가 (FR-050)

#### 주행

```bash
BUDGET_DESIGN=8 BUDGET_JUDGE=8 BUDGET_VERIFY=14 PY=.venv/bin/python ./orchestrate.sh notifier
```

**예산 3종을 전부 명시했다** — 4단계에서 design($5 기본)과 verify($10)가 **둘 다 예산으로
죽었다.** 기본값을 믿지 마라. 근거는 4단계 주행 기록 ①·④.

macOS 는 이대로, PowerShell 은 10절대로 `bash.exe` 를 거친다.
DESIGN·JUDGE 게이트에서 `exit 4` 로 멈춘다 — **`approve.sh` 는 tty 가 필요하고
클로드 세션의 `!` 프리픽스로는 안 된다** (8절 지뢰). 진짜 터미널을 열어라.

**깨질 테스트가 있다.** `discord_pipeline_not_implemented` 를 단언하는 곳
(`test_watcher.py`·`test_cli.py`)이 4단계 때와 같은 방식으로 깨진다. impl 이 `DONE` +
인계로 넘기고 verify 가 고치는 경로(결함 ⑤ 예외)를 타면 정상이다.

### (다) ✅ 해결됨 — cp949 에서 죽는 결함 (0단계 코드)

`src/class_watcher/cli.py:131` (`run_preflight` 의 `--max-files` 초과 안내)에 em dash `—` 가
있다. 한국어 Windows 콘솔에 실제로 출력되면 `UnicodeEncodeError: 'cp949' codec` 로 **죽는다**.
impl 이 이 세션에서 같은 예외를 재현했고, 이 문서를 쓰던 스크립트도 같은 이유로 한 번 죽었다.

기존 테스트가 못 잡는다 — `capsys` 는 실제 콘솔 인코딩을 타지 않는다.
**FR-006 은 P0 이고 그 안내 경로가 실제 사용 환경에서 죽는다.** 설계 범위 밖이라 impl 이
손대지 않았다.

> **2026-08-28 해결.** `—` 를 `.` 로 바꿨다. 회귀 테스트는
> `test_max_files_hint_survives_cp949_console` — 실제로 발생하는 `PreflightError` 메시지를
> `.encode("cp949")` 해 본다. `capsys` 가 못 잡는 이유(콘솔 인코딩을 안 탄다)를 우회하는
> 가장 얇은 형태다. **고치기 전 코드에서 `UnicodeEncodeError` 로 실패하는 것을 확인했다.**

**2026-08-28 실측으로 조건이 좁혀졌다.** 실기기 확인 4세션 동안 콘솔 한글은 한 번도 안 깨졌다 —
진짜 콘솔에서는 파이썬이 `WriteConsoleW` 로 쓰므로 cp949 를 안 탄다. **죽는 건 stdout 이
파이프/파일로 리다이렉트될 때**다 (`sys.stdout.encoding == 'cp949'` 가 된다). 그러면
`class-watcher watch ... > log.txt` 나 CI 로그 수집 같은 평범한 사용에서 터진다.

소스 전체를 훑어 cp949 로 인코딩 불가능한 문자열 리터럴 **8개**를 찾았는데, 그중 **출력되는
것은 `cli.py:131` 하나뿐**이고 나머지 7개는 docstring·주석이라 무해하다.
`[FINALIZING] 감시 중지 · debounce flush` 의 가운뎃점(`·`)은 cp949 에 있어서 안전하다.
**즉 `cli.py:131` 의 `—` 를 `-` 로 바꾸는 한 글자 수정이면 끝난다.**


### (라) ✅ 해결됨 — 중단된 세션의 `session.json` 이 "변경 없음"이라고 거짓말한다 (1단계 코드)

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

> **2026-08-28 해결.** abort 시 `watched_files` 를 전부 `"unknown"` 으로 낮춘다
> (`watcher.unknown_file_statuses`). 필드를 통째로 빼지 않은 것은 **감시 대상 목록 자체는
> 실제 정보**이기 때문이다 — 주장할 수 없는 것은 상태뿐이다. baseline↔현재 해시를 다시
> 비교해 채우는 방법도 있었지만, abort 는 "빨리 끝내라"는 요청이라 파일 스캔을 한 번 더
> 도는 것은 그 의도와 충돌한다.
>
> 회귀 테스트 2개: `test_unknown_file_statuses_is_pure`(순수 함수),
> `test_aborted_session_does_not_claim_files_unchanged`(두 번째 Ctrl+C 를
> `wait_for_stability` 에 주입해 실제 abort 경로를 태운다).
> **고치기 전 코드에서 `unchanged != unknown` 으로 실패하는 것을 확인했다.**

### (마) ✅ 해결됨 — `++i;` 가 ±라인 집계에서 빠지던 결함 (2단계 코드)

파이프라인이 녹색으로 닫힌 뒤 산출물을 읽다가 찾았다. `diffgen._count_lines` 가 헤더를
`line.startswith(("+++", "---"))` 로 걸렀는데, 그러면 **본문 라인이 `++`/`--` 로 시작할 때
헤더로 오인해 안 센다.**

```
before: int i = 0;
after:  int i = 0; / ++i; / --j;
→ diff 본문은 정확: '+++i;' '+--j;'
→ added_lines = 1  (기대 2)
```

**diff 본문은 처음부터 옳았고 집계만 틀렸다.** 그래서 게이트 3종이 전부 녹색이었다.
하필 대상이 자바·JS 다 — `++i;`·`--count;` 는 이 학원 코드에 실제로 나오는 문장이고,
그 수치는 4단계 LLM 프롬프트와 5단계 디스코드 메시지로 그대로 흘러간다.

> **2026-08-28 해결.** per-file diff 는 항상 `--- a/…`·`+++ b/…` 두 줄로 시작하므로
> `splitlines()[2:]` 로 **위치로** 건너뛴다. 회귀 테스트 2개
> (`test_increment_statements_are_counted_as_body_lines`,
> `test_removed_decrement_statement_is_counted`)는 고치기 전 코드에서 실패하는 것을
> 확인했다 — 추가 쪽과 삭제 쪽이 둘 다 틀렸다.

**게이트가 못 잡은 이유가 이 결함의 진짜 교훈이다.** verify 가 쓴 테스트 헬퍼
`_body_lines` 가 `_count_lines` 와 **같은 셈법으로 기대값을 만들었다** — 구현이 틀린
방식 그대로 기대값도 틀렸으니 테스트가 통과할 수밖에 없다. 헬퍼도 같이 고쳤다.
**같은 사람(에이전트)이 구현과 기대값을 같은 논리로 쓰면 테스트는 그 논리의 오류를
증명할 수 없다.** DMS 파이프라인이 impl 과 verify 를 나눈 이유가 이건데, 이번엔
verify 가 impl 의 코드를 읽고 헬퍼를 만들면서 그 분리가 무너졌다.

## 6. 이미 내려진 결정 — 다시 논쟁하지 말 것

| # | 결정 | 근거 |
|---|---|---|
| C-15 | `--subject`/과목 옵션은 **삭제됐다. 누락이 아니다.** | JUDGE 반박 #8 이 FR-004 본문("과목")과 PRD 10.2 옵션표의 불일치를 잡아냈고 사람이 PRD 를 고쳐 해소했다. **옵션의 정본 목록은 10.2 다** — FR 본문과 어긋나면 10.2 를 따르고 FR 을 고친다 |
| — | 패키지 내부 import 는 **상대**(`from .x`) | ruff isort 가 `class_watcher` 를 first-party 로 분류 못 하면 I001 이 난다. 상대 import 는 분류와 무관하게 안전 |
| — | `DEFAULT_INCLUDE` 에서 `*.json`·`*.md` 제외 | PRD 는 "언어별 기본 allowlist" 라고만 쓰고 열거하지 않는다. lock 파일·문서 노이즈 때문에 뺐고 `--include` 로 덮어쓴다 |
| — | 종료 코드 4종 | `EXIT_OK=0`, `EXIT_RUNTIME=1`, `EXIT_CONFIG=2`, `EXIT_ABORTED=130` (PRD 10.3, C-10 으로 7종→4종) |
| — | symlink 디렉터리는 **전부** 미하강 | PRD 13.3 은 "루트 밖으로 나가는 symlink" 한정이지만 구현이 더 엄격한 쪽이라 수용 기준 위반이 아니다 |
| — | 게이트 명령(`pytest`·`ruff`·`mypy`)은 에이전트가 **승인 없이 실행**한다 | 안 열면 에이전트가 자기 산출물을 검증할 수 없다. 세 모듈로만 못박았고 판정권은 여전히 셸에 있다 (`1e7fe58`) |
| — | (다)(라)(마) 세 결함 모두 **파이프라인 밖에서** 고쳤다 | 파이프라인 한 바퀴가 ~$8 인데 고칠 것이 각 1~3줄이었다. 게이트가 못 잡는 결함이라 재시도로도 안 잡히고, diff-engine 설계 범위 밖이라 다음 impl 도 안 건드린다 — (다)를 0단계 impl 이 실제로 그냥 두고 갔다. **회귀 테스트 3개를 같이 넣었고, 고치기 전 코드에서 실패하는 것을 확인했다** |
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
- **`PROTECTED` 지문 감시 목록의 정본은 `orchestrate.sh:838` 한 줄이다** — 외우지 말고 그걸 봐라.
  2026-08-28 실측 기준: `pyproject.toml` · `requirements.txt` · `requirements-dev.txt` ·
  `setup.cfg` · `pytest.ini` · `mypy.ini` · `ruff.toml` · `.ruff.toml` · `conftest.py` ·
  `tests/conftest.py` · `.gitignore` · `AGENTS.md` · `.env.example` · **`PRD.md`**.
  에이전트가 건드리면 즉시 죽는다. **의존성 추가는 사람이 미리 해야 한다.**
  - **없는 파일도 목록에 있다** — `ruff.toml` 을 **새로 만들어** 게이트를 느슨하게 하는
    경로를 막으려고 "(없음)" 도 지문에 넣는다.
  - **`.env` 자체는 목록에 없다.** `.env.example` 만 있다 — 이 문서가 예전에 `.env*` 로
    적었던 것은 틀렸다.
  - **`PRD.md` 는 2026-08-28 에 추가됐다.** 그 전까지 요구사항 정본이 감시 밖이었다 —
    `design.md:9` 가 "가장 먼저 읽는다", `judge.md:65` 가 "설계의 FR 인용을 PRD 원문과
    대조한다"로 쓰는 기준선인데도. 수용 기준을 느슨하게 고친 뒤 "PRD 와 일치한다"고
    판정하는 경로가 열려 있었다. **실제로 일어난 적은 없다** — PRD 개정 2건(v1.1.1·v1.1.2)은
    전부 사람이 C-15·C-16 으로 근거를 남기고 고친 것이다.
- **지문 기준선의 위치가 곧 검사 범위다.** 이 스크립트는 순차 실행이라 `PROTECTED_BASELINE`
  이 찍히는 시점 **이전**의 변경은 기준선에 흡수돼 영원히 안 잡힌다. 2026-08-28 이전에는
  이 블록이 design·judge **뒤에** 있어서 두 단계가 통째로 검사 밖이었다 — PRD 를 가장 많이
  읽는 단계들이 그랬다. 지금은 `preflight` 직후(`orchestrate.sh:820`)로 올라갔고 네 단계
  전부가 검사를 받는다: `check_protected design`(843) · `judge`(859) · `impl`(889) · `verify`(896).
  **이 블록을 어떤 `run_stage` 아래로 내리지 마라** — 내리는 순간 그 위 단계가 조용히
  검사 밖이 된다. 죽는 게 아니라 통과하는 종류의 회귀라 테스트로 안 잡힌다.
- **세션 중 생긴 파일의 이벤트는 계속 `created` 로 찍힌다.** `debounce.py:41` 이 "known_paths 에
  없는 경로의 변경은 신규 편입이라 created 로 승격한다 (FR-017)" 라고 의도를 명시했다. baseline
  기준으로는 계속 신규가 맞고 최종 status 도 `added` 로 정확하다. **2단계 diff 엔진이
  `events.jsonl` 을 읽을 때 "한 파일이 `created` 로 여러 번 나온다"를 전제해야 한다** —
  실측으로 확인했다 (5절 가 1번).
- **중단된 세션의 `session.json` 은 `watched_files` 를 믿으면 안 된다** — 5절 (라).
- **`final.diff`·`stats.json` 은 변경이 있는 세션에서만 생긴다.** `no_change` 세션은 diff
  단계에 들어가지도 않는다 (FR-035 경로 불변). 없다고 오류가 아니다.
- **`stats.json` 의 구조는 PRD 가 명세하지 않아 2단계가 정했다.** `schema_version: "1.1"` 이
  박혀 있으니 후속 단계에서 필드를 늘릴 때 올려라. diff **원문은 안 들어간다** — 원문이
  필요하면 `final.diff` 를 읽어야 한다.
- **diff 는 감시 루트가 아니라 스냅샷 디렉터리에서 읽는다.** 세션이 끝난 뒤 사용자가 파일을
  더 고쳐도 diff 는 종료 시점으로 고정된다.
- **제외된 파일은 원래 status 를 잃는다.** `added` 였던 바이너리도 `stats.json` 에는
  `status: "skipped"` + `skip_reason` 으로만 남는다. 추가였는지 삭제였는지는 알 수 없다.
- **CRLF↔LF 만 바뀐 파일은 `modified` 인데 diff 본문이 비고 ±0 이다.** 해시는 다르고
  (스냅샷이 바이트 원본) 본문은 정규화되기 때문이다 — 모순이 아니라 설계 5.2 의 의도다.
- **`errors.jsonl` 을 실제로 쓰는 첫 코드가 diff 실패 기록이다.** 그 전까지는 경로만 있었다.
- ✅ **`--allow-secrets` 는 이제 실제로 읽힌다** — `cli.py:113` → `WatchConfig` →
  `watcher.py:323` → `redact_diff`. 3단계가 배선을 끝냈다.
- **테스트 헬퍼가 구현과 같은 논리를 쓰면 게이트가 눈을 감는다** — 5절 (마) 가 실제 사례다.
- **`session.json` 의 `watch_root` 는 여전히 절대 경로 + Windows 사용자명이다**
  (`session.py`, 2026-08-28 실측). 고치지 않는 것이 맞다 — PRD C-11 이 "로컬에는 실제 경로
  저장, 해싱·제거는 외부 프롬프트에 넣을 때만"으로 정했다.
  **4단계가 타입으로 막았다**: `build_prompt` 는 `PromptInput` 만 받고 그 안에
  `session.json`·`WatchConfig` 가 들어갈 자리가 없다. 시그니처가 방어선이다
  (`test_prompt_never_contains_absolute_path_markers` 가 단언).
  **5단계도 같은 규율을 지켜라** — Discord payload 빌더에 `WatchConfig` 를 넘기지 마라.
- ✅ **정제된 diff 는 여전히 디스크에 없다 — 그리고 그게 맞다.** `RedactionResult.text` 는
  `_finalize` 지역 변수로만 산다. 4단계는 **같은 함수 안에서** 요약을 호출해 그 값을
  그대로 쓴다 (재정제·재독 없음). **`final.diff` 를 다시 읽는 코드를 쓰지 마라 — 원본이라
  FR-036 우회다.** 5단계도 같다: 보낼 것은 `summary.json` 이지 diff 가 아니다.
- **`redaction.json` 은 diff 가 만들어진 세션에서만 생긴다.** `no_change` 세션은 정제
  단계에 들어가지도 않고, diff 생성이 실패한 세션도 마찬가지다 (`watcher.py:456-460`).
  없다고 오류가 아니다 — `final.diff`·`stats.json` 과 같은 규칙이다.
- **탐지 원문은 어디에도 안 남는다.** `SecretFinding` 이 `(rule_id, rel_path, line_no)`
  세 필드뿐이라 매치 문자열을 담을 자리가 없다 (`redact.py:87`). 유출 방지 산출물이
  유출 경로가 되는 것을 자료구조로 막은 것이다 — **디버깅하겠다고 원문 필드를 늘리지 마라.**
- **종료 코드 1 이 이제 두 가지 뜻이다.** 탐지-중단은 `status: failed` +
  `error: "secrets_detected"`, 과도기는 `status: partial` +
  `error: "summary_pipeline_not_implemented"` 다 (`watcher.py:509,512`).
  **6단계 E2E 는 종료 코드가 아니라 `status`+`error` 쌍으로 판별해야 한다.**
- **경로는 스캔하지만 마스킹하지 않는다.** `config/prod-key.pem` 같은 파일명은 탐지에
  잡혀 전송을 막지만, `--allow-secrets` 로 진행할 때 경로 자체는 가리지 않는다 —
  "경로를 가리면 요약이 무슨 파일인지 말할 수 없다" (`redact.py:296`).
- **`Path()` 를 쓴 테스트는 실행 OS 에 묶인다.** macOS 에서 `Path("Z:/x").drive` 는 빈
  문자열이라 `watchmode` 의 드라이브 분기가 통째로 건너뛰어진다. Windows 경로 픽스처는
  `PureWindowsPath` 로 명시 파싱해야 어디서든 같은 분기를 탄다 (`tests/test_watchmode.py:13`).
  **더 나쁜 쪽은 실패가 아니라 조용한 통과였다** — `test_local_drive_stays_native` 가
  판정 함수를 한 번도 안 부르고 기대값만 우연히 맞았다.
### 4단계가 남긴 지뢰

- **`approve.sh` 는 진짜 tty 가 필요하다. 클로드 세션의 `!` 프리픽스로는 안 된다.**
  스크립트 주석은 *"클로드 세션 안이라면 `! /path/to/approve.sh ...` — `!` 프리픽스는
  사람 키 입력이다"* 라고 안내하지만, **`!` 는 사람 키 입력이 맞아도 tty 를 만들지 않는다**
  (2026-08-30 실측: `/dev/tty: Device not configured`). `read ... < /dev/tty` 가 하드코딩돼
  파이프·리다이렉트로도 못 우회한다. **Terminal.app 등 진짜 터미널 창을 열어라.**
  `./approve.sh --hash <파일> > <파일>.approved` 로 마커를 직접 만들 수도 있지만
  `y` 확인이 빠지므로 스크립트가 경고한 침식 경로다 — 사람이 판단할 것.
- **런처(메인 세션)는 `approve.sh` 를 대신 실행하지 않는다.** 스크립트 주석이 이것을
  "계약"이라고 부른다. 마커를 직접 쓰는 것도 같다. **`.crashed` 파킹의 `mv` 는 다르다** —
  그건 `STATE.md` 가 사람에게 안내하는 절차이고, 사람 결정이 선행하면 실행은 위임 가능하다.
- **`PIPELINE_APPROVED_SCOPE` 는 DESIGN 게이트 뒤에서만 만들어진다** (`orchestrate.sh`).
  `DESIGN.md` 의 "변경 대상 파일" 표 **첫 열**의 백틱 경로만 파싱한다 — 3열(설명)에는
  `summary.json`·`_finalize` 처럼 경로가 아닌 백틱 토큰이 섞여 있다.
  **설계가 그 표에 안 적은 파일은 민감 경로일 때 impl 이 못 고친다.**
  표 형식이 바뀌면 목록이 비고 훅은 원래대로 동작한다(fail-safe) — 셸이 `⚠` 로 경고한다.
- **`summary.json` 은 정제를 통과한 세션에서만, `prompt.json` 은 `--dry-run` 에서만 생긴다.**
  `final.diff`·`stats.json`·`redaction.json` 과 같은 규칙이다. 없다고 오류가 아니다.
- **openai SDK 기본값 2개가 이 제품에 치명적이다** (judge 가 SDK 소스로 실측):
  `DEFAULT_MAX_RETRIES = 2` — 끄지 않으면 FR-030 의 "상한 2회"가 **계수 밖에서** 깨진다.
  `DEFAULT_TIMEOUT = 600초` — 명시하지 않으면 세션 종료가 10분 멈춘다.
  둘 다 `openai_client.py` 가 명시적으로 덮고 `test_sdk_auto_retry_is_disabled`·
  `test_timeout_is_15_seconds` 가 지킨다. **지우지 마라.**
- **설치본은 openai 3.5.0 인데 설계는 1.x 표면을 가정했다** — judge 가 SDK 소스를 읽어
  `chat.completions` 표면이 그대로임을 확인했다(JUDGE #20·#22·#23). 메이저가 2단계 올랐는데
  깨지지 않은 것이라, **SDK 를 업그레이드하면 `openai_client.py` 를 다시 봐야 한다.**
- **4단계 테스트는 실 API 를 한 번도 안 부른다.** 268개 전부 가짜 `CallFn` 주입이다
  (설계 의도 — 네트워크 없는 PC 에서도 게이트가 돈다). **게이트 녹색 ≠ 요약이 실제로
  만들어진다.** 5절 (가) 의 4단계 6건이 그 간극이다.
- **`.pipeline/summarizer/VERIFY.md` 는 verify 에이전트가 쓴 것이 아니다.** 예산 소진으로
  유실돼 사람이 재구성했다. 그 문서 5절이 "이 문서가 못 가진 것" 3가지를 명시한다 —
  특히 **테스트가 구현과 같은 논리를 쓰는지에 대한 적대적 검토가 없다.**

- 판정 함수(`run_preflight` 등)에는 `print` 가 없다. 콘솔 출력은 `main` 에서만 한다.
- verify 는 `tests/test_*.py` 만 수정할 수 있다 (`prompts/verify.md:62`). **소스의 린트·타입 오류는 impl 만 고칠 수 있다.**
- 0단계 JUDGE 미확인 2건(Windows `os.replace` 원자성, `token_hex(2)`)은 147개 테스트가
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
.venv\Scripts\python -m pytest -q       # 268 passed 나와야 정상
```

macOS·리눅스에서는 `python3 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"`
로 같은 결과가 나온다 — **2026-08-30 macOS / Python 3.12.11 에서 268 passed 실측**.
게이트 3종은 OS 를 안 탄다.

`.env` 도 커밋되지 않는다. **테스트는 `.env` 없이 전부 돈다** — 268개가 가짜 `CallFn`
주입이고 `test_cli.py` 의 `isolated_env` 픽스처가 `OPENAI_API_KEY` 를 `delenv` 한다.
**실키가 필요한 것은 사람 확인뿐이다** (5절 가의 4단계 6건). 그때 `.env.example` 을
복사해 채운다 — 지출 한도를 건 전용 OpenAI 프로젝트를 권한다.
모델을 바꾸려면 `.env` 에 `OPENAI_MODEL=<이름>` 한 줄이면 된다 (상수 재빌드 불필요 —
단일 exe 배포에서 이게 유일한 교체 수단이다).
3단계가 `.env` 의 실값을 `known_secret` 백스톱 규칙으로 쓰므로,
**`.env` 가 있으면 스캐너가 한 겹 더 두꺼워진다** (`redact.py`).

**파이프라인은 bash 가 필요하다.** PowerShell 에서는 이렇게 부른다:

```powershell
& "C:\Program Files\Git\bin\bash.exe" ./orchestrate.sh <feature>
& "C:\Program Files\Git\bin\bash.exe" ./approve.sh <feature> DESIGN.md
```

`.pipeline/` 도 커밋되지 않는다. 1·2·3단계 산출물(`.pipeline/watch-engine/`,
`.pipeline/diff-engine/`, `.pipeline/redactor/`)과 승인 마커는 돌린 PC 에만 있다 —
**4단계는 새 feature(`summarizer`) 이므로 문제되지 않는다.** 자세한 것은 2.1 절.

**3단계 주행 실적이 이 리포에 없는 이유가 이것이다** (3절). 그 PC 에 돌아가면
`jq -r '.total_cost_usd' .pipeline/redactor/*.result.json` 로 뽑아 3절 표를 채워라.

### 그 밖의 환경 사실

- Python 3.14.6 (학원 PC), `requires-python >=3.11`
- 의존성은 `pyproject.toml` 에 전부 선언돼 있다 — watchdog 6.0.0, openai, httpx,
  python-dotenv, (dev) pytest·ruff·mypy·pyinstaller. **`PROTECTED` 라 에이전트가 못 건드린다**
- 이 환경에서 `Observer` 는 `WindowsApiObserver`(ReadDirectoryChangesW)로 해석된다
- 비밀값 2개: `OPENAI_API_KEY`, `DISCORD_WEBHOOK_URL`. 공용 PC 에서는 `.env` 를 PC 에
  남기지 말고 exe 와 함께 USB 에 둔다 (FR-054)
- 모델 배치: design·judge·verify = `claude-fable-5`, impl = `claude-opus-5`
- 예산 실적: 0단계 impl $1.3~5.2 / verify $4.8. 1단계 judge $5.6 / impl $2.5 / verify $3.9.
  2단계 design $2.9 / judge $5.5 / impl $2.3 / verify $5.6 — **한 바퀴 $16.4**.
  **3단계는 미기재** — 다른 PC 에서 돌아 `.pipeline/redactor/` 가 여기 없다 (3절).
  4단계 design $5.05 / judge $5.53 / impl $2.60(사망)+$5.42 / verify $10.05 — **$28.65**.
  **기본 예산으로는 design·verify 둘 다 죽는다** — 5단계는 `BUDGET_DESIGN=8`·
  `BUDGET_VERIFY=14` 를 명시해라 (근거: 3절 4단계 주행 기록 ①·④)
