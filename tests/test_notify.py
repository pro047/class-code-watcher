"""notify 모듈 — 렌더링·축소·분할·payload·전달 판정 (FR-033/034/039/050~053).

이 모듈은 네트워크·시계를 만지지 않고 디스크도 write_payload_json 하나뿐이라 전 경로가
결정적으로 돈다. 전송 계층은 SendFn 을 직접 넣어 계수까지 본다 — 실제 Discord 전송은
사람 확인 체크리스트로 넘긴다 (VERIFY.md).

메시지 형식은 C-17 로 changes[]/learning_points[] 에서 keywords[] 로 바뀌었고, C-18 로
키워드 상한이 15 가 되면서 예산 산수가 천장에 붙었다. 그래서 아래 D 절은 값이 아니라
관계식을 단언한다 — 문구를 한 글자 고쳐도 숫자는 움직이고 관계는 남는다.
"""

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from class_watcher import notify
from class_watcher.summarize import KEYWORD_GROUPS

STARTED_AT = "2026-08-26T18:30:00+09:00"
ENDED_AT = "2026-08-26T20:29:00+09:00"

DEFAULT_KEYWORDS = (
    notify.RenderKeyword(
        term="화살표 함수",
        syntax="(a) => a + 1",
        concept="function 키워드 없이 함수를 만드는 짧은 문법이다.",
        group="함수",
        confidence="high",
    ),
    notify.RenderKeyword(
        term="구조 분해",
        syntax="const {a} = obj",
        concept="객체에서 값을 꺼내 같은 이름의 변수에 바로 담는다.",
        group="연산자",
        confidence="medium",
    ),
)


def _render_input(
    *,
    title: str = "Java 로그인 기능 수업",
    started_at: str = STARTED_AT,
    ended_at: str = ENDED_AT,
    files_changed: int = 2,
    added_lines: int = 42,
    deleted_lines: int = 18,
    summary: str = "사용자 로그인 처리 흐름을 새로 만들었습니다.",
    keywords: tuple[notify.RenderKeyword, ...] = DEFAULT_KEYWORDS,
    questions: tuple[str, ...] = ("인증 실패 유형을 어떻게 구분할까?",),
    risks: tuple[str, ...] = ("비밀번호 해싱 여부 확인",),
    rule_based: bool = False,
    truncated: bool = False,
) -> notify.RenderInput:
    return notify.RenderInput(
        title=title,
        started_at=started_at,
        ended_at=ended_at,
        files_changed=files_changed,
        added_lines=added_lines,
        deleted_lines=deleted_lines,
        summary=summary,
        keywords=keywords,
        questions=questions,
        risks=risks,
        rule_based=rule_based,
        truncated=truncated,
    )


def _max_keyword(index: int) -> notify.RenderKeyword:
    """모든 문자열 필드가 상한이고 confidence 표기가 가장 긴 키워드 한 건.

    분류를 6종에 흩뿌린다 — 분류가 갈릴수록 그룹 머리줄이 늘어 렌더가 길어진다.
    """
    return notify.RenderKeyword(
        term="용" * 40,
        syntax="s" * 44,
        concept="설" * 90,
        group=KEYWORD_GROUPS[index % len(KEYWORD_GROUPS)],
        confidence="medium",
    )


def _worst_case_input(*, keyword_count: int = 15) -> notify.RenderInput:
    """모든 필드가 상한인 최악 입력 (설계 4.2 / 8.1 D4).

    표시 두 줄(규칙 기반 · 절단)이 동시에 참인 것까지 최악이다.
    """
    return _render_input(
        title="제" * 300,
        summary="요" * notify.MAX_SUMMARY_CHARS,
        keywords=tuple(_max_keyword(index) for index in range(keyword_count)),
        questions=tuple("질" * 300 for _ in range(notify.MAX_ITEMS_SHOWN)),
        risks=tuple("확" * 300 for _ in range(notify.MAX_ITEMS_SHOWN)),
        rule_based=True,
        truncated=True,
    )


def _realistic_keyword(index: int) -> notify.RenderKeyword:
    """PRD 11.4 예시 정도 길이의 키워드. 상한이 아니라 실제로 나오는 값이다."""
    return notify.RenderKeyword(
        term=f"프로토타입 체인{index}",
        syntax="Object.getPrototypeOf()",
        concept="객체가 상위 객체의 속성을 찾아 올라가는 구조다.",
        group=KEYWORD_GROUPS[index % len(KEYWORD_GROUPS)],
        confidence="high" if index % 2 == 0 else "medium",
    )


# ── FR-051 3중 방어선 ────────────────────────────────────────────────────────


def test_diff_shaped_model_strings_never_render_as_diff_lines() -> None:
    # 모델이 diff 모양 문자열을 어느 필드에 넣어도 렌더 결과에 `+`/`-` 로 시작하는 줄이
    # 생기지 않는다. text·chunks·payload 세 곳 전부를 본다.
    inp = _render_input(
        summary="- 항목 하나를 지웠다",
        keywords=(
            notify.RenderKeyword(
                term="+ 더하기",
                syntax="--i",
                concept="+ if (user == null) {",
                group="연산자",
                confidence="high",
            ),
            notify.RenderKeyword(
                term="- 빼기",
                syntax="- return null;",
                concept="--- a/UserService.java",
                group="함수",
                confidence="low",
            ),
        ),
        questions=("- 왜 그런가?",),
        risks=("+ 확인할 것",),
    )

    plan = notify.plan_message(inp)

    assert notify.find_diff_lines(plan.text) == ()
    for chunk in plan.chunks:
        assert notify.find_diff_lines(chunk) == ()
    doc = notify.payload_doc(plan, generated_at="2026-08-31T20:29:14+09:00")
    payloads = doc["payloads"]
    assert isinstance(payloads, list)
    for payload in payloads:
        assert isinstance(payload, dict)
        content = payload["content"]
        assert isinstance(content, str)
        assert notify.find_diff_lines(content) == ()


