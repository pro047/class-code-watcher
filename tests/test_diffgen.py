"""diffgen 모듈 — 제외 판정·디코드·unified diff·통계·산출물 쓰기 (FR-017, FR-020, FR-022~024).

설계 검증 기준 1~15. 전부 순수 함수 또는 tmp_path 다 — git·네트워크·watchdog 을 부르지
않고, 감시 루트조차 만들지 않는다. 스냅샷 디렉터리만으로 diff 가 나오는 것 자체가
기준 14(스냅샷에서 읽는다)와 FR-020(git 무관)의 증거다.
"""

import json
from pathlib import Path

import pytest

from class_watcher import diffgen
from class_watcher.diffgen import (
    DEFAULT_MAX_DIFF_BYTES,
    SKIP_BINARY,
    SKIP_DECODE_ERROR,
    SKIP_TOO_LARGE,
    STATS_SCHEMA_VERSION,
    build_result,
    change_stats_fields,
    classify_skip,
    decode_snapshot,
    diff_file,
    generate_session_diff,
    load_snapshot_bytes,
    render_final_diff,
    stats_doc,
    watched_file_entries,
)
from class_watcher.session import SessionPaths, make_session_paths

BOM = b"\xef\xbb\xbf"


def _body_lines(diff_text: str) -> list[str]:
    """헤더 두 줄과 @@ 를 뺀 본문 라인 — _count_lines 와 같은 셈법으로 기대값을 만든다.

    앞 두 줄을 위치로 떼는 것이 중요하다. startswith 로 걸러내면 ++/-- 로 시작하는
    본문 라인이 같이 사라져 기대값이 구현과 똑같이 틀린다 (아래 회귀 테스트 참조).
    """
    return [line for line in diff_text.splitlines()[2:] if not line.startswith("@@")]


# ── 기준 1: modified 기본 경로 (FR-020, FR-023) ──────────────────────────────


def test_modified_produces_unified_diff_with_headers() -> None:
    result = diff_file("src/app.py", b"keep\nold\n", b"keep\nnew\nextra\n")
    assert result.status == "modified"
    lines = result.diff_text.splitlines()
    assert lines[0] == "--- a/src/app.py"
    assert lines[1] == "+++ b/src/app.py"
    assert lines[2].startswith("@@")
    assert result.added_lines == 2
    assert result.deleted_lines == 1
    assert result.encoding == "utf-8"
    assert result.skip_reason is None
    assert result.diff_text.endswith("\n")


# ── 기준 2: added — baseline=None 을 빈 상태로 간주 (FR-017 뒷부분) ───────────


def test_added_file_diffs_against_empty_baseline() -> None:
    result = diff_file("new.py", None, b"a\nb\n")
    assert result.status == "added"
    assert _body_lines(result.diff_text) == ["+a", "+b"]
    assert result.added_lines == 2
    assert result.deleted_lines == 0


# ── 기준 3: deleted — final=None ─────────────────────────────────────────────


def test_deleted_file_diffs_to_empty() -> None:
    result = diff_file("gone.py", b"a\nb\n", None)
    assert result.status == "deleted"
    assert _body_lines(result.diff_text) == ["-a", "-b"]
    assert result.added_lines == 0
    assert result.deleted_lines == 2
    # 남아 있는 쪽(baseline)의 인코딩 라벨이 남는다.
    assert result.encoding == "utf-8"


# ── 기준 15: 양쪽 None 은 호출부 버그 ────────────────────────────────────────


def test_diff_file_with_both_sides_none_raises() -> None:
    with pytest.raises(ValueError):
        diff_file("ghost.py", None, None)


# ── 기준 4: 결정성 — 같은 입력이면 같은 출력, 순서는 rel_path 정렬 (FR-020) ──


def test_result_is_deterministic_and_sorted() -> None:
    diffs = [
        diff_file("b.py", b"1\n", b"2\n"),
        diff_file("a.py", None, b"x\n"),
    ]
    first = build_result(diffs)
    second = build_result(list(reversed(diffs)))
    assert [item.rel_path for item in first.files] == ["a.py", "b.py"]
    assert render_final_diff(first) == render_final_diff(second)
    # 같은 입력으로 두 번 돌려도 바이트가 같다.
    again = build_result(
        [diff_file("b.py", b"1\n", b"2\n"), diff_file("a.py", None, b"x\n")]
    )
    assert render_final_diff(again).encode("utf-8") == render_final_diff(first).encode("utf-8")


# ── 기준 5: 바이너리 제외 — 한쪽만 걸려도 파일 전체 (FR-024, 설계 5.4) ────────


