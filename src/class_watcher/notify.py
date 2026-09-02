"""Discord 메시지 렌더링·축소·분할·전달 판정 (FR-033, FR-034, FR-050~FR-053).

httpx 를 import 하지 않는다. 실제 전송은 SendFn 으로 주입받으므로 이 모듈은 네트워크
없이 전 경로가 검증된다. 디스크를 만지는 것은 맨 아래 write_payload_json 하나뿐이다.

렌더러가 알 수 있는 것은 RenderInput 이 전부다. diff·final.diff·정제본이 들어갈 자리가
타입에 아예 없고, RenderKeyword 에는 파일 경로 필드 자체가 없다 — 모델이 diff 원문이나
경로를 어느 필드에 옮겨 와도 메시지에 닿을 경로가 없다 (FR-051). 4단계가 PromptInput 으로
FR-037 을 끊은 것과 같은 수법이다.
"""

import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .config import ENV_DISCORD_WEBHOOK_URL, ENV_OPENAI_API_KEY
from .summarize import (
    CONFIDENCE_FALLBACK,
    CONFIDENCE_HIGH,
    CONFIDENCE_LEVELS,
    KEYWORD_GROUP_FALLBACK,
    KEYWORD_GROUPS,
    KEYWORD_ITEM_FIELDS,
    MAX_ARRAY_ITEMS,
    MAX_CONCEPT_CHARS,
    MAX_KEYWORDS,
    MAX_SUMMARY_CHARS,
    MAX_SYNTAX_CHARS,
    MAX_TERM_CHARS,
    SOURCE_RULE_BASED,
)

# summary.json 과 같은 축이고, stats/session/redaction 과는 여기서 갈라진다 — C-17 로
# 형태가 바뀐(주요 변경 -> 오늘의 키워드) 두 문서만 1.2 다. 옛 discord_payload.json 이
# sessions/ 에 영구히 남으므로(PRD 9.3) 버전이 두 형식을 구분하는 유일한 표식이다.
NOTIFY_SCHEMA_VERSION = "1.2"

# ── 추정 상수 — 저장소 안에 정본이 없다. 실전송 1회가 확정한다 ──────────────────
# `추정`: Discord Webhook 본문 길이 상한.
DISCORD_CONTENT_LIMIT = 2000
# 한 세션이 만드는 webhook 요청 수 상한. 수신자에게 알림을 3개 이상 띄우지 않는다 (FR-052).
MAX_CHUNKS = 2

# ── 로컬 clamp — summarize.py 의 규율 연장 ────────────────────────────────────
MAX_LINE_CHARS = 300
# 질문·확인할 점 한 줄의 상한. MAX_LINE_CHARS 보다 작다 — 이것이 없으면 모든 필드가
# 상한인 입력에서 축소가 발동해 키워드가 2건으로 붕괴한다(아래 FULL_MESSAGE_MAX_CHARS).
# MAX_TITLE_CHARS 와 같은 이유의 로컬 clamp 다. 개념 한 문장이 90자이므로 복습 질문
# 한 문장에 120 은 넉넉하고, 예산에는 여유를 남긴다.
MAX_ITEM_CHARS = 120
# FR-050 이 첫 화면에 요구하는 키워드 건수 = 축소 하한.
FIRST_SCREEN_KEYWORDS = 2
MAX_ITEMS_SHOWN = MAX_ARRAY_ITEMS
# 배열 상한이 갈렸다 (PRD 11.3, C-18) — 키워드만 15 다.
MAX_KEYWORDS_SHOWN = MAX_KEYWORDS

# 첫 화면 전용 상한 — MAX_LINE_CHARS 보다 작다. 이것이 없으면 최악 입력에서 FR-050(첫
# 화면 4요소)과 FR-033(조각 <= limit)이 동시에 성립하지 않는다: session_title 은 4단계가
# 길이를 clamp 하지 않는다. keywords[] 쪽 상한은 4단계가 들고 있어 import 해서 쓴다.
MAX_TITLE_CHARS = 80