def test_find_diff_lines_actually_detects_diff_lines() -> None:
    # 위 그물이 항상 참인 단언이 아님을 고정한다.
    assert notify.find_diff_lines("머리말\n+ 추가된 줄\n본문\n- 지운 줄") == (2, 4)


def test_extra_keyword_fields_never_reach_the_message() -> None:
    # FR-051 의 구조적 방어선: RenderKeyword 에는 경로·근거 필드가 아예 없다. 모델이
    # 옛 스키마의 file/evidence/area 를 되살려 보내도 렌더러가 읽는 키는 다섯 개뿐이다.
    leaked = "+ String pw = user.getPassword();"
    doc: dict[str, object] = {
        "source": "openai",
        "summary": {
            "session_title": "로그인 수업",
            "summary": "로그인 흐름을 만들었다.",
            "change_stats": {"files_changed": 1, "added_lines": 2, "deleted_lines": 0},
            "keywords": [
                {
                    "term": "예외 처리",
                    "syntax": "try/catch",
                    "concept": "오류가 났을 때 흐름을 넘기는 문법이다.",
                    "group": "함수",
                    "confidence": "high",
                    "file": "UserService.java",
                    "area": "인증 코어",
                    "evidence": f"--- a/UserService.java\n{leaked}",
                }
            ],
            "questions_to_review": [],
            "risks_or_todos": [],
        },
    }

    inp = notify.build_render_input(
        doc, title_fallback="대체 제목", started_at=STARTED_AT, ended_at=ENDED_AT
    )

    assert inp is not None
    assert inp.title == "로그인 수업"
    assert inp.keywords[0].term == "예외 처리"
    text = notify.render_message(inp)
    assert leaked not in text
    assert "getPassword" not in text
    assert "인증 코어" not in text
    assert "UserService.java" not in text
    assert notify.find_diff_lines(text) == ()


def test_newlines_in_model_strings_fold_into_one_line() -> None:
    # 모델 문자열 하나 = 렌더 결과 정확히 한 줄 (sanitize_line 의 개행 접기).
    inp = _render_input(
        keywords=(
            notify.RenderKeyword(
                term="클로저",
                syntax="",
                concept="첫 줄\n둘째 줄\r\n셋째 줄",
                group="함수",
                confidence="high",
            ),
        )
    )

    lines = notify.render_message(inp).split("\n")

    assert f"{notify.BULLET}클로저" in lines
    assert notify.CONCEPT_INDENT + "첫 줄 둘째 줄 셋째 줄" in lines


def test_long_keyword_fields_are_clamped_with_truncation_mark() -> None:
    # 상한을 넘는 모델 문자열은 잘리고 표시가 붙는다 — 안 자르면 예산 산수가 가정이 된다.
    inp = _render_input(
        keywords=(
            notify.RenderKeyword(
                term="가" * 300,
                syntax="나" * 300,
                concept="다" * 300,
                group="기타",
                confidence="high",
            ),
        ),
        questions=(),
        risks=(),
    )

    lines = notify.render_message(inp).split("\n")

    [head] = [line for line in lines if line.startswith(notify.BULLET)]
    term, syntax = head[len(notify.BULLET) :].split(notify.SYNTAX_GAP)
    assert len(term) == notify.MAX_TERM_CHARS
    assert term.endswith(notify.TRUNCATION_MARK)
    assert len(syntax) == notify.MAX_SYNTAX_CHARS
    [concept] = [line for line in lines if line.startswith(notify.CONCEPT_INDENT + "다")]
    assert len(concept) - len(notify.CONCEPT_INDENT) == notify.MAX_CONCEPT_CHARS
    assert concept.endswith(notify.TRUNCATION_MARK)


def test_confidence_mark_is_shown_for_everything_but_high() -> None:
    # PRD 11.4: high 는 표기 없음. 목록 밖 값은 CONFIDENCE_FALLBACK 으로 흡수한다 —
    # 모델 문자열이 그대로 줄에 실리면 개행 하나로 FR-051 방어선이 뚫린다.
    assert notify.confidence_mark("high") == ""
    assert notify.confidence_mark("medium") == " (medium)"
    assert notify.confidence_mark("low") == " (low)"
    assert notify.confidence_mark("아무거나\n+주입") == " (low)"


# ── 분류별 묶음 (PRD 11.4) ───────────────────────────────────────────────────


def test_group_keywords_follows_the_group_table_order_and_drops_empty_ones() -> None:
    keywords = (
        notify.RenderKeyword("a", "", "설명", "연산자", "high"),
        notify.RenderKeyword("b", "", "설명", "객체생성", "high"),
        notify.RenderKeyword("c", "", "설명", "연산자", "high"),
    )

    grouped = notify.group_keywords(keywords)

    assert [name for name, _ in grouped] == ["객체생성", "연산자"]
    assert [item.term for item in grouped[1][1]] == ["a", "c"]


