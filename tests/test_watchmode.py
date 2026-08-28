"""watchmode 모듈 — native / polling 판별 (FR-016).

설계 검증 기준 17~21. OS 호출(드라이브 종류)·환경변수는 전부 인자로 주입한다 —
실제 네트워크 드라이브·OneDrive 없이 판정 규칙만 결정적으로 검증한다.
"""

from pathlib import Path, PureWindowsPath

from class_watcher.watchmode import DRIVE_REMOTE, resolve_watch_mode

DRIVE_FIXED = 3  # 로컬 고정 디스크 — DRIVE_REMOTE 가 아닌 아무 값

# Path() resolves per host OS: on POSIX, Path("Z:/x").drive is "" and the drive-type
# branch is skipped entirely. Parse drive-letter fixtures explicitly so these run anywhere.


# ── 기준 17: --polling 강제 지정 → polling ───────────────────────────────────


def test_force_polling_wins(tmp_path: Path) -> None:
    decision = resolve_watch_mode(tmp_path, True, {}, lambda drive: DRIVE_FIXED)
    assert decision.mode == "polling"
    assert decision.reason is not None


# ── 기준 18: UNC 경로 → polling + 사유 문자열 (FR-016 자동 전환) ─────────────


def test_unc_path_switches_to_polling() -> None:
    decision = resolve_watch_mode(Path(r"\\server\share\lesson"), False, {}, None)
    assert decision.mode == "polling"
    assert decision.reason is not None
    assert "UNC" in decision.reason


def test_forward_slash_unc_also_detected() -> None:
    decision = resolve_watch_mode(Path("//server/share/lesson"), False, {}, None)
    assert decision.mode == "polling"


# ── 기준 19: 드라이브 종류가 DRIVE_REMOTE → polling ──────────────────────────


def test_remote_drive_switches_to_polling() -> None:
    queried: list[str] = []

    def drive_type_of(drive_root: str) -> int:
        queried.append(drive_root)
        return DRIVE_REMOTE

    decision = resolve_watch_mode(PureWindowsPath("Z:/lesson"), False, {}, drive_type_of)
    assert decision.mode == "polling"
    assert decision.reason is not None
    # 드라이브 루트("Z:\\" 또는 "Z:/")로 조회해야 한다 — 파일 경로 전체가 아니라.
    assert queried and queried[0].startswith("Z:")


def test_local_drive_stays_native() -> None:
    decision = resolve_watch_mode(
        PureWindowsPath("C:/lesson"), False, {}, lambda drive: DRIVE_FIXED
    )
    assert decision.mode == "native"
    assert decision.reason is None


# ── 기준 20: env 의 OneDrive 경로가 root 의 조상 → polling ───────────────────


def test_onedrive_ancestor_switches_to_polling(tmp_path: Path) -> None:
    onedrive = tmp_path / "OneDrive"
    root = onedrive / "수업" / "code"
    decision = resolve_watch_mode(root, False, {"OneDrive": str(onedrive)}, None)
    assert decision.mode == "polling"
    assert decision.reason is not None
    assert "OneDrive" in decision.reason


def test_onedrive_env_key_is_case_insensitive(tmp_path: Path) -> None:
    onedrive = tmp_path / "OneDrive"
    decision = resolve_watch_mode(
        onedrive / "code", False, {"ONEDRIVE": str(onedrive)}, None
    )
    assert decision.mode == "polling"


def test_sibling_of_onedrive_is_not_ancestor(tmp_path: Path) -> None:
    # "OneDrive-2" 같은 형제 디렉터리가 접두사 일치로 오탐되면 안 된다.
    onedrive = tmp_path / "OneDrive"
    decision = resolve_watch_mode(
        tmp_path / "OneDrive-2" / "code", False, {"OneDrive": str(onedrive)}, None
    )
    assert decision.mode == "native"


# ── 기준 21: 해당 없음 → native, reason=None. drive_type_of=None 도 동작 ──────


def test_plain_local_dir_is_native(tmp_path: Path) -> None:
    decision = resolve_watch_mode(tmp_path, False, {}, None)
    assert decision.mode == "native"
    assert decision.reason is None


def test_empty_onedrive_env_value_ignored(tmp_path: Path) -> None:
    decision = resolve_watch_mode(tmp_path, False, {"OneDrive": "  "}, None)
    assert decision.mode == "native"