# PRD 11.4 예시의 U+1F4DA·U+2022·U+2014·U+2026 은 전부 cp949 불가라 콘솔 리다이렉트에서
# 프로세스를 죽인다. 같은 문자열이 콘솔로도 나가므로 처음부터 안전 문자만 쓴다.
TITLE_PREFIX = "[수업] "
# `- ` 를 쓰면 줄이 `-` 로 시작해 FR-051 검사에 걸린다.
BULLET = "· "
TRUNCATION_MARK = "..."
OVERFLOW_NOTICE = "(이하 생략 - 전체 내용은 세션 폴더의 summary.json 에 있습니다)"
RULE_BASED_NOTICE = "[규칙 기반 요약 - 모델 요약이 아닙니다]"
# PRD 11.4 「절단 표시」. RULE_BASED_NOTICE 와 같은 자리·같은 방식이다 — 둘 다 "이 요약의
# 근거가 온전하지 않다"는 같은 종류의 사실이다. 수신자는 코드를 안 보는 사람이라
# "프롬프트 예산" 같은 도구 내부 용어를 쓰지 않는다. `[` 로 시작하므로 FR-051 검사에
# 걸리지 않고, cp949 안전 문자만 쓴다(같은 문자열이 콘솔로도 나간다).
TRUNCATED_NOTICE = "[근거 일부 누락 - 코드가 많아 일부만 요약했습니다]"

HEADER_SUMMARY = "요약"
HEADER_KEYWORDS = "오늘의 키워드"
HEADER_QUESTIONS = "복습할 질문"
HEADER_RISKS = "확인할 점"

# PRD 11.4 의 `[객체생성]` 머리줄과 `· {term}  {syntax}` / 두 칸 들여쓴 설명 줄.
GROUP_OPEN = "["
GROUP_CLOSE = "]"
SYNTAX_GAP = "  "
CONCEPT_INDENT = "  "

# "{기간} · N개 파일 변경 · +N / -N" 의 상한. 모델이 준 정수라 자릿수 보장이 없어
# render_message 가 이 값으로 실제로 자른다 — 안 자르면 아래 산수가 가정에 불과해진다.
META_LINE_MAX = 60
# "(1/2) "
CHUNK_MARK_MAX = 6


def confidence_mark(confidence: str) -> str:
    """high 면 표시 없음, 아니면 " (medium)" 꼴 (PRD 11.4).

    목록 밖 값을 CONFIDENCE_FALLBACK 으로 흡수한다. 모델 문자열을 그대로 줄에 실으면
    개행 하나로 FR-051 방어선이 뚫리고 아래 MAX_CONFIDENCE_MARK 예산도 무의미해진다.

    상수보다 위가 아니라 상수 블록 한가운데에 있는 이유: MAX_CONFIDENCE_MARK 를 이
    함수에서 파생시켜 표기와 예산이 갈라질 수 없게 했다.
    """
    level = confidence if confidence in CONFIDENCE_LEVELS else CONFIDENCE_FALLBACK
    return "" if level == CONFIDENCE_HIGH else f" ({level})"


MAX_GROUP_LABEL = max(len(name) for name in KEYWORD_GROUPS) + len(GROUP_OPEN) + len(GROUP_CLOSE)
MAX_CONFIDENCE_MARK = max(len(confidence_mark(level)) for level in CONFIDENCE_LEVELS)

# 키워드 한 건이 첫 화면에서 먹는 최악 문자수. 최악은 2건이 서로 다른 분류에 들어가
# 그룹 머리줄을 각각 한 줄씩 쓰는 경우라, 그룹 머리줄을 건당으로 센다.
KEYWORD_BLOCK_MAX = (
    1  # 분류 사이 빈 줄
    + MAX_GROUP_LABEL + 1  # "[객체생성]"
    + len(BULLET) + MAX_TERM_CHARS + MAX_CONFIDENCE_MARK
    + len(SYNTAX_GAP) + MAX_SYNTAX_CHARS + 1  # 키워드 줄
    + len(CONCEPT_INDENT) + MAX_CONCEPT_CHARS + 1  # 설명 줄
)

# 첫 화면 예산 (FR-050 x FR-033). 숫자를 흩뿌리지 않는다 — 상한을 바꾸면 이 식이 따라
# 움직인다. DISCORD_CONTENT_LIMIT 이 `추정`이라 틀릴 수 있으므로 산수도 상수로 둔다.
# 값 자체를 단언하지 말고 <= DISCORD_CONTENT_LIMIT 관계를 단언해라 — 문구를 한 글자만
# 고쳐도 숫자는 움직이고 관계는 남는다. 표시 두 줄이 동시에 참이어도 계상돼 있다.
FIRST_SCREEN_MAX_CHARS = (
    len(TITLE_PREFIX) + MAX_TITLE_CHARS + 1  # 제목 줄
    + META_LINE_MAX + 1  # 메타 줄
    + len(RULE_BASED_NOTICE) + 1  # FR-039 표시
    + len(TRUNCATED_NOTICE) + 1  # PRD 11.4 절단 표시
    + 1  # 빈 줄
    + len(HEADER_SUMMARY) + 1
    + MAX_SUMMARY_CHARS + 1
    + 1  # 빈 줄
    + len(HEADER_KEYWORDS) + 1
    + FIRST_SCREEN_KEYWORDS * KEYWORD_BLOCK_MAX
    + CHUNK_MARK_MAX
)

