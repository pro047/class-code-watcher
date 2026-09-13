# syntax 앞 기호 보존 테스트 문서 (C-29)

대상 `src/class_watcher/notify.py` (`_fold`·`sanitize_inline`·`_guard_diff_line`·`_keyword_lines`·`render_message`)
계획 `docs/PLAN-SYNTAX-LEADING-SIGN.md` (브랜치 `fix/syntax-leading-sign`)

## 1. 무엇을 고쳤나

2026-09-11 「파이썬 자료형」 세션(`20260911-090702-f145`) 실전송본에서 모델이 낸
`keywords[].syntax` **`+/*`** 가 Discord 에 **`· 문자열 연산자  /*`** 로 나갔다.
모델은 옳게 냈고 렌더러가 `+` 를 지웠다 — 수신자는 `/*` 를 C 주석 시작으로 읽는다.

FR-051 방어선을 **필드 단위에서 줄 단위로** 옮겼다.

| | 전 | 후 |
|---|---|---|
| `syntax` 렌더 | `sanitize_line` — 앞 `+`/`-` 제거 | `sanitize_inline` — 개행 접기·clamp 만 |
| FR-051 보장 | 필드마다 앞 기호 제거 | `render_message` 끝의 `_guard_diff_line` (줄 단위) |
| term·concept·summary·질문·확인할 점 | 앞 기호 제거 | **그대로** (그 자리의 `- ` 는 머리표다) |

`MAX_SYNTAX_CHARS`(60)와 C-27 의 구분자 경계 절단은 안 건드렸다 — clamp 는 기호 보존 뒤에 돈다.

## 2. 단위 테스트 — `tests/test_notify.py` 「S1~S8」 절

| # | 테스트 | 확인 내용 |
|---|---|---|
| S1 | `test_syntax_keeps_its_leading_sign` (6건 parametrize) | `+/*`·`--i`·`-=`·`+=`·`++`·`-x` 가 `· {term}  {syntax}` 에 그대로 남고 `find_diff_lines == ()` |
| S2 | 기존 `test_diff_shaped_model_strings_never_render_as_diff_lines` | **수정 없이 통과** — syntax 가 줄 중간이라 diff 줄이 안 생긴다 |
| S3 | `test_guard_prefixes_a_space_to_any_line_that_starts_like_a_diff` | 가드 단독. 그물이 항상 참인 단언이 아님을 고정한다 |
| — | `test_sanitize_inline_keeps_signs_where_sanitize_line_drops_them` | 두 함수가 갈리는 지점 — 같은 입력 `+/*` 에 `+/*` vs `/*` |
| S4 | `test_sanitize_inline_folds_newlines_before_the_sign_survives` | `"+a\n-b"` → `+a -b`. **입력에 앞 기호를 넣어야 판별력이 생긴다** (처음 쓴 `"a\n+b"` 는 고치기 전 코드에서도 통과했다) |
| S5 | `test_empty_term_still_keeps_the_syntax_off_the_line_start` | term 이 비어도 머리표 `·` 가 앞에 있다 — 확인만 |
| S6 | `test_hard_wrapped_syntax_fragment_never_opens_a_line` | `_hard_wrap` 조각 가드가 계속 돈다 — 확인만 |
| S7 | `test_other_fields_still_lose_their_leading_signs` | 회귀 방어. 나머지 다섯 필드는 앞 기호를 계속 버린다 |
| S8 | `test_sign_bearing_syntax_survives_every_delivery_stage` | text·chunks·payload 세 층 전부에 6종 기호가 살아 있다 |
| — | `test_sign_preservation_does_not_break_the_syntax_clamp` | C-27 경계 절단이 기호 보존 뒤에도 그대로 — 60자 이하 · `+` 시작 유지 |

**S8 은 실세션 재생이 아니다.** 계획서 T8 은 `20260911-090702-f145/summary.json` 을 읽게 돼
있었는데 그 산출물은 실행 PC(Windows)에만 있다. 경로에 의존하면 그 PC 밖에서 테스트가 못
돌기 때문에 **관측된 값만 `_OBSERVED_SIGN_SYNTAX` 로 옮겼다.**

## 3. 실행과 결과 — 2026-09-13

환경: macOS · Python 3.12.11 · `main` (`cw:skip` 머지 뒤)

```bash
.venv/bin/python -m pytest
.venv/bin/python -m mypy src
.venv/bin/python -m ruff check .
```

| 항목 | 결과 |
|---|---|
| 전체 pytest | **535 통과**, 실패 0 (머지 직후 521 → +14) |
| mypy `src` | 이상 없음 (17 파일) |
| ruff | 통과 |

### 고치기 전 코드에서 실패하는 것을 확인했다

`src/class_watcher/notify.py` 만 머지 시점으로 되돌리고 새 테스트를 돌렸다:

| | 결과 |
|---|---|
| 실패 | **12개** — S1 6건 · S3 · `sanitize_inline` 대조 · S4 · S7 · S8 · clamp |
| 통과 | 2개 — **S5·S6 은 의도된 통과다.** 판별용이 아니라 기존 가드(머리표·`_hard_wrap`)가 계속 도는지 보는 것이다 |

처음 쓴 S4 는 고치기 전 코드에서도 통과했다(`"a\n+b"` 는 앞 기호가 없어 `lstrip` 이 걸리지
않는다). **입력을 `"+a\n-b"` 로 바꿔 다시 썼고 그 뒤 실패했다.**

## 4. 미확인 — 실전송으로만 닫힌다

| 무엇을 하면 | 무엇이 보여야 하나 |
|---|---|
| 연산자·증감을 다루는 수업 세션을 평소대로 돌린다 | 실전송본에 `· 문자열 연산자  +/*` 처럼 앞 기호가 남는다. `+`/`-` 로 **시작하는** 줄은 0개 |

**09-11 에 나간 메시지는 재전송하지 않는다** — 같은 수업이 두 번 나가면 수신자가 어느 쪽이
맞는지 모른다. 다만 사람 확인 C·D 를 물을 때 그 메시지를 쓰면 안 된다 (`syntax` 가 깎인 상태다).
