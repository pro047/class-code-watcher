"""native / polling 감시 모드 판별 (FR-016).

네트워크·동기화 폴더에서는 OS 알림이 오지 않거나 늦게 온다. 판정은 순수 함수로 두고
드라이브 종류 조회 같은 OS 호출은 호출부가 주입한다.
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DriveTypeFn = Callable[[str], int]

# `추정`: Windows GetDriveTypeW 의 DRIVE_REMOTE. 이 저장소에 정본이 없다.
DRIVE_REMOTE = 4

# `추정`: OneDrive 가 설정하는 환경변수 이름. 대소문자 무시로 조회한다.
ONEDRIVE_ENV_KEYS: tuple[str, ...] = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


@dataclass(frozen=True)
class WatchModeDecision:
    mode: Literal["native", "polling"]
    reason: str | None = None


def _is_unc(root: Path) -> bool:
    return str(root).startswith(("\\\\", "//"))


def _normalized(path: Path) -> str:
    return os.path.normcase(str(path)).rstrip("\\/")


def _is_ancestor(ancestor: Path, child: Path) -> bool:
    top = _normalized(ancestor)
    bottom = _normalized(child)
    if not top:
        return False
    return bottom == top or bottom.startswith(top + os.sep) or bottom.startswith(top + "/")


def _is_remote_drive(root: Path, drive_type_of: DriveTypeFn | None) -> bool:
    """드라이브 문자가 없거나(UNC·상대경로) 조회 함수가 없으면 이 판정만 건너뛴다."""
    if drive_type_of is None or not root.drive:
        return False
    return drive_type_of(root.drive + os.sep) == DRIVE_REMOTE


def _onedrive_roots(env: Mapping[str, str]) -> list[Path]:
    wanted = {key.lower() for key in ONEDRIVE_ENV_KEYS}
    return [Path(value) for key, value in env.items() if key.lower() in wanted and value.strip()]


def resolve_watch_mode(
    root: Path,
    force_polling: bool,
    env: Mapping[str, str],
    drive_type_of: DriveTypeFn | None,
) -> WatchModeDecision:
    if force_polling:
        return WatchModeDecision(mode="polling", reason="--polling 지정")
    if _is_unc(root):
        return WatchModeDecision(mode="polling", reason="UNC 경로")
    if _is_remote_drive(root, drive_type_of):
        return WatchModeDecision(mode="polling", reason="네트워크 드라이브")
    for onedrive in _onedrive_roots(env):
        if _is_ancestor(onedrive, root):
            return WatchModeDecision(mode="polling", reason="OneDrive 동기화 폴더")
    return WatchModeDecision(mode="native", reason=None)
