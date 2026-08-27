"""snapshot 모듈 — baseline/final 저장·SHA-256·manifest (FR-010, FR-014 ③).

설계 검증 기준 13~16. 전부 tmp_path 안에서만 쓴다. git 저장소가 아닌 디렉터리에서
그대로 돌아간다는 사실이 FR-020(git 불요)의 이 단계 몫(스냅샷 재료)을 고정한다.
"""

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath

import pytest

from class_watcher import snapshot
from class_watcher.snapshot import (
    FileMeta,
    SnapshotResult,
    hash_bytes,
    hash_map,
    snapshot_tree,
    write_manifest,
)

# ── 기준 13: 원본 바이트 보존 + manifest SHA-256 이 재계산 값과 일치 (FR-010) ──


def test_baseline_copies_bytes_and_manifest_hash_matches(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    payload = "print('수업')\n".encode()
    (root / "a.py").write_bytes(payload)
    dest = tmp_path / "baseline"

    result = snapshot_tree(root, [PurePosixPath("a.py")], dest)
    write_manifest(dest, result)

    assert (dest / "a.py").read_bytes() == payload
    doc = json.loads((dest / snapshot.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert doc["unstable"] is False
    [entry] = doc["files"]
    assert entry["path"] == "a.py"
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    assert entry["size"] == len(payload)
    assert entry["missing"] is False


# ── 기준 14: 한글 파일명·공백 경로·BOM·바이너리도 디코드 없이 그대로 복사 ─────


def test_copies_verbatim_without_decoding(tmp_path: Path) -> None:
    root = tmp_path / "src"
    cases: dict[str, bytes] = {
        "한글 파일.py": "print('한글')\n".encode(),
        "sub dir/공백 있음.py": b"\xef\xbb\xbfprint('bom')\n",  # UTF-8 BOM 유지
        "bin.py": b"\x00\x01\xfe\xff" * 3,  # UTF-8 로 디코드 불가한 바이트
    }
    for rel, data in cases.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    dest = tmp_path / "snap"

    result = snapshot_tree(root, [PurePosixPath(rel) for rel in cases], dest)

    for rel, data in cases.items():
        assert (dest / rel).read_bytes() == data
    hashes = hash_map(result)
    assert set(hashes) == set(cases)
    assert hashes["bin.py"] == hash_bytes(cases["bin.py"])


# ── 기준 15: 도중에 사라진 파일은 missing=True 로 기록되고 예외가 없다 ────────


def test_missing_file_recorded_not_raised(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()
    (root / "real.py").write_bytes(b"x = 1\n")
    dest = tmp_path / "snap"

    result = snapshot_tree(root, [PurePosixPath("real.py"), PurePosixPath("ghost.py")], dest)

    metas = {meta.rel_path: meta for meta in result.metas}
    assert metas["ghost.py"].missing is True
    assert metas["ghost.py"].sha256 == ""
    assert not (dest / "ghost.py").exists()
    # 해시 비교 맵에서 빠져야 deleted 판정이 성립한다.
    assert "ghost.py" not in hash_map(result)
    assert metas["real.py"].missing is False


# ── 기준 16: manifest 쓰기는 임시파일 → os.replace 원자적 패턴을 따른다 ───────


def test_manifest_write_uses_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def recording(src: "str | Path", dst: "str | Path") -> None:
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(snapshot.os, "replace", recording)
    dest = tmp_path / "snap"
    dest.mkdir()

    write_manifest(dest, SnapshotResult(metas=(FileMeta("a.py", "00", 1, 1.0),)))

    manifest = dest / snapshot.MANIFEST_NAME
    assert manifest.is_file()
    replaced = [(src, dst) for src, dst in calls if Path(dst) == manifest]
    assert len(replaced) == 1
    src, _ = replaced[0]
    assert Path(src).parent == dest  # 같은 디렉터리의 임시파일에서 교체해야 원자적이다
    assert list(dest.glob("*.tmp")) == []  # 잔여 임시파일 없음


# ── FR-020 기반: 스냅샷 재료 생성에 git·subprocess 가 전혀 없다 ───────────────


def test_package_never_invokes_git() -> None:
    package_dir = Path(snapshot.__file__).resolve().parent
    pattern = re.compile(r"^\s*(?:import|from)\s+(?:git|pygit2|subprocess)\b", re.MULTILINE)
    for module_path in sorted(package_dir.glob("*.py")):
        source = module_path.read_text(encoding="utf-8")
        assert not pattern.search(source), f"{module_path.name} 이 git/subprocess 를 쓴다"
