"""openai_client 모듈 — SDK 를 띄우지 않고 검증 가능한 범위 (설계 기준 24, FR-003, FR-042).

make_openai_caller 는 실 SDK 클라이언트를 만들므로 여기서 호출하지 않는다 — 그 안의
`추정`(파라미터명·응답 접근 경로)은 실키 실호출(사람 확인 항목)만이 확정한다.
예외는 SDK 가 쓰는 httpx2 로 실제 인스턴스를 만들어 번역 규칙을 검증한다 (네트워크 없음).
"""

import httpx2
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from class_watcher import openai_client
from class_watcher.summarize import (
    KIND_AUTH,
    KIND_CONNECTION,
    KIND_HTTP,
    KIND_TIMEOUT,
)

_REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")


def _status_error(status: int, message: str = "boom") -> APIStatusError:
    response = httpx2.Response(status, request=_REQUEST)
    return APIStatusError(message, response=response, body=None)


# ── resolve_model: env 유/무/공백 ─────────────────────────────────────────────


def test_resolve_model_uses_env_when_set() -> None:
    env = {openai_client.ENV_OPENAI_MODEL: "gpt-custom"}
    assert openai_client.resolve_model(env) == "gpt-custom"


def test_resolve_model_falls_back_to_default_when_missing() -> None:
    assert openai_client.resolve_model({}) == openai_client.DEFAULT_OPENAI_MODEL


def test_resolve_model_treats_blank_as_missing() -> None:
    env = {openai_client.ENV_OPENAI_MODEL: "   "}
    assert openai_client.resolve_model(env) == openai_client.DEFAULT_OPENAI_MODEL


# ── translate_error: SDK 예외 → 종류 환원 (PRD 12절 표의 행 구분) ─────────────


def test_translate_timeout_wins_over_connection() -> None:
    # APITimeoutError 는 APIConnectionError 의 서브클래스다 — timeout 판정이 먼저여야 한다.
    error = openai_client.translate_error(APITimeoutError(request=_REQUEST))
    assert error.kind == KIND_TIMEOUT
    assert error.http_status is None


def test_translate_connection_error() -> None:
    error = openai_client.translate_error(APIConnectionError(request=_REQUEST))
    assert error.kind == KIND_CONNECTION


@pytest.mark.parametrize("status", [401, 403])
def test_translate_auth_statuses(status: int) -> None:
    error = openai_client.translate_error(_status_error(status))
    assert error.kind == KIND_AUTH
    assert error.http_status == status


def test_translate_server_error_keeps_status() -> None:
    error = openai_client.translate_error(_status_error(500))
    assert error.kind == KIND_HTTP
    assert error.http_status == 500


def test_translate_unknown_exception_defaults_to_http() -> None:
    error = openai_client.translate_error(ValueError("알 수 없는 실패"))
    assert error.kind == KIND_HTTP
    assert error.http_status is None


def test_translate_drops_sdk_message() -> None:
    # SDK 예외 메시지에 키·URL 이 섞여도 번역된 예외에는 남지 않는다 (FR-003, FR-042).
    secret = "sk-leak-abcdef1234567890"
    error = openai_client.translate_error(_status_error(500, message=f"key={secret}"))
    assert secret not in str(error)
    assert secret not in repr(error)


# ── 상수 회귀: FR-030 계수와 PRD 7절 타임아웃을 고정한다 ──────────────────────


def test_sdk_auto_retry_is_disabled() -> None:
    # SDK 기본값(자동 재시도 2회)이 살아나면 FR-030 의 상한 2회가 계수 밖에서 깨진다.
    assert openai_client.OPENAI_MAX_RETRIES == 0


def test_timeout_is_90_seconds() -> None:
    # PRD 7절 (C-22) "각 외부 요청 타임아웃 90초". 15초는 하루치 세션(41KB diff)에서
    # 반드시 openai_timeout 으로 죽는 것이 실측됐고, 90 은 성공이 실측된 유일한 값이다.
    # 값 단언이 약한 테스트임을 알지만 PRD 정본 값의 회귀 방지가 목적이다.
    assert openai_client.OPENAI_TIMEOUT_S == 90.0


def test_client_is_created_with_the_timeout_and_no_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 상수가 90 이어도 클라이언트 생성에 전달되지 않으면 SDK 기본값(600초)이 산다.
    captured: dict[str, object] = {}

    class _FakeOpenAI:
        def __init__(self, *, api_key: str, timeout: float, max_retries: int) -> None:
            captured["api_key"] = api_key
            captured["timeout"] = timeout
            captured["max_retries"] = max_retries

    monkeypatch.setattr(openai_client, "OpenAI", _FakeOpenAI)

    openai_client.make_openai_caller("sk-test", "gpt-4o")

    assert captured["timeout"] == openai_client.OPENAI_TIMEOUT_S == 90.0
    assert captured["max_retries"] == openai_client.OPENAI_MAX_RETRIES == 0
