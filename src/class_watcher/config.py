"""설정 자료구조·기본값·비밀값 로딩과 마스킹.

부작용(환경변수 접근, `.env` 파일 읽기)은 이 모듈에 두지 않는다. 호출부(cli)가 읽어서
Mapping 으로 주입하고, 여기서는 순수 함수로만 판정한다 — 그래야 테스트가 파일시스템 없이 돈다.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# PRD 10.2 는 "언어별 기본 allowlist" 라고만 쓰고 열거하지 않는다. FR-004 가 "문서화된 기본값"을
# 요구하므로 여기서 확정한다. *.json·*.md 는 lock 파일·문서 노이즈 때문에 뺐다 (--include 로 덮어쓴다).
DEFAULT_INCLUDE: tuple[str, ...] = (
    "*.py",
    "*.java",
    "*.js",
    "*.ts",
    "*.jsx",
    "*.tsx",
    "*.html",
    "*.css",
    "*.sql",
    "*.xml",
    "*.kt",
    "*.c",
    "*.cpp",
    "*.h",
    "*.cs",
    "*.go",
)

# FR-005 수용 기준이 이름으로 지목한 7종.
DEFAULT_EXCLUDE: tuple[str, ...] = (
    "node_modules",
    ".git",
    "target",
    "build",
    "dist",
    "__pycache__",
    ".venv",
)

DEFAULT_MAX_FILES: int = 200
DEFAULT_DEBOUNCE_MS: int = 750
DEFAULT_SESSION_DIR: str = "./sessions"

# PRD 13.1 이 지정한 키 이름.
ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
ENV_DISCORD_WEBHOOK_URL = "DISCORD_WEBHOOK_URL"

MASK_PLACEHOLDER = "[MASKED]"

# 이보다 짧은 값은 무관한 문자열까지 지워버려 출력을 망가뜨린다.
MIN_MASKABLE_LENGTH = 8


@dataclass(frozen=True)
class WatchConfig:
    """한 세션의 확정된 설정. 인자 파싱 이후로는 바뀌지 않는다."""

    watch_root: Path
    title: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    max_files: int
    debounce_ms: int
    polling: bool
    history: bool
    session_dir: Path
    dry_run: bool
    no_discord: bool
    allow_secrets: bool


@dataclass(frozen=True, repr=False)
class Secrets:
    """비밀값 보관함.

    repr/str 을 막아 두는 이유: 예외 stack 이나 로깅이 객체를 통째로 찍는 순간 원문이 새기
    때문이다 (FR-003). PRD 13.1 기준에 따라 마지막 4자도 노출하지 않는다.
    """

    openai_api_key: str | None
    discord_webhook_url: str | None

    def __repr__(self) -> str:
        return (
            f"Secrets(openai_api_key={_presence(self.openai_api_key)}, "
            f"discord_webhook_url={_presence(self.discord_webhook_url)})"
        )

    def __str__(self) -> str:
        return self.__repr__()


def _presence(value: str | None) -> str:
    return "<set>" if value else "<missing>"


def _clean(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def load_secrets(env: Mapping[str, str]) -> Secrets:
    """병합된 환경 Mapping 에서 비밀값을 읽는다. 없으면 None — 여기서는 오류가 아니다."""
    return Secrets(
        openai_api_key=_clean(env.get(ENV_OPENAI_API_KEY)),
        discord_webhook_url=_clean(env.get(ENV_DISCORD_WEBHOOK_URL)),
    )


def merge_env(
    dotenv_layers: Sequence[Mapping[str, str | None]],
    environ: Mapping[str, str],
) -> dict[str, str]:
    """`.env` 레이어들과 환경변수를 병합한다. 뒤쪽 레이어가, 그리고 환경변수가 최종 우선이다.

    환경변수를 이기게 두는 이유: 실행 시점에 사람이 명시적으로 준 값이기 때문이다.
    dotenv 는 값 없는 키를 None 으로 주므로 그런 키는 아예 싣지 않는다.
    """
    merged: dict[str, str] = {}
    for layer in dotenv_layers:
        for key, value in layer.items():
            if value is not None:
                merged[key] = value
    merged.update(environ)
    return merged


def mask_secrets(text: str, secrets: Secrets) -> str:
    """콘솔·예외 메시지가 반드시 거쳐 가는 관문 (FR-003, FR-042)."""
    masked = text
    for value in (secrets.openai_api_key, secrets.discord_webhook_url):
        if value and len(value) >= MIN_MASKABLE_LENGTH:
            masked = masked.replace(value, MASK_PLACEHOLDER)
    return masked
