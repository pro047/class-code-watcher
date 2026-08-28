"""감시 루프 오케스트레이션 — 이 기능의 부작용 층 (FR-011~FR-017, FR-035, FR-040/041).

watchdog·큐·시계·파일시스템을 만지는 코드를 여기에 모은다. 병합·안정화·모드 판별·행
구성은 전부 다른 모듈의 순수 함수라 이 파일에는 판정이 거의 없다. 유일하게 판정이 남는
compute_statuses 는 값만 받는 순수 함수로 떼어 뒀다.
"""

import ctypes
import json
import os
import queue
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingObserver

from .config import (
    DEFAULT_STABLE_MS,
    DEFAULT_STABLE_TIMEOUT_MS,
    FINALIZE_ENTER_BUDGET_MS,
    WatchConfig,
)
from .debounce import Debouncer, EventKind, LogicalEvent, RawEvent
from .eventlog import append_jsonl, event_row
from .selector import Selection, is_watched
from .session import SessionPaths, SessionStatus, transition, write_session_json
from .snapshot import SnapshotResult, hash_bytes, hash_map, snapshot_tree, write_manifest
from .stability import wait_for_stability
from .watchmode import DRIVE_REMOTE, resolve_watch_mode

# Ctrl+C 반응 지연의 상한. 큐 대기가 이보다 길면 인터럽트가 늦게 전달될 수 있다.
LOOP_TIMEOUT_S = 0.2

STATUS_UNCHANGED = "unchanged"
STATUS_MODIFIED = "modified"
STATUS_ADDED = "added"
STATUS_DELETED = "deleted"
# 중단된 세션 전용. baseline↔final 비교를 못 했으므로 어느 상태도 주장하지 않는다.
STATUS_UNKNOWN = "unknown"

# 변경이 있는 세션의 종료 사유 — 요약·전송 단계가 아직 없다.
PENDING_PIPELINE_ERROR = "summary_pipeline_not_implemented"
ABORTED_ERROR = "aborted_by_user"


@dataclass(frozen=True)
class WatchOutcome:
    statuses: Mapping[str, str]
    unstable: bool
    logical_event_count: int
    no_change: bool
    aborted: bool


def compute_statuses(
    baseline: Mapping[str, str], final: Mapping[str, str]
) -> dict[str, str]:
    """순수 — baseline/final 해시 맵을 파일별 상태로 환원한다 (FR-017, FR-035 판정 근거)."""
    statuses: dict[str, str] = {}
    for rel_path in sorted(set(baseline) | set(final)):
        before = baseline.get(rel_path)
        after = final.get(rel_path)
        if before is None:
            statuses[rel_path] = STATUS_ADDED
        elif after is None:
            statuses[rel_path] = STATUS_DELETED
        elif before == after:
            statuses[rel_path] = STATUS_UNCHANGED
        else:
            statuses[rel_path] = STATUS_MODIFIED
    return statuses


def unknown_file_statuses(selected: Sequence[PurePosixPath]) -> list[dict[str, str]]:
    """순수 — 중단된 세션의 watched_files.

    시작 시점 doc 은 전 파일을 unchanged 로 적어 둔다. 중단되면 그 값이 그대로 굳어
    "변경 없음"이라고 잘못 말하게 되므로 판정 불가로 낮춘다.
    """
    return [{"path": str(path), "status": STATUS_UNKNOWN} for path in selected]


def is_no_change(statuses: Mapping[str, str]) -> bool:
    return all(status == STATUS_UNCHANGED for status in statuses.values())


def _drive_type_of(drive_root: str) -> int:
    """`추정`: Windows GetDriveTypeW. 조회 실패는 판정 불가(0)로 환원해 native 로 흘린다."""
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return 0
    try:
        return int(windll.kernel32.GetDriveTypeW(drive_root))
    except (OSError, AttributeError, ValueError):
        return 0


def _make_observer(mode: str) -> BaseObserver:
    return PollingObserver() if mode == "polling" else Observer()


