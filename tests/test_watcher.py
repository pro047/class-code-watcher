"""watcher 모듈 — 핸들러 필터·statuses 판정·세션 오케스트레이션 (FR-011~017, 035, 040/041).

설계 검증 기준 24~31. 실제 watchdog Observer 는 띄우지 않는다 — OS·타이밍 의존을 피하기
위해 `_make_observer` 를 더미로, `_drain_queue` 를 스크립트 드라이버로 바꿔 "이벤트를
받은 뒤의 판정·산출물"만 결정적으로 검증한다. 실제 이벤트 수신은 사람 확인 체크리스트로
넘긴다 (VERIFY.md).
"""

import json
import queue
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath

import pytest
from watchdog.events import (
    DirModifiedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from class_watcher import cli, notify, snapshot, summarize, watcher
from class_watcher.config import DEFAULT_EXCLUDE, Secrets, WatchConfig
from class_watcher.debounce import Debouncer, RawEvent
from class_watcher.notify import DeliveryOutcome, DiscordRequestError, find_diff_lines
from class_watcher.selector import Selection, is_watched, scan_files
from class_watcher.session import (
    SessionPaths,
    SessionStatus,
    create_session_dirs,
    initial_session_doc,
    make_session_paths,
    write_session_json,
)
from class_watcher.summarize import (
    KIND_TIMEOUT,
    BuiltPrompt,
    LlmRequestError,
    LlmResponse,
    SummarizeOutcome,
)
from class_watcher.watcher import (
    WatchOutcome,
    compute_statuses,
    is_no_change,
    resolve_session_end,
    resolve_summary_state,
    run_session,
)

NOW = datetime(2026, 8, 27, 10, 0, 0).astimezone()
INCLUDE = ("*.py",)
# run_session 의 5번째 인자 (known-value 탐지 규칙용). 키가 없는 상태가 기본이다.
NO_SECRETS = Secrets(openai_api_key=None, discord_webhook_url=None)
# 요약 경로까지 진입시키는 세션용. 실 SDK 는 make_openai_caller 를 갈아끼워 절대 뜨지 않는다.
FAKE_KEY = "sk-test-abcdef1234567890"
WITH_KEY = Secrets(openai_api_key=FAKE_KEY, discord_webhook_url=None)

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
    tmp_path: Path,
    *,
    history: bool = False,
    allow_secrets: bool = False,
    dry_run: bool = False,
    no_discord: bool = False,
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
        dry_run=dry_run,
        no_discord=no_discord,
        allow_secrets=allow_secrets,
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

    outcome = run_session(config, paths, selection, lines.append, NO_SECRETS)

    assert outcome.no_change is True
    assert outcome.aborted is False
    assert outcome.statuses == {"a.py": "unchanged"}
    doc = _session_doc(paths)
    assert doc["status"] == "completed"
    assert doc["no_change"] is True
    assert doc["watched_files"] == [{"path": "a.py", "status": "unchanged"}]
    assert "error" not in doc
    # 기준 17 · FR-035 경로 불변: no_change 세션은 diff 산출물을 만들지 않는다.
    assert not paths.final_diff.exists()
    assert not paths.stats_json.exists()
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
    # PRD 10.1 의 [DIFF] 콘솔 한 줄 — 건너뛴 파일이 없으면 뒷부분이 붙지 않는다.
    assert "[DIFF] 1개 파일 변경 (+1 / -1)" in capsys.readouterr().out

    doc = _session_doc(paths)
    assert doc["status"] == "partial"
    # 키 없는 세션은 요약 지점에서 호출 없이 실패한다 (4단계 매핑 — 설계 6.6).
    assert doc["error"] == watcher.ERROR_OPENAI_KEY_MISSING
    assert doc["openai"] == {"calls": 0, "retries": 0, "model": None, "request_id": None}
    assert doc["no_change"] is False
    assert doc["watched_files"] == [{"path": "a.py", "status": "modified"}]
    # diff 단계가 PRD 9.2 의 4필드를 채운다 (설계 기준 16).
    assert doc["change_stats"] == {
        "files_changed": 1,
        "events": 1,
        "added_lines": 1,
        "deleted_lines": 1,
    }
    # 기준 16: diff 산출물이 세션 디렉터리에 남고, 시각은 session.json 과 같은 문자열이다.
    assert "--- a/a.py" in paths.final_diff.read_text(encoding="utf-8")
    stats = json.loads(paths.stats_json.read_text(encoding="utf-8"))
    assert stats["totals"] == {
        "files_changed": 1,
        "added_lines": 1,
        "deleted_lines": 1,
        "skipped": 0,
    }
    assert stats["started_at"] == doc["started_at"]
    assert stats["ended_at"] == doc["ended_at"]
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
    outcome = run_session(config, paths, selection, lambda line: None, NO_SECRETS)

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
    outcome = run_session(config, paths, selection, lambda line: None, NO_SECRETS)

    assert outcome.statuses == {"a.py": "deleted"}
    [row] = [
        json.loads(line)
        for line in paths.events_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert row["event_type"] == "deleted"
    assert row["hash"] is None
    assert row["size"] is None


# ── diff 통합 지점 — 설계 5.6 과 PRD 12절 복구 원칙 ──────────────────────────


def test_binary_only_change_is_skipped_but_not_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 설계 5.6: 해시상 변경은 있으나 diff 가능 파일이 0개인 세션 — 사실만 기록한다.
    config, paths, selection = _setup_session(tmp_path)
    root = config.watch_root

    def change(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(b"\x00\x01\x02")
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))

    _script_loop(monkeypatch, [change])
    lines: list[str] = []
    run_session(config, paths, selection, lines.append, NO_SECRETS)

    doc = _session_doc(paths)
    assert doc["no_change"] is False  # no_change 판정은 해시 기준 그대로다
    assert doc["watched_files"] == [{"path": "a.py", "status": "skipped", "reason": "binary"}]
    assert doc["change_stats"] == {
        "files_changed": 0,
        "events": 1,
        "added_lines": 0,
        "deleted_lines": 0,
    }
    # 산출물만 봐도 제외 사실이 남는다 (PRD 9.2 skipped 예시와 같은 형태).
    assert paths.final_diff.read_text(encoding="utf-8") == "# skipped: a.py (binary)\n"
    assert "[DIFF] 0개 파일 변경 (+0 / -0), 1개 건너뜀(binary)" in lines


def test_diff_failure_logs_error_and_keeps_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PRD 12절 복구 원칙: diff 생성이 통째로 실패해도 세션은 계속되고 스냅샷은 남는다.
    config, paths, selection = _setup_session(tmp_path)
    root = config.watch_root

    def change(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(b"x = 2\n")
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("디스크 꽉 참")

    _script_loop(monkeypatch, [change])
    monkeypatch.setattr(watcher, "generate_session_diff", boom)
    lines: list[str] = []
    outcome = run_session(config, paths, selection, lines.append, NO_SECRETS)

    assert outcome.aborted is False
    doc = _session_doc(paths)
    assert doc["status"] == "partial"
    # 라인 수를 세지 못한 세션은 0 을 주장하지 않는다 — 기존 2필드 형태 유지 (IMPL).
    assert doc["change_stats"] == {"files_changed": 1, "events": 1}
    assert doc["watched_files"] == [{"path": "a.py", "status": "modified"}]
    [row] = [
        json.loads(line)
        for line in paths.errors_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert row["stage"] == "diff"
    assert row["error"] == "OSError"
    assert row["timestamp"]
    # 예외 메시지 원문은 기록하지 않는다 — 경로·환경 정보가 새는 통로를 만들지 않는다.
    assert "디스크 꽉 참" not in json.dumps(row, ensure_ascii=False)
    assert any(line.startswith("[WARN]") for line in lines)
    assert not paths.final_diff.exists()
    assert not paths.stats_json.exists()
    # 스냅샷은 그대로 남는다.
    assert (paths.baseline_dir / "a.py").read_bytes() == b"x = 1\n"
    assert (paths.final_dir / "a.py").read_bytes() == b"x = 2\n"


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

    run_session(config, paths, selection, lambda line: None, NO_SECRETS)

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

    run_session(config, paths, selection, lambda line: None, NO_SECRETS)

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
    run_session(config, paths, selection, lambda line: None, NO_SECRETS)

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
        secrets_: Secrets,
        model_: str,
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
        secrets_: Secrets,
        model_: str,
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
        secrets_: Secrets,
        model_: str,
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


# ── 중단된 세션은 파일 상태를 주장하지 않는다 (HANDOFF 5절 라) ────────────────


def test_unknown_file_statuses_is_pure() -> None:
    selected = (PurePosixPath("a.py"), PurePosixPath("pkg/b.py"))
    assert watcher.unknown_file_statuses(selected) == [
        {"path": "a.py", "status": "unknown"},
        {"path": "pkg/b.py", "status": "unknown"},
    ]


def test_aborted_session_does_not_claim_files_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 두 번째 Ctrl+C 는 안정화 대기 중에 들어온다. 그러면 baseline↔final 비교를 못 하는데,
    # 시작 시점 doc 의 unchanged 가 그대로 남으면 "변경 없음"이라고 거짓말하게 된다.
    config, paths, selection = _setup_session(tmp_path)
    root = config.watch_root

    def change(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(b"x = 2\n")
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))

    def second_ctrl_c(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    _script_loop(monkeypatch, [change])
    monkeypatch.setattr(watcher, "wait_for_stability", second_ctrl_c)
    outcome = run_session(config, paths, selection, lambda line: None, NO_SECRETS)

    assert outcome.aborted is True
    doc = _session_doc(paths)
    assert doc["status"] == "failed"
    assert doc["error"] == watcher.ABORTED_ERROR
    assert doc["watched_files"] == [{"path": "a.py", "status": "unknown"}]
    # 판정을 못 했으므로 no_change 는 아예 쓰지 않는다.
    assert "no_change" not in doc
    # 산출물은 남는다 — baseline 은 시작 시점 바이트, final 스냅샷은 뜨지 못했다.
    assert (paths.baseline_dir / "a.py").read_bytes() == b"x = 1\n"


# ── 3단계 정제 배선 — 탐지·차단·마스킹·산출물 (FR-036~FR-038, 설계 기준 17~22) ──

SECRET_VALUE = "sk-fixture0123456789abcdefgh"
SECRET_LINE = f"# {SECRET_VALUE}\n".encode()


def _plant_secret(root: Path) -> Step:
    def change(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(SECRET_LINE)
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))

    return change


def test_secret_session_blocks_and_preserves_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 기준 17: 탐지-중단 세션은 failed+secrets_detected 로 끝나고 로컬 산출물은 전부 남는다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_plant_secret(config.watch_root)])
    lines: list[str] = []

    outcome = run_session(config, paths, selection, lines.append, NO_SECRETS)

    assert outcome.secrets_blocked is True
    assert outcome.aborted is False
    assert outcome.no_change is False
    doc = _session_doc(paths)
    assert doc["status"] == "failed"
    assert doc["error"] == "secrets_detected"
    assert doc["redaction"] == {
        "secrets_found": 1,
        "by_rule": {"openai_api_key": 1},
        # 차단 세션은 정제 본문을 만들지 않는다 (IMPL 2절 4항).
        "paths_relativized": False,
    }
    raw = paths.redaction_json.read_text(encoding="utf-8")
    redaction = json.loads(raw)
    assert redaction["policy"] == "block"
    assert redaction["allow_secrets"] is False
    assert redaction["secrets_found"] == 1
    [finding] = redaction["findings"]
    assert finding["rule"] == "openai_api_key"
    assert finding["path"] == "a.py"
    assert finding["line"] > 0
    # FR-042: 산출물·콘솔 어디에도 탐지 원문(마지막 4자 포함)이 없다.
    assert SECRET_VALUE not in raw
    assert SECRET_VALUE[-4:] not in raw
    assert SECRET_VALUE not in "\n".join(lines)
    # 차단되는 것은 외부 전송뿐이다 — diff·stats·스냅샷은 그대로 보존된다 (PRD 12절).
    assert paths.final_diff.exists()
    assert paths.stats_json.exists()
    assert (paths.baseline_dir / "a.py").read_bytes() == b"x = 1\n"
    assert (paths.final_dir / "a.py").read_bytes() == SECRET_LINE
    assert any(
        line.startswith("[SCAN]") and "외부 전송을 중단합니다" in line for line in lines
    )


