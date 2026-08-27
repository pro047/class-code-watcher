"""종료 직전 파일 안정화 대기 (FR-014 ②③).

에디터가 저장을 끝내기 전에 읽으면 반쪽 파일이 final 로 들어간다. (size, mtime) 이
연속으로 불변인지 확인한 뒤에만 읽는다. 시계·sleep·stat 을 전부 주입받아 실제 시간을
쓰지 않고도 검증할 수 있게 둔다.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

StatFn = Callable[[str], tuple[int, float] | None]

# 안정 판정 간격보다 충분히 촘촘해야 300ms 경계를 놓치지 않는다.
POLL_INTERVAL_MS = 50


@dataclass(frozen=True)
class StabilityReport:
    stable: tuple[str, ...]
    unstable: tuple[str, ...]
    elapsed_ms: float


def wait_for_stability(
    rel_paths: Sequence[str],
    stat_of: StatFn,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    stable_ms: int = 300,
    timeout_ms: int = 3000,
) -> StabilityReport:
    started = clock()
    if not rel_paths:
        return StabilityReport(stable=(), unstable=(), elapsed_ms=0.0)

    stable_s = max(0, stable_ms) / 1000.0
    timeout_s = max(0, timeout_ms) / 1000.0
    poll_s = POLL_INTERVAL_MS / 1000.0

    pending = list(rel_paths)
    stable: list[str] = []
    # rel_path -> (마지막 관측값, 그 값이 처음 관측된 시각)
    observed: dict[str, tuple[tuple[int, float], float]] = {}

    while pending:
        now = clock()
        still_pending: list[str] = []
        for rel_path in pending:
            current = stat_of(rel_path)
            if current is None:
                # 이미 사라진 파일은 더 기다릴 대상이 없다.
                stable.append(rel_path)
                continue
            previous = observed.get(rel_path)
            if previous is None or previous[0] != current:
                observed[rel_path] = (current, now)
                still_pending.append(rel_path)
                continue
            if now - previous[1] >= stable_s:
                stable.append(rel_path)
            else:
                still_pending.append(rel_path)
        pending = still_pending
        if not pending:
            break
        if clock() - started >= timeout_s:
            break
        sleep(poll_s)

    order = {rel_path: index for index, rel_path in enumerate(rel_paths)}
    return StabilityReport(
        stable=tuple(sorted(stable, key=lambda item: order[item])),
        unstable=tuple(sorted(pending, key=lambda item: order[item])),
        elapsed_ms=(clock() - started) * 1000.0,
    )