class _Handler(FileSystemEventHandler):
    """watchdog 스레드에서 도는 유일한 코드. 필터링과 큐 적재만 하고 판단하지 않는다."""

    def __init__(
        self,
        root: Path,
        include: tuple[str, ...],
        exclude: tuple[str, ...],
        sink: "queue.Queue[RawEvent]",
        clock: Callable[[], float],
    ) -> None:
        self._root = root
        self._include = include
        self._exclude = exclude
        self._sink = sink
        self._clock = clock

    def _offer(self, raw_path: str | bytes, kind: EventKind) -> None:
        try:
            rel = Path(os.fsdecode(raw_path)).relative_to(self._root)
        except ValueError:
            # 감시 루트 밖의 경로. watchdog 이 루트 자신의 이벤트를 줄 때도 여기로 온다.
            return
        posix = PurePosixPath(rel.as_posix())
        if not is_watched(posix, self._include, self._exclude):
            return
        self._sink.put(RawEvent(rel_path=posix.as_posix(), kind=kind, at=self._clock()))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._offer(event.src_path, "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._offer(event.src_path, "modified")

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._offer(event.src_path, "deleted")

    def on_moved(self, event: FileSystemEvent) -> None:
        # 원자적 저장(임시파일 → rename)이 여기로 온다. 두 경로를 각각 따로 본다 (FR-013).
        if event.is_directory:
            return
        self._offer(event.src_path, "deleted")
        self._offer(event.dest_path, "moved")


def _format_size(size: int | None) -> str:
    if size is None:
        return "-"
    if size < 1024:
        return f"{size} B"
    return f"{size / 1024:.1f} KB"


def _read_digest(root: Path, rel_path: str) -> tuple[str | None, int | None]:
    """이벤트 로그용 해시·크기. 이미 사라진 파일은 값 없이 지나간다."""
    try:
        data = (root / rel_path).read_bytes()
    except OSError:
        return None, None
    return hash_bytes(data), len(data)


class _Session:
    """run_session 의 지역 상태를 담는 상자. 외부에 노출하지 않는다."""

    def __init__(
        self,
        config: WatchConfig,
        paths: SessionPaths,
        selection: Selection,
        emit: Callable[[str], None],
    ) -> None:
        self.config = config
        self.paths = paths
        self.selection = selection
        self.emit = emit
        self.doc: dict[str, object] = {}
        self.baseline_hashes: dict[str, str] = {}
        self.observed: set[str] = set()
        self.event_count = 0
        self.history_seq = 0

    def write_status(self, status: SessionStatus, **fields: object) -> None:
        self.doc = transition(self.doc, status, **fields)
        write_session_json(self.paths, self.doc)

    def handle(self, logical: LogicalEvent) -> None:
        self.event_count += 1
        self.observed.add(logical.rel_path)

        if logical.kind == "deleted":
            sha256, size = None, None
        else:
            sha256, size = _read_digest(self.config.watch_root, logical.rel_path)

        append_jsonl(
            self.paths.events_jsonl,
            event_row(
                logical,
                wall_time=datetime.now().astimezone(),
                sha256=sha256,
                size=size,
            ),
        )

        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        digest = f"{sha256[:4]}..." if sha256 else "-"
        suffix = " (신규)" if logical.rel_path not in self.baseline_hashes else ""
        label = "삭제 감지" if logical.kind == "deleted" else "변경 감지"
        self.emit(
            f"[{stamp}] {label}  {logical.rel_path}  hash={digest}  "
            f"{_format_size(size)}{suffix}"
        )

        if self.config.history and logical.kind != "deleted":
            self.history_seq += 1
            slot = self.paths.history_dir / f"{self.history_seq:04d}"
            result = snapshot_tree(
                self.config.watch_root, [PurePosixPath(logical.rel_path)], slot
            )
            write_manifest(slot, result)


def _drain_queue(sink: "queue.Queue[RawEvent]", debouncer: Debouncer) -> None:
    """큐를 짧게 기다렸다가 쌓인 것을 한 번에 흡수한다."""
    deadline = debouncer.next_deadline()
    timeout = LOOP_TIMEOUT_S
    if deadline is not None:
        timeout = max(0.0, min(LOOP_TIMEOUT_S, deadline - time.monotonic()))
    try:
        if timeout > 0:
            debouncer.observe(sink.get(timeout=timeout))
        while True:
            debouncer.observe(sink.get_nowait())
    except queue.Empty:
        return


def run_session(
    config: WatchConfig,
    paths: SessionPaths,
    selection: Selection,
    emit: Callable[[str], None],
) -> WatchOutcome:
    """감시 세션 전체. session.json 의 상태 전이도 여기서 밟는다 (FR-040)."""
    state = _Session(config, paths, selection, emit)
    state.doc = json.loads(paths.session_json.read_text(encoding="utf-8"))

    decision = resolve_watch_mode(config.watch_root, config.polling, os.environ, _drive_type_of)
    state.doc["watch_mode"] = decision.mode
    if decision.reason is not None:
        state.doc["watch_mode_reason"] = decision.reason
        if not config.polling:
            emit(f"[WATCH] 폴링 모드로 자동 전환합니다: {decision.reason}")
    write_session_json(paths, state.doc)

    baseline = snapshot_tree(config.watch_root, selection.selected, paths.baseline_dir)
    write_manifest(paths.baseline_dir, baseline)
    state.baseline_hashes = hash_map(baseline)
    emit(f"[OK] baseline 저장: {paths.baseline_dir}")

    state.write_status(SessionStatus.WATCHING)
    emit("[WATCHING] 변경 감시 중... 종료하려면 Ctrl+C")

    sink: queue.Queue[RawEvent] = queue.Queue()
    debouncer = Debouncer(config.debounce_ms, known_paths=state.baseline_hashes)
    handler = _Handler(config.watch_root, config.include, config.exclude, sink, time.monotonic)
    observer = _make_observer(decision.mode)
    observer.schedule(handler, str(config.watch_root), recursive=True)
    observer.start()

    try:
        while True:
            _drain_queue(sink, debouncer)
            for logical in debouncer.due(time.monotonic()):
                state.handle(logical)
    except KeyboardInterrupt:
        emit("")
    finally:
        observer.stop()
        observer.join(timeout=FINALIZE_ENTER_BUDGET_MS / 1000.0)

    return _finalize(state, sink, debouncer)


def _finalize(
    state: _Session, sink: "queue.Queue[RawEvent]", debouncer: Debouncer
) -> WatchOutcome:
    """FR-014 의 종료 흐름. 두 번째 Ctrl+C 는 여기서 잡아 산출물을 남긴 채 끝낸다."""
    state.write_status(SessionStatus.FINALIZING)
    state.emit("[FINALIZING] 감시 중지 · debounce flush · 파일 안정화 확인")

    aborted = False
    unstable = False
    statuses: dict[str, str] = {}
    try:
        while True:
            try:
                debouncer.observe(sink.get_nowait())
            except queue.Empty:
                break
        for logical in debouncer.flush():
            state.handle(logical)

        report = wait_for_stability(
            sorted(state.observed),
            lambda rel: _stat_of(state.config.watch_root, rel),
            time.monotonic,
            time.sleep,
            DEFAULT_STABLE_MS,
            DEFAULT_STABLE_TIMEOUT_MS,
        )
        unstable = bool(report.unstable)

        targets = sorted(set(state.baseline_hashes) | state.observed)
        final = snapshot_tree(
            state.config.watch_root,
            [PurePosixPath(rel) for rel in targets],
            state.paths.final_dir,
        )
        final = SnapshotResult(metas=final.metas, unstable=unstable)
        write_manifest(state.paths.final_dir, final)
        statuses = compute_statuses(state.baseline_hashes, hash_map(final))
    except KeyboardInterrupt:
        aborted = True

    ended_at = datetime.now().astimezone().isoformat()
    if aborted:
        state.write_status(
            SessionStatus.FAILED,
            ended_at=ended_at,
            error=ABORTED_ERROR,
            watched_files=unknown_file_statuses(state.selection.selected),
        )
        state.emit("[ABORTED] 두 번째 종료 요청. 지금까지의 산출물만 남깁니다.")
        return WatchOutcome(
            statuses=statuses,
            unstable=unstable,
            logical_event_count=state.event_count,
            no_change=False,
            aborted=True,
        )

    no_change = is_no_change(statuses)
    changed = sum(1 for status in statuses.values() if status != STATUS_UNCHANGED)
    fields: dict[str, object] = {
        "ended_at": ended_at,
        "watched_files": [
            {"path": rel, "status": status} for rel, status in statuses.items()
        ],
        "change_stats": {"files_changed": changed, "events": state.event_count},
        "no_change": no_change,
    }
    if no_change:
        state.write_status(SessionStatus.COMPLETED, **fields)
    else:
        # 요약~전송(2~5단계)이 아직 없어 세션을 완료로 부를 수 없다 (PRD 10.3 코드 1).
        state.write_status(SessionStatus.PARTIAL, **fields, error=PENDING_PIPELINE_ERROR)

    return WatchOutcome(
        statuses=statuses,
        unstable=unstable,
        logical_event_count=state.event_count,
        no_change=no_change,
        aborted=False,
    )


def _stat_of(root: Path, rel_path: str) -> tuple[int, float] | None:
    try:
        stat = (root / rel_path).stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime)


__all__ = [
    "DRIVE_REMOTE",
    "WatchOutcome",
    "compute_statuses",
    "is_no_change",
    "run_session",
]
