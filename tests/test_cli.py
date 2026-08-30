"""cli 모듈 — 인자 파싱·사전 점검·종료 코드·bootstrap 통합 (FR-001, FR-004, FR-006, FR-040)."""

import argparse
import json
import re
import socket
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from class_watcher import cli, watcher
from class_watcher.config import (
    DEFAULT_EXCLUDE,
    DEFAULT_INCLUDE,
    Secrets,
)
from class_watcher.debounce import Debouncer, RawEvent

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


def test_max_files_hint_survives_cp949_console(tmp_path: Path) -> None:
    # 콘솔 출력이 파이프·파일로 리다이렉트되면 stdout 인코딩이 cp949 가 된다.
    # 안내 문구에 cp949 밖 문자(em dash 등)가 있으면 그 자리에서 UnicodeEncodeError 로
    # 죽는다. capsys 는 실제 인코딩을 타지 않아 못 잡으므로 문구를 직접 인코딩해 본다.
    tree = _make_tree(tmp_path, count=5)
    config = cli.build_config(_parse("watch", str(tree), "--max-files", "3"), NOW)
    with pytest.raises(cli.PreflightError) as excinfo:
        cli.run_preflight(config)
    str(excinfo.value).encode("cp949")


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


# ── 통합: 정상 트리에서 main → 감시 루프 → finalize 가 끝까지 돈다 ───────────
#
# 감시 루프는 Ctrl+C 전까지 반환하지 않으므로, 루프 심장인 `_drain_queue` 를 패치해
# 스크립트를 소진하면 KeyboardInterrupt(1회차)를 흉내낸다. watchdog Observer 는 실제로
# 뜨고, baseline → watching → finalizing → completed/partial 전이와 산출물이 전부
# 실제로 만들어진다 (IMPL 4.1 방식 2).


def _interrupt_after(
    monkeypatch: pytest.MonkeyPatch, steps: list[Callable[[Debouncer], None]]
) -> None:
    iterator = iter(steps)

    def fake_drain(sink: object, debouncer: Debouncer) -> None:
        step = next(iterator, None)
        if step is None:
            raise KeyboardInterrupt
        step(debouncer)

    monkeypatch.setattr(watcher, "_drain_queue", fake_drain)


