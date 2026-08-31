"""cli 모듈 — 인자 파싱·사전 점검·종료 코드·bootstrap 통합 (FR-001, FR-004, FR-006, FR-040)."""

import argparse
import json
import re
import socket
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

import pytest

from class_watcher import cli, notify, watcher
from class_watcher.config import (
    DEFAULT_EXCLUDE,
    DEFAULT_INCLUDE,
    Secrets,
)
from class_watcher.debounce import Debouncer, RawEvent
from class_watcher.notify import DiscordRequestError
from class_watcher.summarize import BuiltPrompt, LlmResponse

NOW = datetime(2026, 8, 26, 18, 30, 0)
FAKE_OPENAI_KEY = "sk-test-abcdef1234567890"
# 실제 Discord 도메인이 아니다. 이 값이 콘솔·산출물로 새는지를 보는 표식이다.
FAKE_WEBHOOK = "https://discord.example/api/webhooks/1234567890/super-secret-token"

# tests/ 에 conftest.py 를 두지 않는다(파이프라인이 설정 파일 신설을 금지한다). 그래서
# 4단계 fixture 를 test_watcher.py 와 같은 내용으로 여기에도 둔다. 한글·ASCII 뿐이라
# cp949 안전하다 — 이모지를 넣으면 아래 cp949 회귀 단언이 깨진다.
VALID_SUMMARY_TEXT = json.dumps(
    {
        "session_title": "검증",
        "summary": "값을 바꾸는 변경을 했다.",
        "change_stats": {"files_changed": 1, "added_lines": 1, "deleted_lines": 1},
        "changes": [
            {
                "file": "file0.py",
                "area": "핵심",
                "type": "modified",
                "description": "변수 값을 바꾸는 코드",
                "evidence": "x = 2",
            }
        ],
        "learning_points": [],
        "questions_to_review": [],
        "risks_or_todos": [],
        "sensitive_data_detected": False,
    },
    ensure_ascii=False,
)


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


def test_network_client_imports_only_in_designated_adapters() -> None:
    """구조적 보장: 네트워크 클라이언트 import 는 지정된 어댑터 2개뿐이다.

    4단계 설계 3.3 이 OpenAI 표면을 openai_client.py 에, 5단계 설계 4.2 가 Discord 표면을
    discord_client.py 에 격리했다. 다른 모듈이 SDK·HTTP 클라이언트를 직접 import 하기
    시작하면 FR-030/FR-035 의 호출 계수가 mock 경계(CallFn·SendFn) 밖으로 샌다.
    """
    package_dir = Path(cli.__file__).resolve().parent
    banned = ("openai", "httpx", "requests", "aiohttp", "urllib", "socket", "http")
    adapters = ("openai_client.py", "discord_client.py")
    for module_path in sorted(package_dir.glob("*.py")):
        if module_path.name in adapters:
            continue
        source = module_path.read_text(encoding="utf-8")
        for name in banned:
            assert f"import {name}" not in source, f"{module_path.name} 이 {name} 을 import 한다"
            assert f"from {name} import" not in source, (
                f"{module_path.name} 이 {name} 을 import 한다"
            )
    # 각 어댑터는 자기 클라이언트 하나만 쓴다 — 표면이 섞이면 격리가 무의미해진다.
    openai_source = (package_dir / "openai_client.py").read_text(encoding="utf-8")
    assert "from openai import" in openai_source
    for name in ("httpx", "requests", "aiohttp", "socket"):
        assert f"import {name}" not in openai_source
    discord_source = (package_dir / "discord_client.py").read_text(encoding="utf-8")
    assert "import httpx" in discord_source
    for name in ("openai", "requests", "aiohttp", "socket"):
        assert f"import {name}" not in discord_source
    # httpx 를 import 하는 파일은 discord_client.py 하나뿐이다 (IMPL 4.1 ①).
    httpx_importers = [
        module_path.name
        for module_path in sorted(package_dir.glob("*.py"))
        if "import httpx" in module_path.read_text(encoding="utf-8")
    ]
    assert httpx_importers == ["discord_client.py"]


