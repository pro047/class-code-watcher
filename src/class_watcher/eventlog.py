"""이벤트 로그 JSON Lines (FR-041).

행 구성(순수)과 append(부작용)를 나눠 둔다. 시각은 호출부가 주입한다.
"""

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from .debounce import LogicalEvent


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
