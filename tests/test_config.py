"""config 모듈 — 기본값·비밀값 로딩·마스킹 (FR-003, FR-004, FR-005, FR-042)."""

from class_watcher.config import (
    DEFAULT_DEBOUNCE_MS,
    DEFAULT_EXCLUDE,
    DEFAULT_INCLUDE,
    DEFAULT_MAX_FILES,
    MASK_PLACEHOLDER,
    Secrets,
    load_secrets,
    mask_secrets,
    merge_env,
)

# 8자 이상이어야 마스킹 대상이다 (config.MIN_MASKABLE_LENGTH = 8).
FAKE_OPENAI_KEY = "sk-test-abcdef1234567890"
FAKE_WEBHOOK = "https://discord.example/api/webhooks/1234/AbCdEf"


def _secrets() -> Secrets:
    return Secrets(openai_api_key=FAKE_OPENAI_KEY, discord_webhook_url=FAKE_WEBHOOK)


# ── FR-004 / FR-005: 문서화된 기본값 ──────────────────────────────────────────


def test_documented_default_constants() -> None:
    assert DEFAULT_MAX_FILES == 200
    assert DEFAULT_DEBOUNCE_MS == 750
    # FR-005 수용 기준이 이름으로 지목한 7종.
    assert DEFAULT_EXCLUDE == (
        "node_modules",
        ".git",
        "target",
        "build",
        "dist",
        "__pycache__",
        ".venv",
    )
    # 설계가 확정한 기본 allowlist 16종. json·md 는 의도적으로 없다.
    assert len(DEFAULT_INCLUDE) == 16
    assert "*.py" in DEFAULT_INCLUDE
    assert "*.java" in DEFAULT_INCLUDE
    assert "*.json" not in DEFAULT_INCLUDE
    assert "*.md" not in DEFAULT_INCLUDE


# ── FR-003: Secrets 의 repr/str 이 원문을 내지 않는다 ────────────────────────


def test_secrets_repr_and_str_hide_values() -> None:
    secrets = _secrets()
    for rendered in (repr(secrets), str(secrets)):
        assert FAKE_OPENAI_KEY not in rendered
        assert FAKE_WEBHOOK not in rendered
        assert "<set>" in rendered
    # PRD 13.1: 마지막 4자조차 출력하지 않는다.
    assert FAKE_OPENAI_KEY[-4:] not in repr(secrets)


def test_secrets_repr_marks_missing() -> None:
    rendered = repr(Secrets(openai_api_key=None, discord_webhook_url=None))
    assert "<missing>" in rendered
    assert "<set>" not in rendered


# ── FR-003 / FR-042: mask_secrets ────────────────────────────────────────────


def test_mask_secrets_replaces_raw_values() -> None:
    masked = mask_secrets(f"에러: {FAKE_OPENAI_KEY} 로 실패 ({FAKE_WEBHOOK})", _secrets())
    assert FAKE_OPENAI_KEY not in masked
    assert FAKE_WEBHOOK not in masked
    assert MASK_PLACEHOLDER in masked


def test_mask_secrets_keeps_unrelated_text() -> None:
    text = "[OK] 감시 루트: C:\\work\\class (대상 12개 / 제외 3개)"
    assert mask_secrets(text, _secrets()) == text


def test_mask_secrets_ignores_short_values() -> None:
    # 8자 미만 값은 치환 폭주 방지를 위해 마스킹하지 않는다 (확정된 동작 계약).
    secrets = Secrets(openai_api_key="short", discord_webhook_url=None)
    assert mask_secrets("short 가 포함된 문장", secrets) == "short 가 포함된 문장"


def test_mask_secrets_with_no_secrets_is_identity() -> None:
    secrets = Secrets(openai_api_key=None, discord_webhook_url=None)
    assert mask_secrets("아무 일도 없다", secrets) == "아무 일도 없다"


# ── FR-003: load_secrets ─────────────────────────────────────────────────────


def test_load_secrets_reads_both_keys() -> None:
    secrets = load_secrets(
        {"OPENAI_API_KEY": FAKE_OPENAI_KEY, "DISCORD_WEBHOOK_URL": FAKE_WEBHOOK}
    )
    assert secrets.openai_api_key == FAKE_OPENAI_KEY
    assert secrets.discord_webhook_url == FAKE_WEBHOOK


def test_load_secrets_missing_keys_are_none() -> None:
    secrets = load_secrets({})
    assert secrets.openai_api_key is None
    assert secrets.discord_webhook_url is None


def test_load_secrets_blank_value_is_none() -> None:
    secrets = load_secrets({"OPENAI_API_KEY": "   "})
    assert secrets.openai_api_key is None


# ── merge_env: .env 레이어와 환경변수의 병합 규칙 ────────────────────────────


def test_merge_env_environ_wins_over_dotenv() -> None:
    merged = merge_env(
        [{"OPENAI_API_KEY": "from-dotenv-file"}],
        {"OPENAI_API_KEY": "from-env-var-123456"},
    )
    assert merged["OPENAI_API_KEY"] == "from-env-var-123456"


def test_merge_env_later_layer_wins() -> None:
    merged = merge_env([{"K": "exe-side"}, {"K": "cwd-side"}], {})
    assert merged["K"] == "cwd-side"


def test_merge_env_drops_none_values() -> None:
    # dotenv 는 값 없는 키를 None 으로 준다 — 병합에 싣지 않는다.
    merged = merge_env([{"EMPTY": None, "K": "v"}], {})
    assert "EMPTY" not in merged
    assert merged["K"] == "v"