def test_binary_bytes_are_skipped() -> None:
    result = diff_file("logo.png", b"\x00\x01", b"\x00\x02")
    assert result.status == "skipped"
    assert result.skip_reason == SKIP_BINARY
    assert result.diff_text == ""
    assert (result.added_lines, result.deleted_lines) == (0, 0)
    assert result.encoding is None


def test_one_binary_side_skips_whole_file() -> None:
    assert classify_skip(b"text\n", b"bin\x00") == SKIP_BINARY
    assert classify_skip(b"bin\x00", b"text\n") == SKIP_BINARY
    assert classify_skip(b"text\n", b"text2\n") is None


# ── 기준 6: 대용량 제외 — 경계값 max_bytes 는 통과 (FR-024) ──────────────────


def test_oversize_is_skipped_and_boundary_passes() -> None:
    limit = 8
    assert classify_skip(b"x" * (limit + 1), b"ok", max_bytes=limit) == SKIP_TOO_LARGE
    assert classify_skip(b"x" * limit, b"ok", max_bytes=limit) is None
    result = diff_file("big.txt", b"x" * (limit + 1), b"y", max_bytes=limit)
    assert (result.status, result.skip_reason) == ("skipped", SKIP_TOO_LARGE)
    # 판정 순서: 크기 먼저 — 1MB 넘는 바이너리는 too_large 로 기록된다 (설계 5.4).
    assert classify_skip(b"\x00" * (limit + 1), None, max_bytes=limit) == SKIP_TOO_LARGE
    # FR-024 의 "기본 1MB".
    assert DEFAULT_MAX_DIFF_BYTES == 1 << 20


# ── 기준 7: UTF-8 BOM 감지, BOM 문자가 본문에 새지 않는다 (FR-022) ────────────


def test_bom_is_detected_and_not_leaked() -> None:
    assert decode_snapshot(BOM + b"hello\n") == ("hello\n", "utf-8-sig")
    assert decode_snapshot(b"hello\n") == ("hello\n", "utf-8")
    result = diff_file("bom.py", BOM + b"old\n", BOM + b"new\n")
    assert result.status == "modified"
    assert result.encoding == "utf-8-sig"
    assert chr(0xFEFF) not in result.diff_text
    assert "-old" in result.diff_text.splitlines()
    assert "+new" in result.diff_text.splitlines()


# ── 기준 8: decode 실패는 해당 파일만 skip (FR-022) ──────────────────────────


def test_decode_error_skips_only_that_file() -> None:
    bad = "한글".encode("cp949")
    assert decode_snapshot(bad) is None
    result = diff_file("legacy.py", bad, b"ok\n")
    assert (result.status, result.skip_reason) == ("skipped", SKIP_DECODE_ERROR)
    # 같은 배치의 다른 파일은 정상 생성된다 — 전체 실패로 번지지 않는다.
    other = diff_file("fine.py", b"1\n", b"2\n")
    assert other.status == "modified"
    assert other.diff_text != ""


# ── 기준 9: 개행 정규화 — CRLF↔LF 만 바뀐 저장은 빈 diff (FR-022, 설계 5.2) ───


def test_crlf_only_change_yields_empty_diff() -> None:
    result = diff_file("style.py", b"x\r\ny\r\n", b"x\ny\n")
    # 해시가 다르므로 상태는 modified 지만, 본문 차이는 없다.
    assert result.status == "modified"
    assert result.diff_text == ""
    assert (result.added_lines, result.deleted_lines) == (0, 0)
    # 파일 말미 개행 없음의 차이도 같은 정규화에 흡수된다.
    trailing = diff_file("t.py", b"x\ny", b"x\ny\n")
    assert trailing.diff_text == ""


def test_crlf_file_with_real_change_diffs_normally() -> None:
    result = diff_file("win.py", b"x\r\ny\r\n", b"x\r\nz\r\n")
    lines = result.diff_text.splitlines()
    assert "-y" in lines
    assert "+z" in lines
    # 출력 개행은 \n 고정 — CR 이 산출물에 남지 않는다.
    assert "\r" not in result.diff_text


# ── 기준 10: 한글 파일명·본문 (PRD 14.1 첫 항목) ─────────────────────────────


def test_korean_path_and_body_survive() -> None:
    result = diff_file("메모/수업 노트.py", "이전\n".encode(), "이후\n".encode())
    assert "--- a/메모/수업 노트.py" in result.diff_text
    assert "-이전" in result.diff_text.splitlines()
    assert "+이후" in result.diff_text.splitlines()
    doc = stats_doc(build_result([result]), event_count=1, started_at="s", ended_at="e")
    files = doc["files"]
    assert isinstance(files, list)
    assert files[0]["path"] == "메모/수업 노트.py"


