"""CLI 진입점.

이 모듈의 main() 이 부작용(인자·환경변수·`.env`·시계·난수)을 한곳에 모으고, 나머지 함수는
주입받은 값으로만 판정한다. 그래야 시각·난수가 걸린 경로도 결정적으로 테스트할 수 있다.
"""

import argparse
import json
import os
import secrets as secrets_module
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

from .config import (
    DEFAULT_DEBOUNCE_MS,
    DEFAULT_EXCLUDE,
    DEFAULT_INCLUDE,
    DEFAULT_MAX_FILES,
    DEFAULT_SESSION_DIR,
    Secrets,
    WatchConfig,
    load_secrets,
    mask_secrets,
    merge_env,
)
from .selector import Selection, scan_files
from .session import (
    SessionPaths,
    SessionStatus,
    create_session_dirs,
    generate_session_id,
    initial_session_doc,
    make_session_paths,
    transition,
    write_session_json,
)
from .watcher import run_session

# PRD 10.3 의 4종.
EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2
EXIT_ABORTED = 130


class PreflightError(Exception):
    """사전 점검 실패. 종료 코드를 메시지와 함께 들고 다닌다."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class Preflight:
    config: WatchConfig
    selection: Selection


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="class-watcher",
        description="수업 중 코드 변경을 감시하고 종료 시 1회 요약하는 도구",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    watch = subparsers.add_parser("watch", help="디렉터리 감시를 시작한다")
    watch.add_argument("dir", nargs="?", default=".", metavar="DIR", help="감시할 디렉터리")
    watch.add_argument("--include", default=None, help="대상 확장자 패턴 (쉼표 구분)")
    watch.add_argument("--exclude", default=None, help="제외 패턴 (쉼표 구분)")
    watch.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES, help="대상 파일 수 상한")
    watch.add_argument("--title", default=None, help="세션 제목")
    watch.add_argument(
        "--debounce-ms", type=int, default=DEFAULT_DEBOUNCE_MS, help="저장 이벤트 병합 시간(ms)"
    )
    watch.add_argument("--polling", action="store_true", help="폴링 기반 감시 강제")
    watch.add_argument("--history", action="store_true", help="중간 snapshot 및 diff 이력 저장")
    watch.add_argument("--session-dir", default=DEFAULT_SESSION_DIR, help="세션 산출물 저장 위치")
    watch.add_argument("--dry-run", action="store_true", help="외부 호출 없이 검증만 수행")
    watch.add_argument("--no-discord", action="store_true", help="요약은 만들되 전송은 생략")
    watch.add_argument("--allow-secrets", action="store_true", help="비밀값 탐지 시 마스킹 후 진행")
    return parser


def _split_patterns(raw: str | None, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return fallback
    patterns = tuple(item.strip() for item in raw.split(",") if item.strip())
    return patterns or fallback


def build_config(args: argparse.Namespace, now: datetime) -> WatchConfig:
    """Namespace → WatchConfig. 기본값 확정은 전부 여기서 끝난다 (FR-004)."""
    watch_root = Path(str(args.dir)).expanduser().resolve()
    title = str(args.title) if args.title else f"{watch_root.name} {now:%H:%M}"
    return WatchConfig(
        watch_root=watch_root,
        title=title,
        include=_split_patterns(args.include, DEFAULT_INCLUDE),
        exclude=_split_patterns(args.exclude, DEFAULT_EXCLUDE),
        max_files=int(args.max_files),
        debounce_ms=int(args.debounce_ms),
        polling=bool(args.polling),
        history=bool(args.history),
        session_dir=Path(str(args.session_dir)).expanduser(),
        dry_run=bool(args.dry_run),
        no_discord=bool(args.no_discord),
        allow_secrets=bool(args.allow_secrets),
    )


def run_preflight(config: WatchConfig) -> Preflight:
    """감시를 시작해도 되는지 판정한다. 여기서 막히면 세션 디렉터리도 만들지 않는다."""
    root = config.watch_root
    if not root.exists():
        raise PreflightError(f"감시 루트를 찾을 수 없습니다: {root}", EXIT_CONFIG)
    if not root.is_dir():
        raise PreflightError(f"감시 루트가 디렉터리가 아닙니다: {root}", EXIT_CONFIG)
    if not os.access(root, os.R_OK):
        raise PreflightError(f"감시 루트를 읽을 권한이 없습니다: {root}", EXIT_CONFIG)

    selection = scan_files(root, config.include, config.exclude)
    if len(selection.selected) > config.max_files:
        raise PreflightError(
            f"감시 대상이 {len(selection.selected)}개로 상한 {config.max_files}개를 넘습니다. "
            "상위 디렉터리를 지정했을 수 있습니다. DIR 을 더 좁게 주거나 "
            "--include 로 범위를 줄이거나 --max-files 를 올리세요.",
            EXIT_CONFIG,
        )
    return Preflight(config=config, selection=selection)


def bootstrap(
    config: WatchConfig,
    secrets: Secrets,
    *,
    now: datetime,
    id_suffix: str,
) -> tuple[Preflight, SessionPaths]:
    """사전 점검 → 세션 디렉터리 → `session.json`(starting) 까지.

    secrets 는 아직 쓰지 않는다. 키 유무는 오류가 아니며, 실제로 필요한 시점(종료
    파이프라인)에 검사하는 것이 FR-035(변경 없음이면 외부 호출 0회)와 맞는다.
    """
    preflight = run_preflight(config)
    session_id = generate_session_id(now, id_suffix)
    paths = make_session_paths(config.session_dir, session_id)
    create_session_dirs(paths)
    watch_mode = "polling" if config.polling else "native"
    doc = initial_session_doc(config, session_id, now, preflight.selection, watch_mode)
    write_session_json(paths, doc)
    return preflight, paths


def run_watch(preflight: Preflight, paths: SessionPaths, secrets: Secrets) -> int:
    """감시 세션을 돌리고 결과를 종료 코드로 환원한다 (PRD 10.3).

    세션 상태 기록은 watcher 쪽이 이미 끝낸다. 여기서는 코드 매핑과 콘솔 마무리만 한다.
    """
    try:
        outcome = run_session(
            preflight.config,
            paths,
            preflight.selection,
            lambda message: _emit(message, secrets),
        )
    except OSError as exc:
        _record_failure(paths, "watch_io_error")
        _emit_error(f"[FAILED] 감시 중 실패: {exc}", secrets)
        _emit_error(f"       세션 산출물은 보존됩니다: {paths.root}", secrets)
        return EXIT_RUNTIME

    if outcome.aborted:
        _emit_error(f"[ABORTED] 세션 산출물은 보존됩니다: {paths.root}", secrets)
        return EXIT_ABORTED

    if outcome.unstable:
        _emit("[WARN] 일부 파일이 안정되지 않아 마지막 관측 상태를 사용했습니다.", secrets)

    if outcome.no_change:
        _emit(f"[DONE] 변경 없음: 요약과 전송을 생략합니다. {paths.root}", secrets)
        return EXIT_OK

    changed = sum(1 for status in outcome.statuses.values() if status != "unchanged")
    _emit(f"[OK] 변경 {changed}개 파일 / 이벤트 {outcome.logical_event_count}건", secrets)
    _emit_error("[FAILED] 요약·전송 단계는 아직 구현되지 않았습니다.", secrets)
    _emit_error(f"       세션 산출물은 보존됩니다: {paths.root}", secrets)
    return EXIT_RUNTIME


def _record_failure(paths: SessionPaths, error: str) -> None:
    """감시 도중 죽은 경우의 최종 상태 기록 (FR-040, PRD 12절)."""
    doc: dict[str, object] = json.loads(paths.session_json.read_text(encoding="utf-8"))
    write_session_json(paths, transition(doc, SessionStatus.FAILED, error=error))


def _emit(message: str, secrets: Secrets) -> None:
    print(mask_secrets(message, secrets))


def _emit_error(message: str, secrets: Secrets) -> None:
    print(mask_secrets(message, secrets), file=sys.stderr)


def _dotenv_candidates() -> tuple[Path, ...]:
    """뒤쪽일수록 우선. exe 옆의 `.env` 도 보는 이유는 USB 실행 전제(FR-054) 때문이다."""
    candidates: list[Path] = [
        Path(sys.executable).resolve().parent / ".env",
        Path.cwd() / ".env",
    ]
    unique: list[Path] = []
    for path in candidates:
        if path not in unique:
            unique.append(path)
    return tuple(unique)


def _load_env_mapping() -> Mapping[str, str]:
    layers: list[Mapping[str, str | None]] = [
        dotenv_values(path, encoding="utf-8") for path in _dotenv_candidates() if path.is_file()
    ]
    return merge_env(layers, os.environ)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        # argparse 는 --help(0) 와 인자 오류(2) 를 모두 SystemExit 으로 던진다. 호출부가
        # 종료 코드를 값으로 받도록 여기서 환원한다.
        return EXIT_OK if exc.code in (0, None) else EXIT_CONFIG

    secrets = load_secrets(_load_env_mapping())

    try:
        now = datetime.now().astimezone()
        config = build_config(args, now)
        preflight, paths = bootstrap(
            config,
            secrets,
            now=now,
            id_suffix=secrets_module.token_hex(2),
        )
    except PreflightError as exc:
        _emit_error(f"[ERROR] {exc}", secrets)
        return exc.exit_code
    except KeyboardInterrupt:
        _emit_error("[ABORTED] 사용자가 종료했습니다.", secrets)
        return EXIT_ABORTED
    except OSError as exc:
        _emit_error(f"[ERROR] 세션 준비 중 실패: {exc}", secrets)
        return EXIT_RUNTIME

    selected = len(preflight.selection.selected)
    _emit(
        f"[OK] 감시 루트: {config.watch_root} "
        f"(대상 {selected}개 / 제외 {preflight.selection.excluded_count}개)",
        secrets,
    )
    _emit(f"[OK] 세션 시작: {paths.root}", secrets)
    if selected == 0:
        _emit("[WARN] 감시 대상 파일이 없습니다. --include 패턴이나 DIR 을 확인하세요.", secrets)

    try:
        return run_watch(preflight, paths, secrets)
    except KeyboardInterrupt:
        _emit_error("[ABORTED] 사용자가 종료했습니다.", secrets)
        return EXIT_ABORTED
    except OSError as exc:
        _emit_error(f"[ERROR] 실행 중 실패: {exc}", secrets)
        return EXIT_RUNTIME


if __name__ == "__main__":
    raise SystemExit(main())
