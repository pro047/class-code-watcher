"""외부 전송 직전의 비밀값 탐지·마스킹과 환경 정보 제거 (FR-036, FR-037, FR-038, FR-042).

디스크를 만지는 것은 맨 아래 write_redaction_json 하나뿐이다. 탐지·판정·정제는 전부
순수 함수라 파일시스템 없이 전 경로가 검증된다.

탐지 결과에 매치 원문·프리뷰·길이를 절대 싣지 않는다 — 유출을 막으려고 만든 산출물이
유출 경로가 되면 안 되기 때문이다 (PRD 13.1: 마지막 4자조차 남기지 않는다).
"""

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .config import MIN_MASKABLE_LENGTH

# stats.json·session.json 과 같은 버전 축을 쓴다. redaction.json 의 구조는 PRD 9.1 이
# 이름만 적고 명세하지 않아 이 단계가 확정했다.
REDACTION_SCHEMA_VERSION = "1.1"

REDACTED_PLACEHOLDER = "[REDACTED]"  # FR-038 수용 기준 문자열 그대로
USER_PLACEHOLDER = "[USER]"
INTERNAL_ADDR_PLACEHOLDER = "[INTERNAL_ADDR]"
HOME_PLACEHOLDER = "~"

# 이보다 짧은 사용자명을 경로 밖에서까지 치환하면 무관한 식별자를 부숴 diff 가 읽히지 않는다.
MIN_BARE_USERNAME_LENGTH = 6

# session.json 의 error 값. 종료 코드는 1 그대로이고 세부 원인만 이 필드가 구분한다 (C-10).
ERROR_SECRETS_DETECTED = "secrets_detected"

POLICY_BLOCK = "block"
POLICY_MASK = "mask"
POLICY_CLEAN = "clean"

RULE_KNOWN_SECRET = "known_secret"

# 파일 경로 자체에서 나온 탐지에는 본문 라인이 없다. 1-base 라인 번호와 겹치지 않는 값.
PATH_FINDING_LINE = 0

_DIFF_HEADER_PREFIX = "+++ b/"

