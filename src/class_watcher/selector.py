"""감시 대상 파일 선택.

판정(`is_watched`)과 순회(`scan_files`)를 분리해 둔다. 후속 기능의 watchdog 이벤트 필터도
같은 판정 함수를 재사용해야 시작 시 목록과 감시 중 필터가 어긋나지 않는다.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePath, PurePosixPath


@dataclass(frozen=True)
class Selection:
    """감시 대상 산출 결과."""

    selected: tuple[PurePosixPath, ...]
    excluded_count: int


def _matches_any(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch(name, pattern) for pattern in patterns)


def is_watched(rel_path: PurePath, include: Sequence[str], exclude: Sequence[str]) -> bool:
    """watch_root 기준 상대 경로 하나에 대한 순수 판정.

    exclude 는 경로의 어느 세그먼트와 맞아도 제외한다 — `node_modules/...` 하위 전체를
    한 번에 걷어내기 위해서다.
    """
    parts = rel_path.parts
    if not parts:
        return False
    if any(_matches_any(segment, exclude) for segment in parts):
        return False
    return _matches_any(parts[-1], include)


def scan_files(root: Path, include: Sequence[str], exclude: Sequence[str]) -> Selection:
    """root 를 훑어 대상 목록을 만든다. 이 모듈에서 파일시스템을 만지는 유일한 함수다."""
    selected: list[PurePosixPath] = []
    excluded_count = 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        # 제외 디렉터리와 심볼릭 링크에는 아예 내려가지 않는다. 링크를 따라가면 감시 루트
        # 밖으로 새어 나갈 수 있다 (PRD 13.3 위협 5).
        dirnames[:] = [
            name
            for name in dirnames
            if not _matches_any(name, exclude) and not (current / name).is_symlink()
        ]

        for filename in sorted(filenames):
            rel = PurePosixPath((current / filename).relative_to(root).as_posix())
            if is_watched(rel, include, exclude):
                selected.append(rel)
            else:
                excluded_count += 1

    return Selection(selected=tuple(sorted(selected)), excluded_count=excluded_count)