def test_unknown_group_is_absorbed_instead_of_being_dropped() -> None:
    # 렌더가 키워드를 조용히 떨어뜨리면 전송은 성공하고 메시지만 비는 실패가 된다 —
    # 게이트가 못 잡는 종류라 2차 그물을 둔다.
    keywords = (notify.RenderKeyword("a", "", "설명", "없는분류", "high"),)

    grouped = notify.group_keywords(keywords)

    assert [name for name, _ in grouped] == [notify.KEYWORD_GROUP_FALLBACK]
    assert grouped[0][1][0].term == "a"


# ── 첫 화면 보장과 축소·분할 (FR-050 x FR-033) ───────────────────────────────


def test_first_chunk_holds_title_summary_and_top_two_keywords() -> None:
    # FR-050: 제목·변경 통계 줄·요약·키워드 상위 2건이 첫 화면에 들어간다 (C-17).
    inp = _render_input()

    plan = notify.plan_message(inp)

    first = plan.chunks[0]
    assert notify.TITLE_PREFIX + inp.title in first
    assert inp.summary in first
    assert "화살표 함수" in first
    assert "구조 분해" in first


def test_oversized_input_is_shrunk_section_by_section_in_order() -> None:
    # FR-033: 항목 수를 줄여 축소하고, 줄인 섹션 이름을 순서대로 남긴다. risks 가 먼저
    # 빠지는 순서는 PRD 11.4 「축소 시 가장 먼저 빠진다」 그대로다.
    plan = notify.plan_message(_worst_case_input(), limit=600, max_chunks=2)

    assert plan.shrunk_sections
    order = ("risks", "questions", "keywords")
    assert plan.shrunk_sections == order[: len(plan.shrunk_sections)]


def test_shrink_never_drops_the_first_screen() -> None:
    # FR-050 축소 하한: 아무리 줄여도 제목·메타·요약·키워드 2건은 남는다.
    inp = _worst_case_input()

    plan = notify.plan_message(inp, limit=600, max_chunks=2)

    first = plan.chunks[0]
    assert notify.TITLE_PREFIX in first
    assert "개 파일 변경" in first
    assert notify.HEADER_SUMMARY in first
    assert notify.HEADER_KEYWORDS in plan.text
    keyword_heads = [
        line for line in plan.text.split("\n") if line.startswith(notify.BULLET)
    ]
    assert len(keyword_heads) >= notify.FIRST_SCREEN_KEYWORDS


def test_worst_case_input_still_fits_the_first_screen() -> None:
    # D7: 제목 300자·요약 600자·키워드 전 필드 상한에서도 첫 조각이 한도 안이고
    # 첫 화면 4요소가 전부 그 안에 있다.
    inp = _worst_case_input()

    plan = notify.plan_message(inp)

    first = plan.chunks[0]
    assert len(first) <= notify.DISCORD_CONTENT_LIMIT
    lines = first.split("\n")
    head = lines[0].split(") ", 1)[-1] if lines[0].startswith("(") else lines[0]
    assert head == notify.TITLE_PREFIX + "제" * (
        notify.MAX_TITLE_CHARS - len(notify.TRUNCATION_MARK)
    ) + notify.TRUNCATION_MARK
    assert len(lines[1]) <= notify.META_LINE_MAX
    assert inp.summary in lines
    keyword_heads = [line for line in lines if line.startswith(notify.BULLET)]
    assert len(keyword_heads) >= notify.FIRST_SCREEN_KEYWORDS


def test_meta_line_is_clamped_even_with_absurd_model_integers() -> None:
    # change_stats 정수는 모델이 준 값이고 4단계가 타입만 본다. 메타 줄 상한이 가정이
    # 아니라 실제 clamp 임을 고정한다.
    huge = 10**80

    inp = _render_input(files_changed=huge, added_lines=huge, deleted_lines=huge)

    meta = notify.render_message(inp).split("\n")[1]
    assert len(meta) <= notify.META_LINE_MAX
    assert meta.endswith(notify.TRUNCATION_MARK)


def test_rendered_template_is_cp949_safe() -> None:
    # HANDOFF (다): 같은 문자열이 콘솔로도 나간다. 📚·•·— 가 되살아나면 여기서 잡힌다.
    plan = notify.plan_message(_render_input(rule_based=True, truncated=True))

    plan.text.encode("cp949")
    for chunk in plan.chunks:
        chunk.encode("cp949")
    for token in (
        notify.TITLE_PREFIX,
        notify.BULLET,
        notify.TRUNCATION_MARK,
        notify.OVERFLOW_NOTICE,
        notify.RULE_BASED_NOTICE,
        notify.TRUNCATED_NOTICE,
        notify.GROUP_OPEN,
        notify.GROUP_CLOSE,
        notify.SYNTAX_GAP,
        notify.CONCEPT_INDENT,
        notify.HEADER_SUMMARY,
        notify.HEADER_KEYWORDS,
        notify.HEADER_QUESTIONS,
        notify.HEADER_RISKS,
    ):
        token.encode("cp949")
    # 불릿이 `- ` 면 줄이 `-` 로 시작해 FR-051 보장이 깨진다.
    assert not notify.BULLET.startswith(("+", "-"))


def _two_chunk_input() -> notify.RenderInput:
    return _render_input(
        summary="요" * notify.MAX_SUMMARY_CHARS,
        keywords=tuple(_max_keyword(index) for index in range(10)),
        questions=(),
        risks=(),
    )


