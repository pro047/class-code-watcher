"""이벤트 로그 JSON Lines (FR-041).

행 구성(순수)과 append(부작용)를 나눠 둔다. 시각은 호출부가 주입한다.
"""

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from .debounce import EventKind, LogicalEvent


def previous_hash(
    rel_path: str,
    recorded: Mapping[str, str | None],
    baseline: Mapping[str, str],
) -> str | None:
    """FR-018 의 "직전 기록값(없으면 baseline)".

    recorded 에 키가 있으면 그 값을 쓴다 — 값이 None(삭제로 기록됨)이어도 baseline 으로
    내려가지 않는다.
    """
    if rel_path in recorded:
        return recorded[rel_path]
    return baseline.get(rel_path)


def should_record(
    kind: EventKind,
    current_sha256: str | None,
    previous_sha256: str | None,
) -> bool:
    """FR-018 의 기록 여부. 이 함수 하나가 규칙 전부다.

    - 삭제는 언제나 True — 대조할 "현재 내용"이 없다.
    - current 가 None(읽기 실패)이면 True — 확인하지 못한 것을 "같다"고 말하지 않는다.
    - 그 외에는 current != previous 일 때만 True.
    """
    if kind == "deleted":
        return True
    if current_sha256 is None:
        return True
    return current_sha256 != previous_sha256


def event_row(
    logical: LogicalEvent,
    *,
    wall_time: datetime,
    sha256: str | None,
    size: int | None,
) -> dict[str, object]:
    return {
        "timestamp": wall_time.isoformat(),
        "path": logical.rel_path,
        "event_type": logical.kind,
        "hash": sha256,
        "size": size,
        # 합쳐진 원시 이벤트 수. 로그 전용이며 요약에는 쓰지 않는다 (C-12).
        "count": logical.count,
    }


def append_jsonl(path: Path, row: Mapping[str, object]) -> None:
    line = json.dumps(dict(row), ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
