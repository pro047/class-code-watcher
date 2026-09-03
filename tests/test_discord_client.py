"""discord_client 어댑터 — 예외 환원·상태 판정 (설계 6절 케이스 21~24).

네트워크는 타지 않는다. httpx.Client 를 가짜로 갈아끼워 "요청 1회"의 계약만 본다 —
실제 Discord 응답(성공 상태 코드·payload 필드명·본문 상한)은 여전히 `추정`이고 실전송
1회로만 닫힌다 (VERIFY.md 사람 확인 A).
"""

from collections.abc import Mapping

import httpx
import pytest

from class_watcher import discord_client
from class_watcher.notify import (
    KIND_CONNECTION,
    KIND_HTTP,
    KIND_TIMEOUT,
    DiscordRequestError,
    discord_error_code,
)

# 실제 Discord 도메인이 아니다. 이 값이 예외·콘솔·산출물로 새는지를 보는 표식이다.
WEBHOOK = "https://discord.example/api/webhooks/1234567890/super-secret-token"


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, *, status: int = 204, error: Exception | None = None
) -> list[tuple[str, object]]:
    """httpx.Client 자리를 막고 실제로 넘어간 (url, json) 을 기록한다."""
    calls: list[tuple[str, object]] = []

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

        def post(self, url: str, *, json: object) -> _FakeResponse:
            calls.append((url, json))
            if error is not None:
                raise error
            return _FakeResponse(status)

    monkeypatch.setattr(discord_client.httpx, "Client", _Client)
    return calls


# ── 케이스 24: 성공 판정 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, True),
        (204, True),
        (299, True),
        (199, False),
        (300, False),
        (404, False),
        (429, False),
        (500, False),
    ],
)
def test_is_success_accepts_only_2xx(status: int, expected: bool) -> None:
    # `추정` — 204 로 알려져 있으나 정본이 없어 2xx 를 통째로 성공으로 본다.
    assert discord_client.is_success(status) is expected


def test_timeout_matches_prd_seven() -> None:
    # PRD 7절 (C-22) "각 외부 요청 타임아웃 90초" — "각 외부 요청"이 두 클라이언트
    # 모두를 가리킨다.
    assert discord_client.DISCORD_TIMEOUT_S == 90.0


def test_client_is_created_with_the_90_second_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 상수가 90 이어도 httpx.Client 생성에 전달되지 않으면 httpx 기본값(5초)이 산다.
    timeouts: list[float] = []

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            timeouts.append(timeout)

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

        def post(self, url: str, *, json: object) -> _FakeResponse:
            return _FakeResponse(204)

    monkeypatch.setattr(discord_client.httpx, "Client", _Client)
    send = discord_client.make_discord_sender(WEBHOOK)

    assert send({"content": "안녕"}) == 204
    assert timeouts == [discord_client.DISCORD_TIMEOUT_S] == [90.0]


# ── 케이스 21: httpx 예외를 종류로 환원 ───────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (httpx.TimeoutException("시간 초과"), KIND_TIMEOUT),
        (httpx.ConnectTimeout("연결 시간 초과"), KIND_TIMEOUT),
        (httpx.ReadTimeout("읽기 시간 초과"), KIND_TIMEOUT),
        (httpx.ConnectError("연결 실패"), KIND_CONNECTION),
        (httpx.ReadError("읽기 실패"), KIND_CONNECTION),
        (httpx.TransportError("전송 실패"), KIND_CONNECTION),
        (ValueError("그 밖의 예외"), KIND_HTTP),
    ],
)
def test_translate_error_reduces_httpx_exceptions_to_kinds(exc: Exception, kind: str) -> None:
    # TimeoutException 이 TransportError 의 서브클래스라 순서가 뒤집히면 timeout 이
    # connection 으로 뭉개진다.
    translated = discord_client.translate_error(exc)

    assert isinstance(translated, DiscordRequestError)
    assert translated.kind == kind
    assert translated.http_status is None


@pytest.mark.parametrize(
    ("exc", "expected_error"),
    [
        (httpx.ConnectTimeout("t"), "discord_timeout"),
        (httpx.ConnectError("c"), "discord_connection"),
    ],
)
def test_translated_kinds_reach_the_session_error_codes(
    exc: Exception, expected_error: str
) -> None:
    assert discord_error_code(discord_client.translate_error(exc)) == expected_error


# ── 케이스 22: 예외 어디에도 Webhook URL 이 없다 (FR-003, FR-042) ─────────────


def test_transport_error_never_carries_the_webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # httpx 예외 메시지에는 요청 URL 이 들어간다. 메시지를 통째로 버리고 종류만 옮긴다.
    leaked = httpx.ConnectError(f"connection failed for url '{WEBHOOK}'")
    _patch_client(monkeypatch, error=leaked)
    send = discord_client.make_discord_sender(WEBHOOK)

    with pytest.raises(DiscordRequestError) as excinfo:
        send({"content": "안녕"})

    exc = excinfo.value
    assert exc.kind == KIND_CONNECTION
    assert WEBHOOK not in str(exc)
    assert WEBHOOK not in repr(exc.args)
    # from None: 원 예외를 chain 하지 않는다 — __cause__ 문자열에 URL 이 남는다.
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


def test_http_error_never_carries_the_webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # raise_for_status() 를 쓰지 않는 이유가 여기다 — 그 예외 메시지가 URL 을 담는다.
    _patch_client(monkeypatch, status=404)
    send = discord_client.make_discord_sender(WEBHOOK)

    with pytest.raises(DiscordRequestError) as excinfo:
        send({"content": "안녕"})

    exc = excinfo.value
    assert WEBHOOK not in str(exc)
    assert WEBHOOK not in repr(exc.args)
    assert WEBHOOK not in discord_error_code(exc)
    assert exc.__cause__ is None


# ── 케이스 23: 비-2xx 응답은 상태 코드를 달고 올라온다 (FR-034) ───────────────


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_non_2xx_response_raises_with_the_status_code(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _patch_client(monkeypatch, status=status)
    send = discord_client.make_discord_sender(WEBHOOK)

    with pytest.raises(DiscordRequestError) as excinfo:
        send({"content": "안녕"})

    assert excinfo.value.kind == KIND_HTTP
    assert excinfo.value.http_status == status
    # 429 도 4xx 그대로 취급하고 자동 재시도하지 않는다 (PRD 12절 표).
    assert discord_error_code(excinfo.value) == f"discord_http_{status}"


def test_successful_send_posts_content_json_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_client(monkeypatch, status=204)
    send = discord_client.make_discord_sender(WEBHOOK)
    payload: Mapping[str, object] = {"content": "[수업] 검증"}

    assert send(payload) == 204

    # 요청 1회 = send 1회. 재시도를 켜지 않으므로 호출이 늘어날 자리가 없다.
    assert calls == [(WEBHOOK, {"content": "[수업] 검증"})]


def test_sender_does_not_retry_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_client(monkeypatch, status=503)
    send = discord_client.make_discord_sender(WEBHOOK)

    with pytest.raises(DiscordRequestError):
        send({"content": "안녕"})

    assert len(calls) == 1
