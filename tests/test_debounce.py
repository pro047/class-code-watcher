"""debounce 모듈 — 저장 이벤트 병합 판정 (FR-012, FR-013, FR-014 ①, FR-017).

설계 검증 기준 1~7. 시각은 전부 RawEvent.at / due(now) 인자로 주입한다 — 실제 시계를
읽는 코드가 없어야 CI 와 다른 PC 에서 같은 결과가 나온다.
"""

from class_watcher.debounce import Debouncer, RawEvent

WINDOW_MS = 750
WINDOW_S = WINDOW_MS / 1000.0


def _debouncer(*known: str) -> Debouncer:
    return Debouncer(WINDOW_MS, known_paths=known)


# ── 기준 1: 창 내 동일 경로 이벤트는 논리 이벤트 1건으로 합쳐진다 (FR-012) ────


def test_three_modified_in_window_merge_to_one() -> None:
    debouncer = _debouncer("a.py")
    for at in (0.0, 0.2, 0.4):
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=at))

    events = debouncer.due(0.4 + WINDOW_S)
    assert len(events) == 1
    event = events[0]
    assert event.rel_path == "a.py"
    assert event.kind == "modified"
    assert event.count == 3
    assert event.first_at == 0.0
    assert event.last_at == 0.4


# ── 기준 2: 서로 다른 경로는 병합되지 않는다 ─────────────────────────────────


def test_distinct_paths_do_not_merge() -> None:
    debouncer = _debouncer("a.py", "b.py")
    debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))
    debouncer.observe(RawEvent(rel_path="b.py", kind="modified", at=0.1))

    events = debouncer.due(10.0)
    assert [(event.rel_path, event.count) for event in events] == [("a.py", 1), ("b.py", 1)]


# ── 기준 3: 타이머는 마지막 이벤트 기준으로 연장된다 ─────────────────────────


def test_window_extends_from_last_event() -> None:
    debouncer = _debouncer("a.py")
    # 0.7초 간격 연타 — 창(0.75초)이 만료되기 직전마다 새 이벤트가 온다.
    for at in (0.0, 0.7, 1.4):
        debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=at))

    # 첫 이벤트 기준으로는 이미 만료됐지만 마지막 이벤트 기준으로는 아직이다.
    assert debouncer.due(1.4 + WINDOW_S - 0.01) == []
    events = debouncer.due(1.4 + WINDOW_S)
    assert len(events) == 1
    assert events[0].count == 3


def test_next_deadline_tracks_last_event() -> None:
    debouncer = _debouncer("a.py")
    assert debouncer.next_deadline() is None
    debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=1.0))
    assert debouncer.next_deadline() == 1.0 + WINDOW_S
    debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=1.5))
    assert debouncer.next_deadline() == 1.5 + WINDOW_S


# ── 기준 4: 삭제 후 창 내 재생성은 같은 파일의 수정으로 재연결한다 (PRD 12절) ─


def test_deleted_then_created_in_window_becomes_modified() -> None:
    debouncer = _debouncer("a.py")
    debouncer.observe(RawEvent(rel_path="a.py", kind="deleted", at=0.0))
    debouncer.observe(RawEvent(rel_path="a.py", kind="created", at=0.1))

    events = debouncer.flush()
    assert [event.kind for event in events] == ["modified"]


def test_deleted_then_moved_in_window_becomes_modified() -> None:
    # 원자적 저장(임시파일 → rename)이 삭제 직후 moved 로 도착하는 경우 (FR-013).
    debouncer = _debouncer("a.py")
    debouncer.observe(RawEvent(rel_path="a.py", kind="deleted", at=0.0))
    debouncer.observe(RawEvent(rel_path="a.py", kind="moved", at=0.1))

    assert [event.kind for event in debouncer.flush()] == ["modified"]


# ── 기준 5: baseline 에 없던 경로는 created 로 승격된다 (FR-017, FR-013) ──────


def test_unknown_path_modified_becomes_created() -> None:
    debouncer = _debouncer("known.py")
    debouncer.observe(RawEvent(rel_path="new.py", kind="modified", at=0.0))
    assert [event.kind for event in debouncer.flush()] == ["created"]


def test_unknown_path_moved_becomes_created() -> None:
    debouncer = _debouncer("known.py")
    debouncer.observe(RawEvent(rel_path="new.py", kind="moved", at=0.0))
    assert [event.kind for event in debouncer.flush()] == ["created"]


def test_known_path_moved_is_modified() -> None:
    debouncer = _debouncer("a.py")
    debouncer.observe(RawEvent(rel_path="a.py", kind="moved", at=0.0))
    assert [event.kind for event in debouncer.flush()] == ["modified"]


# ── 병합 우선순위: 마지막이 deleted 면 deleted, created 는 유지된다 ───────────


def test_created_then_modified_stays_created() -> None:
    debouncer = _debouncer()
    debouncer.observe(RawEvent(rel_path="new.py", kind="created", at=0.0))
    debouncer.observe(RawEvent(rel_path="new.py", kind="modified", at=0.1))
    assert [event.kind for event in debouncer.flush()] == ["created"]


def test_created_then_deleted_ends_as_deleted() -> None:
    # IMPL 2절 결정: 규칙이 겹칠 때 "마지막 kind 가 deleted 면 deleted" 를 먼저 적용한다.
    debouncer = _debouncer()
    debouncer.observe(RawEvent(rel_path="new.py", kind="created", at=0.0))
    debouncer.observe(RawEvent(rel_path="new.py", kind="deleted", at=0.1))
    assert [event.kind for event in debouncer.flush()] == ["deleted"]


# ── 기준 6: flush 는 창 만료와 무관하게 pending 전부를 방출한다 (FR-014 ①) ───


def test_flush_emits_everything_immediately() -> None:
    debouncer = _debouncer("a.py", "b.py")
    debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.0))
    debouncer.observe(RawEvent(rel_path="b.py", kind="deleted", at=0.05))

    events = debouncer.flush()
    assert {(event.rel_path, event.kind) for event in events} == {
        ("a.py", "modified"),
        ("b.py", "deleted"),
    }
    # flush 후에는 pending 이 없다 — 두 번 방출되지 않는다.
    assert debouncer.flush() == []
    assert debouncer.next_deadline() is None


# ── 기준 7: due 는 주입된 now 로만 판정한다 ──────────────────────────────────


def test_due_uses_injected_clock_only() -> None:
    # 실제 시계와 무관한 큰 at 값 — 주입된 now 가 창을 넘기 전에는 절대 방출되지 않는다.
    debouncer = _debouncer("a.py")
    debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=1_000_000.0))
    assert debouncer.due(1_000_000.0 + WINDOW_S - 0.001) == []
    assert len(debouncer.due(1_000_000.0 + WINDOW_S)) == 1


def test_due_emits_in_last_at_order() -> None:
    debouncer = _debouncer("a.py", "b.py")
    debouncer.observe(RawEvent(rel_path="b.py", kind="modified", at=0.0))
    debouncer.observe(RawEvent(rel_path="a.py", kind="modified", at=0.5))
    events = debouncer.due(10.0)
    # (last_at, rel_path) 정렬 — 로그·history 순서가 결정적이어야 한다.
    assert [event.rel_path for event in events] == ["b.py", "a.py"]