# 규칙 표는 설계 5.1 이 확정한 스펙이다. FR-036 수용 기준의 5범주(API 키·토큰·비밀번호·
# 프라이빗 키 헤더·커넥션 문자열)를 각각 하나 이상 덮는다. 벤더 키 형식은 외부 표면이라
# 저장소 안에 정본이 없다 — 틀려도 고칠 자리가 한 행이고, 실제 `.env` 값은 known_secret 이
# 형식과 무관하게 백스톱한다.
_RULE_SPECS: tuple[tuple[str, str], ...] = (
    ("private_key_header", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("openai_api_key", r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    ("aws_access_key_id", r"\bAKIA[0-9A-Z]{16}\b"),
    ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    ("slack_token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"),
    ("bearer_token", r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    ("discord_webhook", r"discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+"),
    ("url_credentials", r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@'\"]+:[^@\s'\"]+@"),
    ("jdbc_credentials", r"(?i)jdbc:[^\s'\"]*(?:password|pwd)=[^\s&;'\"]+"),
    (
        "assignment_secret",
        r"(?i)\b(?:api[_-]?key|apikey|secret|passwd|password|pwd|token|access[_-]?key)\b"
        r"\s*[:=]\s*[\"'][^\"']{8,}[\"']",
    ),
)

# RFC1918 사설 IPv4 3대역. 공인 IP 는 내부 주소가 아니므로 건드리지 않는다.
_PRIVATE_IPV4 = re.compile(
    r"\b(?:"
    r"10\.(?:\d{1,3}\.){2}\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r")\b"
)

_USER_DIR_PREFIX = r"[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]"


@dataclass(frozen=True)
class SecretRule:
    rule_id: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class SecretFinding:
    """탐지 위치만 담는다. 매치 원문을 필드로 두지 않는 것이 이 자료구조의 요점이다."""

    rule_id: str
    rel_path: str | None
    line_no: int


@dataclass(frozen=True)
class EnvMarkers:
    """환경정제에 필요한 값들. os.environ 접근은 build_env_markers 에만 격리한다."""

    watch_root_variants: tuple[str, ...]
    user_profile_variants: tuple[str, ...]
    username: str | None
    bare_username_maskable: bool


@dataclass(frozen=True)
class EnvReplacements:
    watch_root_paths: int
    user_paths: int
    bare_usernames: int
    internal_addresses: int


_NO_REPLACEMENTS = EnvReplacements(
    watch_root_paths=0, user_paths=0, bare_usernames=0, internal_addresses=0
)


@dataclass(frozen=True)
class RedactionResult:
    """4단계의 유일한 diff 입력. blocked 면 text 가 없어 타입 수준에서 보낼 것이 없다."""

    text: str | None
    findings: tuple[SecretFinding, ...]
    blocked: bool
    masked: bool
    env: EnvReplacements
    bare_username_skipped: bool


def default_rules(known_values: Sequence[str] = ()) -> tuple[SecretRule, ...]:
    """표 규칙 + `.env` 실값의 정확 일치 규칙.

    짧은 값까지 정확 일치로 걸면 무관한 문자열이 통째로 차단 사유가 된다 —
    mask_secrets 와 같은 하한(MIN_MASKABLE_LENGTH)을 쓴다.
    """
    rules = [
        SecretRule(rule_id=rule_id, pattern=re.compile(pattern))
        for rule_id, pattern in _RULE_SPECS
    ]
    for value in known_values:
        if len(value) >= MIN_MASKABLE_LENGTH:
            rules.append(
                SecretRule(rule_id=RULE_KNOWN_SECRET, pattern=re.compile(re.escape(value)))
            )
    return tuple(rules)


def _path_variants(raw: str) -> tuple[str, ...]:
    """백슬래시·슬래시 두 표기를 모두 만든다 — 코드 본문에는 두 형태가 다 나온다.

    긴 것부터 돌려준다. 짧은 표기가 긴 표기의 조각을 먼저 먹으면 잔여물이 남는다.
    """
    value = raw.strip()
    if not value:
        return ()
    variants = {value, value.replace("\\", "/"), value.replace("/", "\\")}
    return tuple(sorted(variants, key=lambda item: (-len(item), item)))


def build_env_markers(watch_root: Path, env: Mapping[str, str]) -> EnvMarkers:
    """USERNAME·USERPROFILE 에서 정제 표식을 만든다. 사용자명은 하드코딩하지 않는다."""
    profile = env.get("USERPROFILE", "").strip()
    username = env.get("USERNAME", "").strip()
    if not username and profile:
        # USERPROFILE 은 Windows 표기라 호스트 OS 와 무관하게 Windows 규칙으로 자른다.
        username = PureWindowsPath(profile).name
    name = username or None
    return EnvMarkers(
        watch_root_variants=_path_variants(str(watch_root)),
        user_profile_variants=_path_variants(profile),
        username=name,
        bare_username_maskable=name is not None and len(name) >= MIN_BARE_USERNAME_LENGTH,
    )


def scan_text(text: str, rules: Sequence[SecretRule]) -> tuple[SecretFinding, ...]:
    """라인 단위 탐지. 추가·삭제·컨텍스트를 가리지 않는다 — 삭제 라인도 전송 대상이다.

    파일 귀속은 `+++ b/…` 헤더 추적으로 한다. 본문 라인은 diff 에서 항상 한 칸 들여
    쓰이므로(`+`/`-`/공백) 열 0 에서 이 접두가 나오는 것은 헤더뿐이다.
    """
    findings: list[SecretFinding] = []
    current_path: str | None = None
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.startswith(_DIFF_HEADER_PREFIX):
            current_path = line[len(_DIFF_HEADER_PREFIX) :].strip() or None
        for rule in rules:
            findings.extend(
                SecretFinding(rule_id=rule.rule_id, rel_path=current_path, line_no=line_no)
                for _ in rule.pattern.finditer(line)
            )
    return tuple(findings)


def scan_paths(paths: Sequence[str], rules: Sequence[SecretRule]) -> tuple[SecretFinding, ...]:
    """파일 경로 자체를 같은 규칙으로 훑는다 (FR-036 의 구멍 막기).

    diff 본문이 비는 파일(개행만 바뀐 modified, 빈 파일 added/deleted)은 `+++ b/…`
    헤더조차 생성되지 않아 경로가 스캔 대상 텍스트에 실리지 않는다. 그 파일명에 키가
    박혀 있으면 차단 게이트를 통째로 우회한다.
    """
    findings: list[SecretFinding] = []
    for rel_path in paths:
        for rule in rules:
            findings.extend(
                SecretFinding(
                    rule_id=rule.rule_id, rel_path=rel_path, line_no=PATH_FINDING_LINE
                )
                for _ in rule.pattern.finditer(rel_path)
            )
    return tuple(findings)


def mask_text(text: str, rules: Sequence[SecretRule]) -> str:
    """매치 구간 전체를 [REDACTED] 로 바꾼다. 재스캔 시 findings 0 이 사후조건이다."""
    masked = text
    for rule in rules:
        masked = rule.pattern.sub(REDACTED_PLACEHOLDER, masked)
    return masked


def _relative_repl(match: re.Match[str]) -> str:
    # 뒤따르는 구분자까지 지워야 `src/A.java` 가 남는다. 루트 자체는 `.` 이다.
    return "" if match.group(1) else "."


def sanitize_environment(text: str, markers: EnvMarkers) -> tuple[str, EnvReplacements]:
    """FR-037. 치환 순서는 긴 패턴부터로 고정한다 — 짧은 것이 먼저 먹으면 잔여물이 남는다."""
    result = text
    watch_root_paths = 0
    user_paths = 0
    bare_usernames = 0

    for variant in markers.watch_root_variants:
        result, count = re.subn(re.escape(variant) + r"([\\/])?", _relative_repl, result)
        watch_root_paths += count

    for variant in markers.user_profile_variants:
        result, count = re.subn(re.escape(variant), HOME_PLACEHOLDER, result)
        user_paths += count

    if markers.username is not None:
        result, count = re.subn(
            _USER_DIR_PREFIX + re.escape(markers.username),
            HOME_PLACEHOLDER,
            result,
            flags=re.IGNORECASE,
        )
        user_paths += count
        if markers.bare_username_maskable:
            result, bare_usernames = re.subn(
                r"\b" + re.escape(markers.username) + r"\b",
                USER_PLACEHOLDER,
                result,
                flags=re.IGNORECASE,
            )

    result, internal_addresses = _PRIVATE_IPV4.subn(INTERNAL_ADDR_PLACEHOLDER, result)

    return result, EnvReplacements(
        watch_root_paths=watch_root_paths,
        user_paths=user_paths,
        bare_usernames=bare_usernames,
        internal_addresses=internal_addresses,
    )


def redact_diff(
    diff_text: str,
    *,
    scanned_paths: Sequence[str] = (),
    allow_secrets: bool,
    markers: EnvMarkers,
    rules: Sequence[SecretRule],
) -> RedactionResult:
    """scan → 판정 → mask → sanitize.

    스캔을 원문에 먼저 돌리는 이유: 환경정제가 먼저 돌면 커넥션 문자열 속 사용자명
    치환 등으로 비밀값 패턴이 깨져 검사를 빠져나갈 수 있다. 차단이 목적인 검사를
    변형된 텍스트에 돌리면 방어선이 약해진다.
    """
    findings = scan_text(diff_text, rules) + scan_paths(scanned_paths, rules)
    # 경로가 짧아 bare 치환을 건너뛴 사실은 정제 수행 여부와 무관하게 같은 값으로 남긴다.
    skipped = markers.username is not None and not markers.bare_username_maskable

    if findings and not allow_secrets:
        return RedactionResult(
            text=None,
            findings=findings,
            blocked=True,
            masked=False,
            env=_NO_REPLACEMENTS,
            bare_username_skipped=skipped,
        )

    # 마스킹·정제 대상은 diff 본문뿐이다. 경로를 가리면 요약이 무슨 파일인지 말할 수 없다.
    body = mask_text(diff_text, rules) if findings else diff_text
    sanitized, env = sanitize_environment(body, markers)
    return RedactionResult(
        text=sanitized,
        findings=findings,
        blocked=False,
        masked=bool(findings),
        env=env,
        bare_username_skipped=skipped,
    )


def redaction_policy(result: RedactionResult) -> str:
    if result.blocked:
        return POLICY_BLOCK
    return POLICY_MASK if result.masked else POLICY_CLEAN


def by_rule_counts(findings: Sequence[SecretFinding]) -> dict[str, int]:
    """규칙별 탐지 건수 (FR-038 의 "유형"). 정렬해야 같은 입력이 같은 바이트를 낸다."""
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.rule_id] = counts.get(finding.rule_id, 0) + 1
    return {rule_id: counts[rule_id] for rule_id in sorted(counts)}


def redaction_doc(
    result: RedactionResult, *, scanned_at: str, allow_secrets: bool
) -> dict[str, object]:
    """redaction.json 본문. 시각은 주입받아 문서 빌더 자체는 결정적이다."""
    return {
        "schema_version": REDACTION_SCHEMA_VERSION,
        "scanned_at": scanned_at,
        "source": "final.diff",
        "policy": redaction_policy(result),
        "allow_secrets": allow_secrets,
        "secrets_found": len(result.findings),
        "findings": [
            {"rule": item.rule_id, "path": item.rel_path, "line": item.line_no}
            for item in result.findings
        ],
        "by_rule": by_rule_counts(result.findings),
        "env": {
            # 정제를 실제로 수행했는지. 차단된 세션은 본문을 만들지 않으므로 False 다.
            "paths_relativized": result.text is not None,
            "watch_root_paths": result.env.watch_root_paths,
            "user_paths": result.env.user_paths,
            "bare_usernames": result.env.bare_usernames,
            "internal_addresses": result.env.internal_addresses,
            "bare_username_skipped": result.bare_username_skipped,
        },
    }


def session_redaction_fields(result: RedactionResult) -> dict[str, object]:
    """session.json 의 redaction 필드 (PRD 9.2 + FR-038 의 "건수와 유형")."""
    return {
        "secrets_found": len(result.findings),
        "by_rule": by_rule_counts(result.findings),
        "paths_relativized": result.text is not None,
    }


def write_redaction_json(path: Path, doc: Mapping[str, object]) -> None:
    """diffgen·snapshot 과 같은 원자적 교체 — 중간에 죽어도 반쪽 산출물이 남지 않는다."""
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".redaction-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