# 축소되지 않는 머리 부분 (제목·메타·표시 2줄·요약·키워드 머리).
NON_SHRINKABLE_MAX_CHARS = (
    FIRST_SCREEN_MAX_CHARS - FIRST_SCREEN_KEYWORDS * KEYWORD_BLOCK_MAX - CHUNK_MARK_MAX
)

# shrink 가 다 깎은 뒤에도 남는 바닥. risks 는 통째로 빠지고 질문은 1건까지만 줄어든다.
QUESTION_FLOOR_MAX_CHARS = 1 + len(HEADER_QUESTIONS) + 1 + len(BULLET) + MAX_ITEM_CHARS + 1

# PRD 11.3 이 요구한 관계식의 좌변 — 키워드 상한이 붕괴하지 않음의 증명 대상이다.
# 값이 아니라 이 식과 (DISCORD_CONTENT_LIMIT - CHUNK_MARK_MAX) * MAX_CHUNKS 의 관계를
# 단언해라.
FULL_MESSAGE_MAX_CHARS = (
    NON_SHRINKABLE_MAX_CHARS
    + MAX_KEYWORDS_SHOWN * KEYWORD_BLOCK_MAX
    + QUESTION_FLOOR_MAX_CHARS
)

# WatchOutcome.discord_state — cli 의 콘솔·종료 코드 매핑 기준.
DISCORD_SENT = "sent"
DISCORD_FAILED = "failed"
# 정책상 생략 — 전송 0회가 정상인 경우다.
DISCORD_SKIPPED = "skipped"
# 전송 판정 지점에 도달하지 못했다 (abort 등).
DISCORD_NOT_RUN = "not_run"

# DeliveryOutcome.skip_reason — "전송 0회"의 원인을 구분한다 (FR-035, FR-052).
SKIP_NO_CHANGE = "no_change"
SKIP_NO_DISCORD = "no_discord_option"
SKIP_DRY_RUN = "dry_run"
SKIP_SECRETS_BLOCKED = "secrets_blocked"
SKIP_NO_SUMMARY = "no_summary"

# DeliveryOutcome.error → session.json 의 error 값.
ERROR_DISCORD_TIMEOUT = "discord_timeout"
ERROR_DISCORD_CONNECTION = "discord_connection"
ERROR_DISCORD_URL_MISSING = "discord_url_missing"
# 상태 코드를 못 얻은 경우만. 그 외 HTTP 실패는 f"discord_http_{status}" 다.
ERROR_DISCORD_HTTP = "discord_http_error"
# payload 를 못 남긴 채로 전송하지 않는다 — FR-034 의 로컬 보존이 성립하지 않는 상태다.
ERROR_DISCORD_PAYLOAD_FAILED = "discord_payload_write_failed"
# 전송 판정 지점까지 갔는데 sender 를 부르지도, 생략 사유를 정하지도 못한 상태.
# 5갈래 skip 은 전부 resolve_session_end 앞쪽 분기가 먼저 잡으므로 정상 경로에는 없다.
ERROR_DISCORD_NOT_ATTEMPTED = "discord_not_attempted"

# DiscordRequestError.kind — 전송 계층 실패의 종류.
KIND_TIMEOUT = "timeout"
KIND_CONNECTION = "connection"
KIND_HTTP = "http"

_DIFF_LINE_PREFIXES = ("+", "-")


