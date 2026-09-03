"""openai SDK 어댑터 — 이 기능의 유일한 네트워크 부작용 지점 (FR-030, FR-031).

외부 API 표면에 대한 가정을 전부 이 파일 하나에 가둔다. 파라미터명·응답 접근 경로가
틀렸다면 고칠 자리가 make_openai_caller 하나다.

SDK 예외는 메시지를 버리고 종류만 LlmRequestError 로 옮긴다. 예외 문자열에 키나 요청
URL 이 섞여 콘솔·errors.jsonl 로 흘러가는 경로를 아예 만들지 않기 위해서다
(FR-003, FR-042). 원 예외를 chain 하지 않는 것(`from None`)도 같은 이유다.
"""

from collections.abc import Mapping
from typing import Any, cast

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from .summarize import (
    KIND_AUTH,
    KIND_CONNECTION,
    KIND_HTTP,
    KIND_TIMEOUT,
    BuiltPrompt,
    CallFn,
    LlmRequestError,
    LlmResponse,
    response_schema,
)

# `추정` — 저장소 안에 정본이 없다. 실 API 가 이 모델을 서빙하지 않으면 OPENAI_MODEL 로
# 덮어쓸 수 있게 해 두었다. 배포가 단일 exe(FR-054)라 상수뿐이면 교체에 재빌드가 필요하다.
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
ENV_OPENAI_MODEL = "OPENAI_MODEL"

# PRD 7절 "각 외부 요청 타임아웃 90초" (C-22). SDK 기본값은 600초라 명시가 필수다.
# 15초는 하루치 세션에서 반드시 죽는다 — 2026-09-02 실측(41KB diff)이 openai_timeout 이었고
# 같은 입력을 90초로 준 스크립트만 성공했다. 90 은 성공이 실측된 유일한 값이다.
OPENAI_TIMEOUT_S = 90.0

# SDK 기본값은 자동 재시도 2회다. 그대로 두면 FR-030 의 계수 밖에서 호출이 늘어난다.
OPENAI_MAX_RETRIES = 0

SCHEMA_NAME = "class_session_summary"

# 401/403 은 키 문제라 재호출이 무의미하다 (PRD 12절 표).
_AUTH_STATUSES = frozenset({401, 403})


def resolve_model(env: Mapping[str, str]) -> str:
    """모델명은 비밀값이 아니므로 Secrets 에 넣지 않고 병합된 env 에서 직접 읽는다."""
    return env.get(ENV_OPENAI_MODEL, "").strip() or DEFAULT_OPENAI_MODEL


def translate_error(exc: Exception) -> LlmRequestError:
    """SDK 예외를 종류로 환원한다. 원 메시지는 버린다.

    APITimeoutError 가 APIConnectionError 의 서브클래스라 timeout 을 먼저 본다.
    """
    if isinstance(exc, APITimeoutError):
        return LlmRequestError(KIND_TIMEOUT)
    if isinstance(exc, APIConnectionError):
        return LlmRequestError(KIND_CONNECTION)
    if isinstance(exc, APIStatusError):
        status = int(exc.status_code)
        kind = KIND_AUTH if status in _AUTH_STATUSES else KIND_HTTP
        return LlmRequestError(kind, http_status=status)
    return LlmRequestError(KIND_HTTP)


def make_openai_caller(api_key: str, model: str) -> CallFn:
    """호출 1회짜리 함수를 만든다. 상위 계층은 이 함수만 알고 SDK 를 모른다."""
    client = OpenAI(
        api_key=api_key, timeout=OPENAI_TIMEOUT_S, max_retries=OPENAI_MAX_RETRIES
    )
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": SCHEMA_NAME,
            "strict": True,
            "schema": response_schema(),
        },
    }

    def call(prompt: BuiltPrompt) -> LlmResponse:
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": prompt.system},
                    {"role": "user", "content": prompt.user},
                ],
                # SDK 의 TypedDict 는 리터럴 dict 를 그대로 받지 못한다. 스키마 자체는
                # response_schema() 가 확정하므로 여기서만 타입을 눌러 준다.
                response_format=cast(Any, response_format),
            )
        except Exception as exc:
            raise translate_error(exc) from None
        content = completion.choices[0].message.content if completion.choices else None
        # content 는 Optional 이다. 빈 문자열은 수신 검증에서 JSON 파싱 실패로 흡수된다.
        return LlmResponse(
            text=content or "", request_id=completion.id, model=completion.model
        )

    return call