def test_allow_secrets_session_masks_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 기준 18: 같은 세션 + --allow-secrets 는 기존 과도기 경로(partial)로 진행한다 (FR-038).
    config, paths, selection = _setup_session(tmp_path, allow_secrets=True)
    _script_loop(monkeypatch, [_plant_secret(config.watch_root)])
    lines: list[str] = []

    outcome = run_session(config, paths, selection, lines.append, NO_SECRETS)

    assert outcome.secrets_blocked is False
    doc = _session_doc(paths)
    assert doc["status"] == "partial"
    # 마스킹 후 계속 진행하면 요약 지점에 닿고, 키가 없으므로 거기서 멈춘다 (설계 6.6).
    assert doc["error"] == watcher.ERROR_OPENAI_KEY_MISSING
    assert doc["redaction"] == {
        "secrets_found": 1,
        "by_rule": {"openai_api_key": 1},
        "paths_relativized": True,
    }
    raw = paths.redaction_json.read_text(encoding="utf-8")
    redaction = json.loads(raw)
    assert redaction["policy"] == "mask"
    assert redaction["allow_secrets"] is True
    assert SECRET_VALUE not in raw
    assert any("마스킹 후 진행합니다" in line for line in lines)


def test_clean_changed_session_writes_clean_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 기준 19: 비밀값 없는 변경 세션은 기존 상태 전이 그대로 + redaction 산출물이 추가된다.
    config, paths, selection = _setup_session(tmp_path)
    root = config.watch_root

    def change(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(b"x = 2\n")
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))

    _script_loop(monkeypatch, [change])
    lines: list[str] = []
    outcome = run_session(config, paths, selection, lines.append, NO_SECRETS)

    assert outcome.secrets_blocked is False
    doc = _session_doc(paths)
    assert doc["status"] == "partial"
    # 깨끗한 세션도 키가 없으면 요약 지점에서 멈춘다 (설계 6.6).
    assert doc["error"] == watcher.ERROR_OPENAI_KEY_MISSING
    assert doc["redaction"] == {
        "secrets_found": 0,
        "by_rule": {},
        "paths_relativized": True,
    }
    redaction = json.loads(paths.redaction_json.read_text(encoding="utf-8"))
    assert redaction["policy"] == "clean"
    assert redaction["secrets_found"] == 0
    assert redaction["findings"] == []
    assert "[SCAN] 비밀정보 패턴 탐지 없음" in lines


def test_no_change_session_skips_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 기준 20 · FR-035 경로 불변: 변경 없음이면 스캔이 돌지 않고 redaction.json 도 없다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [])
    lines: list[str] = []

    run_session(config, paths, selection, lines.append, NO_SECRETS)

    assert not paths.redaction_json.exists()
    assert "redaction" not in _session_doc(paths)
    assert not any(line.startswith("[SCAN]") for line in lines)


