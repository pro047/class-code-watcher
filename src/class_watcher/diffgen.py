"""baseline↔final unified diff 와 변경 통계 (FR-020, FR-022, FR-023, FR-024).

판정·생성·집계는 전부 순수 함수다. 디스크를 만지는 것은 맨 아래 두 함수뿐이라, 스냅샷
바이트만 넣으면 파일시스템 없이 전 경로가 검증된다.

git 은 부르지 않는다 (C-04). difflib 단일 경로라 git 설치 여부·저장소 여부와 무관하게
같은 형식이 나온다 (FR-020 수용 기준).
"""

import difflib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .session import SessionPaths

DEFAULT_MAX_DIFF_BYTES = 1 << 20  # FR-024 "기본 1MB"

SKIP_BINARY = "binary"
SKIP_TOO_LARGE = "too_large"
SKIP_DECODE_ERROR = "decode_error"
SKIP_WHITESPACE_ONLY = "whitespace_only"

# watcher 의 STATUS_* 와 같은 값. watcher 가 이 모듈을 부르므로 거꾸로 import 하면 순환이다.
STATUS_ADDED = "added"
STATUS_MODIFIED = "modified"
STATUS_DELETED = "deleted"
STATUS_SKIPPED = "skipped"

# diff 대상. unchanged 는 본문을 볼 이유가 없다.
_DIFF_TARGET_STATUSES = frozenset({STATUS_ADDED, STATUS_MODIFIED, STATUS_DELETED})

# stats.json 의 구조는 PRD 9.1 이 명세하지 않아 이 단계가 정했다. 후속 단계가 필드를
# 늘릴 때 소비자가 구분할 수 있도록 버전을 박아 둔다.
STATS_SCHEMA_VERSION = "1.1"

_BOM = b"\xef\xbb\xbf"
_ENCODING_BOM = "utf-8-sig"
_ENCODING_PLAIN = "utf-8"


@dataclass(frozen=True)
class FileDiff:
    """파일 한 개의 diff 결과."""

    rel_path: str
    status: str
    diff_text: str
    added_lines: int
    deleted_lines: int
    skip_reason: str | None
    encoding: str | None


@dataclass(frozen=True)
class DiffResult:
    files: tuple[FileDiff, ...]
    files_changed: int
    added_lines: int
    deleted_lines: int
    skipped: tuple[FileDiff, ...]


def classify_skip(
    baseline: bytes | None, final: bytes | None, max_bytes: int = DEFAULT_MAX_DIFF_BYTES
) -> str | None:
    """FR-024 의 제외 판정. 어느 한쪽만 걸려도 파일 전체를 제외한다.

    반쪽만 텍스트인 diff 는 읽는 사람에게 의미가 없기 때문이다. 크기를 먼저 보는 것은
    1MB 넘는 파일까지 NUL 스캔을 돌리는 비용을 피하려는 것이다.
    """
    sides = tuple(data for data in (baseline, final) if data is not None)
    if any(len(data) > max_bytes for data in sides):
        return SKIP_TOO_LARGE
    if any(b"\x00" in data for data in sides):
        return SKIP_BINARY
    return None


def decode_snapshot(data: bytes) -> tuple[str, str] | None:
    """FR-022. 성공하면 (본문, 인코딩 라벨), 실패하면 None.

    utf-8 strict 는 BOM 을 에러 없이 U+FEFF 문자로 통과시킨다. 그 문자가 diff 본문
    첫 줄에 새지 않도록 BOM 을 먼저 감지해 utf-8-sig 로 읽는다. cp949 같은 폴백은
    두지 않는다 — 잘못 추측해 깨진 텍스트를 싣느니 건너뛰고 사유를 남기는 편이
    수용 기준("해당 파일만 건너뛰고 사유를 기록")에 맞다.
    """
    encoding = _ENCODING_BOM if data.startswith(_BOM) else _ENCODING_PLAIN
    try:
        return data.decode(encoding), encoding
    except UnicodeDecodeError:
        return None


