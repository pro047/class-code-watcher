"""watcher 모듈 — 핸들러 필터·statuses 판정·세션 오케스트레이션 (FR-011~017, 035, 040/041).

설계 검증 기준 24~31. 실제 watchdog Observer 는 띄우지 않는다 — OS·타이밍 의존을 피하기
위해 `_make_observer` 를 더미로, `_drain_queue` 를 스크립트 드라이버로 바꿔 "이벤트를
받은 뒤의 판정·산출물"만 결정적으로 검증한다. 실제 이벤트 수신은 사람 확인 체크리스트로
넘긴다 (VERIFY.md).
"""

import json
import queue
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest
from watchdog.events import (
    DirModifiedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from class_watcher import cli, snapshot, watcher
from class_watcher.config import DEFAULT_EXCLUDE, Secrets, WatchConfig
from class_watcher.debounce import Debouncer, RawEvent
from class_watcher.selector import Selection, is_watched, scan_files
from class_watcher.session import (
    SessionPaths,
    create_session_dirs,
    initial_session_doc,
    make_session_paths,
    write_session_json,
)
from class_watcher.watcher import WatchOutcome, compute_statuses, is_no_change, run_session

NOW = datetime(2026, 8, 27, 10, 0, 0).astimezone()
INCLUDE = ("*.py",)

# ── 기준 24: 핸들러가 exclude 경로·allowlist 밖 확장자를 버린다 (FR-011) ──────


def _handler_with_sink(root: Path) -> tuple[watcher._Handler, "queue.Queue[RawEvent]"]:
    sink: queue.Queue[RawEvent] = queue.Queue()
    handler = watcher._Handler(root, INCLUDE, DEFAULT_EXCLUDE, sink, lambda: 42.0)
    return handler, sink


def _drain(sink: "queue.Queue[RawEvent]") -> list[RawEvent]:
    events = []
    while not sink.empty():
        events.append(sink.get_nowait())
    return events


def test_handler_filters_like_selector(tmp_path: Path) -> None:
    handler, sink = _handler_with_sink(tmp_path)
    cases: dict[str, bool] = {
        "a.py": True,  # allowlist 대상
        "notes.txt": False,  # allowlist 밖 확장자
        "node_modules/lib.py": False,  # exclude 세그먼트
        ".git/hook.py": False,
        "sub dir/한글.py": True,  # 공백·한글 경로도 대상
    }
    for rel, _expected in cases.items():
        handler.on_modified(FileModifiedEvent(str(tmp_path / rel)))

    enqueued = {event.rel_path for event in _drain(sink)}
    for rel, expected in cases.items():
        # 시작 스캔(scan_files)과 같은 판정 함수를 쓰므로 결과가 어긋나지 않는다.
        assert is_watched(Path(rel), INCLUDE, DEFAULT_EXCLUDE) is expected
        assert (rel in enqueued) is expected


def test_handler_drops_paths_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "inside"
    root.mkdir()
    handler, sink = _handler_with_sink(root)
    handler.on_modified(FileModifiedEvent(str(tmp_path / "outside.py")))
    assert _drain(sink) == []


def test_handler_ignores_directory_events(tmp_path: Path) -> None:
    handler, sink = _handler_with_sink(tmp_path)
    handler.on_modified(DirModifiedEvent(str(tmp_path / "sub")))
    assert _drain(sink) == []


def test_handler_stamps_injected_clock(tmp_path: Path) -> None:
    handler, sink = _handler_with_sink(tmp_path)
    handler.on_created(FileModifiedEvent(str(tmp_path / "a.py")))
    [event] = _drain(sink)
    assert event.at == 42.0  # 시각은 주입된 clock 에서만 나온다


# ── 기준 25: on_moved 는 src=deleted / dest=moved 로 분해된다 (FR-013) ────────


def test_moved_splits_into_deleted_and_moved(tmp_path: Path) -> None:
    handler, sink = _handler_with_sink(tmp_path)
    handler.on_moved(FileMovedEvent(str(tmp_path / "old.py"), str(tmp_path / "new.py")))
    events = [(event.rel_path, event.kind) for event in _drain(sink)]
    assert events == [("old.py", "deleted"), ("new.py", "moved")]


def test_atomic_save_rename_keeps_only_watched_side(tmp_path: Path) -> None:
    # 임시파일 → rename 저장: src(.tmp)는 allowlist 밖이라 버려지고 dest 만 남는다.
    handler, sink = _handler_with_sink(tmp_path)
    handler.on_moved(FileMovedEvent(str(tmp_path / "a.py.tmp"), str(tmp_path / "a.py")))
    events = [(event.rel_path, event.kind) for event in _drain(sink)]
    assert events == [("a.py", "moved")]


# ── 기준 26: statuses 계산 — unchanged/modified/added/deleted (FR-017) ────────


def test_compute_statuses_covers_all_four() -> None:
    statuses = compute_statuses(
        baseline={"same.py": "h1", "edit.py": "h2", "gone.py": "h3"},
        final={"same.py": "h1", "edit.py": "h2x", "new.py": "h4"},
    )
    assert statuses == {
        "same.py": "unchanged",
        "edit.py": "modified",
        "gone.py": "deleted",
        "new.py": "added",
    }


def test_is_no_change_requires_all_unchanged() -> None:
    assert is_no_change({"a.py": "unchanged", "b.py": "unchanged"}) is True
    assert is_no_change({"a.py": "unchanged", "b.py": "modified"}) is False
    # 대상 0개인 세션도 "변경 없음"이다 — 빈 all() (IMPL ⑥).
    assert is_no_change({}) is True


# ── run_session 판정부 — Observer 없이 스크립트로 돌린다 ─────────────────────


class _DummyObserver:
    """watchdog Observer 자리의 더미. 스레드도 OS 훅도 만들지 않는다."""

    def __init__(self) -> None:
        self.stopped = False

    def schedule(self, handler: object, path: str, recursive: bool = False) -> None:
        return None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout: float | None = None) -> None:
        return None