def test_second_ctrl_c_during_redaction_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 기준 21: 정제 도중 두 번째 Ctrl+C 도 abort 경로(FAILED+aborted_by_user)로 빠진다.
    config, paths, selection = _setup_session(tmp_path)
    root = config.watch_root

    def change(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(b"x = 2\n")
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))

    def second_ctrl_c(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    _script_loop(monkeypatch, [change])
    monkeypatch.setattr(watcher, "redact_diff", second_ctrl_c)
    outcome = run_session(config, paths, selection, lambda line: None, NO_SECRETS)

    assert outcome.aborted is True
    assert outcome.secrets_blocked is False
    doc = _session_doc(paths)
    assert doc["status"] == "failed"
    assert doc["error"] == watcher.ABORTED_ERROR


def test_redaction_write_failure_fails_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 설계 5.5 마지막 행: redaction.json 쓰기 실패는 errors.jsonl 에 남고 세션은 계속되되,
    # 스캔 통과로 오인되지 않는다 (redaction 필드 없음 = 4단계에 넘길 text 없음).
    config, paths, selection = _setup_session(tmp_path)
    root = config.watch_root

    def change(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(b"x = 2\n")
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("디스크 꽉 참")

    _script_loop(monkeypatch, [change])
    monkeypatch.setattr(watcher, "write_redaction_json", boom)
    lines: list[str] = []
    outcome = run_session(config, paths, selection, lines.append, NO_SECRETS)

    assert outcome.aborted is False
    assert outcome.secrets_blocked is False
    doc = _session_doc(paths)
    assert doc["status"] == "partial"
    # 정제 산출물을 못 쓴 세션은 스캔 통과를 주장할 수 없다 — 요약 지점에 진입하지 않는다.
    assert doc["error"] == watcher.ERROR_REDACTION_FAILED
    assert doc["openai"] == {"calls": 0, "retries": 0, "model": None, "request_id": None}
    assert "redaction" not in doc
    assert not paths.redaction_json.exists()
    [row] = [
        json.loads(line)
        for line in paths.errors_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert row["stage"] == "redaction"
    assert row["error"] == "OSError"
    [warn] = [line for line in lines if line.startswith("[WARN]")]
    assert "외부 전송은 하지 않습니다" in warn
    warn.encode("cp949")  # 리다이렉트(cp949) 콘솔에서도 안 깨진다 (HANDOFF (다))


def test_secrets_blocked_maps_to_exit_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 기준 22: cli 는 탐지-중단 세션에 전용 [FAILED] 안내를 내고 코드 1 로 끝난다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_plant_secret(config.watch_root)])
    rc = cli.run_watch(
        cli.Preflight(config=config, selection=selection), paths, NO_SECRETS
    )
    assert rc == cli.EXIT_RUNTIME
    captured = capsys.readouterr()
    assert "[FAILED] 비밀정보 패턴이 탐지되어 외부 전송을 중단했습니다." in captured.err
    assert "redaction.json" in captured.err
    assert "세션 산출물은 보존됩니다" in captured.err
    # 차단 분기는 과도기 [FAILED] 문구(Discord 미구현)를 내지 않는다.
    assert "Discord 전송 단계는 아직 구현되지 않았습니다" not in captured.err
    assert "외부 전송을 중단합니다" in captured.out  # [SCAN] 라인
    # 회귀: 새 콘솔 문자열 전부 cp949 로 인코딩된다 (HANDOFF (다) 결함 재발 방지).
    captured.out.encode("cp949")
    captured.err.encode("cp949")


# ── 4단계 요약 배선 — 호출 계수·산출물·상태 매핑 (FR-030~032, 035, 037, 039, 042) ──
#
# mock 경계는 CallFn 하나다: watcher.make_openai_caller 를 가짜 팩토리로 갈아끼우면
# 실 SDK·네트워크 없이 전 경로가 돈다 (설계 6.7, IMPL 4.3).

VALID_SUMMARY_TEXT = json.dumps(
    {
        "session_title": "검증",
        "summary": "값을 바꾸는 변경을 했다.",
        "change_stats": {"files_changed": 1, "added_lines": 1, "deleted_lines": 1},
        # C-17 로 changes[]/learning_points[] 가 keywords[] 하나로 합쳐졌다.
        "keywords": [
            {
                "term": "대입 연산자",
                "concept": "변수에 값을 넣는 연산자다.",
                "syntax": "=",
                # C-19: 모델이 짓는 동적 제목이다 — 구 6종 enum 에 없던 값이 그대로 흐른다.
                "group": "변수와 대입",
                "confidence": "high",
            }
        ],
        "questions_to_review": [],
        "risks_or_todos": [],
        "sensitive_data_detected": False,
    },
    ensure_ascii=False,
)


class _FakeCaller:
    """make_openai_caller 자리의 가짜 팩토리. 호출 횟수와 프롬프트를 기록한다."""

    def __init__(self, outcomes: list[str | LlmRequestError]) -> None:
        self.outcomes = list(outcomes)
        self.prompts: list[BuiltPrompt] = []
        self.factory_args: list[tuple[str, str]] = []

    def factory(self, api_key: str, model: str) -> Callable[[BuiltPrompt], LlmResponse]:
        self.factory_args.append((api_key, model))
        return self._call

    def _call(self, prompt: BuiltPrompt) -> LlmResponse:
        self.prompts.append(prompt)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, LlmRequestError):
            raise outcome
        return LlmResponse(text=outcome, request_id="req-1", model="fake-model")


def _forbid_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid(api_key: str, model: str) -> Callable[[BuiltPrompt], LlmResponse]:
        raise AssertionError("이 경로는 OpenAI 호출 함수를 만들면 안 된다 (FR-030/FR-035)")

    monkeypatch.setattr(watcher, "make_openai_caller", forbid)


def _change_a_py(root: Path) -> Step:
    def change(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(b"x = 2\n")
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))

    return change


def test_no_change_session_never_builds_caller_even_with_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 불변식 (FR-035): 변경 없음이면 키가 있어도 OpenAI 호출 0회. calls: 0 이 증거로 남는다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [])
    _forbid_caller(monkeypatch)

    outcome = run_session(config, paths, selection, lambda line: None, WITH_KEY)

    assert outcome.no_change is True
    assert outcome.summary_state == watcher.SUMMARY_NOT_RUN
    doc = _session_doc(paths)
    assert doc["status"] == "completed"
    assert doc["openai"] == {"calls": 0, "retries": 0, "model": None, "request_id": None}
    assert not paths.summary_json.exists()


def test_blocked_session_never_builds_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 불변식 (FR-036): secret scan 미통과면 키가 있어도 외부 전송(요약 호출)이 없다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_plant_secret(config.watch_root)])
    _forbid_caller(monkeypatch)

    outcome = run_session(config, paths, selection, lambda line: None, WITH_KEY)

    assert outcome.secrets_blocked is True
    assert outcome.summary_state == watcher.SUMMARY_NOT_RUN
    doc = _session_doc(paths)
    assert doc["status"] == "failed"
    assert doc["error"] == "secrets_detected"
    assert doc["openai"] == {"calls": 0, "retries": 0, "model": None, "request_id": None}


