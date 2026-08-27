"""session 모듈 — 세션 ID·상태 enum·원자적 쓰기·초기 문서 (FR-002, FR-040)."""

import json
from datetime import datetime
from pathlib import Path, PurePosixPath

from class_watcher.config import WatchConfig
from class_watcher.selector import Selection
from class_watcher.session import (
    SessionStatus,
    create_session_dirs,
    generate_session_id,
    initial_session_doc,
    make_session_paths,
    write_session_json,
)

NOW = datetime(2026, 8, 26, 18, 30, 0)


def _config(watch_root: Path, session_dir: Path) -> WatchConfig:
    return WatchConfig(
        watch_root=watch_root,
        title="수업 18:30",
        include=("*.py",),
        exclude=(),
        max_files=200,
        debounce_ms=750,
        polling=False,
        history=False,
        session_dir=session_dir,
        dry_run=False,
        no_discord=False,
        allow_secrets=False,
    )


# ── FR-002: 세션 ID ──────────────────────────────────────────────────────────


def test_generate_session_id_format() -> None:
    assert generate_session_id(NOW, "a1b2") == "20260826-183000-a1b2"


def test_generate_session_id_differs_by_suffix() -> None:
    # 같은 시각이라도 suffix 가 다르면 ID 가 다르다 — 충돌 회피 메커니즘.
    assert generate_session_id(NOW, "a1b2") != generate_session_id(NOW, "c3d4")


# ── FR-040: 상태 enum 6종 ────────────────────────────────────────────────────


def test_session_status_values_match_prd() -> None:
    assert {status.value for status in SessionStatus} == {
        "starting",
        "watching",
        "finalizing",
        "completed",
        "partial",
        "failed",
    }


# ── 9.1: 세션 디렉터리 뼈대 ──────────────────────────────────────────────────


def test_make_session_paths_layout(tmp_path: Path) -> None:
    paths = make_session_paths(tmp_path / "sessions", "20260826-183000-a1b2")
    root = tmp_path / "sessions" / "20260826-183000-a1b2"
    assert paths.root == root
    assert paths.session_json == root / "session.json"
    assert paths.baseline_dir == root / "baseline"
    assert paths.final_dir == root / "final"
    assert paths.events_jsonl == root / "events.jsonl"
    assert paths.errors_jsonl == root / "errors.jsonl"


def test_create_session_dirs_makes_skeleton_only(tmp_path: Path) -> None:
    paths = make_session_paths(tmp_path / "sessions", "20260826-183000-a1b2")
    create_session_dirs(paths)
    assert paths.root.is_dir()
    assert paths.baseline_dir.is_dir()
    assert paths.final_dir.is_dir()
    # jsonl 파일은 경로만 정의되고 이 단계에서 생성되지 않는다 (확정된 동작 계약).
    assert not paths.events_jsonl.exists()
    assert not paths.errors_jsonl.exists()


# ── FR-040: write_session_json 원자적 쓰기 ───────────────────────────────────


def test_write_session_json_leaves_no_tmp_file(tmp_path: Path) -> None:
    paths = make_session_paths(tmp_path / "sessions", "20260826-183000-a1b2")
    create_session_dirs(paths)
    write_session_json(paths, {"status": "starting"})
    assert json.loads(paths.session_json.read_text(encoding="utf-8")) == {"status": "starting"}
    assert list(paths.root.glob(".session-*.tmp")) == []


def test_write_session_json_replaces_previous_content(tmp_path: Path) -> None:
    paths = make_session_paths(tmp_path / "sessions", "20260826-183000-a1b2")
    create_session_dirs(paths)
    write_session_json(paths, {"status": "starting", "old": True})
    write_session_json(paths, {"status": "failed"})
    doc = json.loads(paths.session_json.read_text(encoding="utf-8"))
    assert doc == {"status": "failed"}
    assert list(paths.root.glob(".session-*.tmp")) == []


def test_write_session_json_keeps_korean_readable(tmp_path: Path) -> None:
    # ensure_ascii=False — 사람이 열어 볼 로컬 산출물이라 한글이 이스케이프되면 안 된다.
    paths = make_session_paths(tmp_path / "sessions", "20260826-183000-a1b2")
    create_session_dirs(paths)
    write_session_json(paths, {"title": "자바 수업 3일차"})
    raw = paths.session_json.read_text(encoding="utf-8")
    assert "자바 수업 3일차" in raw
    assert raw.endswith("\n")


# ── 9.2: 초기 session.json 문서 ──────────────────────────────────────────────


def test_initial_session_doc_start_fields(tmp_path: Path) -> None:
    started_at = NOW.astimezone()
    config = _config(tmp_path, tmp_path / "sessions")
    selection = Selection(selected=(PurePosixPath("a.py"),), excluded_count=2)
    doc = initial_session_doc(config, "20260826-183000-a1b2", started_at, selection, "native")
    assert doc["schema_version"] == "1.1"
    assert doc["session_id"] == "20260826-183000-a1b2"
    assert doc["title"] == "수업 18:30"
    assert doc["watch_root"] == str(tmp_path)
    assert doc["watched_files"] == [{"path": "a.py", "status": "unchanged"}]
    assert doc["status"] == "starting"
    assert doc["watch_mode"] == "native"
    assert doc["diff_engine"] == "difflib"
    # started_at 은 tz 포함 ISO8601 이다.
    parsed = datetime.fromisoformat(str(doc["started_at"]))
    assert parsed.tzinfo is not None
    assert doc["started_at"] == started_at.isoformat()