def _format_range(start: int, stop: int) -> str:
    """unified diff 의 범위 표기. difflib 의 비공개 _format_range_unified 와 같은 규칙이다.

    길이 1 은 숫자 하나로, 길이 0 은 "범위 바로 앞 줄"로 적는 것이 unified diff 관례다.
    직접 적는 이유는 hunk 를 우리가 만들기 때문이고(F17), 표기가 어긋나면
    summarize.split_hunks 가 세는 hunk 와 사람이 읽는 diff 가 달라진다.
    """
    beginning = start + 1
    length = stop - start
    if length == 1:
        return str(beginning)
    if not length:
        beginning -= 1
    return f"{beginning},{length}"


def unified_diff_text(rel_path: str, before: str, after: str, context: int = 3) -> str:
    """FR-022 의 "명시적 정규화" 쪽을 택한 diff 본문.

    splitlines() 로 개행을 떼면 CRLF/LF/CR 이 같은 라인 시퀀스가 되어, 에디터 설정
    차이로 개행만 바뀐 저장이 전 라인 변경으로 부풀지 않는다. 출력 개행은 \\n 고정이다.
    경로에 a/·b/ 접두만 붙여 절대 경로·사용자명이 산출물에 들어가지 않게 한다.

    **공백만 다른 줄은 변경으로 싣지 않는다** (F17). 짝을 맞추는 것은 공백을 지운
    키이고, 실제로 출력하는 것은 원본 줄이다 — `git diff -w` 와 같은 규칙이다.
    공백을 지운 채로 실으면 모델이 읽는 것이 코드가 아니게 되므로 키와 출력을 나눈다.

    2026-09-09 오전 세션이 이 함수를 고치게 했다: prettier 가 손으로 쓴 파일을 처음
    만지면서 전체를 재들여쓰기했고, 내용이 같은 283줄이 +/- 양쪽에 실려 diff 69,752자
    중 29,171자(44%)를 먹었다. **added_lines 는 이미 공백을 접고 세는데
    (meaningful_line_counts) 본문만 안 접고 있었다** — 그 불일치가 결함의 정체다.

    문맥 줄은 after 쪽을 싣는다. 공백만 다른 줄은 여기서 문맥이 되는데, 파일의 현재
    모습을 보여주는 편이 옛 들여쓰기를 보여주는 것보다 낫다.
    """
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(
        None,
        [strip_all_whitespace(line) for line in before_lines],
        [strip_all_whitespace(line) for line in after_lines],
        autojunk=False,
    )
    groups = list(matcher.get_grouped_opcodes(context))
    if not groups:
        return ""

    out = [f"--- a/{rel_path}", f"+++ b/{rel_path}"]
    for group in groups:
        first, last = group[0], group[-1]
        out.append(
            f"@@ -{_format_range(first[1], last[2])} +{_format_range(first[3], last[4])} @@"
        )
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out.extend(" " + line for line in after_lines[j1:j2])
                continue
            if tag in ("replace", "delete"):
                out.extend("-" + line for line in before_lines[i1:i2])
            if tag in ("replace", "insert"):
                out.extend("+" + line for line in after_lines[j1:j2])
    return "\n".join(out) + "\n"


def strip_all_whitespace(line: str) -> str:
    """FR-025 ① 의 "공백을 모두 제거". 앞뒤뿐 아니라 줄 가운데도 제거한다.

    인자 없는 str.split() 의 유니코드 공백 정의를 그대로 빌린다. 별도의 공백 문자
    목록을 손으로 적지 않는다.
    """
    return "".join(line.split())


def normalized_lines(text: str) -> list[str]:
    """공백 제거 후 빈 줄을 버린 라인 시퀀스. FR-025 판정의 유일한 입력 형태.

    splitlines() 는 unified_diff_text(:104-110) 와 같은 개행 정규화를 재사용한다.
    """
    stripped = (strip_all_whitespace(line) for line in text.splitlines())
    return [line for line in stripped if line]