Step = Callable[[Debouncer], None]


def _script_loop(monkeypatch: pytest.MonkeyPatch, steps: list[Step]) -> _DummyObserver:
    """감시 루프를 결정적으로 만든다: 각 step 실행 후, 소진되면 Ctrl+C(1회차)를 흉내낸다."""
    observer = _DummyObserver()
    iterator: Iterator[Step] = iter(steps)

    def fake_drain(sink: "queue.Queue[RawEvent]", debouncer: Debouncer) -> None:
        step = next(iterator, None)
        if step is None:
            raise KeyboardInterrupt
        step(debouncer)

    monkeypatch.setattr(watcher, "_make_observer", lambda mode: observer)
    monkeypatch.setattr(watcher, "_drain_queue", fake_drain)
    return observer


def _setup_session(
    tmp_path: Path, *, history: bool = False
) -> tuple[WatchConfig, SessionPaths, Selection]:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.py").write_bytes(b"x = 1\n")
    config = WatchConfig(
        watch_root=root,
        title="검증",
        include=INCLUDE,
        exclude=DEFAULT_EXCLUDE,
        max_files=200,
        debounce_ms=750,
        polling=False,
        history=history,
        session_dir=tmp_path / "sessions",
        dry_run=False,
        no_discord=False,
        allow_secrets=False,
    )
    selection = scan_files(root, config.include, config.exclude)
    session_id = "20260827-100000-ab12"
    paths = make_session_paths(config.session_dir, session_id)
    create_session_dirs(paths)
    write_session_json(paths, initial_session_doc(config, session_id, NOW, selection, "native"))
    return config, paths, selection


def _session_doc(paths: SessionPaths) -> dict[str, object]:
    doc: dict[str, object] = json.loads(paths.session_json.read_text(encoding="utf-8"))
    return doc


# ── 기준 27: 전 파일 hash 동일 → no_change → completed + 코드 0 (FR-035) ──────


def test_no_change_session_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [])
    lines: list[str] = []

    outcome = run_session(config, paths, selection, lines.append)

    assert outcome.no_change is True
    assert outcome.aborted is False
    assert outcome.statuses == {"a.py": "unchanged"}
    doc = _session_doc(paths)
    assert doc["status"] == "completed"
    assert doc["no_change"] is True
    assert doc["watched_files"] == [{"path": "a.py", "status": "unchanged"}]
    assert "error" not in doc
    # 기준 31 의 절반: 콘솔 라인은 전부 주입된 emit 으로만 나간다 — print 직접 사용 금지.
    assert lines
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_no_change_maps_to_exit_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [])
    rc = cli.run_watch(
        cli.Preflight(config=config, selection=selection),
        paths,
        Secrets(openai_api_key=None, discord_webhook_url=None),
    )
    assert rc == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "[DONE] 변경 없음" in captured.out
    assert "[FAILED]" not in captured.err