# ── 기준 11: 다중 파일 fixture 합산 (FR-023, PRD 14절 완료 기준) ──────────────


def test_totals_across_multi_file_fixture() -> None:
    diffs = [
        diff_file("m1.py", b"a\n", b"b\n"),  # +1 / -1
        diff_file("m2.py", b"a\nb\n", b"a\nc\nd\n"),  # +2 / -1
        diff_file("new.py", None, b"n1\nn2\n"),  # +2 / -0
        diff_file("gone.py", b"g\n", None),  # +0 / -1
        diff_file("logo.png", b"\x00", b"\x00\x01"),  # skipped
    ]
    result = build_result(diffs)
    assert result.files_changed == 4  # skipped 는 세지 않는다 (설계 5.5)
    assert result.added_lines == 5
    assert result.deleted_lines == 3
    assert len(result.skipped) == 1
    assert result.skipped[0].rel_path == "logo.png"


# ── 기준 12: stats.json 구조 — diff 원문은 넣지 않는다 (FR-023) ──────────────


def test_stats_doc_structure_without_diff_text() -> None:
    result = build_result(
        [
            diff_file("a.py", b"old-secret-line\n", b"new\n"),
            diff_file("logo.png", b"\x00", b"\x00"),
        ]
    )
    doc = stats_doc(
        result,
        event_count=3,
        started_at="2026-08-28T10:00:00+09:00",
        ended_at="2026-08-28T10:05:00+09:00",
    )
    assert set(doc) == {"schema_version", "started_at", "ended_at", "events", "totals", "files"}
    assert doc["schema_version"] == STATS_SCHEMA_VERSION
    assert doc["events"] == 3
    assert doc["totals"] == {
        "files_changed": 1,
        "added_lines": 1,
        "deleted_lines": 1,
        "skipped": 1,
    }
    files = doc["files"]
    assert isinstance(files, list)
    expected_keys = {"path", "status", "added_lines", "deleted_lines", "encoding", "skip_reason"}
    assert [set(entry) for entry in files] == [expected_keys, expected_keys]
    # diff 원문이 stats 로 새지 않는다 — 소비자가 코드 없이 재사용할 구조여야 한다.
    assert "old-secret-line" not in json.dumps(doc, ensure_ascii=False)


# ── 기준 13: change_stats / watched_files 는 PRD 9.2 형태 그대로 ─────────────


def test_change_stats_and_watched_entries_follow_prd_92() -> None:
    result = build_result(
        [
            diff_file("edit.py", b"1\n", b"2\n"),
            diff_file("logo.png", b"\x00", b"\x00"),
        ]
    )
    assert change_stats_fields(result, 7) == {
        "files_changed": 1,
        "events": 7,
        "added_lines": 1,
        "deleted_lines": 1,
    }
    statuses = {"edit.py": "modified", "logo.png": "modified", "same.py": "unchanged"}
    assert watched_file_entries(statuses, result) == [
        {"path": "edit.py", "status": "modified"},
        {"path": "logo.png", "status": "skipped", "reason": "binary"},
        {"path": "same.py", "status": "unchanged"},
    ]


# ── render_final_diff — skip 주석 표기와 빈 세션 (설계 5.6, PRD 9.1) ──────────


def test_render_final_diff_marks_skips_and_empty_result() -> None:
    result = build_result([diff_file("logo.png", b"\x00", b"\x00")])
    # 변경은 있으나 diff 가능 파일이 0개인 세션 — 산출물만 봐도 제외 사실이 남는다.
    assert render_final_diff(result) == "# skipped: logo.png (binary)\n"
    assert result.files_changed == 0
    assert render_final_diff(build_result([])) == ""


# ── 기준 14: generate_session_diff — 스냅샷 디렉터리만으로 산출물 생성 ────────


def _snapshot_paths(tmp_path: Path) -> SessionPaths:
    paths = make_session_paths(tmp_path / "sessions", "20260828-100000-test")
    paths.baseline_dir.mkdir(parents=True)
    paths.final_dir.mkdir(parents=True)
    return paths