def test_dry_run_writes_prompt_json_and_completes_without_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PRD 10.2: dry-run 은 외부 호출 없이 프롬프트까지만 검증하고 completed 로 끝난다.
    config, paths, selection = _setup_session(tmp_path, dry_run=True)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    _forbid_caller(monkeypatch)
    lines: list[str] = []

    outcome = run_session(config, paths, selection, lines.append, WITH_KEY)

    assert outcome.summary_state == watcher.SUMMARY_DRY_RUN
    doc = _session_doc(paths)
    assert doc["status"] == "completed"
    assert "error" not in doc
    assert doc["dry_run"] is True
    assert doc["openai"] == {"calls": 0, "retries": 0, "model": None, "request_id": None}
    prompt = json.loads(paths.prompt_json.read_text(encoding="utf-8"))
    assert "<diff>" in prompt["user"]
    assert prompt["response_schema"] == summarize.response_schema()
    # 프롬프트 산출물에도 감시 루트 절대 경로가 없다 (FR-037).
    assert str(config.watch_root) not in prompt["user"] + prompt["system"]
    assert not paths.summary_json.exists()
    assert any(line.startswith("[DRY-RUN]") for line in lines)
    "\n".join(lines).encode("cp949")


def test_successful_summary_writes_artifacts_and_counts_one_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    caller = _FakeCaller([VALID_SUMMARY_TEXT])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)
    lines: list[str] = []

    outcome = run_session(config, paths, selection, lines.append, WITH_KEY)

    # 정상 경로 정확히 1회 (FR-030). 팩토리에는 키·모델이 그대로 전달된다.
    assert len(caller.prompts) == 1
    assert caller.factory_args == [(FAKE_KEY, "gpt-4o-mini")]
    assert outcome.summary_state == watcher.SUMMARY_OK
    doc = _session_doc(paths)
    # WITH_KEY 에는 Webhook URL 이 없다. 사용자가 --no-discord 를 주지 않았으므로 전송을
    # 기대한 것이고, 그래서 생략이 아니라 실패다 (5단계 설계 5.6).
    assert doc["status"] == "partial"
    assert doc["error"] == notify.ERROR_DISCORD_URL_MISSING
    assert doc["discord"] == {
        "delivered": False,
        "http_status": None,
        "requests": 0,
        "chunks": 0,
        "skip_reason": None,
    }
    assert doc["openai"] == {
        "calls": 1,
        "retries": 0,
        "model": "fake-model",
        "request_id": "req-1",
    }
    summary = json.loads(paths.summary_json.read_text(encoding="utf-8"))
    assert summary["source"] == "openai"
    assert summary["openai"] == {"calls": 1, "retries": 0, "request_id": "req-1"}
    assert summary["summary"]["session_title"] == "검증"
    # FR-037: 실제로 나간 프롬프트에 감시 루트 절대 경로·키가 없다.
    [prompt] = caller.prompts
    assert str(config.watch_root) not in prompt.user + prompt.system
    assert FAKE_KEY not in prompt.user + prompt.system
    assert "--- a/a.py" in prompt.user
    assert any(line.startswith("[AI] 요약 저장:") for line in lines)
    "\n".join(lines).encode("cp949")


def test_summary_ok_with_no_discord_completes_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, paths, selection = _setup_session(tmp_path, no_discord=True)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    caller = _FakeCaller([VALID_SUMMARY_TEXT])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)

    rc = cli.run_watch(cli.Preflight(config=config, selection=selection), paths, WITH_KEY)

    # 사용자가 전송 생략을 명시했으므로 이 설정에서 할 일이 전부 끝났다 (설계 6.6, PRD 10.3).
    assert rc == cli.EXIT_OK
    doc = _session_doc(paths)
    assert doc["status"] == "completed"
    assert "error" not in doc
    captured = capsys.readouterr()
    assert "[DONE] 요약까지 완료했습니다. 전송은 생략합니다" in captured.out
    captured.out.encode("cp949")
    captured.err.encode("cp949")


def test_double_schema_failure_falls_back_and_masks_error_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2회 모두 스키마 실패 → 호출 2회 상한 + 규칙 기반 fallback (FR-030, FR-039).
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    bad_text = f"응답에 키가 되비쳤다 {FAKE_KEY}"
    caller = _FakeCaller([bad_text, bad_text])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)
    lines: list[str] = []

    outcome = run_session(config, paths, selection, lines.append, WITH_KEY)

    assert len(caller.prompts) == 2
    assert outcome.summary_state == watcher.SUMMARY_FALLBACK
    doc = _session_doc(paths)
    assert doc["status"] == "partial"
    # 위와 같은 이유 — WITH_KEY 에는 Webhook URL 이 없다 (5단계 설계 5.6).
    assert doc["error"] == notify.ERROR_DISCORD_URL_MISSING
    openai_fields = doc["openai"]
    assert isinstance(openai_fields, dict)
    assert openai_fields["calls"] == 2
    assert openai_fields["retries"] == 1
    summary = json.loads(paths.summary_json.read_text(encoding="utf-8"))
    assert summary["source"] == "rule_based"
    assert summary["model"] is None
    assert summary["summary"]["summary"].startswith("[규칙 기반 요약]")
    # FR-031 "제한적 보관" + FR-042: 발췌는 2건, 원문 키는 [MASKED] 로 치환된다.
    raw_rows = paths.errors_jsonl.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in raw_rows]
    assert [row["attempt"] for row in rows] == [1, 2]
    assert all(row["stage"] == "summarize" for row in rows)
    assert all(row["error"] == "schema_validation" for row in rows)
    assert all("[MASKED]" in row["raw_excerpt"] for row in rows)
    assert FAKE_KEY not in "\n".join(raw_rows)
    assert "[AI] 재시도까지 실패. 규칙 기반 요약으로 대체합니다" in lines
    "\n".join(lines).encode("cp949")


def test_transport_failure_records_partial_without_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PRD 12절: 전송 실패는 재시도·fallback 없이 partial 로 남긴다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    caller = _FakeCaller([LlmRequestError(KIND_TIMEOUT)])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)

    outcome = run_session(config, paths, selection, lambda line: None, WITH_KEY)

    assert len(caller.prompts) == 1
    assert outcome.summary_state == watcher.SUMMARY_FAILED
    doc = _session_doc(paths)
    assert doc["status"] == "partial"
    assert doc["error"] == "openai_timeout"
    openai_fields = doc["openai"]
    assert isinstance(openai_fields, dict)
    assert openai_fields["calls"] == 1
    assert not paths.summary_json.exists()


def test_summary_write_failure_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 요약을 만들고도 산출물을 못 썼다면 성공이 아니다 — 5단계가 없는 파일을 읽게 된다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    caller = _FakeCaller([VALID_SUMMARY_TEXT])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("디스크 꽉 참")

    monkeypatch.setattr(watcher, "write_summary_json", boom)
    outcome = run_session(config, paths, selection, lambda line: None, WITH_KEY)

    assert outcome.summary_state == watcher.SUMMARY_FAILED
    doc = _session_doc(paths)
    assert doc["status"] == "partial"
    assert doc["error"] == watcher.ERROR_SUMMARY_WRITE_FAILED
    [row] = [
        json.loads(line)
        for line in paths.errors_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert row["stage"] == "summarize"
    assert row["error"] == "OSError"


def test_llm_sensitive_flag_is_added_to_redaction_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PRD 11.3: 모델의 사후 신고는 전송 차단이 아니라 redaction.json 기록 + 경고 1줄이다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    flagged = json.loads(VALID_SUMMARY_TEXT)
    flagged["sensitive_data_detected"] = True
    caller = _FakeCaller([json.dumps(flagged, ensure_ascii=False)])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)
    lines: list[str] = []

    outcome = run_session(config, paths, selection, lines.append, WITH_KEY)

    assert outcome.summary_state == watcher.SUMMARY_OK
    redaction = json.loads(paths.redaction_json.read_text(encoding="utf-8"))
    assert redaction["llm_sensitive_flag"] is True
    assert paths.summary_json.exists()
    assert any("모델이 비밀정보 의심을 신고했습니다" in line for line in lines)
    "\n".join(lines).encode("cp949")


