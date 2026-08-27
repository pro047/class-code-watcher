"""cli 모듈 — 인자 파싱·사전 점검·종료 코드·bootstrap 통합 (FR-001, FR-004, FR-006, FR-040)."""

import argparse
import json
import re
import socket
from datetime import datetime
from pathlib import Path

import pytest

from class_watcher import cli
from class_watcher.config import (
    DEFAULT_EXCLUDE,
    DEFAULT_INCLUDE,
    Secrets,
)

NOW = datetime(2026, 8, 26, 18, 30, 0)
FAKE_OPENAI_KEY = "sk-test-abcdef1234567890"


def _parse(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(list(argv))


def _make_tree(root: Path, count: int = 1) -> Path:
    tree = root / "tree"
    tree.mkdir(exist_ok=True)
    for index in range(count):
        (tree / f"file{index}.py").write_text(f"# {index}\n", encoding="utf-8")
    return tree


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """main() 이 저장소의 `.env` 나 실제 환경변수를 읽지 않게 격리한다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_dotenv_candidates", tuple)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    return tmp_path


# ── FR-001: 경로 검증 → 종료 코드 2, 세션 디렉터리 미생성 ────────────────────


def test_missing_dir_exits_config_without_session(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sessions = isolated_env / "sessions"
    rc = cli.main(["watch", str(isolated_env / "없는-경로"), "--session-dir", str(sessions)])
    assert rc == cli.EXIT_CONFIG
    assert not sessions.exists()
    assert "[ERROR]" in capsys.readouterr().err


def test_file_as_dir_exits_config(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = isolated_env / "not-a-dir.py"
    target.write_text("x\n", encoding="utf-8")
    sessions = isolated_env / "sessions"
    rc = cli.main(["watch", str(target), "--session-dir", str(sessions)])
    assert rc == cli.EXIT_CONFIG
    assert not sessions.exists()
    assert "디렉터리가 아닙니다" in capsys.readouterr().err


def test_default_dir_is_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    config = cli.build_config(_parse("watch"), NOW)
    assert config.watch_root == Path.cwd().resolve()


# ── FR-004 / 10.2: 기본값과 옵션 파싱 ────────────────────────────────────────


def test_all_defaults_when_options_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    config = cli.build_config(_parse("watch"), NOW)
    assert config.include == DEFAULT_INCLUDE
    assert config.exclude == DEFAULT_EXCLUDE
    assert config.max_files == 200
    assert config.debounce_ms == 750
    assert config.session_dir == Path("sessions")
    assert config.polling is False
    assert config.history is False
    assert config.dry_run is False
    assert config.no_discord is False
    assert config.allow_secrets is False


def test_include_option_splits_on_comma(tmp_path: Path) -> None:
    config = cli.build_config(_parse("watch", str(tmp_path), "--include", "*.java,*.xml"), NOW)
    assert config.include == ("*.java", "*.xml")


def test_include_option_strips_spaces(tmp_path: Path) -> None:
    config = cli.build_config(_parse("watch", str(tmp_path), "--include", "*.java, *.xml"), NOW)
    assert config.include == ("*.java", "*.xml")


def test_default_title_is_dirname_plus_time(tmp_path: Path) -> None:
    config = cli.build_config(_parse("watch", str(tmp_path)), NOW)
    assert config.title == f"{config.watch_root.name} 18:30"


def test_explicit_title_wins(tmp_path: Path) -> None:
    config = cli.build_config(_parse("watch", str(tmp_path), "--title", "자바 수업"), NOW)
    assert config.title == "자바 수업"


def test_help_returns_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == cli.EXIT_OK
    assert "class-watcher" in capsys.readouterr().out


def test_bad_argument_returns_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["watch", "--max-files", "abc"]) == cli.EXIT_CONFIG
    capsys.readouterr()


# ── FR-006: 파일 수 상한 ─────────────────────────────────────────────────────


def test_over_max_files_exits_config_with_hint(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _make_tree(isolated_env, count=5)
    sessions = isolated_env / "sessions"
    rc = cli.main(["watch", str(tree), "--max-files", "3", "--session-dir", str(sessions)])
    assert rc == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "상한 3개를 넘습니다" in err
    assert "상위 디렉터리를 지정했을 수 있습니다" in err
    # preflight 실패면 세션 디렉터리는 만들지 않는다.
    assert not sessions.exists()


def test_exactly_at_max_files_passes(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path, count=5)
    config = cli.build_config(_parse("watch", str(tree), "--max-files", "5"), NOW)
    preflight = cli.run_preflight(config)
    assert len(preflight.selection.selected) == 5


# ── FR-040: bootstrap 이 session.json(starting) 을 기록한다 ──────────────────


def test_bootstrap_writes_starting_session_json(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path, count=1)
    sessions = tmp_path / "sessions"
    config = cli.build_config(_parse("watch", str(tree), "--session-dir", str(sessions)), NOW)
    started_at = NOW.astimezone()
    secrets = Secrets(openai_api_key=None, discord_webhook_url=None)

    preflight, paths = cli.bootstrap(config, secrets, now=started_at, id_suffix="a1b2")

    assert paths.root == sessions / "20260826-183000-a1b2"
    assert paths.baseline_dir.is_dir()
    assert paths.final_dir.is_dir()
    assert len(preflight.selection.selected) == 1

    doc = json.loads(paths.session_json.read_text(encoding="utf-8"))
    assert doc["schema_version"] == "1.1"
    assert doc["session_id"] == "20260826-183000-a1b2"
    assert doc["status"] == "starting"
    assert doc["watch_root"] == str(config.watch_root)
    assert doc["watched_files"] == [{"path": "file0.py", "status": "unchanged"}]
    assert doc["watch_mode"] == "native"
    assert doc["diff_engine"] == "difflib"
    assert doc["started_at"] == started_at.isoformat()


def test_bootstrap_polling_flag_sets_watch_mode(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path, count=1)
    sessions = tmp_path / "sessions"
    config = cli.build_config(
        _parse("watch", str(tree), "--polling", "--session-dir", str(sessions)), NOW
    )
    secrets = Secrets(openai_api_key=None, discord_webhook_url=None)
    _, paths = cli.bootstrap(config, secrets, now=NOW.astimezone(), id_suffix="a1b2")
    doc = json.loads(paths.session_json.read_text(encoding="utf-8"))
    assert doc["watch_mode"] == "polling"


# ── 통합: 정상 트리에서 main → run_watch 스텁이 실패를 정직하게 기록 ─────────


def test_main_happy_path_records_stub_failure(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _make_tree(isolated_env, count=2)
    sessions = isolated_env / "sessions"
    rc = cli.main(["watch", str(tree), "--session-dir", str(sessions)])
    assert rc == cli.EXIT_RUNTIME

    session_roots = list(sessions.iterdir())
    assert len(session_roots) == 1
    root = session_roots[0]
    # 실제 main 이 만든 ID: 로컬 시각 + token_hex(2) suffix(소문자 hex 4자) — FR-002.
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", root.name)
    assert (root / "baseline").is_dir()
    assert (root / "final").is_dir()

    doc = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "failed"
    assert doc["error"] == "not_implemented"

    captured = capsys.readouterr()
    assert "[OK] 감시 루트:" in captured.out
    assert "대상 2개" in captured.out
    assert "[FAILED]" in captured.err


def test_main_warns_on_empty_selection_but_continues(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = isolated_env / "empty"
    empty.mkdir()
    sessions = isolated_env / "sessions"
    rc = cli.main(["watch", str(empty), "--session-dir", str(sessions)])
    # 대상 0개는 오류가 아니다 — 경고 후 계속 진행해 스텁 실패(1)로 끝난다.
    assert rc == cli.EXIT_RUNTIME
    assert "[WARN]" in capsys.readouterr().out
    assert len(list(sessions.iterdir())) == 1


# ── FR-003: 환경에 키가 있어도 stderr/stdout 에 원문이 없다 ──────────────────


def test_stderr_never_contains_secret_on_bad_args(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    rc = cli.main(["watch", "--max-files", "abc"])
    assert rc == cli.EXIT_CONFIG
    captured = capsys.readouterr()
    assert FAKE_OPENAI_KEY not in captured.err
    assert FAKE_OPENAI_KEY not in captured.out


def test_output_never_contains_secret_on_preflight_error(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    rc = cli.main(["watch", str(isolated_env / "없는-경로")])
    assert rc == cli.EXIT_CONFIG
    captured = capsys.readouterr()
    assert FAKE_OPENAI_KEY not in captured.err
    assert FAKE_OPENAI_KEY not in captured.out


# ── .env 로딩 우선순위 (설계 검증 기준 8·9) ──────────────────────────────────


def test_env_var_beats_dotenv_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=from-dotenv-file\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "from-env-var-123456")
    assert cli._load_env_mapping()["OPENAI_API_KEY"] == "from-env-var-123456"


def test_dotenv_file_used_when_env_var_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=from-dotenv-file\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert cli._load_env_mapping()["OPENAI_API_KEY"] == "from-dotenv-file"


# ── 불변식 회귀: 이 단계는 네트워크를 전혀 만지지 않는다 (FR-030/FR-035 기반) ──


def test_full_run_never_touches_network(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _forbid(*args: object, **kwargs: object) -> object:
        raise AssertionError("bootstrap-cli 는 네트워크를 만지면 안 된다 (FR-030/FR-035)")

    monkeypatch.setattr(socket, "socket", _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)

    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"
    # 정상 경로와 preflight 실패 경로 모두 소켓을 열지 않는다.
    assert cli.main(["watch", str(tree), "--session-dir", str(sessions)]) == cli.EXIT_RUNTIME
    assert cli.main(["watch", str(isolated_env / "없음")]) == cli.EXIT_CONFIG
    capsys.readouterr()


def test_package_has_no_network_client_imports() -> None:
    """구조적 보장: openai·httpx 등 네트워크 클라이언트를 import 하는 모듈이 없다."""
    package_dir = Path(cli.__file__).resolve().parent
    banned = ("openai", "httpx", "requests", "aiohttp", "urllib", "socket", "http")
    for module_path in sorted(package_dir.glob("*.py")):
        source = module_path.read_text(encoding="utf-8")
        for name in banned:
            assert f"import {name}" not in source, f"{module_path.name} 이 {name} 을 import 한다"
            assert f"from {name} import" not in source, (
                f"{module_path.name} 이 {name} 을 import 한다"
            )