class DiscordRequestError(Exception):
    """전송 계층 실패.

    httpx 예외 메시지를 옮기지 않는다 — 메시지에 요청 URL(= 비밀값)이 들어 있어 콘솔·
    errors.jsonl 로 새는 통로가 된다 (FR-003, FR-042).
    """

    def __init__(self, kind: str, *, http_status: int | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.http_status = http_status


# 요청 1회 = 이 함수 1회. 테스트의 mock 경계이자 FR-035/FR-052 계수의 기준점이다.
SendFn = Callable[[Mapping[str, object]], int]


@dataclass(frozen=True)
class RenderKeyword:
    """PRD 11.4 "오늘의 키워드" 한 항목의 재료.

    file·area·evidence 를 필드로 두지 않는 것이 FR-051 의 구조적 방어선이다.
    옛 RenderChange 는 file 을 들고 있었지만 keywords[] 에는 경로 개념 자체가 없다.
    """

    term: str
    syntax: str
    concept: str
    group: str
    confidence: str


@dataclass(frozen=True)
class RenderInput:
    """렌더러가 알 수 있는 것의 전부."""

    title: str
    # ISO 문자열. 파싱 실패는 원문 표시로 흡수한다.
    started_at: str
    ended_at: str
    files_changed: int
    added_lines: int
    deleted_lines: int
    summary: str
    keywords: tuple[RenderKeyword, ...]
    questions: tuple[str, ...]
    risks: tuple[str, ...]
    # True 면 FR-039 "LLM 요약이 아님" 표시를 첫 화면에 박는다.
    rule_based: bool
    # 프롬프트 예산 초과로 diff 가 잘린 세션. summary.json 의 input.truncated 에서 온다.
    # MessagePlan.truncated(조각 하드 절단)와 다른 것이다 — 이름이 같아 착각하기 쉽다.
    truncated: bool = False


@dataclass(frozen=True)
class MessagePlan:
    # 실제로 전송될 조각들 (번호 표시까지 끝난 최종 문자열).
    chunks: tuple[str, ...]
    # 콘솔용 전체본 (분할 전, 축소 후).
    text: str
    # 어떤 섹션을 줄였는지 (FR-033 증거).
    shrunk_sections: tuple[str, ...]
    # MAX_CHUNKS 를 넘겨 하드 절단했는가.
    truncated: bool


@dataclass(frozen=True)
class DeliveryOutcome:
    delivered: bool
    # 실제로 나간 webhook 요청 수 (FR-035/FR-052 계수).
    requests: int
    chunks: int
    # 마지막 응답 또는 실패 응답의 상태 코드 (FR-034).
    http_status: int | None
    error: str | None
    skip_reason: str | None


# ── 렌더링 ────────────────────────────────────────────────────────────────────


def _fold(text: str) -> str:
    """개행을 공백으로 접고 앞쪽의 공백·`+`·`-` 를 없앤다.

    모델 문자열 하나가 렌더 결과의 정확히 한 줄이 되고, 그 줄이 diff 라인처럼 시작할 수
    없게 만드는 곳이다 (FR-051 2단계 방어선).
    """
    folded = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return folded.lstrip(" \t+-").strip()


def sanitize_line(text: str, limit: int = MAX_LINE_CHARS) -> str:
    cleaned = _fold(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - len(TRUNCATION_MARK))] + TRUNCATION_MARK


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_period(started_at: str, ended_at: str) -> str:
    """수업 시간 표기. 파싱 불가는 원문 표기로 떨어진다 — 여기서 예외를 내지 않는다."""
    start = _parse_iso(started_at)
    end = _parse_iso(ended_at)
    if start is None or end is None:
        return f"{started_at} - {ended_at}".strip()
    if start.date() == end.date():
        return f"{start:%Y-%m-%d %H:%M}-{end:%H:%M}"
    return f"{start:%Y-%m-%d %H:%M} - {end:%Y-%m-%d %H:%M}"


def _meta_line(
    started_at: str, ended_at: str, files_changed: int, added: int, deleted: int
) -> str:
    """메타 줄은 모델이 준 정수로 조립되므로 자릿수 보장이 없다. META_LINE_MAX 로 자른다."""
    period = format_period(started_at, ended_at)
    return sanitize_line(
        f"{period} · {files_changed}개 파일 변경 · +{added} / -{deleted}",
        limit=META_LINE_MAX,
    )


def _as_int(value: object) -> int:
    # bool 은 int 의 서브클래스라 그냥 두면 True 가 1로 실린다.
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _render_keywords(block: object) -> tuple[RenderKeyword, ...]:
    """summary.json 의 keywords[] → RenderKeyword.

    한 항목이 망가져도 continue 로 넘긴다 — 옛 형식 doc 이나 손으로 고친 doc 에서
    나머지 키워드까지 통째로 잃지 않게 한다. 여기서 읽는 키는 다섯 개뿐이라
    evidence·file 같은 키가 섞여 있어도 렌더에 닿을 자리가 없다 (FR-051).
    """
    if not isinstance(block, list):
        return ()
    items: list[RenderKeyword] = []
    for entry in block[:MAX_KEYWORDS_SHOWN]:
        if not isinstance(entry, dict):
            continue
        fields = {key: entry.get(key) for key in KEYWORD_ITEM_FIELDS}
        if any(not isinstance(value, str) for value in fields.values()):
            continue
        items.append(
            RenderKeyword(
                term=str(fields["term"]),
                syntax=str(fields["syntax"]),
                concept=str(fields["concept"]),
                group=str(fields["group"]),
                confidence=str(fields["confidence"]),
            )
        )
    return tuple(items)