def test_split_marks_each_chunk_and_cuts_only_at_line_boundaries() -> None:
    # FR-033: 2조각으로 나뉘면 각 조각 앞에 (1/2)·(2/2) 표시가 붙고, 자르는 자리는
    # 줄 경계뿐이라 조각을 다시 이으면 원문이 된다.
    plan = notify.plan_message(_two_chunk_input())

    assert plan.truncated is False
    assert len(plan.chunks) == 2
    assert plan.chunks[0].startswith("(1/2) ")
    assert plan.chunks[1].startswith("(2/2) ")
    for chunk in plan.chunks:
        assert len(chunk) <= notify.DISCORD_CONTENT_LIMIT
    rebuilt = "\n".join(chunk.split(" ", 1)[1] for chunk in plan.chunks)
    assert rebuilt == plan.text


def test_overflow_beyond_max_chunks_is_truncated_with_a_notice() -> None:
    # FR-052: 조각 수 상한을 넘기면 초과분을 버리고 마지막 조각에 안내를 붙인다.
    # 한 세션이 알림을 3개 이상 띄우지 않는다.
    limit = 300

    plan = notify.plan_message(_worst_case_input(), limit=limit, max_chunks=notify.MAX_CHUNKS)

    assert plan.truncated is True
    assert len(plan.chunks) == notify.MAX_CHUNKS
    assert plan.chunks[-1].endswith(notify.OVERFLOW_NOTICE)
    for chunk in plan.chunks:
        assert len(chunk) <= limit
        assert notify.find_diff_lines(chunk) == ()


def test_rule_based_notice_lands_on_the_first_screen() -> None:
    # FR-039: "LLM 요약이 아님"이 메시지에 명시된다.
    plan = notify.plan_message(_render_input(rule_based=True))

    assert plan.text.split("\n")[2] == notify.RULE_BASED_NOTICE
    assert notify.RULE_BASED_NOTICE in plan.chunks[0]
    assert notify.RULE_BASED_NOTICE not in notify.plan_message(_render_input()).text


# ── C1~C7: 절단 표시 (F2, PRD 11.4 「절단 표시」) ─────────────────────────────
#
# 근거 없는 요약이 근거 있는 요약과 같은 모습으로 나가면 안 된다. 수신자가 구별할 수
# 있는 유일한 표식이 이 한 줄이다.


def test_truncated_notice_sits_right_under_the_meta_line() -> None:
    # C1: PRD 11.4 원문 "메타 줄 아래에 한 줄로". 정확히 1회여야 한다.
    text = notify.render_message(_render_input(truncated=True))

    lines = text.split("\n")
    assert lines[2] == notify.TRUNCATED_NOTICE
    assert text.count(notify.TRUNCATED_NOTICE) == 1


def test_whole_evidence_session_has_no_truncated_notice() -> None:
    # C2: 절단이 없는 세션이 정상 경로다. 표시가 상시로 붙으면 뜻을 잃는다.
    text = notify.render_message(_render_input(truncated=False))

    assert notify.TRUNCATED_NOTICE not in text


def test_rule_based_notice_comes_before_the_truncated_notice() -> None:
    # C3: "요약을 누가 만들었나"가 "근거가 온전한가"보다 상위 사실이다. 둘 다
    # FIRST_SCREEN_MAX_CHARS 에 계상돼 있어 동시 발생이 첫 화면 보장을 깨지 않는다.
    lines = notify.render_message(
        _render_input(rule_based=True, truncated=True)
    ).split("\n")

    assert lines[2] == notify.RULE_BASED_NOTICE
    assert lines[3] == notify.TRUNCATED_NOTICE


def test_build_render_input_reads_input_truncated_from_the_summary_doc() -> None:
    # C4: 배선은 summary.json 의 input.truncated 한 줄뿐이다 (watcher.py 는 안 바뀐다).
    doc: dict[str, object] = {
        "source": "openai",
        "input": {
            "truncated": True,
            "omitted_files": [],
            "partial_files": [
                {"path": "09_함수.html", "included_hunks": 12, "total_hunks": 31}
            ],
            "diff_chars": 59_912,
        },
        "summary": {
            "session_title": "함수 수업",
            "summary": "화살표 함수를 다뤘다.",
            "change_stats": {"files_changed": 5, "added_lines": 300, "deleted_lines": 100},
            "keywords": [],
            "questions_to_review": [],
            "risks_or_todos": [],
        },
    }

    inp = notify.build_render_input(
        doc, title_fallback="대체", started_at=STARTED_AT, ended_at=ENDED_AT
    )

    assert inp is not None
    assert inp.truncated is True
    assert notify.TRUNCATED_NOTICE in notify.render_message(inp)
    # 경로·hunk 수는 프롬프트 메타에만 있고 메시지에는 닿지 않는다 (FR-051).
    assert "09_함수.html" not in notify.render_message(inp)


@pytest.mark.parametrize(
    "block",
    [
        None,
        {"omitted_files": [], "diff_chars": 42},
        "블록이 아니라 문자열",
        [],
        {"truncated": False},
    ],
)
def test_missing_or_malformed_input_block_flows_to_not_truncated(block: object) -> None:
    # C5: sessions/ 에 영구히 남는 1.1/1.2 산출물에는 input 블록이 없거나 형태가 다르다.
    # 마이그레이션 대신 .get() 기본값으로 흡수한다 — 여기서 예외가 나면 안 된다.
    doc: dict[str, object] = {
        "source": "openai",
        "summary": {
            "session_title": "옛 세션",
            "summary": "옛 형식 문서다.",
            "change_stats": {"files_changed": 1, "added_lines": 1, "deleted_lines": 1},
            "keywords": [],
            "questions_to_review": [],
            "risks_or_todos": [],
        },
    }
    if block is not None:
        doc["input"] = block

    inp = notify.build_render_input(
        doc, title_fallback="대체", started_at=STARTED_AT, ended_at=ENDED_AT
    )

    assert inp is not None
    assert inp.truncated is False
    assert notify.TRUNCATED_NOTICE not in notify.render_message(inp)