def test_main_happy_path_no_change_completes(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _make_tree(isolated_env, count=2)
    sessions = isolated_env / "sessions"
    _interrupt_after(monkeypatch, [])
    rc = cli.main(["watch", str(tree), "--session-dir", str(sessions)])
    # 변경 없음 → completed / 코드 0 (FR-035).
    assert rc == cli.EXIT_OK

    session_roots = list(sessions.iterdir())
    assert len(session_roots) == 1
    root = session_roots[0]
    # 실제 main 이 만든 ID: 로컬 시각 + token_hex(2) suffix(소문자 hex 4자) — FR-002.
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", root.name)
    assert (root / "baseline").is_dir()
    assert (root / "final").is_dir()

    doc = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "completed"
    assert doc["no_change"] is True
    assert "error" not in doc
    assert doc["ended_at"]
    assert {file["status"] for file in doc["watched_files"]} == {"unchanged"}

    captured = capsys.readouterr()
    assert "[OK] 감시 루트:" in captured.out
    assert "대상 2개" in captured.out
    assert "[DONE] 변경 없음" in captured.out
    # 변경 없음 경로에는 [FAILED] 가 나오지 않는다.
    assert "[FAILED]" not in captured.err


def test_main_changed_session_ends_partial(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"

    def change(debouncer: Debouncer) -> None:
        (tree / "file0.py").write_text("# changed\n", encoding="utf-8")
        debouncer.observe(RawEvent(rel_path="file0.py", kind="modified", at=0.0))

    _interrupt_after(monkeypatch, [change])
    rc = cli.main(["watch", str(tree), "--session-dir", str(sessions)])
    # 변경 있음 + 키 없음(isolated_env) → 요약 실패 → partial / 코드 1 (설계 6.6 매핑).
    assert rc == cli.EXIT_RUNTIME

    [root] = list(sessions.iterdir())
    doc = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "partial"
    assert doc["error"] == "openai_key_missing"
    assert doc["openai"] == {"calls": 0, "retries": 0, "model": None, "request_id": None}

    captured = capsys.readouterr()
    assert "[FAILED] 요약을 만들지 못했습니다." in captured.err
    captured.err.encode("cp949")


def test_main_secret_session_blocks_without_network(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # FR-036 + 불변식(FR-030/FR-035 기반): 탐지-차단 세션도 네트워크를 전혀 만지지 않고,
    # 로컬 산출물(redaction.json 포함)만 남긴 채 코드 1 로 끝난다.
    def _forbid(*args: object, **kwargs: object) -> object:
        raise AssertionError("차단 세션은 네트워크를 만지면 안 된다 (FR-036)")

    monkeypatch.setattr(socket, "socket", _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)
    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"
    planted = "sk-fixture0123456789abcdefgh"

    def change(debouncer: Debouncer) -> None:
        (tree / "file0.py").write_text(f"# {planted}\n", encoding="utf-8")
        debouncer.observe(RawEvent(rel_path="file0.py", kind="modified", at=0.0))

    _interrupt_after(monkeypatch, [change])
    rc = cli.main(["watch", str(tree), "--session-dir", str(sessions)])
    assert rc == cli.EXIT_RUNTIME

    [root] = list(sessions.iterdir())
    doc = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "failed"
    assert doc["error"] == "secrets_detected"
    assert (root / "redaction.json").is_file()

    captured = capsys.readouterr()
    assert "[FAILED] 비밀정보 패턴이 탐지되어 외부 전송을 중단했습니다." in captured.err
    assert "redaction.json" in captured.err
    # FR-042: 콘솔 어디에도 탐지 원문이 없다.
    assert planted not in captured.out + captured.err


def test_main_allow_secrets_masks_and_continues(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # FR-038: --allow-secrets 는 마스킹 후 기존 과도기 경로(partial / 코드 1)로 진행한다.
    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"

    def change(debouncer: Debouncer) -> None:
        (tree / "file0.py").write_text("# sk-fixture0123456789abcdefgh\n", encoding="utf-8")
        debouncer.observe(RawEvent(rel_path="file0.py", kind="modified", at=0.0))

    _interrupt_after(monkeypatch, [change])
    rc = cli.main(["watch", str(tree), "--allow-secrets", "--session-dir", str(sessions)])
    assert rc == cli.EXIT_RUNTIME

    [root] = list(sessions.iterdir())
    doc = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "partial"
    # 마스킹 후 요약 지점까지 진행했고, 키가 없으므로(isolated_env) 거기서 멈춘다.
    assert doc["error"] == "openai_key_missing"
    redaction = json.loads((root / "redaction.json").read_text(encoding="utf-8"))
    assert redaction["policy"] == "mask"
    assert redaction["allow_secrets"] is True

    captured = capsys.readouterr()
    assert "마스킹 후 진행합니다" in captured.out
    assert "비밀정보 패턴이 탐지되어 외부 전송을 중단했습니다" not in captured.err


def test_main_warns_on_empty_selection_but_continues(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = isolated_env / "empty"
    empty.mkdir()
    sessions = isolated_env / "sessions"
    _interrupt_after(monkeypatch, [])
    rc = cli.main(["watch", str(empty), "--session-dir", str(sessions)])
    # 대상 0개는 오류가 아니다 — 경고 후 계속 진행하고, 변경 없음이므로 completed/0.
    assert rc == cli.EXIT_OK
    assert "[WARN]" in capsys.readouterr().out
    [root] = list(sessions.iterdir())
    doc = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "completed"
    assert doc["no_change"] is True


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
        raise AssertionError("watch 세션은 네트워크를 만지면 안 된다 (FR-030/FR-035)")

    monkeypatch.setattr(socket, "socket", _forbid)
    monkeypatch.setattr(socket, "create_connection", _forbid)

    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"

    # ① 변경 없음 전체 세션 (감시 → finalize): OpenAI·Discord 모두 0회 (FR-035).
    _interrupt_after(monkeypatch, [])
    assert cli.main(["watch", str(tree), "--session-dir", str(sessions)]) == cli.EXIT_OK

    # ② 저장 이벤트가 있는 세션: 이벤트 처리(해시·로그·snapshot) 중에도 호출 0회 (FR-030).
    def change(debouncer: Debouncer) -> None:
        (tree / "file0.py").write_text("# saved\n", encoding="utf-8")
        debouncer.observe(RawEvent(rel_path="file0.py", kind="modified", at=0.0))

    _interrupt_after(monkeypatch, [change])
    assert cli.main(["watch", str(tree), "--session-dir", str(sessions)]) == cli.EXIT_RUNTIME

    # ③ preflight 실패 경로도 소켓을 열지 않는다.
    assert cli.main(["watch", str(isolated_env / "없음")]) == cli.EXIT_CONFIG
    capsys.readouterr()


def test_network_client_imports_only_in_designated_adapter() -> None:
    """구조적 보장: 네트워크 클라이언트 import 는 어댑터(openai_client.py) 한 곳뿐이다.

    설계 3.3 이 외부 API 표면을 openai_client.py 하나에 격리했다. 다른 모듈이 SDK 를
    직접 import 하기 시작하면 FR-030 의 호출 계수가 mock 경계(CallFn) 밖으로 샌다.
    """
    package_dir = Path(cli.__file__).resolve().parent
    banned = ("openai", "httpx", "requests", "aiohttp", "urllib", "socket", "http")
    adapter = "openai_client.py"
    for module_path in sorted(package_dir.glob("*.py")):
        if module_path.name == adapter:
            continue
        source = module_path.read_text(encoding="utf-8")
        for name in banned:
            assert f"import {name}" not in source, f"{module_path.name} 이 {name} 을 import 한다"
            assert f"from {name} import" not in source, (
                f"{module_path.name} 이 {name} 을 import 한다"
            )
    # 어댑터 자신은 openai 만 쓴다 — httpx 등 다른 클라이언트를 늘리지 않는다.
    adapter_source = (package_dir / adapter).read_text(encoding="utf-8")
    assert "from openai import" in adapter_source
    for name in ("httpx", "requests", "aiohttp", "socket"):
        assert f"import {name}" not in adapter_source