def group_keywords(
    keywords: Sequence[RenderKeyword],
) -> tuple[tuple[str, tuple[RenderKeyword, ...]], ...]:
    """분류별 묶음. KEYWORD_GROUPS 순서 고정, 빈 분류는 빠진다 (PRD 11.4).

    KEYWORD_GROUPS 밖의 group 은 KEYWORD_GROUP_FALLBACK 버킷으로 흡수한다 — 렌더가
    키워드를 조용히 떨어뜨리는 경로를 만들지 않는다. 전송은 성공하고 메시지만 비는
    실패는 게이트가 못 잡기 때문이다 (validate_summary 의 강등에 이은 2차 그물).
    """
    buckets: dict[str, list[RenderKeyword]] = {name: [] for name in KEYWORD_GROUPS}
    for keyword in keywords:
        name = keyword.group if keyword.group in buckets else KEYWORD_GROUP_FALLBACK
        buckets[name].append(keyword)
    return tuple((name, tuple(buckets[name])) for name in KEYWORD_GROUPS if buckets[name])


def _render_strings(block: object) -> tuple[str, ...]:
    if not isinstance(block, list):
        return ()
    return tuple(entry for entry in block[:MAX_ITEMS_SHOWN] if isinstance(entry, str))


def _input_truncated(block: object) -> bool:
    """summary.json 의 input.truncated. 블록이 없는 옛 형식 doc 은 False 로 흐른다."""
    return bool(block.get("truncated")) if isinstance(block, dict) else False


def build_render_input(
    summary_doc: Mapping[str, object],
    *,
    title_fallback: str,
    started_at: str,
    ended_at: str,
) -> RenderInput | None:
    """summary.json 전체 doc → RenderInput. 필수 재료가 없으면 None (stats-only 로 흐른다).

    `summary` 블록에서 읽는 것은 RenderInput 에 자리가 있는 필드뿐이다 (FR-051).
    keywords 가 없는 옛 형식 doc 은 None 이 아니라 keywords=() 로 흐른다 — 요약과
    통계는 여전히 보낼 수 있다.
    """
    block = summary_doc.get("summary")
    if not isinstance(block, dict):
        return None
    summary = block.get("summary")
    if not isinstance(summary, str):
        return None
    title = block.get("session_title")
    stats = block.get("change_stats")
    stats_map: Mapping[str, object] = stats if isinstance(stats, dict) else {}
    return RenderInput(
        title=title if isinstance(title, str) and title.strip() else title_fallback,
        started_at=started_at,
        ended_at=ended_at,
        files_changed=_as_int(stats_map.get("files_changed")),
        added_lines=_as_int(stats_map.get("added_lines")),
        deleted_lines=_as_int(stats_map.get("deleted_lines")),
        summary=summary,
        keywords=_render_keywords(block.get("keywords")),
        questions=_render_strings(block.get("questions_to_review")),
        risks=_render_strings(block.get("risks_or_todos")),
        rule_based=summary_doc.get("source") == SOURCE_RULE_BASED,
        truncated=_input_truncated(summary_doc.get("input")),
    )


def _keyword_lines(keyword: RenderKeyword) -> list[str]:
    """`· {term}  {syntax}` 와 두 칸 들여쓴 설명 줄 (PRD 11.4).

    syntax 에도 sanitize_line 을 균일하게 건다. `--i` 처럼 +/- 로 시작하는 표기는 앞
    기호를 잃지만, 예외를 두면 FR-051 방어선이 "렌더가 syntax 를 줄 맨 앞에 두지
    않는다"는 배치 순서에 의존하게 된다. P0 를 배치에 걸지 않는다.
    """
    term = sanitize_line(keyword.term, limit=MAX_TERM_CHARS) + confidence_mark(keyword.confidence)
    syntax = sanitize_line(keyword.syntax, limit=MAX_SYNTAX_CHARS)
    head = f"{BULLET}{term}{SYNTAX_GAP}{syntax}" if syntax else f"{BULLET}{term}"
    return [head, CONCEPT_INDENT + sanitize_line(keyword.concept, limit=MAX_CONCEPT_CHARS)]