def test_calls_happen_only_at_finalize_not_per_save_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 불변식 (FR-030): 저장 이벤트가 여러 번이어도 호출은 종료 시 1회뿐이다.
    config, paths, selection = _setup_session(tmp_path)
    root = config.watch_root

    def second_change(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(b"x = 3\n")
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=1.0))

    _script_loop(monkeypatch, [_change_a_py(root), second_change])
    caller = _FakeCaller([VALID_SUMMARY_TEXT])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)

    outcome = run_session(config, paths, selection, lambda line: None, WITH_KEY)

    assert outcome.logical_event_count == 2
    assert len(caller.prompts) == 1
    doc = _session_doc(paths)
    openai_fields = doc["openai"]
    assert isinstance(openai_fields, dict)
    assert openai_fields["calls"] == 1


# ── 5단계 전송 배선 — 계수·산출물·상태 매핑 (FR-033~035, 050~052) ─────────────
#
# mock 경계는 SendFn 하나다: watcher.make_discord_sender 를 가짜 팩토리로 갈아끼우면
# httpx·네트워크 없이 전 경로가 돈다 (설계 6절 케이스 25~32, IMPL 4.2).
#
# 주의: WITH_KEY 의 discord_webhook_url 은 None 이다. 실제 전송 경로를 타려면 아래
# WITH_DISCORD 를 쓰고 반드시 팩토리를 갈아끼워야 한다 — 안 그러면 진짜 요청이 나간다.

FAKE_WEBHOOK = "https://discord.example/api/webhooks/1234567890/super-secret-token"
WITH_DISCORD = Secrets(openai_api_key=FAKE_KEY, discord_webhook_url=FAKE_WEBHOOK)


class _FakeSender:
    """make_discord_sender 자리의 가짜 팩토리. 요청 1회 = _send 1회다."""

    def __init__(self, outcomes: list[int | DiscordRequestError]) -> None:
        self.outcomes = list(outcomes)
        self.payloads: list[dict[str, object]] = []
        self.urls: list[str] = []

    def factory(self, webhook_url: str) -> Callable[[Mapping[str, object]], int]:
        self.urls.append(webhook_url)
        return self._send

    def _send(self, payload: Mapping[str, object]) -> int:
        self.payloads.append(dict(payload))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, DiscordRequestError):
            raise outcome
        return outcome


def _forbid_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid(webhook_url: str) -> Callable[[Mapping[str, object]], int]:
        raise AssertionError("이 경로는 Discord 전송 함수를 만들면 안 된다 (FR-035/FR-052)")

    monkeypatch.setattr(watcher, "make_discord_sender", forbid)


def _discord_fields(paths: SessionPaths) -> dict[str, object]:
    fields = _session_doc(paths)["discord"]
    assert isinstance(fields, dict)
    return fields


def test_no_change_session_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 케이스 26 · 불변식 (FR-035): 변경 없음이면 OpenAI·Discord 모두 0회. 두 계수가
    # 전부 session.json 에 남아 사후에 증명된다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [])
    _forbid_caller(monkeypatch)
    _forbid_sender(monkeypatch)

    outcome = run_session(config, paths, selection, lambda line: None, WITH_DISCORD)

    assert outcome.no_change is True
    assert outcome.discord_state == notify.DISCORD_SKIPPED
    doc = _session_doc(paths)
    assert doc["status"] == "completed"
    assert doc["openai"] == {"calls": 0, "retries": 0, "model": None, "request_id": None}
    assert doc["discord"] == {
        "delivered": False,
        "http_status": None,
        "requests": 0,
        "chunks": 0,
        "skip_reason": notify.SKIP_NO_CHANGE,
    }
    assert not paths.discord_payload_json.exists()


def test_blocked_session_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 케이스 27 · 불변식 (FR-036): 비밀값 탐지 세션은 요약도 전송도 하지 않는다.
    # 차단 판정을 재해석하는 분기가 생기면 여기서 잡힌다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_plant_secret(config.watch_root)])
    _forbid_caller(monkeypatch)
    _forbid_sender(monkeypatch)

    outcome = run_session(config, paths, selection, lambda line: None, WITH_DISCORD)

    assert outcome.secrets_blocked is True
    assert outcome.discord_state == notify.DISCORD_SKIPPED
    doc = _session_doc(paths)
    assert doc["status"] == "failed"
    assert doc["error"] == "secrets_detected"
    assert _discord_fields(paths)["skip_reason"] == notify.SKIP_SECRETS_BLOCKED
    assert _discord_fields(paths)["requests"] == 0
    assert not paths.discord_payload_json.exists()


def test_dry_run_session_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 케이스 28: dry-run 은 성공적 생략이다 — "요약 없음"과 같은 분기로 묶지 않는다.
    config, paths, selection = _setup_session(tmp_path, dry_run=True)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    _forbid_caller(monkeypatch)
    _forbid_sender(monkeypatch)

    outcome = run_session(config, paths, selection, lambda line: None, WITH_DISCORD)

    assert outcome.discord_state == notify.DISCORD_SKIPPED
    doc = _session_doc(paths)
    assert doc["status"] == "completed"
    assert "error" not in doc
    assert _discord_fields(paths)["skip_reason"] == notify.SKIP_DRY_RUN
    assert not paths.discord_payload_json.exists()


def test_no_discord_session_renders_to_console_and_keeps_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 케이스 29 (판정 8번): --no-discord 세션도 실행자가 요약을 볼 수 있어야 하고,
    # 나중에 손으로 붙여 넣을 payload 가 남아야 한다.
    config, paths, selection = _setup_session(tmp_path, no_discord=True)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    caller = _FakeCaller([VALID_SUMMARY_TEXT])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)
    _forbid_sender(monkeypatch)
    lines: list[str] = []

    outcome = run_session(config, paths, selection, lines.append, WITH_DISCORD)

    assert outcome.discord_state == notify.DISCORD_SKIPPED
    doc = _session_doc(paths)
    assert doc["status"] == "completed"
    assert "error" not in doc
    assert _discord_fields(paths)["skip_reason"] == notify.SKIP_NO_DISCORD
    assert _discord_fields(paths)["requests"] == 0
    # 콘솔에 렌더된 요약 전문이 나간다 (전송 성공·실패·생략을 가리지 않는다).
    joined = "\n".join(lines)
    assert notify.TITLE_PREFIX + "검증" in joined
    assert "값을 바꾸는 변경을 했다." in joined
    joined.encode("cp949")
    # 전송 시도 전에 쓰므로 --no-discord 에서도 남는다 (PRD 12절 복구 원칙).
    payload = json.loads(paths.discord_payload_json.read_text(encoding="utf-8"))
    assert payload["schema_version"] == notify.NOTIFY_SCHEMA_VERSION == "1.2"
    assert payload["chunks"] == 1
    assert FAKE_WEBHOOK not in paths.discord_payload_json.read_text(encoding="utf-8")


