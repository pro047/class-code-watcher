"""redact 모듈 — 탐지·차단·마스킹·환경정제 순수 함수 (FR-036, FR-037, FR-038, FR-042).

설계 검증 기준 1~16. 전부 순수 함수라 파일시스템·환경변수 없이 값만 넣어 검증한다
(write_redaction_json 만 tmp_path 를 쓴다). 통합 배선(기준 17~22)은 tests/test_watcher.py
와 tests/test_cli.py 에 있다.
"""

import json
from pathlib import Path

import pytest

from class_watcher import redact
from class_watcher.redact import (
    PATH_FINDING_LINE,
    EnvMarkers,
    SecretFinding,
    build_env_markers,
    by_rule_counts,
    default_rules,
    redact_diff,
    redaction_doc,
    redaction_policy,
    sanitize_environment,
    scan_paths,
    scan_text,
    session_redaction_fields,
    write_redaction_json,
)
from class_watcher.watcher import _scan_console_line

RULES = default_rules()

# 환경정제가 아무것도 바꾸지 않는 중립 표식 — 탐지·정책 판정만 보는 테스트에 쓴다.
NEUTRAL_MARKERS = EnvMarkers(
    watch_root_variants=(),
    user_profile_variants=(),
    username=None,
    bare_username_maskable=False,
)

OPENAI_FIXTURE = "sk-abcdefghijklmnopqrstuvwx"


def _windows_markers() -> EnvMarkers:
    """가짜 env Mapping 으로 만든 Windows 표식 — os.environ 을 만지지 않는다."""
    return build_env_markers(
        Path("C:/proj/demo"),
        {"USERNAME": "student1", "USERPROFILE": "C:\\Users\\student1"},
    )


# ── 기준 1: 규칙 12종 fixture 전량 탐지 (FR-036, PRD 14.1) ───────────────────
#
# 표 규칙 11종. 12번째 known_secret 은 아래 기준 4 에서 따로 본다. 벤더 키 형식은
# 외부 표면이라 자기충족 검증의 한계가 남는다 (JUDGE #33) — AWS 키는 문서화된 공개
# 예시 키(AKIAIOSFODNN7EXAMPLE)를 썼고, 나머지는 발급 형식 규칙에 맞춘 가짜다.