def test_truncated_notice_is_console_and_fr051_safe() -> None:
    # C6: 같은 문자열이 한국어 Windows 콘솔로도 나간다 (watcher 가 렌더 전체본을 emit).
    # `[` 로 시작하므로 FR-051 검사에 걸리지 않는다.
    notify.TRUNCATED_NOTICE.encode("cp949")
    assert not notify.TRUNCATED_NOTICE.startswith(("+", "-"))
    assert notify.find_diff_lines(notify.TRUNCATED_NOTICE) == ()
    # 수신자는 코드를 안 보는 사람이다 — 도구 내부 용어를 쓰지 않는다 (FR-050).
    assert "프롬프트" not in notify.TRUNCATED_NOTICE
    assert "hunk" not in notify.TRUNCATED_NOTICE


def test_worst_case_truncated_message_carries_no_diff_lines() -> None:
    # C7 (FR-051 회귀): 표시 줄이 두 개 늘어난 최악 메시지에서도 조각 어디에도
    # `+`/`-` 로 시작하는 줄이 없다.
    plan = notify.plan_message(_worst_case_input())

    assert notify.find_diff_lines(plan.text) == ()
    for chunk in plan.chunks:
        assert notify.find_diff_lines(chunk) == ()
    doc = notify.payload_doc(plan, generated_at="2026-09-01T20:29:14+09:00")
    payloads = doc["payloads"]
    assert isinstance(payloads, list)
    for payload in payloads:
        assert isinstance(payload, dict)
        content = payload["content"]
        assert isinstance(content, str)
        assert notify.find_diff_lines(content) == ()


# ── D1~D9: 렌더 예산 관계식 (F5, PRD 11.3) ───────────────────────────────────
#
# PRD 11.3 이 "값이 아니라 관계를 단언해라"고 못박은 자리다. DISCORD_CONTENT_LIMIT 이
# `추정`이라 실전송으로 정정되면 이 단언들이 즉시 깨져 알려 준다.


def test_first_screen_budget_fits_one_chunk() -> None:
    # D1 (R1): FR-050 x FR-033 동시 성립의 유일한 기계적 근거. 표시 두 줄(규칙 기반 ·
    # 절단)이 동시에 참인 경우까지 계상돼 있다.
    assert notify.FIRST_SCREEN_MAX_CHARS <= notify.DISCORD_CONTENT_LIMIT
    # 첫 화면 전용 상한은 일반 상한보다 작아야 예산 식이 성립한다.
    assert notify.MAX_TITLE_CHARS < notify.MAX_LINE_CHARS
    assert notify.MAX_ITEM_CHARS < notify.MAX_LINE_CHARS
    assert notify.FIRST_SCREEN_KEYWORDS <= notify.MAX_KEYWORDS_SHOWN


def test_full_keyword_load_fits_the_prd_relation() -> None:
    # D2: PRD 11.3 원문 관계식 —
    # 고정부 + MAX_KEYWORDS * KEYWORD_BLOCK_MAX <= DISCORD_CONTENT_LIMIT * MAX_CHUNKS.
    left = (
        notify.NON_SHRINKABLE_MAX_CHARS
        + notify.MAX_KEYWORDS_SHOWN * notify.KEYWORD_BLOCK_MAX
    )
    assert left <= notify.DISCORD_CONTENT_LIMIT * notify.MAX_CHUNKS


def test_full_message_fits_the_real_two_chunk_capacity() -> None:
    # D3: split_text 는 2조각이 되는 순간 각 조각에서 조각 번호 자리를 빼고 자른다 —
    # 실제 수용량은 limit 이 아니라 (limit - CHUNK_MARK_MAX) 다. 축소 후에도 남는
    # 질문 1건(바닥)까지 넣어야 "키워드가 2건으로 붕괴하지 않는다"가 증명된다.
    assert notify.FULL_MESSAGE_MAX_CHARS == (
        notify.NON_SHRINKABLE_MAX_CHARS
        + notify.MAX_KEYWORDS_SHOWN * notify.KEYWORD_BLOCK_MAX
        + notify.QUESTION_FLOOR_MAX_CHARS
    )
    assert notify.FULL_MESSAGE_MAX_CHARS <= (
        notify.DISCORD_CONTENT_LIMIT - notify.CHUNK_MARK_MAX
    ) * notify.MAX_CHUNKS


def test_worst_case_never_collapses_keywords_to_two() -> None:
    # D4: PRD 11.3 이 경고한 실패 — "상한을 없애면 키워드가 많이 나온 날 오히려 2개짜리
    # 메시지가 나간다". MAX_ITEM_CHARS 가 없으면 상한 15 에서도 이 붕괴가 도달 가능하다.
    plan = notify.plan_message(_worst_case_input())

    assert "keywords" not in plan.shrunk_sections
    assert len(plan.chunks) <= notify.MAX_CHUNKS
    assert plan.truncated is False
    for chunk in plan.chunks:
        assert len(chunk) <= notify.DISCORD_CONTENT_LIMIT
    heads = [line for line in plan.text.split("\n") if line.startswith(notify.BULLET)]
    # 키워드 15건의 머리줄 + 축소 후 남은 질문 1건.
    assert len([line for line in heads if line.startswith(notify.BULLET + "용")]) == 15


