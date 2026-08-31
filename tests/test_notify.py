"""notify 모듈 — 렌더링·축소·분할·payload·전달 판정 (FR-033/034/039/050~053).

설계 6절 [테스트 가능] 케이스 1~20 과 7-1~7-4. 이 모듈은 네트워크·시계를 만지지 않고
디스크도 write_payload_json 하나뿐이라 전 경로가 결정적으로 돈다. 전송 계층은 SendFn
을 직접 넣어 계수까지 본다 — 실제 Discord 전송은 사람 확인 체크리스트로 넘긴다
(VERIFY.md).
"""

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from class_watcher import notify

STARTED_AT = "2026-08-26T18:30:00+09:00"
ENDED_AT = "2026-08-26T20:29:00+09:00"

DEFAULT_CHANGES = (
    notify.RenderChange(
        file="UserService.java", type="modified", description="login() 을 추가했다"
    ),
    notify.RenderChange(
        file="UserController.java", type="added", description="POST /login 엔드포인트"
    ),
)
DEFAULT_LEARNING = (
    notify.RenderLearning(topic="예외 처리", explanation="입력 검증과 책임 분리"),
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
    changes: tuple[notify.RenderChange, ...] = DEFAULT_CHANGES,
    learning_points: tuple[notify.RenderLearning, ...] = DEFAULT_LEARNING,
    questions: tuple[str, ...] = ("인증 실패 유형을 어떻게 구분할까?",),
    risks: tuple[str, ...] = ("비밀번호 해싱 여부 확인",),
    rule_based: bool = False,
) -> notify.RenderInput:
    return notify.RenderInput(
        title=title,
        started_at=started_at,
        ended_at=ended_at,
        files_changed=files_changed,
        added_lines=added_lines,
        deleted_lines=deleted_lines,
        summary=summary,
        changes=changes,
        learning_points=learning_points,
        questions=questions,
        risks=risks,
        rule_based=rule_based,
    )


def _bulky_input() -> notify.RenderInput:
    """각 섹션을 상한까지 꽉 채운 초장문 입력 (케이스 6·7)."""
    return _render_input(
        summary="요" * notify.MAX_SUMMARY_CHARS,
        changes=tuple(
            notify.RenderChange(
                file=f"pkg/File{index}.java", type="modified", description="설" * 300
            )
            for index in range(5)
        ),
        learning_points=tuple(
            notify.RenderLearning(topic=f"주제{index}", explanation="설" * 300)
            for index in range(5)
        ),
        questions=tuple("질" * 300 for _ in range(5)),
        risks=tuple("확" * 300 for _ in range(5)),
    )


# ── 케이스 1·2·3·4: FR-051 3중 방어선 ────────────────────────────────────────