RULE_FIXTURES: tuple[tuple[str, str], ...] = (
    ("private_key_header", "-----BEGIN RSA PRIVATE KEY-----"),
    ("openai_api_key", OPENAI_FIXTURE),
    ("aws_access_key_id", "AKIAIOSFODNN7EXAMPLE"),
    ("github_token", "ghp_" + "a" * 36),
    ("slack_token", "xoxb-1234567890-abcdefghij"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcde12345"),
    ("bearer_token", "Bearer abcdefghijklmnopqrstuv"),
    ("discord_webhook", "https://discord.com/api/webhooks/123456789012/AbCd_efGh-1234"),
    ("url_credentials", "postgres://admin:hunter2secret@db.internal.example/app"),
    ("jdbc_credentials", "jdbc:mysql://db/app?password=supersecretpw"),
    ("assignment_secret", 'password = "hunter2hunter2"'),
)


@pytest.mark.parametrize(("rule_id", "fixture"), RULE_FIXTURES)
def test_each_rule_detects_its_fixture(rule_id: str, fixture: str) -> None:
    findings = scan_text(fixture, RULES)
    # 정확히 그 규칙 하나로만 탐지된다 — 다른 규칙이 겹쳐 물면 fixture 를 바꿔라.
    assert [item.rule_id for item in findings] == [rule_id]


def test_rule_table_has_eleven_rules_plus_known_values() -> None:
    assert len(RULES) == len(RULE_FIXTURES)


# ── 기준 2: 여러 종류가 각자의 파일·라인에 귀속된다 (`+++ b/…` 헤더 추적) ────


def test_findings_attach_to_diff_header_paths() -> None:
    text = "\n".join(
        [
            "sk-headerlessheaderless00",  # 헤더 앞 → rel_path 없음
            "--- a/src/App.java",
            "+++ b/src/App.java",
            f"+{OPENAI_FIXTURE}",
            "--- a/notes.py",
            "+++ b/notes.py",
            '+password = "hunter2hunter2"',
        ]
    )
    findings = scan_text(text, RULES)
    assert findings == (
        SecretFinding(rule_id="openai_api_key", rel_path=None, line_no=1),
        SecretFinding(rule_id="openai_api_key", rel_path="src/App.java", line_no=4),
        SecretFinding(rule_id="assignment_secret", rel_path="notes.py", line_no=7),
    )


def test_deleted_lines_are_scanned_too() -> None:
    # 삭제 라인도 프롬프트에 실려 나간다 — 스캔 범위는 diff 전 라인이다 (설계 5.1).
    findings = scan_text(f"-{OPENAI_FIXTURE}", RULES)
    assert [item.rule_id for item in findings] == ["openai_api_key"]


# ── 기준 3: 파일 경로 자체의 패턴도 탐지된다 (빈 diff 파일의 우회 방지) ──────


def test_secret_pattern_in_file_path_is_detected() -> None:
    path = f"secrets/{OPENAI_FIXTURE}.java"
    findings = scan_paths([path], RULES)
    assert findings == (
        SecretFinding(rule_id="openai_api_key", rel_path=path, line_no=PATH_FINDING_LINE),
    )


def test_path_finding_blocks_even_when_diff_body_is_empty() -> None:
    # 개행만 바뀐 modified·빈 added 파일은 diff 본문이 비어 헤더가 없다 — 경로 스캔이
    # 유일한 방어선이다 (설계 5.1, JUDGE #12).
    result = redact_diff(
        "",
        scanned_paths=[f"{OPENAI_FIXTURE}.py"],
        allow_secrets=False,
        markers=NEUTRAL_MARKERS,
        rules=RULES,
    )
    assert result.blocked is True
    assert result.text is None
    assert result.findings[0].line_no == PATH_FINDING_LINE


def test_path_findings_do_not_mask_paths() -> None:
    # 마스킹 대상은 diff 본문뿐이다 — 경로를 가리면 요약이 무슨 파일인지 말할 수 없다.
    result = redact_diff(
        "+x = 1",
        scanned_paths=[f"{OPENAI_FIXTURE}.py"],
        allow_secrets=True,
        markers=NEUTRAL_MARKERS,
        rules=RULES,
    )
    assert result.masked is True
    assert result.text == "+x = 1"


# ── 기준 4: known_secret — `.env` 실값의 정확 일치, 8자 미만은 규칙 미생성 ────


def test_known_env_value_is_detected_exactly() -> None:
    rules = default_rules(["real-webhook-value-Zz42"])
    findings = scan_text("+leaked: real-webhook-value-Zz42", rules)
    assert [item.rule_id for item in findings] == [redact.RULE_KNOWN_SECRET]


def test_short_known_value_creates_no_rule() -> None:
    # MIN_MASKABLE_LENGTH(8) 미만은 무관한 문자열을 통째로 차단 사유로 만들 수 있다.
    assert len(default_rules(["short07"])) == len(default_rules())
    assert scan_text("+short07", default_rules(["short07"])) == ()


# ── 기준 5: 오탐 스모크 — 평범한 수업 코드에서 findings 0 ────────────────────


def test_ordinary_code_diff_has_no_findings() -> None:
    text = "\n".join(
        [
            "+++ b/Main.java",
            "+for (int i = 0; i < n; ++i) {",
            '+String url = "http://example.com/path";',
            '+String s = "short";',
            "+int token = 3;",
            "-System.out.println(token);",
        ]
    )
    assert scan_text(text, RULES) == ()


# ── 기준 6~8: 차단 / 마스킹 / clean 정책 (FR-036·FR-038) ─────────────────────


def test_findings_without_allow_secrets_block() -> None:
    result = redact_diff(
        f'+api_key = "{OPENAI_FIXTURE}"',
        allow_secrets=False,
        markers=NEUTRAL_MARKERS,
        rules=RULES,
    )
    assert result.blocked is True
    assert result.masked is False
    assert result.text is None  # 보낼 것 자체가 없다 (설계 5.6 계약 1)
    assert redaction_policy(result) == redact.POLICY_BLOCK


MASKABLE_SECRETS = ("sk-maskmaskmaskmaskmaskQz19", "xoxb-1234567890-maskWw28")


def test_allow_secrets_masks_and_rescan_finds_nothing() -> None:
    text = "\n".join(
        [
            "+++ b/config.py",
            f'+api_key = "{MASKABLE_SECRETS[0]}"',
            f"+{MASKABLE_SECRETS[1]}",
        ]
    )
    result = redact_diff(text, allow_secrets=True, markers=NEUTRAL_MARKERS, rules=RULES)
    assert result.blocked is False
    assert result.masked is True
    assert result.text is not None
    # mask_text 의 사후조건: 마스킹된 텍스트를 재스캔하면 findings 0 (설계 4절).
    assert scan_text(result.text, RULES) == ()
    for secret in MASKABLE_SECRETS:
        assert secret not in result.text
        assert secret[-4:] not in result.text
    assert redact.REDACTED_PLACEHOLDER in result.text
    assert redaction_policy(result) == redact.POLICY_MASK


def test_clean_text_passes_with_sanitize_only() -> None:
    result = redact_diff(
        "+x = 1", allow_secrets=False, markers=NEUTRAL_MARKERS, rules=RULES
    )
    assert result.findings == ()
    assert result.blocked is False
    assert result.masked is False
    assert result.text == "+x = 1"
    assert redaction_policy(result) == redact.POLICY_CLEAN


# ── 기준 9: 비유출 — 산출물·콘솔 어디에도 원문(뒤 4자 포함)이 없다 (FR-042) ──

LEAK_SECRETS = ("sk-leakleakleakleakleakQx77", "-----BEGIN RSA PRIVATE KEY-----")


def test_artifacts_never_contain_secret_text() -> None:
    text = "\n".join(["+++ b/a.py"] + [f"+{secret}" for secret in LEAK_SECRETS])
    result = redact_diff(text, allow_secrets=False, markers=NEUTRAL_MARKERS, rules=RULES)
    assert result.blocked is True
    doc_json = json.dumps(
        redaction_doc(result, scanned_at="2026-08-28T00:00:00+09:00", allow_secrets=False),
        ensure_ascii=False,
    )
    session_json = json.dumps(session_redaction_fields(result), ensure_ascii=False)
    console = _scan_console_line(result)
    for secret in LEAK_SECRETS:
        for artifact in (doc_json, session_json, console):
            assert secret not in artifact
            # PRD 13.1: 마지막 4자조차 남기지 않는다.
            assert secret[-4:] not in artifact


def test_scan_console_lines_encode_cp949() -> None:
    # 회귀 (HANDOFF (다)): 리다이렉트된 콘솔(cp949)에서 죽는 문자를 쓰지 않는다.
    kwargs = {"markers": NEUTRAL_MARKERS, "rules": RULES}
    blocked = redact_diff(f"+{OPENAI_FIXTURE}", allow_secrets=False, **kwargs)
    masked = redact_diff(f"+{OPENAI_FIXTURE}", allow_secrets=True, **kwargs)
    clean = redact_diff("+x = 1", allow_secrets=False, **kwargs)
    for result in (blocked, masked, clean):
        _scan_console_line(result).encode("cp949")


# ── 기준 10~14: 환경 정제 (FR-037) ───────────────────────────────────────────


def test_watch_root_paths_become_relative() -> None:
    markers = _windows_markers()
    text = "C:\\proj\\demo\\src\\A.java 그리고 C:/proj/demo/src/B.java"
    sanitized, env = sanitize_environment(text, markers)
    assert sanitized == "src\\A.java 그리고 src/B.java"
    assert env.watch_root_paths == 2


def test_user_profile_paths_become_home() -> None:
    markers = _windows_markers()
    # USERPROFILE 전체 경로와, 다른 드라이브의 Users\<이름> 패턴 둘 다 지워진다 (설계 5.2).
    text = "로그: C:\\Users\\student1\\out.log / D:\\Users\\student1\\backup"
    sanitized, env = sanitize_environment(text, markers)
    assert sanitized == "로그: ~\\out.log / ~\\backup"
    assert env.user_paths == 2
    assert env.bare_usernames == 0


def test_long_username_is_masked_bare() -> None:
    sanitized, env = sanitize_environment("작성자: student1", _windows_markers())
    assert sanitized == f"작성자: {redact.USER_PLACEHOLDER}"
    assert env.bare_usernames == 1


def test_short_username_is_not_masked_bare_but_recorded() -> None:
    short_markers = build_env_markers(
        Path("C:/proj/demo"), {"USERNAME": "kim", "USERPROFILE": "C:\\Users\\kim"}
    )
    assert short_markers.bare_username_maskable is False
    sanitized, env = sanitize_environment("작성자: kim, 경로: C:\\Users\\kim\\a.py", short_markers)
    # 경로 문맥은 지우되(항상), bare 는 짧아서 건너뛴다.
    assert sanitized == "작성자: kim, 경로: ~\\a.py"
    assert env.bare_usernames == 0
    # 건너뛴 사실은 숨은 실패가 아니라 기록된 한계다 (설계 5.2).
    result = redact_diff(
        "+x = 1", allow_secrets=False, markers=short_markers, rules=RULES
    )
    assert result.bare_username_skipped is True
    doc = redaction_doc(result, scanned_at="t", allow_secrets=False)
    env_doc = doc["env"]
    assert isinstance(env_doc, dict)
    assert env_doc["bare_username_skipped"] is True


def test_private_ipv4_is_replaced_and_public_kept() -> None:
    text = "10.0.0.5 192.168.1.10 172.16.0.1 172.31.255.255 172.15.0.1 172.32.0.1 8.8.8.8"
    sanitized, env = sanitize_environment(text, NEUTRAL_MARKERS)
    masked = redact.INTERNAL_ADDR_PLACEHOLDER
    assert sanitized == (
        f"{masked} {masked} {masked} {masked} 172.15.0.1 172.32.0.1 8.8.8.8"
    )
    assert env.internal_addresses == 4


def test_sanitized_text_has_no_environment_traces() -> None:
    markers = _windows_markers()
    text = "\n".join(
        [
            "C:\\proj\\demo\\Main.java",
            "C:/proj/demo/Main.java",
            "C:\\Users\\student1\\.m2\\settings.xml",
            "student1 이 저장함",
        ]
    )
    sanitized, _ = sanitize_environment(text, markers)
    for trace in ("C:\\proj\\demo", "C:/proj/demo", "C:\\Users\\student1", "student1"):
        assert trace not in sanitized


def test_build_env_markers_falls_back_to_userprofile_name() -> None:
    markers = build_env_markers(Path("C:/proj"), {"USERPROFILE": "C:\\Users\\student1"})
    assert markers.username == "student1"
    assert markers.bare_username_maskable is True


def test_build_env_markers_without_user_env() -> None:
    markers = build_env_markers(Path("C:/proj"), {})
    assert markers.username is None
    assert markers.bare_username_maskable is False
    # username 이 아예 없으면 "짧아서 건너뜀"도 아니다.
    result = redact_diff("+x = 1", allow_secrets=False, markers=markers, rules=RULES)
    assert result.bare_username_skipped is False


# ── 기준 15·16: redaction.json 결정성·스키마·원자적 쓰기 ─────────────────────


def test_redaction_doc_is_deterministic() -> None:
    def build() -> str:
        result = redact_diff(
            '+password = "hunter2hunter2"',
            allow_secrets=False,
            markers=NEUTRAL_MARKERS,
            rules=RULES,
        )
        return json.dumps(
            redaction_doc(result, scanned_at="2026-08-28T00:00:00+09:00", allow_secrets=False),
            ensure_ascii=False,
        )

    assert build() == build()


def test_by_rule_counts_is_sorted_and_counted() -> None:
    findings = (
        SecretFinding(rule_id="slack_token", rel_path=None, line_no=1),
        SecretFinding(rule_id="openai_api_key", rel_path=None, line_no=2),
        SecretFinding(rule_id="slack_token", rel_path=None, line_no=3),
    )
    counts = by_rule_counts(findings)
    assert counts == {"openai_api_key": 1, "slack_token": 2}
    assert list(counts) == ["openai_api_key", "slack_token"]


def test_write_redaction_json_writes_full_schema(tmp_path: Path) -> None:
    result = redact_diff(
        "+x = 1", allow_secrets=False, markers=NEUTRAL_MARKERS, rules=RULES
    )
    doc = redaction_doc(result, scanned_at="2026-08-28T00:00:00+09:00", allow_secrets=False)
    target = tmp_path / "session" / "redaction.json"

    write_redaction_json(target, doc)

    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert set(loaded) == {
        "schema_version",
        "scanned_at",
        "source",
        "policy",
        "allow_secrets",
        "secrets_found",
        "findings",
        "by_rule",
        "env",
    }
    assert set(loaded["env"]) == {
        "paths_relativized",
        "watch_root_paths",
        "user_paths",
        "bare_usernames",
        "internal_addresses",
        "bare_username_skipped",
    }
    assert loaded["schema_version"] == redact.REDACTION_SCHEMA_VERSION
    assert loaded["source"] == "final.diff"
    assert loaded["policy"] == redact.POLICY_CLEAN
    # 원자적 교체의 임시파일이 남지 않는다.
    assert list(target.parent.glob(".redaction-*")) == []