def test_realistic_fifteen_keywords_fit_one_chunk_without_shrinking() -> None:
    # D5: 4절 산수는 상한이고 실제 값은 훨씬 짧다. 정상 경로에서 축소가 새로 발동하면
    # 안 된다 — shrink 예산을 12자 낮춘 변경(4.3)의 안전 확인이기도 하다.
    inp = _render_input(
        keywords=tuple(_realistic_keyword(index) for index in range(15)),
        questions=("오늘 배운 문법 중 직접 못 써 본 것은 무엇인가?",),
        risks=("실습 파일을 저장하지 않은 것이 있는지 확인",),
    )

    plan = notify.plan_message(inp)

    assert plan.shrunk_sections == ()
    assert len(plan.chunks) == 1
    assert plan.truncated is False
    assert len(plan.chunks[0]) <= notify.DISCORD_CONTENT_LIMIT


def test_render_shows_at_most_fifteen_keywords() -> None:
    # D6: 4단계의 clamp 를 통과하지 못한 doc(손으로 고친 것 등)이 와도 렌더 쪽에서
    # 한 번 더 막는다. 여기가 없으면 예산 산수가 입력 신뢰에 의존하게 된다.
    doc: dict[str, object] = {
        "source": "openai",
        "summary": {
            "session_title": "많은 키워드",
            "summary": "키워드가 16개인 문서다.",
            "change_stats": {"files_changed": 1, "added_lines": 1, "deleted_lines": 1},
            "keywords": [
                {
                    "term": f"키워드{index}",
                    "syntax": "",
                    "concept": "설명",
                    "group": "기타",
                    "confidence": "high",
                }
                for index in range(16)
            ],
            "questions_to_review": [],
            "risks_or_todos": [],
        },
    }

    inp = notify.build_render_input(
        doc, title_fallback="대체", started_at=STARTED_AT, ended_at=ENDED_AT
    )

    assert inp is not None
    assert len(inp.keywords) == notify.MAX_KEYWORDS_SHOWN == 15
    assert "키워드15" not in notify.render_message(inp)


def test_item_lines_are_clamped_to_max_item_chars() -> None:
    # D8: 질문·확인할 점 줄의 로컬 clamp. MAX_LINE_CHARS(300)로 두면 최악 입력에서
    # shrink 가 키워드를 2건으로 붕괴시킨다 (설계 4.2 R3).
    inp = _render_input(questions=("질" * 300,), risks=("확" * 300,))

    lines = notify.render_message(inp).split("\n")

    for prefix in ("질", "확"):
        [line] = [item for item in lines if item.startswith(notify.BULLET + prefix)]
        body = line[len(notify.BULLET) :]
        assert len(body) == notify.MAX_ITEM_CHARS
        assert body.endswith(notify.TRUNCATION_MARK)


def test_shrink_budget_subtracts_the_chunk_mark() -> None:
    # D9: shrink 의 판정 예산은 limit * max_chunks 가 아니라
    # (limit - CHUNK_MARK_MAX) * max_chunks 다. 이 12자 차이는 키워드 5건 시절에는
    # 닿을 일이 없었지만 15건에서는 최악값이 천장에 붙어 닿는다.
    limit, max_chunks = 100, 2
    target = (limit - notify.CHUNK_MARK_MAX) * max_chunks + 1
    probe = _render_input(summary="요", keywords=(), questions=(), risks=("확",))
    base = len(notify.render_message(probe))
    assert base < target
    inp = replace(probe, summary="요" * (1 + target - base))

    # 옛 예산(limit * max_chunks)으로는 축소가 발동하지 않는 크기다.
    assert len(notify.render_message(inp)) == target
    assert target > (limit - notify.CHUNK_MARK_MAX) * max_chunks
    assert target <= limit * max_chunks

    _, shrunk = notify.shrink(inp, limit=limit, max_chunks=max_chunks)

    assert shrunk == ("risks",)


# ── 전달 판정과 계수 (FR-034, FR-035, FR-052) ────────────────────────────────


def _plan_of(chunks: tuple[str, ...]) -> notify.MessagePlan:
    return notify.MessagePlan(
        chunks=chunks, text="\n".join(chunks), shrunk_sections=(), truncated=False
    )


def test_deliver_counts_one_request_per_chunk() -> None:
    # 요청 1회 = SendFn 1회. 이 계수가 FR-035/FR-052 주장의 기준점이다.
    sent: list[Mapping[str, object]] = []

    def send(payload: Mapping[str, object]) -> int:
        sent.append(dict(payload))
        return 204

    outcome = notify.deliver(_plan_of(("첫 조각", "둘째 조각")), send)

    assert outcome.delivered is True
    assert outcome.requests == 2
    assert outcome.chunks == 2
    assert outcome.http_status == 204
    assert outcome.error is None
    assert outcome.skip_reason is None
    assert sent == [{"content": "첫 조각"}, {"content": "둘째 조각"}]
    assert notify.resolve_discord_state(outcome) == notify.DISCORD_SENT


