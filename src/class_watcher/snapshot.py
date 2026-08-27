"""baseline·final·history 스냅샷 저장과 SHA-256 기록 (FR-010, FR-014 ③, FR-015).

바이트를 그대로 복사하고 디코드하지 않는다. 인코딩 판정과 바이너리 판별은 diff 엔진의
몫이라(FR-022) 여기서 미리 해석하면 원본이 왜곡된다.
"""

import hashlib
import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MANIFEST_NAME = ".meta.json"

# 큰 파일을 통째로 메모리에 올리지 않기 위한 읽기 단위.
_CHUNK = 1 << 20


@dataclass(frozen=True)
class FileMeta:
    """스냅샷 시점의 파일 한 개."""

    rel_path: str
    sha256: str
    size: int
    mtime: float
    # 스냅샷 도중 사라진 파일. 본문을 저장하지 않았다는 표시이며 해시 비교에서 제외된다.
    missing: bool = False


@dataclass(frozen=True)
class SnapshotResult:
    metas: tuple[FileMeta, ...]
    unstable: bool = False


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    """session.py 의 원자적 쓰기와 같은 패턴 — 중간에 죽어도 반쪽 파일이 남지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".meta-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def snapshot_file(root: Path, rel_path: PurePosixPath, dest_dir: Path) -> FileMeta:
    """원본 바이트를 dest_dir 아래 같은 상대 경로로 복사하고 해시를 낸다.

    대상이 사라졌거나 읽을 수 없으면 예외를 올리지 않고 missing 으로 기록한다. 파일 하나
    때문에 세션 전체 산출물을 잃는 것이 더 나쁘다 (PRD 12절 복구 원칙).
    """
    rel = rel_path.as_posix()
    source = root / rel_path
    target = dest_dir / rel_path

    digest = hashlib.sha256()
    size = 0
    try:
        stat = source.stat()
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(source, "rb") as src, open(target, "wb") as dst:
            while True:
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
    except OSError:
        return FileMeta(rel_path=rel, sha256="", size=0, mtime=0.0, missing=True)

    return FileMeta(rel_path=rel, sha256=digest.hexdigest(), size=size, mtime=stat.st_mtime)


def snapshot_tree(
    root: Path, rel_paths: Sequence[PurePosixPath], dest_dir: Path
) -> SnapshotResult:
    dest_dir.mkdir(parents=True, exist_ok=True)
    metas = tuple(snapshot_file(root, rel_path, dest_dir) for rel_path in rel_paths)
    return SnapshotResult(metas=metas, unstable=False)


def manifest_doc(result: SnapshotResult) -> dict[str, object]:
    """순수 — 매니페스트 본문 구성. 쓰기는 write_manifest 가 한다."""
    return {
        "unstable": result.unstable,
        "files": [
            {
                "path": meta.rel_path,
                "sha256": meta.sha256,
                "size": meta.size,
                "mtime": meta.mtime,
                "missing": meta.missing,
            }
            for meta in result.metas
        ],
    }


def write_manifest(dest_dir: Path, result: SnapshotResult) -> None:
    text = json.dumps(manifest_doc(result), ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(dest_dir / MANIFEST_NAME, text)


def hash_map(result: SnapshotResult) -> dict[str, str]:
    """해시 비교용 맵. missing 은 '그 시점에 없던 파일'이므로 넣지 않는다."""
    return {meta.rel_path: meta.sha256 for meta in result.metas if not meta.missing}
