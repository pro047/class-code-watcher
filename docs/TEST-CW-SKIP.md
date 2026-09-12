# cw:skip 테스트 문서

브랜치 `feat/cw-skip-marker` · 대상 `src/class_watcher/diffgen.py` (`has_opt_out_marker`, `diff_file`)

## 1. 기능

파일 맨 위 주석 안에 `cw:skip` 이 있으면 세션 종료 시 그 파일을 diff 에서 제외한다.
제외된 파일은 요약 입력(prompt)·Discord 전송·변경 통계(`files_changed`)에 들어가지 않고,
`final.diff` 에 `# skipped: <경로> (opt_out)` 한 줄만 남는다.

```python
# 오늘 연습용 cw:skip
x = 1
```

| 규칙 | 내용 |
|---|---|
| 판정 범위 | 빈 줄을 건너뛰고, 첫 코드 줄 전까지 이어지는 주석 전체 |
| 주석 문법 | `#` · `//` · `--` 줄 주석, `/* */` · `<!-- -->` 블록 주석 (여러 줄 가능). 확장자 무관 |
| 대소문자 | 무시 (`CW:SKIP` 도 인정) |
| 판정 시점 | 세션 종료 시 final 스냅샷. 삭제된 파일은 baseline |
| 인정 안 함 | 코드 뒤의 주석, 문자열 안, Python docstring, `cw-skip` 같은 변형 |
| 전 파일 제외 | 기존 FR-025 경로 — `no_meaningful_change` 로 요약·전송 생략 |

## 2. 단위 테스트 — `tests/test_diffgen.py`

| # | 테스트 | 확인 내용 |
|---|---|---|
| U1 | `test_opt_out_marker_in_leading_comment_is_detected` (10건) | `#`·`//`·`--`·`/* */`·여러 줄 `/* */`·`<!-- -->`·여러 줄 `<!-- -->`·앞 빈 줄·shebang/인코딩 줄 뒤·블록 뒤 줄 주석 |
| U2 | `test_opt_out_marker_elsewhere_is_ignored` (6건) | 빈 파일·코드 뒤 주석·문자열 안·docstring·코드 뒤 줄 주석·`cw-skip` |
| U3 | `test_opt_out_file_is_skipped_for_every_status` | added·modified·deleted 모두 `opt_out`, diff 본문 없음 |
| U4 | `test_removing_the_marker_during_class_brings_the_file_back` | 수업 중 표식 삭제 → modified 로 요약 포함 |
| U5 | `test_opt_out_file_never_reaches_final_diff` | `generate_session_diff` 산출물에 내용이 새지 않고 skipped 한 줄만 남음 |

실행:

```bash
.venv/Scripts/python -m pytest tests/test_diffgen.py -k "opt_out or marker" -v
.venv/Scripts/python -m pytest            # 전체 회귀
.venv/Scripts/python -m mypy src
.venv/Scripts/python -m ruff check .
```

## 3. E2E — `test/e2e_cw_skip.py`

실제 `cli.main` 을 `--dry-run` 으로 돌린다. watchdog 감시 → 스냅샷 → diff → 정제 → `prompt.json`
까지 실제 경로를 타고, Ctrl+C 와 같은 KeyboardInterrupt 로 종료한다. OpenAI·Discord 호출은 없다.

```bash
.venv/Scripts/python -X utf8 test/e2e_cw_skip.py <임시 폴더>
```

### 시나리오 A — 섞인 세션

| 파일 | 수업 전 | 수업 중 | 기대 |
|---|---|---|---|
| `keep.py` | 표식 없음 | 줄 추가 | 요약 포함 |
| `scratch.py` | — | `# 연습 cw:skip` 로 신규 | 제외 |
| `page.html` | — | 여러 줄 `<!-- ... CW:SKIP -->` 로 신규 | 제외 |
| `Main.java` | — | `/* cw:skip */` 로 신규 | 제외 |
| `later.py` | 표식 없음 | 맨 위에 표식 추가 | 제외 |
| `unmark.py` | 표식 있음 | 표식 삭제 | 요약 포함 |
| `mid.py` | — | 코드 뒤에 `# cw:skip` | 요약 포함 |
| `gone.py` | 표식 있음 | 파일 삭제 | 제외 |

검사: 파일별 `skip_reason`, `final.diff` skipped 줄, 제외 파일 고유 토큰이 `final.diff`·`prompt.json`
에 없음, 포함 파일 토큰은 `prompt.json` 에 있음, `files_changed=3`·`skipped=5`,
`session.json` 의 `watched_files` 사유와 `status=completed`.

### 시나리오 B — 표식 파일만 바뀐 세션

`scratch.py` 하나만 `# cw:skip` 로 추가. 기대: `prompt.json`·`summary.json` 미생성,
`discord.skip_reason=no_meaningful_change`, `no_change=false`.

## 4. 수동 확인 (현장)

1. 다음 수업에서 연습 파일 맨 위에 `# cw:skip` (Java 는 `// cw:skip`) 을 적는다
2. 종료 콘솔의 `[DIFF] … N개 건너뜀(opt_out)` 를 확인한다
3. Discord 요약에 그 파일 내용이 없는지 확인한다

## 5. 결과 — 2026-09-11

환경: Windows 11 · Python 3.14.6 · 브랜치 `feat/cw-skip-marker` (main `18e8f40` 기준, 미커밋)

| 항목 | 결과 |
|---|---|
| 단위 테스트 U1~U5 | **19/19 통과** |
| 전체 pytest | **521 통과**, 실패 0 |
| mypy `src` | 이상 없음 (17 파일) |
| ruff | 통과 |
| E2E 시나리오 A | **27/27 통과** — `files_changed=3, +5/-1, skipped=5` |
| E2E 시나리오 B | **5/5 통과** — `[DIFF] 0개 파일 변경 (+0 / -0), 1개 건너뜀(opt_out)` → 요약·전송 생략 |

시나리오 A 의 `final.diff` 제외 줄:

```
# skipped: Main.java (opt_out)
# skipped: gone.py (opt_out)
# skipped: later.py (opt_out)
# skipped: page.html (opt_out)
# skipped: scratch.py (opt_out)
```

미확인: 실제 OpenAI 요약·Discord 전송 경로 (dry-run 이라 호출 없음). 4절 수동 확인으로 대체한다.