def render_message(inp: RenderInput) -> str:
    """섹션 순서가 곧 FR-050 의 우선순위다. 첫 화면은 제목·메타·요약·키워드 2건이다."""
    lines: list[str] = [
        TITLE_PREFIX + sanitize_line(inp.title, limit=MAX_TITLE_CHARS),
        _meta_line(
            inp.started_at, inp.ended_at, inp.files_changed, inp.added_lines, inp.deleted_lines
        ),
    ]
    # 둘 다 참이면 규칙 기반 표시가 먼저다 — "요약을 누가 만들었나"가 "근거가 온전한가"
    # 보다 상위 사실이다 (PRD 11.4: 두 표시는 메타 줄 아래 같은 자리).
    if inp.rule_based:
        lines.append(RULE_BASED_NOTICE)
    if inp.truncated:
        lines.append(TRUNCATED_NOTICE)
    lines.extend(["", HEADER_SUMMARY, sanitize_line(inp.summary, limit=MAX_SUMMARY_CHARS)])

    if inp.keywords:
        lines.extend(["", HEADER_KEYWORDS])
        for group, items in group_keywords(inp.keywords):
            lines.extend(["", f"{GROUP_OPEN}{group}{GROUP_CLOSE}"])
            for keyword in items:
                lines.extend(_keyword_lines(keyword))
    if inp.questions:
        lines.extend(["", HEADER_QUESTIONS])
        lines.extend(
            f"{BULLET}{sanitize_line(question, limit=MAX_ITEM_CHARS)}"
            for question in inp.questions
        )
    if inp.risks:
        lines.extend(["", HEADER_RISKS])
        lines.extend(
            f"{BULLET}{sanitize_line(risk, limit=MAX_ITEM_CHARS)}" for risk in inp.risks
        )
    return "\n".join(lines)


def render_stats_only(
    *,
    title: str,
    started_at: str,
    ended_at: str,
    files_changed: int,
    added_lines: int,
    deleted_lines: int,
    session_root: str,
    reason: str,
) -> str:
    """요약이 없는 세션에서도 실행자에게 뭐라도 보이게 한다. 전송은 하지 않는다 (FR-052)."""
    return "\n".join(
        [
            f"[요약 없음] {sanitize_line(title, limit=MAX_TITLE_CHARS)}",
            _meta_line(started_at, ended_at, files_changed, added_lines, deleted_lines),
            f"요약을 만들지 못해 통계만 표시합니다 (사유: {sanitize_line(reason)}).",
            f"산출물: {session_root}",
        ]
    )


# ── 축소·분할 (FR-033) ────────────────────────────────────────────────────────


def _shrink_risks(inp: RenderInput) -> RenderInput:
    return replace(inp, risks=())


def _shrink_questions(inp: RenderInput) -> RenderInput:
    return replace(inp, questions=inp.questions[:1])


def _shrink_keywords(inp: RenderInput) -> RenderInput:
    """모델이 준 배열 순서로 앞 2건을 남긴다.

    분류별 묶음은 이 절단 뒤에 일어나므로 상위 2건이 서로 다른 분류여도 둘 다 렌더된다.
    """
    return replace(inp, keywords=inp.keywords[:FIRST_SCREEN_KEYWORDS])


# 축소 하한은 여기까지다. 제목·메타·summary·키워드 2건은 어떤 경우에도 줄이지 않는다
# — FR-050 의 수용 기준이 그 넷을 첫 화면에 요구한다. risks 가 먼저 빠지는 순서는
# PRD 11.4 「축소 시 가장 먼저 빠진다」 그대로다.
_SHRINK_STEPS: tuple[tuple[str, Callable[[RenderInput], RenderInput]], ...] = (
    ("risks", _shrink_risks),
    ("questions", _shrink_questions),
    ("keywords", _shrink_keywords),
)


def shrink(
    inp: RenderInput, *, limit: int, max_chunks: int
) -> tuple[RenderInput, tuple[str, ...]]:
    """항목 수를 줄여 축소한다. 실제로 줄인 단계만 이름을 남긴다."""
    # split_text 는 2조각이 되는 순간 각 조각에서 조각 번호 자리를 빼고 자르므로 실제
    # 수용량은 limit 보다 그만큼 작다. 키워드가 15건까지 늘면서 최악값이 천장에 붙어
    # 이 차이에 닿는다.
    budget = (limit - CHUNK_MARK_MAX) * max_chunks
    current = inp
    shrunk: list[str] = []
    for name, reducer in _SHRINK_STEPS:
        if len(render_message(current)) <= budget:
            break
        reduced = reducer(current)
        if reduced != current:
            current = reduced
            shrunk.append(name)
    return current, tuple(shrunk)