def test_generate_session_diff_writes_artifacts_from_snapshots(tmp_path: Path) -> None:
    paths = _snapshot_paths(tmp_path)
    (paths.baseline_dir / "edit.py").write_bytes(b"x = 1\n")
    (paths.final_dir / "edit.py").write_bytes(b"x = 2\n")
    (paths.final_dir / "new.py").write_bytes(b"n = 1\n")
    (paths.baseline_dir / "legacy.py").write_bytes("한글".encode("cp949"))
    (paths.final_dir / "legacy.py").write_bytes(b"ok\n")
    statuses = {
        "edit.py": "modified",
        "new.py": "added",
        "legacy.py": "modified",
        "same.py": "unchanged",  # unchanged 는 읽지도 않는다 — 스냅샷 파일이 없어도 된다
    }

    result = generate_session_diff(
        paths,
        statuses,
        event_count=2,
        started_at="2026-08-28T10:00:00+09:00",
        ended_at="2026-08-28T10:05:00+09:00",
    )

    # 감시 루트는 아예 만들지 않았다 — 스냅샷 디렉터리만으로 diff 가 나온다.
    diff_text = paths.final_diff.read_text(encoding="utf-8")
    assert "--- a/edit.py" in diff_text
    assert "+n = 1" in diff_text
    assert "# skipped: legacy.py (decode_error)" in diff_text
    # rel_path 정렬 순서로 연결된다.
    assert (
        diff_text.index("--- a/edit.py")
        < diff_text.index("# skipped: legacy.py")
        < diff_text.index("--- a/new.py")
    )
    stats = json.loads(paths.stats_json.read_text(encoding="utf-8"))
    assert stats["schema_version"] == STATS_SCHEMA_VERSION
    assert stats["started_at"] == "2026-08-28T10:00:00+09:00"
    assert stats["ended_at"] == "2026-08-28T10:05:00+09:00"
    assert stats["events"] == 2
    assert stats["totals"] == {
        "files_changed": 2,
        "added_lines": 2,
        "deleted_lines": 1,
        "skipped": 1,
    }
    assert [entry["path"] for entry in stats["files"]] == ["edit.py", "legacy.py", "new.py"]
    assert result.files_changed == 2


def test_unreadable_snapshot_downgrades_single_file(tmp_path: Path) -> None:
    # modified 로 판정됐지만 baseline 스냅샷 바이트가 없는 경우 — 그 파일만 낮춘다
    # (PRD 12절 복구 원칙: 세션 전체를 죽이지 않는다).
    paths = _snapshot_paths(tmp_path)
    (paths.final_dir / "ghost.py").write_bytes(b"g\n")
    (paths.baseline_dir / "ok.py").write_bytes(b"1\n")
    (paths.final_dir / "ok.py").write_bytes(b"2\n")
    statuses = {"ghost.py": "modified", "ok.py": "modified"}

    result = generate_session_diff(
        paths, statuses, event_count=1, started_at="s", ended_at="e"
    )

    by_path = {item.rel_path: item for item in result.files}
    assert by_path["ghost.py"].status == "skipped"
    assert by_path["ghost.py"].skip_reason == SKIP_DECODE_ERROR
    assert by_path["ok.py"].status == "modified"
    assert result.files_changed == 1


def test_load_snapshot_bytes_missing_returns_none(tmp_path: Path) -> None:
    assert load_snapshot_bytes(tmp_path, "none.py") is None
    (tmp_path / "a.py").write_bytes(b"x")
    assert load_snapshot_bytes(tmp_path, "a.py") == b"x"


# ── FR-020 회귀 방어: difflib 단일 경로 — subprocess/git 이 다시 들어오면 잡는다 ──


def test_diffgen_never_shells_out() -> None:
    module_file = diffgen.__file__
    assert module_file is not None
    source = Path(module_file).read_text(encoding="utf-8")
    assert "subprocess" not in source


# ── 회귀: ++/-- 로 시작하는 본문 라인이 헤더로 오인돼 안 세어지던 것 ──────────
#
# 파이프라인 밖에서 발견했다. diff 본문은 처음부터 정확했고 ±라인 **집계만** 틀렸는데,
# 그 수치가 4단계 LLM 프롬프트와 5단계 디스코드 메시지로 그대로 흘러간다.
# 게이트가 못 잡은 이유: 위 _body_lines 헬퍼가 _count_lines 와 같은 셈법이라
# 기대값도 같이 틀렸다. 그래서 이 테스트는 헬퍼를 쓰지 않고 숫자를 직접 적는다.


def test_increment_statements_are_counted_as_body_lines() -> None:
    """`++i;`·`--count;` 는 자바·JS 에 실제로 나오는 문장이다."""
    result = diff_file("Loop.java", b"int i = 0;\n", b"int i = 0;\n++i;\n--j;\n")

    assert result.diff_text.splitlines()[4:] == ["+++i;", "+--j;"]
    assert result.added_lines == 2
    assert result.deleted_lines == 0


def test_removed_decrement_statement_is_counted() -> None:
    result = diff_file("Loop.java", b"int i = 0;\n--j;\n", b"int i = 0;\n")

    assert result.added_lines == 0
    assert result.deleted_lines == 1
