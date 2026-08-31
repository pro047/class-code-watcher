"""httpx 어댑터 — 이 기능의 유일한 네트워크 부작용 지점 (FR-033, FR-034).

외부 API 표면에 대한 가정을 전부 이 파일 하나에 가둔다. 넷 중 무엇이 틀려도 고칠 자리가
make_discord_sender 하나다:
  · payload 필드명이 `content` 다
  · Content-Type: application/json 으로 POST 한다
  · 성공은 2xx 다 (204 No Content 로 알려져 있으나 저장소 안에 정본이 없다)
  · 4xx/5xx 는 응답 본문을 읽지 않고 상태 코드만 쓴다 — 본문에 URL 이 되비칠 수 있다

httpx 예외는 메시지를 버리고 종류만 DiscordRequestError 로 옮긴다. 예외 문자열에 요청
URL(= 비밀값)이 섞여 콘솔·errors.jsonl 로 흘러가는 경로를 아예 만들지 않기 위해서다
(FR-003, FR-042). 원 예외를 chain 하지 않는 것(`from None`)도 같은 이유다.
"""

from collections.abc import Mapping

import httpx

from .notify import (
    KIND_CONNECTION,
    KIND_HTTP,
    KIND_TIMEOUT,
    DiscordRequestError,
    SendFn,
)

# PRD 7절 "각 외부 요청 타임아웃 15초".
DISCORD_TIMEOUT_S = 15.0


def is_success(status: int) -> bool:
    """`추정` — 2xx 를 통째로 성공으로 본다. 어느 쪽으로 틀려도 안전한 범위다."""
    return 200 <= status < 300


def translate_error(exc: Exception) -> DiscordRequestError:
    """httpx 예외를 종류로 환원한다. 원 메시지는 버린다.

    TimeoutException 이 TransportError 의 서브클래스라 timeout 을 먼저 본다.
    """
    if isinstance(exc, httpx.TimeoutException):
        return DiscordRequestError(KIND_TIMEOUT)
    if isinstance(exc, httpx.TransportError):
        return DiscordRequestError(KIND_CONNECTION)
    return DiscordRequestError(KIND_HTTP)


def make_discord_sender(webhook_url: str) -> SendFn:
    """요청 1회짜리 함수를 만든다. 상위 계층은 이 함수만 알고 httpx 를 모른다.

    자동 재시도를 켜지 않는다 — PRD 12절 표가 Discord 4xx/5xx 를 재시도 없이 partial 로
    끝낸다. 429 도 여기서는 4xx 그대로 취급하고 사람이 session.json 으로 읽는다.
    """

    def send(payload: Mapping[str, object]) -> int:
        try:
            with httpx.Client(timeout=DISCORD_TIMEOUT_S) as client:
                response = client.post(webhook_url, json=dict(payload))
        except Exception as exc:
            raise translate_error(exc) from None
        status = int(response.status_code)
        if not is_success(status):
            # raise_for_status() 를 쓰지 않는다 — 그 예외 메시지에 요청 URL 이 들어간다.
            raise DiscordRequestError(KIND_HTTP, http_status=status)
        return status

    return send