# ── 5단계 전송: 종료 코드 매핑·실패 안내·정리 안내·마스킹 (케이스 33~36) ──────
#
# main() 전체를 돌리되 두 어댑터 팩토리를 갈아끼운다 — 네트워크는 나가지 않는다.


def _patch_summary(monkeypatch: pytest.MonkeyPatch, text: str) -> list[BuiltPrompt]:
    prompts: list[BuiltPrompt] = []

    def call(prompt: BuiltPrompt) -> LlmResponse:
        prompts.append(prompt)
        return LlmResponse(text=text, request_id="req-1", model="fake-model")

    def factory(api_key: str, model: str) -> Callable[[BuiltPrompt], LlmResponse]:
        return call

    monkeypatch.setattr(watcher, "make_openai_caller", factory)
    return prompts


def _patch_sender(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[int | DiscordRequestError]
) -> list[dict[str, object]]:
    remaining = list(outcomes)
    sent: list[dict[str, object]] = []

    def send(payload: Mapping[str, object]) -> int:
        sent.append(dict(payload))
        outcome = remaining.pop(0)
        if isinstance(outcome, DiscordRequestError):
            raise outcome
        return outcome

    def factory(webhook_url: str) -> Callable[[Mapping[str, object]], int]:
        return send

    monkeypatch.setattr(watcher, "make_discord_sender", factory)
    return sent


def _forbid_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory(webhook_url: str) -> Callable[[Mapping[str, object]], int]:
        raise AssertionError("이 경로는 Discord 전송 함수를 만들면 안 된다 (FR-035/FR-052)")

    monkeypatch.setattr(watcher, "make_discord_sender", factory)


def _change_file0(tree: Path) -> Callable[[Debouncer], None]:
    def change(debouncer: Debouncer) -> None:
        (tree / "file0.py").write_text("# changed\n", encoding="utf-8")
        debouncer.observe(RawEvent(rel_path="file0.py", kind="modified", at=0.0))

    return change


