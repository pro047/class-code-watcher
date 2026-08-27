"""저장 이벤트 debounce 병합 (FR-012, FR-013, FR-017).

시계를 주입받는 순수 상태 기계다. watchdog 스레드도 메인 루프도 여기서 시간을 읽지
않는다 — 그래야 가짜 시각으로 병합 규칙을 결정적으로 검증할 수 있다.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

EventKind = Literal["created", "modified", "moved", "deleted"]


@dataclass(frozen=True)
class RawEvent:
    rel_path: str
    kind: EventKind
    at: float


@dataclass(frozen=True)
class LogicalEvent:
    rel_path: str
    kind: EventKind
    first_at: float
    last_at: float
    count: int


@dataclass
class _Pending:
    kind: EventKind
    first_at: float
    last_at: float
    count: int


class Debouncer:
    """동일 경로의 연속 이벤트를 하나로 합친다.

    known_paths 는 baseline 에 있던 경로 집합이다. 여기 없는 경로의 변경은 신규 편입이라
    created 로 승격한다 (FR-017).
    """

    def __init__(self, window_ms: int, *, known_paths: Iterable[str] = ()) -> None:
        self._window = max(0, window_ms) / 1000.0
        self._known = set(known_paths)
        self._pending: dict[str, _Pending] = {}

    def observe(self, event: RawEvent) -> None:
        current = self._pending.get(event.rel_path)
        if current is None:
            self._pending[event.rel_path] = _Pending(
                kind=self._initial_kind(event),
                first_at=event.at,
                last_at=event.at,
                count=1,
            )
            return
        current.kind = self._merge(current.kind, event.kind, event.rel_path)
        # 타이머를 마지막 이벤트 기준으로 연장한다 — 연타 저장이 끝날 때까지 기다린다.
        current.last_at = max(current.last_at, event.at)
        current.count += 1

    def due(self, now: float) -> list[LogicalEvent]:
        ready = [
            rel_path
            for rel_path, pending in self._pending.items()
            if pending.last_at + self._window <= now
        ]
        return self._emit(ready)

    def flush(self) -> list[LogicalEvent]:
        """FR-014 ① — 남은 pending 을 창 만료와 무관하게 즉시 방출한다."""
        return self._emit(list(self._pending))

    def next_deadline(self) -> float | None:
        if not self._pending:
            return None
        return min(pending.last_at for pending in self._pending.values()) + self._window

    def _emit(self, rel_paths: list[str]) -> list[LogicalEvent]:
        events = []
        for rel_path in rel_paths:
            pending = self._pending.pop(rel_path)
            events.append(
                LogicalEvent(
                    rel_path=rel_path,
                    kind=pending.kind,
                    first_at=pending.first_at,
                    last_at=pending.last_at,
                    count=pending.count,
                )
            )
        events.sort(key=lambda event: (event.last_at, event.rel_path))
        return events

    def _initial_kind(self, event: RawEvent) -> EventKind:
        if event.kind in ("created", "deleted"):
            return event.kind
        # moved 도착 경로와 modified 는 같은 취급이되, baseline 에 없던 경로면 신규다.
        return "modified" if event.rel_path in self._known else "created"

    def _merge(self, previous: EventKind, incoming: EventKind, rel_path: str) -> EventKind:
        # 순서가 규칙이다: 삭제로 끝났으면 앞이 무엇이든 삭제다.
        if incoming == "deleted":
            return "deleted"
        if previous == "created":
            return "created"
        # 삭제 후 창 안에서 되살아나면 같은 파일의 수정으로 재연결한다 (PRD 12절).
        if previous == "deleted":
            return "modified"
        return "modified" if rel_path in self._known else "created"