def test_diff_shaped_model_strings_never_render_as_diff_lines() -> None:
    # 케이스 1 (FR-051): 모델이 diff 모양 문자열을 줘도 렌더 결과에 `+`/`-` 로 시작하는
    # 줄이 생기지 않는다. text·chunks·payload 세 곳 전부를 본다.
    inp = _render_input(
        summary="- 항목 하나를 지웠다",
        changes=(
            notify.RenderChange(
                file="+++ b/UserService.java",
                type="modified",
                description="+ if (user == null) {",
            ),
            notify.RenderChange(
                file="--- a/UserService.java", type="modified", description="- return null;"
            ),
        ),
        learning_points=(notify.RenderLearning(topic="- 목록", explanation="+ 더하기"),),
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
    # 케이스 1 의 그물이 항상 참인 단언이 아님을 고정한다.
    assert notify.find_diff_lines("머리말\n+ 추가된 줄\n본문\n- 지운 줄") == (2, 4)


def test_evidence_and_area_never_reach_the_message() -> None:
    # 케이스 2 (FR-051, 판정 9번 ③): evidence 는 summary.json 스키마에 남지만 렌더러가
    # 그 필드를 읽지 않는다. area 도 같다 (설계 5.1 이탈표).
    leaked = "+ String pw = user.getPassword();"
    doc: dict[str, object] = {
        "source": "openai",
        "summary": {
            "session_title": "로그인 수업",
            "summary": "로그인 흐름을 만들었다.",
            "change_stats": {"files_changed": 1, "added_lines": 2, "deleted_lines": 0},
            "changes": [
                {
                    "file": "UserService.java",
                    "area": "인증 코어",
                    "type": "modified",
                    "description": "login() 추가",
                    "evidence": f"--- a/UserService.java\n{leaked}",
                }
            ],
            "learning_points": [],
            "questions_to_review": [],
            "risks_or_todos": [],
        },
    }

    inp = notify.build_render_input(
        doc, title_fallback="대체 제목", started_at=STARTED_AT, ended_at=ENDED_AT
    )

    assert inp is not None
    assert inp.title == "로그인 수업"
    assert inp.changes[0].file == "UserService.java"
    text = notify.render_message(inp)
    assert leaked not in text
    assert "getPassword" not in text
    assert "인증 코어" not in text
    assert notify.find_diff_lines(text) == ()


def test_newlines_in_model_strings_fold_into_one_line() -> None:
    # 케이스 3: 모델 문자열 하나 = 렌더 결과 정확히 한 줄 (sanitize_line 의 개행 접기).
    inp = _render_input(
        changes=(
            notify.RenderChange(
                file="a.java", type="modified", description="첫 줄\n둘째 줄\r\n셋째 줄"
            ),
        )
    )

    lines = notify.render_message(inp).split("\n")

    [line] = [item for item in lines if item.startswith("1. ")]
    assert line == "1. a.java - 첫 줄 둘째 줄 셋째 줄"


def test_long_description_is_clamped_with_truncation_mark() -> None:
    # 케이스 4: 300자 넘는 description 은 MAX_LINE_CHARS 로 잘리고 표시가 붙는다.
    inp = _render_input(
        changes=(
            notify.RenderChange(file="a.java", type="modified", description="가" * 500),
        )
    )

    [line] = [
        item for item in notify.render_message(inp).split("\n") if item.startswith("1. ")
    ]

    body = line[len("1. a.java - ") :]
    assert len(body) == notify.MAX_LINE_CHARS
    assert body.endswith(notify.TRUNCATION_MARK)


# ── 케이스 5·6·7 + 7-1~7-4: 첫 화면 보장과 축소·분할 (FR-050 x FR-033) ────────


def test_first_chunk_holds_title_summary_and_top_two_changes() -> None:
    # 케이스 5 (FR-050): 제목·요약·주요 변경 상위 2건이 첫 조각 안에 있다.
    inp = _render_input()

    plan = notify.plan_message(inp)

    first = plan.chunks[0]
    assert notify.TITLE_PREFIX + inp.title in first
    assert inp.summary in first
    assert "1. UserService.java" in first
    assert "2. UserController.java" in first


def test_oversized_input_is_shrunk_section_by_section_in_order() -> None:
    # 케이스 6 (FR-033): 항목 수를 줄여 축소하고, 줄인 섹션 이름을 순서대로 남긴다.
    plan = notify.plan_message(_bulky_input())

    assert len(plan.chunks) <= notify.MAX_CHUNKS
    for chunk in plan.chunks:
        assert len(chunk) <= notify.DISCORD_CONTENT_LIMIT
    order = ("risks", "questions", "learning_points", "changes")
    assert plan.shrunk_sections
    assert plan.shrunk_sections == order[: len(plan.shrunk_sections)]


def test_shrink_never_drops_the_first_screen() -> None:
    # 케이스 7 (FR-050 축소 하한): 아무리 줄여도 제목·메타·요약·변경 2건은 남는다.
    inp = _bulky_input()

    plan = notify.plan_message(inp)

    first = plan.chunks[0]
    assert notify.TITLE_PREFIX in first
    assert "개 파일 변경" in first
    assert notify.HEADER_SUMMARY in first
    assert inp.summary in first
    assert "1. pkg/File0.java" in first
    assert "2. pkg/File1.java" in first


def test_first_screen_budget_fits_in_one_chunk() -> None:
    # 케이스 7-1: FR-050 x FR-033 동시 성립의 유일한 기계적 근거. DISCORD_CONTENT_LIMIT
    # 이 `추정`이라 실전송으로 정정되면 이 단언이 즉시 깨져 알려 준다 (설계 5.4).
    assert notify.FIRST_SCREEN_MAX_CHARS <= notify.DISCORD_CONTENT_LIMIT
    # 설계 4.1 식에 FR-039 표시 줄이 빠져 있다 (IMPL 2.7). 그 줄은 모델 입력이 아니라
    # 고정 상수라 최악값이 결정적이므로 여기서 함께 고정한다.
    assert (
        notify.FIRST_SCREEN_MAX_CHARS + len(notify.RULE_BASED_NOTICE) + 1
        <= notify.DISCORD_CONTENT_LIMIT
    )
    # 첫 화면 전용 상한은 일반 상한보다 작아야 예산 식이 성립한다.
    assert notify.MAX_TITLE_CHARS < notify.MAX_LINE_CHARS
    assert notify.MAX_PATH_CHARS < notify.MAX_LINE_CHARS
    assert notify.FIRST_SCREEN_CHANGES <= notify.MAX_CHANGES_SHOWN


def test_worst_case_input_still_fits_the_first_screen() -> None:
    # 케이스 7-2: 제목 300자·경로 300자·설명 300자·요약 600자의 최악 입력에서도 첫
    # 조각이 한도 안이고 첫 화면 4요소가 전부 그 안에 있다.
    inp = _render_input(
        title="제" * 300,
        summary="요" * notify.MAX_SUMMARY_CHARS,
        changes=(
            notify.RenderChange(file="가" * 300, type="modified", description="설" * 300),
            notify.RenderChange(file="나" * 300, type="modified", description="명" * 300),
        ),
        learning_points=(),
        questions=(),
        risks=(),
    )

    plan = notify.plan_message(inp)

    first = plan.chunks[0]
    assert len(first) <= notify.DISCORD_CONTENT_LIMIT
    lines = first.split("\n")
    assert lines[0] == notify.TITLE_PREFIX + "제" * (
        notify.MAX_TITLE_CHARS - len(notify.TRUNCATION_MARK)
    ) + notify.TRUNCATION_MARK
    assert len(lines[1]) <= notify.META_LINE_MAX
    assert inp.summary in lines
    change_lines = [item for item in lines if item.startswith(("1. ", "2. "))]
    assert len(change_lines) == 2
    for line in change_lines:
        path = line[len("1. ") :].split(" - ")[0]
        assert len(path) == notify.MAX_PATH_CHARS


def test_long_path_keeps_the_tail_so_the_filename_survives() -> None:
    # 케이스 7-3: 경로는 앞이 아니라 뒤를 남긴다 — 앞을 남기면 어느 파일인지 못 읽는다.
    path = "src/com/academy/" + "sub/" * 40 + "LoginFailedException.java"
    assert len(path) > notify.MAX_PATH_CHARS

    result = notify.sanitize_path(path)

    assert len(result) == notify.MAX_PATH_CHARS
    assert result.startswith(notify.TRUNCATION_MARK)
    assert result.endswith("LoginFailedException.java")


def test_rendered_template_is_cp949_safe() -> None:
    # 케이스 7-4 (HANDOFF (다)): 같은 문자열이 콘솔로도 나간다. 📚·•·— 가 되살아나면
    # 여기서 잡힌다. 모델 문자열 자체는 console_safe 가 처리한다.
    plan = notify.plan_message(_render_input(rule_based=True))

    plan.text.encode("cp949")
    for chunk in plan.chunks:
        chunk.encode("cp949")
    for token in (
        notify.TITLE_PREFIX,
        notify.BULLET,
        notify.TRUNCATION_MARK,
        notify.OVERFLOW_NOTICE,
        notify.RULE_BASED_NOTICE,
        notify.HEADER_SUMMARY,
        notify.HEADER_CHANGES,
        notify.HEADER_LEARNING,
        notify.HEADER_QUESTIONS,
        notify.HEADER_RISKS,
    ):
        token.encode("cp949")
    # 불릿이 `- ` 면 줄이 `-` 로 시작해 FR-051 보장이 깨진다 (설계 5.2).
    assert not notify.BULLET.startswith(("+", "-"))


def _two_chunk_input() -> notify.RenderInput:
    return _render_input(
        summary="요" * notify.MAX_SUMMARY_CHARS,
        changes=tuple(
            notify.RenderChange(
                file=f"pkg/File{index}.java", type="modified", description="설" * 300
            )
            for index in range(5)
        ),
        learning_points=(notify.RenderLearning(topic="주제", explanation="설" * 300),),
        questions=(),
        risks=(),
    )


def test_split_marks_each_chunk_and_cuts_only_at_line_boundaries() -> None:
    # 케이스 8 (FR-033): 2조각으로 나뉘면 각 조각 앞에 (1/2)·(2/2) 표시가 붙고, 자르는
    # 자리는 줄 경계뿐이라 조각을 다시 이으면 원문이 된다.
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
    # 케이스 9 (FR-052): 조각 수 상한을 넘기면 초과분을 버리고 마지막 조각에 안내를
    # 붙인다. 한 세션이 알림을 3개 이상 띄우지 않는다.
    limit = 300

    plan = notify.plan_message(_bulky_input(), limit=limit, max_chunks=notify.MAX_CHUNKS)

    assert plan.truncated is True
    assert len(plan.chunks) == notify.MAX_CHUNKS
    assert plan.chunks[-1].endswith(notify.OVERFLOW_NOTICE)
    for chunk in plan.chunks:
        assert len(chunk) <= limit
        assert notify.find_diff_lines(chunk) == ()


def test_rule_based_notice_lands_on_the_first_screen() -> None:
    # 케이스 10 (FR-039): "LLM 요약이 아님"이 메시지에 명시된다.
    plan = notify.plan_message(_render_input(rule_based=True))

    assert plan.text.split("\n")[2] == notify.RULE_BASED_NOTICE
    assert notify.RULE_BASED_NOTICE in plan.chunks[0]
    assert notify.RULE_BASED_NOTICE not in notify.plan_message(_render_input()).text


def test_meta_line_is_clamped_even_with_absurd_model_integers() -> None:
    # JUDGE #54: change_stats 정수는 모델이 준 값이고 4단계가 타입만 본다. 메타 줄
    # 상한이 가정이 아니라 실제 clamp 임을 고정한다 (IMPL 2.1).
    huge = 10**80

    inp = _render_input(files_changed=huge, added_lines=huge, deleted_lines=huge)

    meta = notify.render_message(inp).split("\n")[1]
    assert len(meta) <= notify.META_LINE_MAX
    assert meta.endswith(notify.TRUNCATION_MARK)


# ── 케이스 11~15: 전달 판정과 계수 (FR-034, FR-035, FR-052) ───────────────────


def _plan_of(chunks: tuple[str, ...]) -> notify.MessagePlan:
    return notify.MessagePlan(
        chunks=chunks, text="\n".join(chunks), shrunk_sections=(), truncated=False
    )


def test_deliver_counts_one_request_per_chunk() -> None:
    # 케이스 11: 요청 1회 = SendFn 1회. 이 계수가 FR-035/FR-052 주장의 기준점이다.
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
        # 케이스 12·13: 4xx / 5xx / timeout / 연결 실패를 error 값으로 가른다 (FR-034).
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
    # 케이스 14: 2조각 중 1조각만 나갔으면 requests 는 1이다. 이미 나간 것을 되돌릴 수
    # 없으므로 계수는 사실대로 남긴다 (설계 3절).
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
    # 케이스 15 (FR-035/FR-052): 생략 5갈래는 전부 requests 0 이고 사유가 그대로 남는다.
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
    # 전송을 시도조차 못 한 실패는 생략이 아니다 — 사용자는 전송을 기대했다 (설계 5.6).
    outcome = notify.failed_delivery(notify.ERROR_DISCORD_URL_MISSING)

    assert outcome.requests == 0
    assert outcome.skip_reason is None
    assert outcome.error == notify.ERROR_DISCORD_URL_MISSING
    assert notify.resolve_discord_state(outcome) == notify.DISCORD_FAILED


def test_session_discord_fields_of_none_is_the_abort_shape() -> None:
    # abort 세션에도 discord 필드가 남는다 — "전송 0회"를 사후 집계로 증명하려면 전
    # 세션에 계수가 있어야 한다 (설계 5.5).
    assert notify.session_discord_fields(None) == {
        "delivered": False,
        "http_status": None,
        "requests": 0,
        "chunks": 0,
        "skip_reason": None,
    }
    assert notify.resolve_discord_state(None) == notify.DISCORD_NOT_RUN


# ── 케이스 16~20: 산출물·정리 안내·콘솔 가드·입력 환원 ────────────────────────


def test_payload_doc_carries_no_webhook_url() -> None:
    # 케이스 16: discord_payload.json 은 사람이 열어보는 수동 복사 경로다. URL 은
    # 비밀값이라 담지 않는다 (PRD 13.1).
    plan = notify.plan_message(_render_input())

    doc = notify.payload_doc(plan, generated_at="2026-08-31T20:29:14+09:00")

    assert doc["schema_version"] == "1.1"
    assert doc["generated_at"] == "2026-08-31T20:29:14+09:00"
    assert doc["chunks"] == len(plan.chunks)
    assert doc["truncated"] is False
    assert doc["shrunk_sections"] == []
    raw = json.dumps(doc, ensure_ascii=False)
    assert "webhook" not in raw.lower()
    assert "discord.com" not in raw


def test_write_payload_json_leaves_no_temp_file(tmp_path: Path) -> None:
    # 다른 산출물과 같은 원자적 교체 — 반쪽 파일이 남지 않는다 (설계 5.5).
    target = tmp_path / "session" / "discord_payload.json"
    doc = notify.payload_doc(
        notify.plan_message(_render_input()), generated_at="2026-08-31T20:29:14+09:00"
    )

    notify.write_payload_json(target, doc)

    assert json.loads(target.read_text(encoding="utf-8")) == doc
    assert [path.name for path in target.parent.glob(".discord-*")] == []


def test_cleanup_notice_lists_session_root_and_dotenv_paths() -> None:
    # 케이스 17 (FR-053): 산출물 위치 + .env 잔존 + 환경변수 이름 3항목. 환경변수는
    # 이름만 알리고 값·존재 여부를 찍지 않는다 (FR-003).
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
    # 케이스 18: 모델 문자열에는 이모지가 올 수 있다. 인코딩 불가 문자가 프로세스를
    # 죽이지 않게 출력 경계에서 한 번 무해화한다 (HANDOFF (다)).
    result = notify.console_safe("📚 오늘 요약", "cp949")

    result.encode("cp949")
    assert "오늘 요약" in result
    assert notify.console_safe("📚 x", None) == "📚 x"
    # 알 수 없는 코덱 이름이면 무해화를 포기한다 — 죽는 것보다 낫다.
    assert notify.console_safe("📚 x", "존재하지-않는-코덱") == "📚 x"


def test_format_period_falls_back_to_raw_strings() -> None:
    # 케이스 19: 파싱 불가 문자열에서 예외를 내지 않는다 (렌더링이 멈추면 안 된다).
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
    # 케이스 20: 필수 재료가 없으면 None 을 돌려 stats-only 경로로 흐른다 (설계 5.10).
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
            "changes": "리스트가 아니다",
            "learning_points": [{"topic": "주제", "explanation": "설명"}],
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
    assert inp.changes == ()
    assert inp.questions == ("질문",)


def test_render_stats_only_shows_numbers_and_reason(tmp_path: Path) -> None:
    # 판정 10번: 요약이 없는 세션에서도 실행자에게 통계와 산출물 경로가 보인다.
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