def _hard_wrap(line: str, budget: int) -> list[str]:
    """한 줄이 통째로 한도를 넘을 때만 문자 단위로 자른다 (정상 경로에서는 안 탄다)."""
    if budget <= 1 or len(line) <= budget:
        return [line]
    pieces = [line[:budget]]
    rest = line[budget:]
    while rest:
        head, rest = rest[: budget - 1], rest[budget - 1 :]
        # 잘린 조각이 +/- 로 시작하면 diff 라인처럼 보인다 (FR-051).
        pieces.append(f" {head}" if head[:1] in _DIFF_LINE_PREFIXES else head)
    return pieces


def _split_lines(text: str, budget: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for raw_line in text.split("\n"):
        for line in _hard_wrap(raw_line, budget):
            extra = len(line) + (1 if current else 0)
            if current and size + extra > budget:
                chunks.append("\n".join(current))
                current, size = [line], len(line)
            else:
                current.append(line)
                size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def split_text(text: str, limit: int) -> tuple[str, ...]:
    """줄 경계에서만 자른다. 2조각 이상이면 조각 번호 표시 자리를 미리 비워 둔다."""
    chunks = _split_lines(text, limit)
    if len(chunks) == 1:
        return tuple(chunks)
    return tuple(_split_lines(text, limit - CHUNK_MARK_MAX))


def _append_overflow(chunk: str, budget: int) -> str:
    """하드 절단한 마지막 조각. 안내가 들어갈 자리를 만들 때까지 뒤에서 줄을 뺀다."""
    lines = chunk.split("\n")
    while lines and len("\n".join(lines)) + 1 + len(OVERFLOW_NOTICE) > budget:
        lines.pop()
    return "\n".join([*lines, OVERFLOW_NOTICE])


def _number_chunks(chunks: Sequence[str]) -> tuple[str, ...]:
    if len(chunks) <= 1:
        return tuple(chunks)
    total = len(chunks)
    return tuple(f"({index}/{total}) {chunk}" for index, chunk in enumerate(chunks, start=1))


def plan_message(
    inp: RenderInput, *, limit: int = DISCORD_CONTENT_LIMIT, max_chunks: int = MAX_CHUNKS
) -> MessagePlan:
    """축소 → 분할 → 조각 번호 부여. 콘솔에는 분할 전 전체본(text)이 나간다."""
    reduced, shrunk = shrink(inp, limit=limit, max_chunks=max_chunks)
    text = render_message(reduced)
    raw = split_text(text, limit)
    truncated = len(raw) > max_chunks
    kept = list(raw[:max_chunks])
    if truncated and kept:
        kept[-1] = _append_overflow(kept[-1], limit - CHUNK_MARK_MAX)
    return MessagePlan(
        chunks=_number_chunks(kept), text=text, shrunk_sections=shrunk, truncated=truncated
    )


# ── FR-051 검사 ───────────────────────────────────────────────────────────────


def find_diff_lines(text: str) -> tuple[int, ...]:
    """`+`/`-` 로 시작하는 줄의 1-기준 줄 번호. 정상 경로에서는 항상 빈 튜플이다."""
    return tuple(
        index
        for index, line in enumerate(text.split("\n"), start=1)
        if line.startswith(_DIFF_LINE_PREFIXES)
    )


# ── payload / 전달 ────────────────────────────────────────────────────────────


def webhook_payload(chunk: str) -> dict[str, object]:
    return {"content": chunk}


def discord_error_code(exc: DiscordRequestError) -> str:
    """DiscordRequestError → session.json 의 error 값 (PRD 12절 표의 행 구분)."""
    if exc.kind == KIND_TIMEOUT:
        return ERROR_DISCORD_TIMEOUT
    if exc.kind == KIND_CONNECTION:
        return ERROR_DISCORD_CONNECTION
    if exc.http_status is not None:
        return f"discord_http_{exc.http_status}"
    return ERROR_DISCORD_HTTP


def deliver(plan: MessagePlan, send: SendFn) -> DeliveryOutcome:
    """조각당 요청 1회. 중간에 실패하면 거기서 멈추고 나간 만큼만 계수에 남긴다."""
    requests = 0
    status: int | None = None
    for chunk in plan.chunks:
        try:
            status = send(webhook_payload(chunk))
        except DiscordRequestError as exc:
            return DeliveryOutcome(
                delivered=False,
                requests=requests,
                chunks=len(plan.chunks),
                http_status=exc.http_status,
                error=discord_error_code(exc),
                skip_reason=None,
            )
        requests += 1
    return DeliveryOutcome(
        delivered=True,
        requests=requests,
        chunks=len(plan.chunks),
        http_status=status,
        error=None,
        skip_reason=None,
    )


def skipped_delivery(reason: str) -> DeliveryOutcome:
    """정책상 생략 — 전송 0회가 정상인 경우다 (FR-035, FR-052)."""
    return DeliveryOutcome(
        delivered=False,
        requests=0,
        chunks=0,
        http_status=None,
        error=None,
        skip_reason=reason,
    )


def failed_delivery(error: str) -> DeliveryOutcome:
    """전송을 시도조차 못 한 실패. 사용자는 전송을 기대했으므로 생략이 아니다."""
    return DeliveryOutcome(
        delivered=False,
        requests=0,
        chunks=0,
        http_status=None,
        error=error,
        skip_reason=None,
    )


def resolve_discord_state(outcome: DeliveryOutcome | None) -> str:
    """DeliveryOutcome → WatchOutcome.discord_state (cli 종료 코드 매핑 기준)."""
    if outcome is None:
        return DISCORD_NOT_RUN
    if outcome.delivered:
        return DISCORD_SENT
    if outcome.error is not None:
        return DISCORD_FAILED
    if outcome.skip_reason is not None:
        return DISCORD_SKIPPED
    return DISCORD_NOT_RUN


# ── 산출물 / 세션 필드 ────────────────────────────────────────────────────────


def payload_doc(plan: MessagePlan, *, generated_at: str) -> dict[str, object]:
    """discord_payload.json 본문 (PRD 9.1).

    Webhook URL 을 담지 않는다 — 이 파일은 사람이 열어보는 수동 복사 경로다 (PRD 13.1).
    """
    return {
        "schema_version": NOTIFY_SCHEMA_VERSION,
        "generated_at": generated_at,
        "chunks": len(plan.chunks),
        "truncated": plan.truncated,
        "shrunk_sections": list(plan.shrunk_sections),
        "payloads": [webhook_payload(chunk) for chunk in plan.chunks],
    }


def write_payload_json(path: Path, doc: Mapping[str, object]) -> None:
    """diffgen·redact·summarize 와 같은 원자적 교체 — 반쪽 산출물이 남지 않는다."""
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".discord-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def session_discord_fields(outcome: DeliveryOutcome | None) -> dict[str, object]:
    """session.json 의 discord 필드 (PRD 9.2 확장).

    전 세션에 남긴다 — "전송 0회"를 사후 집계로 증명할 수 있어야 FR-035·FR-052 준수를
    주장할 수 있다 (4단계 openai.calls: 0 과 같은 논리).
    """
    if outcome is None:
        return {
            "delivered": False,
            "http_status": None,
            "requests": 0,
            "chunks": 0,
            "skip_reason": None,
        }
    return {
        "delivered": outcome.delivered,
        "http_status": outcome.http_status,
        "requests": outcome.requests,
        "chunks": outcome.chunks,
        "skip_reason": outcome.skip_reason,
    }


# ── FR-053 ────────────────────────────────────────────────────────────────────


def cleanup_notice(session_root: str, existing_dotenv: Sequence[str]) -> tuple[str, ...]:
    """공용 PC 정리 안내. 환경변수는 이름만 알리고 값·존재 여부를 찍지 않는다 (FR-003)."""
    dotenv = ", ".join(existing_dotenv) if existing_dotenv else "없음"
    return (
        "[정리 안내] 공용 PC 사용 중이라면 종료 전 확인하세요:",
        f"  {BULLET}세션 산출물: {session_root} - 보관 여부 확인",
        f"  {BULLET}.env 파일 잔존: {dotenv}",
        f"  {BULLET}환경변수 {ENV_OPENAI_API_KEY} / {ENV_DISCORD_WEBHOOK_URL} 잔존 여부",
    )


# ── 콘솔 인코딩 가드 ──────────────────────────────────────────────────────────


def console_safe(text: str, encoding: str | None) -> str:
    """두 번째 방어선. 첫 번째는 애초에 cp949 안전 문자만 쓰는 것이다 (상수 정의 참조).

    모델이 준 문자열에는 이모지가 올 수 있고, 한국어 Windows 에서 stdout 이 파일로
    리다이렉트되면 cp949 인코딩 불가 문자가 프로세스를 죽인다.
    """
    if encoding is None:
        return text
    try:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except LookupError:
        # 알 수 없는 코덱 이름. 무해화를 못 하는 것이 죽는 것보다 낫다.
        return text
