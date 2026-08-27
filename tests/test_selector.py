"""selector 모듈 — allowlist·제외 패턴 판정과 트리 스캔 (FR-005)."""

from pathlib import Path, PurePosixPath

import pytest

from class_watcher.config import DEFAULT_EXCLUDE, DEFAULT_INCLUDE
from class_watcher.selector import is_watched, scan_files

# ── FR-005: 기본 제외 디렉터리 — 세그먼트 일치로 하위 전체 제외 ──────────────


@pytest.mark.parametrize(
    "rel",
    [
        "node_modules/x/y.js",
        ".git/config",
        "build/a.py",
        "__pycache__/m.pyc",
        ".venv/lib/x.py",
        "target/App.class",
        "dist/app.js",
    ],
)
def test_default_excluded_segments(rel: str) -> None:
    assert is_watched(PurePosixPath(rel), DEFAULT_INCLUDE, DEFAULT_EXCLUDE) is False


def test_excluded_segment_in_middle_of_path() -> None:
    rel = PurePosixPath("src/node_modules/pkg/index.js")
    assert is_watched(rel, DEFAULT_INCLUDE, DEFAULT_EXCLUDE) is False


# ── FR-005: 기본 allowlist 판정 ──────────────────────────────────────────────


def test_allowlisted_extension_is_watched() -> None:
    rel = PurePosixPath("src/UserService.java")
    assert is_watched(rel, DEFAULT_INCLUDE, DEFAULT_EXCLUDE) is True


def test_non_allowlisted_extension_is_not_watched() -> None:
    rel = PurePosixPath("src/logo.png")
    assert is_watched(rel, DEFAULT_INCLUDE, DEFAULT_EXCLUDE) is False


def test_korean_and_space_paths_are_judged_normally() -> None:
    # PRD 14.1 체크리스트: 한글 파일명·공백 포함 경로.
    assert is_watched(PurePosixPath("소스 코드/수업 메모.py"), DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    assert is_watched(PurePosixPath("한글 파일.java"), DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    assert not is_watched(
        PurePosixPath("소스 코드/스크린샷 1.png"), DEFAULT_INCLUDE, DEFAULT_EXCLUDE
    )


def test_exclude_pattern_applies_to_filename_segment() -> None:
    # exclude 는 세그먼트별 fnmatch 라 파일명 패턴(*.min.js)도 동작한다 (확정된 동작 계약).
    rel = PurePosixPath("src/app.min.js")
    assert is_watched(rel, DEFAULT_INCLUDE, ("*.min.js",)) is False
    assert is_watched(rel, DEFAULT_INCLUDE, DEFAULT_EXCLUDE) is True


def test_empty_path_is_not_watched() -> None:
    assert is_watched(PurePosixPath(), DEFAULT_INCLUDE, DEFAULT_EXCLUDE) is False


# ── FR-005: scan_files — 제외 디렉터리에 하강하지 않는다 ─────────────────────


def _build_tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "src" / "유틸 함수.py").write_text("# 한글\n", encoding="utf-8")
    (root / "readme.md").write_text("# 문서\n", encoding="utf-8")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("x\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "build").mkdir()
    (root / "build" / "out.py").write_text("y\n", encoding="utf-8")


def test_scan_files_skips_excluded_directories(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    selection = scan_files(tmp_path, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    assert selection.selected == (
        PurePosixPath("src/main.py"),
        PurePosixPath("src/유틸 함수.py"),
    )
    # 제외 디렉터리 하위(*.js, *.py 포함)는 하강 자체를 하지 않아 선택에 없다.
    assert all("node_modules" not in path.parts for path in selection.selected)
    assert all("build" not in path.parts for path in selection.selected)


def test_scan_files_excluded_count_only_counts_visited_files(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    selection = scan_files(tmp_path, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    # 가지치기된 디렉터리 하위는 세지 않는다 — readme.md 하나만 걸러진다 (확정된 동작 계약).
    assert selection.excluded_count == 1


def test_scan_files_returns_sorted_relative_posix_paths(tmp_path: Path) -> None:
    (tmp_path / "b.py").write_text("b\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    selection = scan_files(tmp_path, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    assert selection.selected == (PurePosixPath("a.py"), PurePosixPath("b.py"))


def test_scan_files_empty_tree_is_valid(tmp_path: Path) -> None:
    # 대상 0개는 오류가 아니다 (FR-035 유추 — 변경 없음 세션도 유효).
    selection = scan_files(tmp_path, DEFAULT_INCLUDE, DEFAULT_EXCLUDE)
    assert selection.selected == ()
    assert selection.excluded_count == 0
