"""stability 모듈 — 종료 직전 안정화 판정 (FR-014 ②③④).

설계 검증 기준 8~12. clock·sleep·stat 을 전부 가짜로 주입한다 — sleep 이 가짜 시계를
전진시키는 방식이라 실제 시간을 전혀 읽지 않고, 어느 PC 에서든 같은 결과가 나온다.
"""

from class_watcher.stability import POLL_INTERVAL_MS, wait_for_stability


class FakeClock:
    """sleep 이 시계를 전진시키는 가짜 시간. 실제로는 전혀 기다리지 않는다."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# ── 기준 8: (size, mtime) 연속 300ms 불변이면 stable (FR-014 ②) ──────────────


def test_constant_stat_becomes_stable_after_stable_ms() -> None:
    fake = FakeClock()
    report = wait_for_stability(
        ["a.py"], lambda rel: (10, 1.0), fake.clock, fake.sleep, stable_ms=300, timeout_ms=3000
    )
    assert report.stable == ("a.py",)
    assert report.unstable == ()
    # 300ms 를 채우기 전에는 stable 로 판정할 수 없고, 채우면 바로 반환한다.
    assert 300 <= report.elapsed_ms < 3000


# ── 기준 9: 계속 바뀌는 stat 은 3000ms 에서 unstable 로 분류된다 (FR-014 ③) ──


def test_ever_changing_stat_is_unstable_at_timeout() -> None:
    fake = FakeClock()
    report = wait_for_stability(
        ["a.py"],
        lambda rel: (10, fake.now),  # 폴링마다 mtime 이 변한다 — 저장이 끝나지 않는 파일
        fake.clock,
        fake.sleep,
        stable_ms=300,
        timeout_ms=3000,
    )
    assert report.stable == ()
    assert report.unstable == ("a.py",)
    assert report.elapsed_ms >= 3000


# ── 기준 10: 안정 → 변동 → 안정이면 연속 카운터가 리셋된다 ────────────────────


def test_change_resets_stability_counter() -> None:
    fake = FakeClock()

    def stat_of(rel: str) -> tuple[int, float] | None:
        # 0.2초 시점에 값이 한 번 바뀐다 — 그 뒤로 다시 300ms 를 채워야 한다.
        return (10, 2.0) if fake.now >= 0.2 else (10, 1.0)

    report = wait_for_stability(
        ["a.py"], stat_of, fake.clock, fake.sleep, stable_ms=300, timeout_ms=3000
    )
    assert report.stable == ("a.py",)
    # 리셋 없이는 300ms 에 끝났을 것이다. 변동 시점(200ms) + 300ms 이상 걸려야 한다.
    assert report.elapsed_ms >= 500


# ── 기준 11: stat=None(삭제된 파일)은 즉시 stable — 기다릴 대상이 없다 ────────


def test_missing_file_is_immediately_stable() -> None:
    fake = FakeClock()
    calls: list[str] = []

    def stat_of(rel: str) -> tuple[int, float] | None:
        calls.append(rel)
        return None

    report = wait_for_stability(
        ["gone.py"], stat_of, fake.clock, fake.sleep, stable_ms=300, timeout_ms=3000
    )
    assert report.stable == ("gone.py",)
    assert report.unstable == ()
    assert report.elapsed_ms == 0.0
    assert calls == ["gone.py"]  # 재시도 없이 한 번만 본다


# ── 기준 12: 가짜 시계 기준으로 timeout_ms 를 넘겨 기다리지 않는다 (FR-014 ④ 근거) ──


def test_returns_within_timeout_budget() -> None:
    fake = FakeClock()
    report = wait_for_stability(
        ["a.py"],
        lambda rel: (10, fake.now),
        fake.clock,
        fake.sleep,
        stable_ms=300,
        timeout_ms=3000,
    )
    # 폴링 간격 하나 이상 초과하지 않는다 — 5초 종료 예산(FR-014 ④)의 근거.
    assert report.elapsed_ms <= 3000 + POLL_INTERVAL_MS


# ── 혼합: 안정·불안정이 섞여도 입력 순서가 보존된다 ──────────────────────────


def test_report_preserves_input_order() -> None:
    fake = FakeClock()

    def stat_of(rel: str) -> tuple[int, float] | None:
        if rel == "unstable.py":
            return (1, fake.now)
        return (1, 1.0)

    report = wait_for_stability(
        ["b.py", "unstable.py", "a.py"], stat_of, fake.clock, fake.sleep,
        stable_ms=300, timeout_ms=3000,
    )
    assert report.stable == ("b.py", "a.py")
    assert report.unstable == ("unstable.py",)