def test_delivery_failure_preserves_every_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 케이스 30 (FR-034, PRD 12절): 전송이 실패해도 로컬 산출물은 전부 남고 상태 코드가
    # 기록된다. payload 는 전송 "전"에 썼으므로 수동 복사 경로가 성립한다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    caller = _FakeCaller([VALID_SUMMARY_TEXT])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)
    sender = _FakeSender([DiscordRequestError(notify.KIND_HTTP, http_status=404)])
    monkeypatch.setattr(watcher, "make_discord_sender", sender.factory)
    lines: list[str] = []

    outcome = run_session(config, paths, selection, lines.append, WITH_DISCORD)

    assert outcome.discord_state == notify.DISCORD_FAILED
    doc = _session_doc(paths)
    assert doc["status"] == "partial"
    # 4xx 와 5xx 를 error 값으로 가른다 — 종료 코드는 둘 다 1이다 (C-10).
    assert doc["error"] == "discord_http_404"
    assert doc["discord"] == {
        "delivered": False,
        "http_status": 404,
        "requests": 0,
        "chunks": 1,
        "skip_reason": None,
    }
    for path in (
        paths.baseline_dir / "a.py",
        paths.final_dir / "a.py",
        paths.final_diff,
        paths.stats_json,
        paths.summary_json,
        paths.discord_payload_json,
        paths.session_json,
    ):
        assert path.exists(), f"{path} 가 사라졌다"
    joined = "\n".join(lines)
    assert "Discord 전송에 실패했습니다" in joined
    assert FAKE_WEBHOOK not in joined
    joined.encode("cp949")


def test_session_without_summary_shows_stats_only_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 케이스 31 (판정 10번): 보낼 내용이 없는데 알림을 쓰는 것은 FR-052 와 어긋난다.
    # 대신 실행자에게 통계와 산출물 경로를 보여 준다. 4단계 error 는 그대로 남는다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    caller = _FakeCaller([LlmRequestError(KIND_TIMEOUT)])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)
    _forbid_sender(monkeypatch)
    lines: list[str] = []

    outcome = run_session(config, paths, selection, lines.append, WITH_DISCORD)

    assert outcome.summary_state == watcher.SUMMARY_FAILED
    assert outcome.discord_state == notify.DISCORD_SKIPPED
    doc = _session_doc(paths)
    assert doc["status"] == "partial"
    assert doc["error"] == "openai_timeout"
    assert _discord_fields(paths)["skip_reason"] == notify.SKIP_NO_SUMMARY
    assert _discord_fields(paths)["requests"] == 0
    assert not paths.discord_payload_json.exists()
    joined = "\n".join(lines)
    assert "[요약 없음] 검증" in joined
    assert "openai_timeout" in joined
    assert str(paths.root) in joined
    joined.encode("cp949")


def test_successful_delivery_counts_one_request_and_records_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 케이스 32: 정상 세션은 OpenAI 1회 + Discord 1회다 (FR-030, FR-052).
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    caller = _FakeCaller([VALID_SUMMARY_TEXT])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)
    sender = _FakeSender([204])
    monkeypatch.setattr(watcher, "make_discord_sender", sender.factory)
    lines: list[str] = []

    outcome = run_session(config, paths, selection, lines.append, WITH_DISCORD)

    assert len(caller.prompts) == 1
    assert len(sender.payloads) == 1
    assert sender.urls == [FAKE_WEBHOOK]
    assert outcome.discord_state == notify.DISCORD_SENT
    doc = _session_doc(paths)
    assert doc["status"] == "completed"
    assert "error" not in doc
    assert doc["discord"] == {
        "delivered": True,
        "http_status": 204,
        "requests": 1,
        "chunks": 1,
        "skip_reason": None,
    }
    content = sender.payloads[0]["content"]
    assert isinstance(content, str)
    assert content.startswith(notify.TITLE_PREFIX)
    # 산출물의 payload 와 실제로 나간 것이 같아야 수동 복사 경로가 의미를 갖는다.
    saved = json.loads(paths.discord_payload_json.read_text(encoding="utf-8"))
    assert saved["payloads"] == [{"content": content}]
    assert FAKE_WEBHOOK not in "\n".join(lines)


def test_delivered_payload_carries_no_diff_lines_or_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 불변식 (FR-051): 모델이 diff 원문을 어느 필드에 옮겨 와도 전송 payload 에는
    # `+`/`-` 로 시작하는 줄이 없고, 스키마 밖 필드(옛 evidence 등)는 실리지 않는다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    leaked = "+ String pw = user.getPassword();"
    payload_text = json.dumps(
        {
            "session_title": "검증",
            "summary": "- 값을 바꾸는 변경을 했다.",
            "change_stats": {"files_changed": 1, "added_lines": 1, "deleted_lines": 1},
            "keywords": [
                {
                    "term": "- 대입 연산자",
                    "concept": "--- a/a.py 를 고쳤다",
                    "syntax": "+=",
                    "group": "변수와 대입",
                    "confidence": "high",
                    # 스키마 밖 필드. 4단계 검증도 5단계 렌더도 읽지 않는다.
                    "evidence": f"--- a/a.py\n{leaked}",
                    "area": "핵심",
                }
            ],
            "questions_to_review": [],
            "risks_or_todos": [],
            "sensitive_data_detected": False,
        },
        ensure_ascii=False,
    )
    caller = _FakeCaller([payload_text])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)
    sender = _FakeSender([204])
    monkeypatch.setattr(watcher, "make_discord_sender", sender.factory)

    run_session(config, paths, selection, lambda line: None, WITH_DISCORD)

    [sent] = sender.payloads
    content = sent["content"]
    assert isinstance(content, str)
    assert find_diff_lines(content) == ()
    assert leaked not in content
    assert "getPassword" not in content
    assert "핵심" not in content
    # 산출물 쪽 payload 도 같은 보장을 받는다.
    saved = json.loads(paths.discord_payload_json.read_text(encoding="utf-8"))
    for entry in saved["payloads"]:
        assert find_diff_lines(entry["content"]) == ()
    # C-17 로 evidence 가 스키마에서 사라졌다 — 4단계 검증이 keywords[] 의 다섯 필드만
    # 통과시키므로 summary.json 에도 남지 않는다 (렌더 이전에 끊긴다).
    summary = json.loads(paths.summary_json.read_text(encoding="utf-8"))
    [keyword] = summary["summary"]["keywords"]
    assert set(keyword) == {"term", "concept", "syntax", "group", "confidence"}
    assert leaked not in paths.summary_json.read_text(encoding="utf-8")


def test_payload_write_failure_stops_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 설계 5.10: 로컬 보존이 성립하지 않는 상태로 외부에 나가지 않는다 (PRD 12절).
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    caller = _FakeCaller([VALID_SUMMARY_TEXT])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)
    _forbid_sender(monkeypatch)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("디스크 꽉 참")

    monkeypatch.setattr(watcher, "write_payload_json", boom)
    lines: list[str] = []

    outcome = run_session(config, paths, selection, lines.append, WITH_DISCORD)

    assert outcome.discord_state == notify.DISCORD_FAILED
    doc = _session_doc(paths)
    assert doc["status"] == "partial"
    assert doc["error"] == notify.ERROR_DISCORD_PAYLOAD_FAILED
    assert _discord_fields(paths)["requests"] == 0
    rows = [
        json.loads(line)
        for line in paths.errors_jsonl.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["stage"] for row in rows] == ["notify"]
    assert rows[0]["error"] == "OSError"
    assert any("외부 전송은 하지 않습니다" in line for line in lines)


