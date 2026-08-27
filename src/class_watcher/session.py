"""세션 ID·디렉터리·`session.json`.

상태 전이는 후속 기능들이 `write_session_json` 을 재사용해서 처리한다. 원자적 교체를 한
곳에 모아 둬야 중간에 죽어도 반쯤 쓰인 JSON 이 남지 않는다 (FR-040, PRD 7절).
"""

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from .config import WatchConfig
from .selector import Selection

SCHEMA_VERSION = "1.1"
DIFF_ENGINE = "difflib"

# 시작 시점에는 아직 아무 변경도 관측하지 않았다. 후속 기능이 modified/added/skipped 로 바꾼다.
INITIAL_FILE_STATUS = "unchanged"


class SessionStatus(StrEnum):
    """FR-040 이 지정한 6종."""

    STARTING = "starting"
    WATCHING = "watching"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


def generate_session_id(now: datetime, suffix: str) -> str:
    """시각과 suffix 를 주입받는 순수 함수 (FR-002). 시계·난수는 호출부가 공급한다."""
    return f"{now:%Y%m%d-%H%M%S}-{suffix}"


@dataclass(frozen=True)
class SessionPaths:
    """PRD 9.1 세션 디렉터리 구조 중 이 단계가 확정하는 뼈대."""

    root: Path
    session_json: Path
    baseline_dir: Path
    final_dir: Path
    events_jsonl: Path
    errors_jsonl: Path
    # `--history` 일 때만 실제로 만들어진다 (PRD 9.1).
    history_dir: Path


def make_session_paths(session_dir: Path, session_id: str) -> SessionPaths:
    root = session_dir / session_id
    return SessionPaths(
        root=root,
        session_json=root / "session.json",
        baseline_dir=root / "baseline",
        final_dir=root / "final",
        events_jsonl=root / "events.jsonl",
        errors_jsonl=root / "errors.jsonl",
        history_dir=root / "history",
    )


def create_session_dirs(paths: SessionPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.baseline_dir.mkdir(parents=True, exist_ok=True)
    paths.final_dir.mkdir(parents=True, exist_ok=True)


def write_session_json(paths: SessionPaths, doc: dict[str, object]) -> None:
    """임시파일에 다 쓴 뒤 os.replace 로 갈아끼운다 — 독자가 반쪽 파일을 보는 일이 없도록."""
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=paths.root, prefix=".session-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, paths.session_json)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def transition(
    doc: dict[str, object], status: SessionStatus, **fields: object
) -> dict[str, object]:
    """순수 — 상태와 추가 필드를 얹은 새 doc 을 만든다. 쓰기는 write_session_json 이 한다."""
    updated = dict(doc)
    updated["status"] = status.value
    updated.update(fields)
    return updated


def initial_session_doc(
    config: WatchConfig,
    session_id: str,
    started_at: datetime,
    selection: Selection,
    watch_mode: str,
) -> dict[str, object]:
    """PRD 9.2 필드 중 시작 시점에 확정되는 것만 담는다. 나머지는 종료 파이프라인이 채운다."""
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "title": config.title,
        # C-11: 로컬 파일에는 해시가 아니라 실제 절대 경로를 남긴다.
        "watch_root": str(config.watch_root),
        "watched_files": [
            {"path": str(path), "status": INITIAL_FILE_STATUS} for path in selection.selected
        ],
        "started_at": started_at.isoformat(),
        "status": SessionStatus.STARTING.value,
        "watch_mode": watch_mode,
        "diff_engine": DIFF_ENGINE,
    }
