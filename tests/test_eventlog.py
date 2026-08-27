"""eventlog 모듈 — events.jsonl 행 구성과 append (FR-041).

설계 검증 기준 22~23. 시각은 wall_time 인자로 주입한다.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from class_watcher.debounce import EventKind, LogicalEvent
from class_watcher.eventlog import append_jsonl, event_row

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