@pytest.mark.parametrize(
    ("kind", "http_status", "expected_error"),
    [
        # 4xx / 5xx / timeout / 연결 실패를 error 값으로 가른다 (FR-034).
        (notify.KIND_HTTP, 404, "discord_http_404"),
        (notify.KIND_HTTP, 503, "discord_http_503"),
        (notify.KIND_HTTP, 429, "discord_http_429"),
        (notify.KIND_HTTP, None, notify.ERROR_DISCORD_HTTP),
        (notify.KIND_TIMEOUT, None, notify.ERROR_DISCORD_TIMEOUT),
        (notify.KIND_CONNECTION, None, notify.ERROR_DISCORD_CONNECTION),
    ],
)
def test_delivery_failures_map_to_session_error_codes(
    kind: str, http_status: int | None, expected_error: str
) -> None:
    def send(payload: Mapping[str, object]) -> int:
        raise notify.DiscordRequestError(kind, http_status=http_status)

    outcome = notify.deliver(_plan_of(("조각",)), send)

    assert outcome.delivered is False
    assert outcome.error == expected_error
    assert outcome.http_status == http_status
    assert outcome.requests == 0
    assert outcome.chunks == 1
    assert notify.resolve_discord_state(outcome) == notify.DISCORD_FAILED
    assert notify.session_discord_fields(outcome)["http_status"] == http_status


def test_partial_delivery_records_what_actually_went_out() -> None:
    # 2조각 중 1조각만 나갔으면 requests 는 1이다. 이미 나간 것을 되돌릴 수 없으므로
    # 계수는 사실대로 남긴다.
    attempts: list[int] = []

    def send(payload: Mapping[str, object]) -> int:
        attempts.append(1)
        if len(attempts) == 1:
            return 204
        raise notify.DiscordRequestError(notify.KIND_HTTP, http_status=500)

    outcome = notify.deliver(_plan_of(("첫 조각", "둘째 조각")), send)

    assert outcome.requests == 1
    assert outcome.chunks == 2
    assert outcome.delivered is False
    assert outcome.error == "discord_http_500"


@pytest.mark.parametrize(
    "reason",
    [
        notify.SKIP_NO_CHANGE,
        notify.SKIP_NO_DISCORD,
        notify.SKIP_DRY_RUN,
        notify.SKIP_SECRETS_BLOCKED,
        notify.SKIP_NO_SUMMARY,
    ],
)
def test_skipped_delivery_keeps_a_zero_request_counter(reason: str) -> None:
    # FR-035/FR-052: 생략 5갈래는 전부 requests 0 이고 사유가 그대로 남는다.
    outcome = notify.skipped_delivery(reason)

    assert outcome.requests == 0
    assert outcome.chunks == 0
    assert outcome.delivered is False
    assert outcome.error is None
    assert notify.session_discord_fields(outcome) == {
        "delivered": False,
        "http_status": None,
        "requests": 0,
        "chunks": 0,
        "skip_reason": reason,
    }
    assert notify.resolve_discord_state(outcome) == notify.DISCORD_SKIPPED


def test_failed_delivery_is_not_a_skip() -> None:
    # 전송을 시도조차 못 한 실패는 생략이 아니다 — 사용자는 전송을 기대했다.
    outcome = notify.failed_delivery(notify.ERROR_DISCORD_URL_MISSING)

    assert outcome.requests == 0
    assert outcome.skip_reason is None
    assert outcome.error == notify.ERROR_DISCORD_URL_MISSING
    assert notify.resolve_discord_state(outcome) == notify.DISCORD_FAILED


def test_session_discord_fields_of_none_is_the_abort_shape() -> None:
    # abort 세션에도 discord 필드가 남는다 — "전송 0회"를 사후 집계로 증명하려면 전
    # 세션에 계수가 있어야 한다.
    assert notify.session_discord_fields(None) == {
        "delivered": False,
        "http_status": None,
        "requests": 0,
        "chunks": 0,
        "skip_reason": None,
    }
    assert notify.resolve_discord_state(None) == notify.DISCORD_NOT_RUN


# ── 산출물·정리 안내·콘솔 가드·입력 환원 ─────────────────────────────────────


def test_payload_doc_carries_no_webhook_url() -> None:
    # discord_payload.json 은 사람이 열어보는 수동 복사 경로다. URL 은 비밀값이라
    # 담지 않는다 (PRD 13.1).
    plan = notify.plan_message(_render_input())

    doc = notify.payload_doc(plan, generated_at="2026-08-31T20:29:14+09:00")

    # 필드 변화가 없어 C-18 에서도 1.2 로 남는다 (설계 5.5).
    assert doc["schema_version"] == notify.NOTIFY_SCHEMA_VERSION == "1.2"
    assert doc["generated_at"] == "2026-08-31T20:29:14+09:00"
    assert doc["chunks"] == len(plan.chunks)
    assert doc["truncated"] is False
    assert doc["shrunk_sections"] == []
    raw = json.dumps(doc, ensure_ascii=False)
    assert "webhook" not in raw.lower()
    assert "discord.com" not in raw


def test_write_payload_json_leaves_no_temp_file(tmp_path: Path) -> None:
    # 다른 산출물과 같은 원자적 교체 — 반쪽 파일이 남지 않는다.
    target = tmp_path / "session" / "discord_payload.json"
    doc = notify.payload_doc(
        notify.plan_message(_render_input()), generated_at="2026-08-31T20:29:14+09:00"
    )

    notify.write_payload_json(target, doc)

    assert json.loads(target.read_text(encoding="utf-8")) == doc
    assert [path.name for path in target.parent.glob(".discord-*")] == []