def test_aborted_session_still_records_a_discord_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 전 세션에 discord 필드가 남는다 — "전송 0회"를 사후 집계로 증명하려면 abort
    # 세션에도 계수가 있어야 한다 (설계 5.5, 4단계 openai.calls: 0 과 같은 논리).
    config, paths, selection = _setup_session(tmp_path)

    def second_ctrl_c(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    monkeypatch.setattr(watcher, "wait_for_stability", second_ctrl_c)
    _forbid_sender(monkeypatch)

    outcome = run_session(config, paths, selection, lambda line: None, WITH_DISCORD)

    assert outcome.aborted is True
    assert outcome.discord_state == notify.DISCORD_NOT_RUN
    assert _discord_fields(paths) == {
        "delivered": False,
        "http_status": None,
        "requests": 0,
        "chunks": 0,
        "skip_reason": None,
    }


# ── 불변식 회귀 — 5단계 배선이 앞 단계의 계수·순서를 흔들지 않는다 ──────────────


def test_repeated_save_events_still_send_exactly_one_discord_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 불변식 (FR-030 저장 이벤트 중 0회 + FR-052 알림 예산): 저장이 몇 번이든 외부
    # 요청은 종료 시 OpenAI 1회 / Discord 1회다. 5단계 배선이 이벤트 루프 안으로
    # 내려오면 여기서 잡힌다.
    config, paths, selection = _setup_session(tmp_path)
    root = config.watch_root

    def second_change(debouncer: Debouncer) -> None:
        (root / "a.py").write_bytes(b"x = 3\n")
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=1.0))

    _script_loop(monkeypatch, [_change_a_py(root), second_change])
    caller = _FakeCaller([VALID_SUMMARY_TEXT])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)
    sender = _FakeSender([204])
    monkeypatch.setattr(watcher, "make_discord_sender", sender.factory)

    outcome = run_session(config, paths, selection, lambda line: None, WITH_DISCORD)

    assert outcome.logical_event_count == 2
    assert len(caller.prompts) == 1
    assert len(sender.payloads) == 1
    doc = _session_doc(paths)
    assert doc["openai"] == {
        "calls": 1,
        "retries": 0,
        "model": "fake-model",
        "request_id": "req-1",
    }
    assert _discord_fields(paths)["requests"] == 1
    # 불변식 (FR-020): 이 세션에는 git 저장소가 없다. 그래도 diff 와 집계가 만들어졌고
    # 그 결과가 전송 경로까지 흘러간다 — 전송 배선이 diff 출처를 바꾸지 않는다.
    assert not (root / ".git").exists()
    assert not (paths.root / ".git").exists()
    diff_text = paths.final_diff.read_text(encoding="utf-8")
    assert "+x = 3" in diff_text
    stats = json.loads(paths.stats_json.read_text(encoding="utf-8"))
    assert stats["totals"] == {
        "files_changed": 1,
        "added_lines": 1,
        "deleted_lines": 1,
        "skipped": 0,
    }


def test_fallback_session_caps_openai_at_two_calls_and_sends_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 불변식 (FR-030 상한 2회): 재시도까지 실패해도 호출은 2회에서 멈추고, 규칙 기반
    # 요약이 Discord 로 1회 나간다. FR-039 표시가 실제로 나간 본문에 있는지도 본다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    bad_text = "스키마에 맞지 않는 응답"
    caller = _FakeCaller([bad_text, bad_text])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)
    sender = _FakeSender([204])
    monkeypatch.setattr(watcher, "make_discord_sender", sender.factory)

    outcome = run_session(config, paths, selection, lambda line: None, WITH_DISCORD)

    assert outcome.summary_state == watcher.SUMMARY_FALLBACK
    assert len(caller.prompts) == 2
    assert len(sender.payloads) == 1
    doc = _session_doc(paths)
    assert doc["status"] == "completed"
    openai_fields = doc["openai"]
    assert isinstance(openai_fields, dict)
    assert openai_fields["calls"] == 2
    assert _discord_fields(paths) == {
        "delivered": True,
        "http_status": 204,
        "requests": 1,
        "chunks": 1,
        "skip_reason": None,
    }
    content = sender.payloads[0]["content"]
    assert isinstance(content, str)
    assert notify.RULE_BASED_NOTICE in content


def test_delivery_only_happens_after_a_clean_secret_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 불변식 (FR-036): 외부로 나간 세션은 반드시 스캔을 통과한 세션이다. 차단 쪽은
    # test_blocked_session_sends_nothing 이 보고, 여기서는 통과 쪽을 고정한다 —
    # 스캔을 건너뛰고 전송하는 경로가 생기면 redaction.json 이 없어 잡힌다.
    config, paths, selection = _setup_session(tmp_path)
    _script_loop(monkeypatch, [_change_a_py(config.watch_root)])
    caller = _FakeCaller([VALID_SUMMARY_TEXT])
    monkeypatch.setattr(watcher, "make_openai_caller", caller.factory)
    sender = _FakeSender([204])
    monkeypatch.setattr(watcher, "make_discord_sender", sender.factory)

    run_session(config, paths, selection, lambda line: None, WITH_DISCORD)

    assert len(sender.payloads) == 1
    redaction = json.loads(paths.redaction_json.read_text(encoding="utf-8"))
    assert redaction["policy"] == "clean"
    assert redaction["secrets_found"] == 0
    assert redaction["findings"] == []
    assert _session_doc(paths)["redaction"] == {
        "secrets_found": 0,
        "by_rule": {},
        "paths_relativized": True,
    }


# ── 순수 판정 함수 — summary_state 환원과 종료 매핑 표 전수 (설계 6.6, 기준 21) ──


def _outcome_of(source: str | None, error: str | None) -> SummarizeOutcome:
    return SummarizeOutcome(
        source=source,
        doc=None,
        calls=1,
        retries=0,
        request_id=None,
        model=None,
        error=error,
        http_status=None,
        llm_sensitive_flag=False,
        schema_failures=(),
    )


def test_resolve_summary_state_covers_all_branches() -> None:
    ok = _outcome_of(summarize.SOURCE_OPENAI, None)
    fallback = _outcome_of(summarize.SOURCE_RULE_BASED, None)
    failed = _outcome_of(None, "openai_timeout")

    assert resolve_summary_state(None, attempted=False, dry_run=False) == "not_run"
    assert resolve_summary_state(None, attempted=True, dry_run=True) == "dry_run"
    assert resolve_summary_state(None, attempted=True, dry_run=False) == "not_run"
    assert resolve_summary_state(failed, attempted=True, dry_run=False) == "failed"
    assert resolve_summary_state(fallback, attempted=True, dry_run=False) == "fallback"
    assert resolve_summary_state(ok, attempted=True, dry_run=False) == "ok"


def _delivered(status: int) -> DeliveryOutcome:
    return DeliveryOutcome(
        delivered=True, requests=1, chunks=1, http_status=status, error=None, skip_reason=None
    )