def test_main_successful_delivery_exits_zero(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 케이스 33 (PRD 10.3): 전송 성공은 completed / 코드 0 이다.
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", FAKE_WEBHOOK)
    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"
    _interrupt_after(monkeypatch, [_change_file0(tree)])
    _patch_summary(monkeypatch, VALID_SUMMARY_TEXT)
    sent = _patch_sender(monkeypatch, [204])

    rc = cli.main(["watch", str(tree), "--session-dir", str(sessions)])

    assert rc == cli.EXIT_OK
    assert len(sent) == 1
    [root] = list(sessions.iterdir())
    doc = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "completed"
    assert "error" not in doc
    assert doc["discord"] == {
        "delivered": True,
        "http_status": 204,
        "requests": 1,
        "chunks": 1,
        "skip_reason": None,
    }
    captured = capsys.readouterr()
    assert "[DONE] 요약과 Discord 전송을 완료했습니다" in captured.out
    captured.out.encode("cp949")
    captured.err.encode("cp949")


def test_main_delivery_failure_points_at_the_payload_file(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 케이스 34 (FR-034): 전송 실패는 코드 1 이고, 콘솔이 수동 복사 경로를 알려 준다.
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", FAKE_WEBHOOK)
    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"
    _interrupt_after(monkeypatch, [_change_file0(tree)])
    _patch_summary(monkeypatch, VALID_SUMMARY_TEXT)
    _patch_sender(monkeypatch, [DiscordRequestError(notify.KIND_HTTP, http_status=503)])

    rc = cli.main(["watch", str(tree), "--session-dir", str(sessions)])

    assert rc == cli.EXIT_RUNTIME
    [root] = list(sessions.iterdir())
    doc = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "partial"
    assert doc["error"] == "discord_http_503"
    assert (root / "discord_payload.json").is_file()
    captured = capsys.readouterr()
    assert "[FAILED] Discord 전송에 실패했습니다." in captured.err
    assert str(root / "discord_payload.json") in captured.err
    assert "session.json" in captured.err
    captured.err.encode("cp949")


def test_main_missing_webhook_url_is_a_failure_not_a_skip(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 케이스 33 (설계 5.6): --no-discord 를 안 준 사용자는 전송을 기대했다. 생략이 아니다.
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"
    _interrupt_after(monkeypatch, [_change_file0(tree)])
    _patch_summary(monkeypatch, VALID_SUMMARY_TEXT)
    _forbid_sender(monkeypatch)

    rc = cli.main(["watch", str(tree), "--session-dir", str(sessions)])

    assert rc == cli.EXIT_RUNTIME
    [root] = list(sessions.iterdir())
    doc = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "partial"
    assert doc["error"] == "discord_url_missing"
    assert capsys.readouterr().err.count("[FAILED]") >= 1


def test_main_no_discord_option_exits_zero_without_sending(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 케이스 33 (PRD 10.2): --no-discord 는 성공적 생략이다 — 전송 0회, 코드 0.
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", FAKE_WEBHOOK)
    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"
    _interrupt_after(monkeypatch, [_change_file0(tree)])
    _patch_summary(monkeypatch, VALID_SUMMARY_TEXT)
    _forbid_sender(monkeypatch)

    rc = cli.main(["watch", str(tree), "--no-discord", "--session-dir", str(sessions)])

    assert rc == cli.EXIT_OK
    [root] = list(sessions.iterdir())
    doc = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "completed"
    assert doc["discord"]["skip_reason"] == notify.SKIP_NO_DISCORD
    assert doc["discord"]["requests"] == 0
    captured = capsys.readouterr()
    assert "전송은 생략합니다" in captured.out
    captured.out.encode("cp949")


def test_main_dry_run_exits_zero_without_sending(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 케이스 33: dry-run 도 전송 0회 / 코드 0 이다. "요약 없음"과 다른 분기다.
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", FAKE_WEBHOOK)
    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"
    _interrupt_after(monkeypatch, [_change_file0(tree)])
    _forbid_sender(monkeypatch)

    rc = cli.main(["watch", str(tree), "--dry-run", "--session-dir", str(sessions)])

    assert rc == cli.EXIT_OK
    [root] = list(sessions.iterdir())
    doc = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "completed"
    assert doc["discord"]["skip_reason"] == notify.SKIP_DRY_RUN
    assert not (root / "discord_payload.json").exists()
    capsys.readouterr()


def test_main_second_ctrl_c_exits_one_thirty(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 케이스 33 (PRD 10.3): 두 번째 Ctrl+C 는 130. 전송 배선이 이 값을 바꾸지 않는다.
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", FAKE_WEBHOOK)
    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"

    def second_ctrl_c(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    _interrupt_after(monkeypatch, [_change_file0(tree)])
    monkeypatch.setattr(watcher, "wait_for_stability", second_ctrl_c)
    _forbid_sender(monkeypatch)

    rc = cli.main(["watch", str(tree), "--session-dir", str(sessions)])

    assert rc == cli.EXIT_ABORTED
    [root] = list(sessions.iterdir())
    doc = json.loads((root / "session.json").read_text(encoding="utf-8"))
    assert doc["status"] == "failed"
    assert doc["discord"]["requests"] == 0
    capsys.readouterr()


@pytest.mark.parametrize(
    ("argv_extra", "planted", "expected_rc"),
    [
        # 케이스 35: 세션 디렉터리가 만들어진 모든 종료 경로에서 마지막에 나온다.
        ((), None, cli.EXIT_OK),  # 변경 없음
        (("--no-discord",), "# changed\n", cli.EXIT_OK),  # 요약 성공 + 전송 생략
        ((), "# sk-fixture0123456789abcdefgh\n", cli.EXIT_RUNTIME),  # 비밀값 차단
    ],
)
def test_cleanup_notice_closes_every_session_path(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv_extra: tuple[str, ...],
    planted: str | None,
    expected_rc: int,
) -> None:
    # FR-053: 공용 PC 정리 안내. run_watch 의 return 지점이 9개라 main 의 finally 에
    # 한 번만 붙인다 — 한 곳을 빠뜨리면 P1 요구사항이 조용히 새는 형태가 된다.
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", FAKE_WEBHOOK)
    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"
    steps: list[Callable[[Debouncer], None]] = []
    if planted is not None:
        content = planted

        def change(debouncer: Debouncer) -> None:
            (tree / "file0.py").write_text(content, encoding="utf-8")
            debouncer.observe(RawEvent(rel_path="file0.py", kind="modified", at=0.0))

        steps.append(change)
    _interrupt_after(monkeypatch, steps)
    _patch_summary(monkeypatch, VALID_SUMMARY_TEXT)
    _forbid_sender(monkeypatch)

    rc = cli.main(["watch", str(tree), *argv_extra, "--session-dir", str(sessions)])

    assert rc == expected_rc
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[-4].startswith("[정리 안내]")
    tail = "\n".join(lines[-4:])
    [root] = list(sessions.iterdir())
    assert str(root) in tail
    assert "OPENAI_API_KEY" in tail
    assert "DISCORD_WEBHOOK_URL" in tail
    # 값·존재 여부는 찍지 않는다 (FR-003).
    assert FAKE_OPENAI_KEY not in captured.out
    assert FAKE_WEBHOOK not in captured.out
    captured.out.encode("cp949")


def test_cleanup_notice_closes_the_delivery_failure_path(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 케이스 35 의 나머지 한 갈래 — 위 parametrize 는 전송을 금지하므로 실패 경로를
    # 못 탄다. 안내는 종료 코드와 무관하게 나와야 한다 (설계 5.9).
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", FAKE_WEBHOOK)
    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"
    _interrupt_after(monkeypatch, [_change_file0(tree)])
    _patch_summary(monkeypatch, VALID_SUMMARY_TEXT)
    _patch_sender(monkeypatch, [DiscordRequestError(notify.KIND_HTTP, http_status=500)])

    rc = cli.main(["watch", str(tree), "--session-dir", str(sessions)])

    assert rc == cli.EXIT_RUNTIME
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[-4].startswith("[정리 안내]")
    [root] = list(sessions.iterdir())
    assert str(root) in "\n".join(lines[-4:])
    assert FAKE_WEBHOOK not in captured.out


def test_cleanup_notice_is_absent_when_no_session_was_created(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 안내할 산출물이 없으면 안내하지 않는다 (설계 5.9).
    rc = cli.main(["watch", str(isolated_env / "없는-경로")])

    assert rc == cli.EXIT_CONFIG
    captured = capsys.readouterr()
    assert "[정리 안내]" not in captured.out + captured.err


def test_webhook_url_never_appears_in_any_output_or_artifact(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 케이스 36 (FR-003, FR-042): Webhook URL 은 비밀값이다(PRD 13.1). 전송 실패 세션은
    # httpx 예외 메시지가 URL 을 물고 올라올 수 있는 유일한 경로라 여기서 본다.
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI_KEY)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", FAKE_WEBHOOK)
    tree = _make_tree(isolated_env, count=1)
    sessions = isolated_env / "sessions"
    _interrupt_after(monkeypatch, [_change_file0(tree)])
    _patch_summary(monkeypatch, VALID_SUMMARY_TEXT)
    leaked = DiscordRequestError(notify.KIND_HTTP, http_status=404)
    _patch_sender(monkeypatch, [leaked])

    rc = cli.main(["watch", str(tree), "--session-dir", str(sessions)])

    assert rc == cli.EXIT_RUNTIME
    captured = capsys.readouterr()
    assert FAKE_WEBHOOK not in captured.out
    assert FAKE_WEBHOOK not in captured.err
    assert FAKE_OPENAI_KEY not in captured.out + captured.err
    [root] = list(sessions.iterdir())
    for name in ("session.json", "discord_payload.json", "errors.jsonl", "summary.json"):
        path = root / name
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
            assert FAKE_WEBHOOK not in raw, f"{name} 에 Webhook URL 이 남았다"
            assert FAKE_OPENAI_KEY not in raw, f"{name} 에 키가 남았다"