def meaningful_line_counts(before: str, after: str) -> tuple[int, int]:
    """(added, deleted) — 공백 전용 줄을 뺀 ± 라인 수 (FR-025 ①④).

    normalized_lines 로 접은 두 시퀀스에 difflib.unified_diff(lineterm="", n=0) 를
    한 번 더 돌리고 앞 두 줄(헤더)을 위치로 건너뛴 뒤 ± 를 센다. 위치 기준인 이유는
    `++i;`·`--count;` 처럼 ++/-- 로 시작하는 본문 라인이 헤더로 오인되면 안 되기
    때문이다. 차이가 없으면 difflib 이 아무 줄도 내지 않으므로 (0, 0) 이다.
    """
    diff = list(
        difflib.unified_diff(normalized_lines(before), normalized_lines(after), lineterm="", n=0)
    )
    if not diff:
        return 0, 0
    added = 0
    deleted = 0
    for line in diff[2:]:
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added, deleted


def is_whitespace_only_change(before: str, after: str) -> bool:
    """FR-025 ② — meaningful_line_counts 가 (0, 0) 이면 True."""
    return meaningful_line_counts(before, after) == (0, 0)


def _skipped(rel_path: str, reason: str) -> FileDiff:
    return FileDiff(
        rel_path=rel_path,
        status=STATUS_SKIPPED,
        diff_text="",
        added_lines=0,
        deleted_lines=0,
        skip_reason=reason,
        encoding=None,
    )


def diff_file(
    rel_path: str,
    baseline: bytes | None,
    final: bytes | None,
    max_bytes: int = DEFAULT_MAX_DIFF_BYTES,
) -> FileDiff:
    """파일 하나의 전체 판정: 제외 → 디코드 → unified diff → 라인 집계.

    baseline=None 은 세션 중 생긴 파일, final=None 은 삭제된 파일이다. 없는 쪽은 빈
    본문으로 간주해 diff 에 포함한다 (FR-017 수용 기준).
    """
    if baseline is None and final is None:
        raise ValueError(f"양쪽 스냅샷이 모두 없다: {rel_path}")

    if baseline is None:
        status = STATUS_ADDED
    elif final is None:
        status = STATUS_DELETED
    else:
        status = STATUS_MODIFIED

    reason = classify_skip(baseline, final, max_bytes)
    if reason is not None:
        return _skipped(rel_path, reason)

    decoded_before = decode_snapshot(baseline) if baseline is not None else ("", _ENCODING_PLAIN)
    decoded_after = decode_snapshot(final) if final is not None else ("", _ENCODING_PLAIN)
    if decoded_before is None or decoded_after is None:
        return _skipped(rel_path, SKIP_DECODE_ERROR)

    before_text, after_text = decoded_before[0], decoded_after[0]
    added, deleted = meaningful_line_counts(before_text, after_text)
    if (added, deleted) == (0, 0):
        return _skipped(rel_path, SKIP_WHITESPACE_ONLY)

    diff_text = unified_diff_text(rel_path, before_text, after_text)
    # 남아 있는 쪽의 인코딩이 그 파일의 현재 인코딩이다.
    encoding = decoded_after[1] if final is not None else decoded_before[1]
    return FileDiff(
        rel_path=rel_path,
        status=status,
        diff_text=diff_text,
        added_lines=added,
        deleted_lines=deleted,
        skip_reason=None,
        encoding=encoding,
    )


def build_result(diffs: Sequence[FileDiff]) -> DiffResult:
    """정렬과 합산. 정렬해야 같은 입력이 항상 같은 바이트를 낸다 (FR-020 결정성)."""
    files = tuple(sorted(diffs, key=lambda item: item.rel_path))
    skipped = tuple(item for item in files if item.status == STATUS_SKIPPED)
    changed = tuple(item for item in files if item.status != STATUS_SKIPPED)
    return DiffResult(
        files=files,
        files_changed=len(changed),
        added_lines=sum(item.added_lines for item in changed),
        deleted_lines=sum(item.deleted_lines for item in changed),
        skipped=skipped,
    )


def render_final_diff(result: DiffResult) -> str:
    """final.diff 본문 (PRD 9.1 "파일별 unified diff 연결본")."""
    parts: list[str] = []
    for item in result.files:
        if item.status == STATUS_SKIPPED:
            # 산출물만 봐도 무엇이 왜 빠졌는지 알 수 있게 한 줄 남긴다.
            parts.append(f"# skipped: {item.rel_path} ({item.skip_reason})\n")
        elif item.diff_text:
            parts.append(item.diff_text)
    return "".join(parts)