def _delivery_failed(error: str, http_status: int | None = None) -> DeliveryOutcome:
    return DeliveryOutcome(
        delivered=False,
        requests=0,
        chunks=1,
        http_status=http_status,
        error=error,
        skip_reason=None,
    )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # 4단계 설계 6.6 + 5단계 설계 5.6 매핑 표 전 행. 앞쪽 4단계 행이 그대로 남아
        # 있는 것 자체가 판정 1번(전송 배선이 기존 판정을 바꾸지 않는다)의 회귀 그물이다.
        # 종료 코드는 아래 run_watch 매핑 테스트가 고정한다.
        ({"no_change": True}, (SessionStatus.COMPLETED, None)),
        ({"secrets_blocked": True}, (SessionStatus.FAILED, "secrets_detected")),
        ({"diff_failed": True}, (SessionStatus.PARTIAL, "diff_failed")),
        ({"redaction_failed": True}, (SessionStatus.PARTIAL, "redaction_failed")),
        ({"summary_state": "dry_run"}, (SessionStatus.COMPLETED, None)),
        (
            {"summary_state": "failed", "summary_error": "openai_key_missing"},
            (SessionStatus.PARTIAL, "openai_key_missing"),
        ),
        (
            {"summary_state": "failed", "summary_error": "openai_auth"},
            (SessionStatus.PARTIAL, "openai_auth"),
        ),
        ({"summary_state": "ok", "no_discord": True}, (SessionStatus.COMPLETED, None)),
        ({"summary_state": "fallback", "no_discord": True}, (SessionStatus.COMPLETED, None)),
        # 전송 판정 지점에 도달하지 못한 경우(abort 등). 정상 경로에는 없다 (설계 4.1).
        (
            {"summary_state": "ok"},
            (SessionStatus.PARTIAL, notify.ERROR_DISCORD_NOT_ATTEMPTED),
        ),
        (
            {"summary_state": "fallback"},
            (SessionStatus.PARTIAL, notify.ERROR_DISCORD_NOT_ATTEMPTED),
        ),
        # 우선순위: 차단이 요약 실패보다 앞선다.
        (
            {"secrets_blocked": True, "summary_state": "failed", "summary_error": "openai_timeout"},
            (SessionStatus.FAILED, "secrets_detected"),
        ),
        # ── 5단계 설계 5.6: 전송 결과가 마지막 한 줄을 정한다 ──────────────────
        ({"summary_state": "ok", "discord": _delivered(204)}, (SessionStatus.COMPLETED, None)),
        (
            {"summary_state": "fallback", "discord": _delivered(200)},
            (SessionStatus.COMPLETED, None),
        ),
        (
            {"summary_state": "ok", "discord": _delivery_failed("discord_url_missing")},
            (SessionStatus.PARTIAL, "discord_url_missing"),
        ),
        (
            {"summary_state": "ok", "discord": _delivery_failed("discord_http_404", 404)},
            (SessionStatus.PARTIAL, "discord_http_404"),
        ),
        (
            {"summary_state": "ok", "discord": _delivery_failed("discord_http_503", 503)},
            (SessionStatus.PARTIAL, "discord_http_503"),
        ),
        (
            {"summary_state": "ok", "discord": _delivery_failed("discord_timeout")},
            (SessionStatus.PARTIAL, "discord_timeout"),
        ),
        (
            {"summary_state": "ok", "discord": _delivery_failed("discord_connection")},
            (SessionStatus.PARTIAL, "discord_connection"),
        ),
        # 생략 5갈래는 전부 앞쪽 분기가 먼저 잡는다 — 전송 결과를 넘겨도 값이 안 바뀐다.
        (
            {
                "summary_state": "ok",
                "no_discord": True,
                "discord": notify.skipped_delivery(notify.SKIP_NO_DISCORD),
            },
            (SessionStatus.COMPLETED, None),
        ),
        (
            {"no_change": True, "discord": notify.skipped_delivery(notify.SKIP_NO_CHANGE)},
            (SessionStatus.COMPLETED, None),
        ),
        (
            {
                "secrets_blocked": True,
                "discord": notify.skipped_delivery(notify.SKIP_SECRETS_BLOCKED),
            },
            (SessionStatus.FAILED, "secrets_detected"),
        ),
        (
            {
                "summary_state": "dry_run",
                "discord": notify.skipped_delivery(notify.SKIP_DRY_RUN),
            },
            (SessionStatus.COMPLETED, None),
        ),
        (
            {
                "summary_state": "failed",
                "summary_error": "openai_timeout",
                "discord": notify.skipped_delivery(notify.SKIP_NO_SUMMARY),
            },
            (SessionStatus.PARTIAL, "openai_timeout"),
        ),
    ],
)
def test_resolve_session_end_matches_design_table(
    overrides: dict[str, object], expected: tuple[SessionStatus, str | None]
) -> None:
    params: dict[str, object] = {
        "no_change": False,
        "secrets_blocked": False,
        "diff_failed": False,
        "redaction_failed": False,
        "summary_state": "not_run",
        "summary_error": None,
        "no_discord": False,
        "discord": None,
    }
    params.update(overrides)
    assert resolve_session_end(**params) == expected  # type: ignore[arg-type]


# ── cli 매핑 — summary_state → 콘솔·종료 코드 (설계 6.6 오른쪽 열) ─────────────


@pytest.mark.parametrize(
    ("summary_state", "no_discord", "discord_state", "expected_rc", "snippet"),
    [
        (watcher.SUMMARY_DRY_RUN, False, notify.DISCORD_SKIPPED, cli.EXIT_OK, "[DONE] dry-run"),
        (
            watcher.SUMMARY_NOT_RUN,
            False,
            notify.DISCORD_SKIPPED,
            cli.EXIT_RUNTIME,
            "요약 단계까지 진행하지 못했습니다",
        ),
        (
            watcher.SUMMARY_FAILED,
            False,
            notify.DISCORD_SKIPPED,
            cli.EXIT_RUNTIME,
            "요약을 만들지 못했습니다",
        ),
        (watcher.SUMMARY_OK, True, notify.DISCORD_SKIPPED, cli.EXIT_OK, "전송은 생략합니다"),
        (
            watcher.SUMMARY_FALLBACK,
            True,
            notify.DISCORD_SKIPPED,
            cli.EXIT_OK,
            "규칙 기반 요약을 저장했습니다",
        ),
        # ── 5단계: 전송 결과가 마지막 두 갈래를 정한다 (설계 5.6) ──────────────
        (
            watcher.SUMMARY_OK,
            False,
            notify.DISCORD_SENT,
            cli.EXIT_OK,
            "[DONE] 요약과 Discord 전송을 완료했습니다",
        ),
        (
            watcher.SUMMARY_FALLBACK,
            False,
            notify.DISCORD_SENT,
            cli.EXIT_OK,
            "[DONE] 요약과 Discord 전송을 완료했습니다",
        ),
        (
            watcher.SUMMARY_OK,
            False,
            notify.DISCORD_FAILED,
            cli.EXIT_RUNTIME,
            "[FAILED] Discord 전송에 실패했습니다.",
        ),
        # 전송 판정 지점에 도달하지 못한 세션도 성공으로 보지 않는다.
        (
            watcher.SUMMARY_OK,
            False,
            notify.DISCORD_NOT_RUN,
            cli.EXIT_RUNTIME,
            "[FAILED] Discord 전송에 실패했습니다.",
        ),
    ],
)
def test_run_watch_maps_summary_state_to_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    summary_state: str,
    no_discord: bool,
    discord_state: str,
    expected_rc: int,
    snippet: str,
) -> None:
    config, paths, selection = _setup_session(tmp_path, no_discord=no_discord)
    outcome = WatchOutcome(
        statuses={"a.py": "modified"},
        unstable=False,
        logical_event_count=1,
        no_change=False,
        aborted=False,
        secrets_blocked=False,
        summary_state=summary_state,
        discord_state=discord_state,
    )

    def fake_run_session(
        config_: WatchConfig,
        paths_: SessionPaths,
        selection_: Selection,
        emit: Callable[[str], None],
        secrets_: Secrets,
        model_: str,
    ) -> WatchOutcome:
        return outcome

    monkeypatch.setattr(cli, "run_session", fake_run_session)
    rc = cli.run_watch(cli.Preflight(config=config, selection=selection), paths, WITH_KEY)

    assert rc == expected_rc
    captured = capsys.readouterr()
    assert snippet in captured.out + captured.err
    # 기준 23: 새 콘솔 문자열 전부 cp949 콘솔에서 안 깨진다 (HANDOFF (다) 재발 방지).
    captured.out.encode("cp949")
    captured.err.encode("cp949")