# ── 기준 28: 변경 있음 → partial + 코드 1, baseline/final/events.jsonl 보존 ───


def test_changed_session_is_partial_and_preserves_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, paths, selection = _setup_session(tmp_path)
    root = config.watch_root

    def change(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(b"x = 2\n")
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))

    _script_loop(monkeypatch, [change])
    rc = cli.run_watch(
        cli.Preflight(config=config, selection=selection),
        paths,
        Secrets(openai_api_key=None, discord_webhook_url=None),
    )
    assert rc == cli.EXIT_RUNTIME
    capsys.readouterr()

    doc = _session_doc(paths)
    assert doc["status"] == "partial"
    assert doc["error"] == watcher.PENDING_PIPELINE_ERROR
    assert doc["no_change"] is False
    assert doc["watched_files"] == [{"path": "a.py", "status": "modified"}]
    assert doc["change_stats"] == {"files_changed": 1, "events": 1}
    # 산출물 보존: baseline 은 시작 시점, final 은 종료 시점 바이트다.
    assert (paths.baseline_dir / "a.py").read_bytes() == b"x = 1\n"
    assert (paths.final_dir / "a.py").read_bytes() == b"x = 2\n"
    rows = [
        json.loads(line)
        for line in paths.events_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert [(row["path"], row["event_type"]) for row in rows] == [("a.py", "modified")]
    assert rows[0]["hash"] == snapshot.hash_bytes(b"x = 2\n")
    assert rows[0]["size"] == 6
    # FR-014 ③ 의 unstable 은 final/ 디렉터리 안 .meta.json 최상위 키다 (IMPL ④).
    manifest = json.loads(
        (paths.final_dir / snapshot.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest["unstable"] is False
    # --history 미지정이면 history/ 는 생기지 않는다 (PRD 9.1).
    assert not paths.history_dir.exists()


def test_new_file_during_session_recorded_as_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FR-017: 세션 중 생성된 파일은 baseline 없이 final 에 편입되고 status=added.
    config, paths, selection = _setup_session(tmp_path)
    root = config.watch_root

    def create(debouncer: Debouncer) -> None:
        (root / "new.py").write_bytes(b"n = 1\n")
        debouncer.observe(RawEvent(rel_path="new.py", kind="created", at=0.0))

    _script_loop(monkeypatch, [create])
    outcome = run_session(config, paths, selection, lambda line: None)

    assert outcome.statuses == {"a.py": "unchanged", "new.py": "added"}
    assert outcome.no_change is False
    assert (paths.final_dir / "new.py").read_bytes() == b"n = 1\n"
    assert not (paths.baseline_dir / "new.py").exists()


def test_deleted_file_recorded_with_null_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, paths, selection = _setup_session(tmp_path)
    root = config.watch_root

    def delete(debouncer: Debouncer) -> None:
        (root / "a.py").unlink()
        debouncer.observe(RawEvent(rel_path="a.py", kind="deleted", at=0.0))

    _script_loop(monkeypatch, [delete])
    outcome = run_session(config, paths, selection, lambda line: None)

    assert outcome.statuses == {"a.py": "deleted"}
    [row] = [
        json.loads(line)
        for line in paths.events_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert row["event_type"] == "deleted"
    assert row["hash"] is None
    assert row["size"] is None


# ── 기준 29: 상태 전이 starting→watching→finalizing→(completed|partial) (FR-040) ──


def _record_transitions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorded: list[str] = []
    real_write = watcher.write_session_json

    def spy(paths: SessionPaths, doc: dict[str, object]) -> None:
        recorded.append(str(doc["status"]))
        real_write(paths, doc)

    monkeypatch.setattr(watcher, "write_session_json", spy)
    return recorded


def test_status_transitions_in_order_for_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [])
    recorded = _record_transitions(monkeypatch)

    run_session(config, paths, selection, lambda line: None)

    # 첫 기록(watch_mode 반영)은 아직 starting 이다. 전이 순서만 고정한다.
    assert [status for status in recorded if status != "starting"] == [
        "watching",
        "finalizing",
        "completed",
    ]


def test_status_transitions_end_partial_when_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, paths, selection = _setup_session(tmp_path)
    root = config.watch_root

    def change(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(b"x = 9\n")
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))

    _script_loop(monkeypatch, [change])
    recorded = _record_transitions(monkeypatch)

    run_session(config, paths, selection, lambda line: None)

    assert [status for status in recorded if status != "starting"] == [
        "watching",
        "finalizing",
        "partial",
    ]


# ── 기준 30: --history 는 논리 이벤트마다 history/<seq>/ 사본과 hash 를 남긴다 (FR-015) ──


def test_history_writes_sequential_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, paths, selection = _setup_session(tmp_path, history=True)
    root = config.watch_root

    def first(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(b"x = 2\n")
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))

    def second(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(b"x = 3\n")
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))

    _script_loop(monkeypatch, [first, second])
    run_session(config, paths, selection, lambda line: None)

    assert (paths.history_dir / "0001" / "a.py").read_bytes() == b"x = 2\n"
    assert (paths.history_dir / "0002" / "a.py").read_bytes() == b"x = 3\n"
    assert not (paths.history_dir / "0003").exists()
    manifest = json.loads(
        (paths.history_dir / "0001" / snapshot.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    [entry] = manifest["files"]
    assert entry["sha256"] == snapshot.hash_bytes(b"x = 2\n")
    # FR-015 의 timestamp 는 events.jsonl 행에 있다 — 이벤트 수와 같은 행 수.
    rows = [
        json.loads(line)
        for line in paths.events_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 2
    assert all(row["timestamp"] for row in rows)
    assert rows[0]["hash"] != rows[1]["hash"]


# ── WatchOutcome → 종료 코드 매핑의 나머지 (설계 4절 표) ─────────────────────


def test_aborted_outcome_maps_to_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, paths, selection = _setup_session(tmp_path)

    def fake_run_session(
        config_: WatchConfig,
        paths_: SessionPaths,
        selection_: Selection,
        emit: Callable[[str], None],
    ) -> WatchOutcome:
        return WatchOutcome(
            statuses={}, unstable=False, logical_event_count=0, no_change=False, aborted=True
        )

    monkeypatch.setattr(cli, "run_session", fake_run_session)
    rc = cli.run_watch(
        cli.Preflight(config=config, selection=selection),
        paths,
        Secrets(openai_api_key=None, discord_webhook_url=None),
    )
    assert rc == cli.EXIT_ABORTED
    assert "[ABORTED]" in capsys.readouterr().err


def test_oserror_during_watch_records_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, paths, selection = _setup_session(tmp_path)

    def fake_run_session(
        config_: WatchConfig,
        paths_: SessionPaths,
        selection_: Selection,
        emit: Callable[[str], None],
    ) -> WatchOutcome:
        raise OSError("디스크가 사라졌다")

    monkeypatch.setattr(cli, "run_session", fake_run_session)
    rc = cli.run_watch(
        cli.Preflight(config=config, selection=selection),
        paths,
        Secrets(openai_api_key=None, discord_webhook_url=None),
    )
    assert rc == cli.EXIT_RUNTIME
    doc = _session_doc(paths)
    assert doc["status"] == "failed"
    assert doc["error"] == "watch_io_error"
    assert "[FAILED]" in capsys.readouterr().err


# ── 기준 31: cli 가 주입하는 emit 은 mask_secrets 관문을 거친다 (FR-003) ──────


def test_emitted_lines_are_masked_by_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, paths, selection = _setup_session(tmp_path)
    secret_value = "sk-test-abcdef1234567890"

    def fake_run_session(
        config_: WatchConfig,
        paths_: SessionPaths,
        selection_: Selection,
        emit: Callable[[str], None],
    ) -> WatchOutcome:
        emit(f"디버그 출력에 섞인 키: {secret_value}")
        return WatchOutcome(
            statuses={}, unstable=False, logical_event_count=0, no_change=True, aborted=False
        )

    monkeypatch.setattr(cli, "run_session", fake_run_session)
    rc = cli.run_watch(
        cli.Preflight(config=config, selection=selection),
        paths,
        Secrets(openai_api_key=secret_value, discord_webhook_url=None),
    )
    assert rc == cli.EXIT_OK
    captured = capsys.readouterr()
    assert secret_value not in captured.out
    assert secret_value not in captured.err
    assert "[MASKED]" in captured.out