def stats_doc(
    result: DiffResult,
    *,
    event_count: int,
    started_at: str,
    ended_at: str,
) -> dict[str, object]:
    """stats.json 본문 (FR-023).

    diff 원문은 넣지 않는다 — 요약 입력과 Discord 렌더러가 코드 없이 재사용할 수 있는
    구조여야 한다는 수용 기준 때문이다. 원문이 필요하면 final.diff 를 읽는다.
    """
    return {
        "schema_version": STATS_SCHEMA_VERSION,
        "started_at": started_at,
        "ended_at": ended_at,
        "events": event_count,
        "totals": {
            "files_changed": result.files_changed,
            "added_lines": result.added_lines,
            "deleted_lines": result.deleted_lines,
            "skipped": len(result.skipped),
        },
        "files": [
            {
                "path": item.rel_path,
                "status": item.status,
                "added_lines": item.added_lines,
                "deleted_lines": item.deleted_lines,
                "encoding": item.encoding,
                "skip_reason": item.skip_reason,
            }
            for item in result.files
        ],
    }


def change_stats_fields(result: DiffResult, event_count: int) -> dict[str, object]:
    """session.json 의 change_stats (PRD 9.2). files_changed 는 skipped 를 세지 않는다."""
    return {
        "files_changed": result.files_changed,
        "events": event_count,
        "added_lines": result.added_lines,
        "deleted_lines": result.deleted_lines,
    }


def watched_file_entries(
    statuses: Mapping[str, str], result: DiffResult
) -> list[dict[str, str]]:
    """session.json 의 watched_files. diff 단계에서 제외된 파일만 사유와 함께 덮어쓴다."""
    reasons = {item.rel_path: item.skip_reason for item in result.skipped}
    entries: list[dict[str, str]] = []
    for rel_path, status in statuses.items():
        reason = reasons.get(rel_path)
        if reason is None:
            entries.append({"path": rel_path, "status": status})
        else:
            entries.append({"path": rel_path, "status": STATUS_SKIPPED, "reason": reason})
    return entries


def _atomic_write_text(path: Path, text: str) -> None:
    """snapshot.py 와 같은 패턴 — 중간에 죽어도 반쪽 산출물이 남지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".diff-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def load_snapshot_bytes(dest_dir: Path, rel_path: str) -> bytes | None:
    """스냅샷 디렉터리에서 읽는다 — 세션 종료 후 사용자가 더 고쳐도 diff 는 고정된다."""
    try:
        return (dest_dir / rel_path).read_bytes()
    except OSError:
        return None


def generate_session_diff(
    paths: SessionPaths,
    statuses: Mapping[str, str],
    *,
    event_count: int,
    started_at: str,
    ended_at: str,
    max_bytes: int = DEFAULT_MAX_DIFF_BYTES,
) -> DiffResult:
    """스냅샷을 읽어 final.diff·stats.json 을 쓰고 결과를 돌려준다."""
    diffs: list[FileDiff] = []
    for rel_path, status in statuses.items():
        if status not in _DIFF_TARGET_STATUSES:
            continue
        baseline = (
            None if status == STATUS_ADDED else load_snapshot_bytes(paths.baseline_dir, rel_path)
        )
        final = (
            None if status == STATUS_DELETED else load_snapshot_bytes(paths.final_dir, rel_path)
        )
        # 있어야 할 쪽을 못 읽었으면 그 파일만 제외로 낮춘다 (PRD 12절 복구 원칙).
        missing_side = (status != STATUS_ADDED and baseline is None) or (
            status != STATUS_DELETED and final is None
        )
        if missing_side:
            diffs.append(_skipped(rel_path, SKIP_DECODE_ERROR))
            continue
        diffs.append(diff_file(rel_path, baseline, final, max_bytes))

    result = build_result(diffs)
    _atomic_write_text(paths.final_diff, render_final_diff(result))
    doc = stats_doc(result, event_count=event_count, started_at=started_at, ended_at=ended_at)
    _atomic_write_text(paths.stats_json, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return result