def test_cleanup_notice_lists_session_root_and_dotenv_paths() -> None:
    # FR-053: 산출물 위치 + .env 잔존 + 환경변수 이름 3항목. 환경변수는 이름만 알리고
    # 값·존재 여부를 찍지 않는다 (FR-003).
    lines = notify.cleanup_notice("C:/sessions/20260831-202914-ab12", ["C:/work/.env"])

    joined = "\n".join(lines)
    assert lines[0].startswith("[정리 안내]")
    assert "C:/sessions/20260831-202914-ab12" in joined
    assert "C:/work/.env" in joined
    assert "OPENAI_API_KEY" in joined
    assert "DISCORD_WEBHOOK_URL" in joined
    joined.encode("cp949")
    assert "없음" in "\n".join(notify.cleanup_notice("root", []))


def test_console_safe_replaces_unencodable_chars_without_raising() -> None:
    # 모델 문자열에는 이모지가 올 수 있다. 인코딩 불가 문자가 프로세스를 죽이지 않게
    # 출력 경계에서 한 번 무해화한다 (HANDOFF (다)).
    result = notify.console_safe("📚 오늘 요약", "cp949")

    result.encode("cp949")
    assert "오늘 요약" in result
    assert notify.console_safe("📚 x", None) == "📚 x"
    # 알 수 없는 코덱 이름이면 무해화를 포기한다 — 죽는 것보다 낫다.
    assert notify.console_safe("📚 x", "존재하지-않는-코덱") == "📚 x"


def test_format_period_falls_back_to_raw_strings() -> None:
    # 파싱 불가 문자열에서 예외를 내지 않는다 (렌더링이 멈추면 안 된다).
    assert notify.format_period("언제", "몰라") == "언제 - 몰라"
    assert notify.format_period(STARTED_AT, ENDED_AT) == "2026-08-26 18:30-20:29"
    across_days = notify.format_period(STARTED_AT, "2026-08-27T01:00:00+09:00")
    assert across_days == "2026-08-26 18:30 - 2026-08-27 01:00"


@pytest.mark.parametrize(
    "doc",
    [
        {},
        {"summary": "블록이 아니라 문자열"},
        {"summary": {"session_title": "제목만 있고 본문이 없다"}},
        {"summary": {"summary": 42}},
    ],
)
def test_build_render_input_returns_none_without_the_summary_block(
    doc: dict[str, object],
) -> None:
    # 필수 재료가 없으면 None 을 돌려 stats-only 경로로 흐른다.
    assert (
        notify.build_render_input(
            doc, title_fallback="대체", started_at=STARTED_AT, ended_at=ENDED_AT
        )
        is None
    )


def test_build_render_input_uses_fallback_title_and_reads_source() -> None:
    doc: dict[str, object] = {
        "source": "rule_based",
        "summary": {
            "session_title": "   ",
            "summary": "규칙 기반 요약이다.",
            "change_stats": {"files_changed": 3, "added_lines": "많음", "deleted_lines": True},
            "keywords": "리스트가 아니다",
            "questions_to_review": ["질문", 7],
            "risks_or_todos": [],
        },
    }

    inp = notify.build_render_input(
        doc, title_fallback="대체 제목", started_at=STARTED_AT, ended_at=ENDED_AT
    )

    assert inp is not None
    assert inp.title == "대체 제목"
    assert inp.rule_based is True
    assert inp.files_changed == 3
    # 타입이 어긋난 값과 bool 은 0 으로 떨어진다 (숫자를 지어내지 않는다).
    assert inp.added_lines == 0
    assert inp.deleted_lines == 0
    assert inp.keywords == ()
    assert inp.questions == ("질문",)
    assert inp.truncated is False


def test_broken_keyword_entry_does_not_drop_the_rest() -> None:
    # 옛 형식 doc 이나 손으로 고친 doc 에서 나머지 키워드까지 통째로 잃지 않는다.
    doc: dict[str, object] = {
        "source": "openai",
        "summary": {
            "session_title": "혼합",
            "summary": "일부 항목이 망가진 문서다.",
            "change_stats": {"files_changed": 1, "added_lines": 1, "deleted_lines": 1},
            "keywords": [
                "문자열이 왔다",
                {"term": "클래스", "syntax": "class", "concept": "설명", "group": "객체생성",
                 "confidence": "high"},
                {"term": "누락", "concept": "설명", "group": "함수", "confidence": "high"},
            ],
            "questions_to_review": [],
            "risks_or_todos": [],
        },
    }

    inp = notify.build_render_input(
        doc, title_fallback="대체", started_at=STARTED_AT, ended_at=ENDED_AT
    )

    assert inp is not None
    assert [item.term for item in inp.keywords] == ["클래스"]


def test_render_stats_only_shows_numbers_and_reason(tmp_path: Path) -> None:
    # 요약이 없는 세션에서도 실행자에게 통계와 산출물 경로가 보인다.
    block = notify.render_stats_only(
        title="검증 세션",
        started_at=STARTED_AT,
        ended_at=ENDED_AT,
        files_changed=2,
        added_lines=42,
        deleted_lines=18,
        session_root=str(tmp_path),
        reason="openai_timeout",
    )

    assert block.startswith("[요약 없음] 검증 세션")
    assert "2개 파일 변경" in block
    assert "openai_timeout" in block
    assert str(tmp_path) in block
    assert notify.find_diff_lines(block) == ()
    block.encode("cp949")
