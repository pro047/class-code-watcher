"""eventlog 모듈 — events.jsonl 행 구성과 append (FR-041).

설계 검증 기준 22~23. 시각은 wall_time 인자로 주입한다.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from class_watcher.debounce import EventKind, LogicalEvent
from class_watcher.eventlog import append_jsonl, event_row, previous_hash, should_record

WALL = datetime(2026, 8, 27, 10, 0, 0, tzinfo=UTC)


def _logical(kind: EventKind = "modified", count: int = 1) -> LogicalEvent:
    return LogicalEvent(
        rel_path="src/수업.py",
        kind=kind,
        first_at=0.0,
        last_at=0.4,
        count=count,
    )


# ── 기준 22: 행에 timestamp·path·event_type·hash·size 가 있다 (FR-041) ────────


def test_event_row_has_required_fields() -> None:
    row = event_row(_logical(count=3), wall_time=WALL, sha256="ab" * 32, size=42)
    assert row["timestamp"] == WALL.isoformat()
    assert row["path"] == "src/수업.py"
    assert row["event_type"] == "modified"
    assert row["hash"] == "ab" * 32
    assert row["size"] == 42
    assert row["count"] == 3
    # 정확히 이 키들뿐이다 — 코드 원문·diff 라인이 이벤트 로그에 실리지 않는다.
    assert set(row) == {"timestamp", "path", "event_type", "hash", "size", "count"}


# ── 기준 23: deleted 행은 hash=None, size=None ───────────────────────────────


def test_deleted_row_has_null_hash_and_size() -> None:
    row = event_row(_logical(kind="deleted"), wall_time=WALL, sha256=None, size=None)
    assert row["event_type"] == "deleted"
    assert row["hash"] is None
    assert row["size"] is None


# ── append_jsonl: 한 행 = 한 줄 JSON, 한글은 이스케이프 없이 그대로 ──────────


def test_append_jsonl_appends_parseable_lines(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    append_jsonl(log, event_row(_logical(), wall_time=WALL, sha256="00", size=1))
    append_jsonl(log, event_row(_logical(kind="deleted"), wall_time=WALL, sha256=None, size=None))

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert rows[0]["event_type"] == "modified"
    assert rows[1]["hash"] is None
    assert "수업" in lines[0]  # ensure_ascii=False — 사람이 읽을 수 있는 로그


# ── FR-018(C-25): 내용이 안 바뀐 알림은 논리 이벤트로 기록하지 않는다 ──────────
#
# 설계 검증 기준 19~25. 규칙 전부가 이 두 순수 함수 안에 있어서 파일시스템 없이
# 전 분기를 검증한다 (설계 D5). 실제 배선은 test_watcher.py 가 본다.


def test_deleted_event_is_always_recorded() -> None:
    # 기준 19: 삭제는 대조할 "현재 내용"이 없다. 수용 기준의 "삭제가 아닌 이벤트는".
    assert should_record("deleted", None, "abc") is True
    assert should_record("deleted", None, None) is True


def test_same_hash_is_not_recorded() -> None:
    # 기준 20: C-25 의 atime 오탐이 여기서 죽는다.
    assert should_record("modified", "abc", "abc") is False


def test_different_hash_is_recorded() -> None:
    assert should_record("modified", "abc", "def") is True


def test_unreadable_current_hash_falls_back_to_recording() -> None:
    # 기준 22 (설계 D7): 확인하지 못한 것을 "같다"고 말하지 않는다 — 기본값은 기록이다.
    assert should_record("modified", None, "abc") is True
    assert should_record("modified", None, None) is True


def test_new_file_without_previous_hash_is_recorded() -> None:
    # 기준 23: baseline 에도 없던 신규 파일.
    assert should_record("created", "abc", None) is True
    # moved 도 삭제가 아닌 같은 분기를 탄다 (EventKind 4종 중 나머지 하나).
    assert should_record("moved", "abc", "abc") is False
    assert should_record("moved", "abc", "def") is True


def test_previous_hash_prefers_recorded_over_baseline() -> None:
    # 기준 24: 직전 기록값 → 없으면 baseline → 둘 다 없으면 None.
    assert previous_hash("a.py", {"a.py": "recorded"}, {"a.py": "base"}) == "recorded"
    assert previous_hash("a.py", {}, {"a.py": "base"}) == "base"
    assert previous_hash("a.py", {}, {}) is None


def test_previous_hash_keeps_explicit_none_from_deletion() -> None:
    # 기준 25 (설계 D6): 삭제로 None 이 기록된 경로는 baseline 으로 내려가지 않는다.
    # 안 그러면 "삭제 → 같은 내용으로 복원"이 baseline 과 같아 삼켜진다.
    assert previous_hash("a.py", {"a.py": None}, {"a.py": "base"}) is None
    assert should_record("modified", "base", None) is True
