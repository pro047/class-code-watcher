"""감시 루프 오케스트레이션 — 이 기능의 부작용 층 (FR-011~FR-017, FR-035, FR-040/041).

watchdog·큐·시계·파일시스템을 만지는 코드를 여기에 모은다. 병합·안정화·모드 판별·행
구성은 전부 다른 모듈의 순수 함수라 이 파일에는 판정이 거의 없다. 유일하게 판정이 남는
compute_statuses 는 값만 받는 순수 함수로 떼어 뒀다.
"""

import ctypes
import json
import os
import queue
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingObserver

from .config import (
    DEFAULT_STABLE_MS,
    DEFAULT_STABLE_TIMEOUT_MS,
    FINALIZE_ENTER_BUDGET_MS,
    Secrets,
    WatchConfig,
    mask_secrets,
)
from .debounce import Debouncer, EventKind, LogicalEvent, RawEvent
from .diffgen import (
    DiffResult,
    change_stats_fields,
    generate_session_diff,
    render_final_diff,
    watched_file_entries,
)
from .discord_client import make_discord_sender
from .eventlog import append_jsonl, event_row
from .notify import (
    DISCORD_NOT_RUN,
    ERROR_DISCORD_NOT_ATTEMPTED,
    ERROR_DISCORD_PAYLOAD_FAILED,
    ERROR_DISCORD_URL_MISSING,
    SKIP_DRY_RUN,
    SKIP_NO_CHANGE,
    SKIP_NO_DISCORD,
    SKIP_NO_SUMMARY,
    SKIP_SECRETS_BLOCKED,
    DeliveryOutcome,
    build_render_input,
    deliver,
    failed_delivery,
    payload_doc,
    plan_message,
    render_stats_only,
    resolve_discord_state,
    session_discord_fields,
    skipped_delivery,
    write_payload_json,
)
from .openai_client import DEFAULT_OPENAI_MODEL, make_openai_caller
from .redact import (
    ERROR_SECRETS_DETECTED,
    RedactionResult,
    build_env_markers,
    by_rule_counts,
    default_rules,
    redact_diff,
    redaction_doc,
    session_redaction_fields,
    write_redaction_json,
)
from .selector import Selection, is_watched
from .session import SessionPaths, SessionStatus, transition, write_session_json
from .snapshot import SnapshotResult, hash_bytes, hash_map, snapshot_tree, write_manifest
from .stability import wait_for_stability
from .summarize import (
    SOURCE_RULE_BASED,
    PromptInput,
    SummarizeOutcome,
    build_prompt,
    prompt_doc,
    prompt_file_stats,
    run_summarize,
    schema_error_row,
    session_openai_fields,
    write_prompt_json,
    write_summary_json,
)
from .watchmode import DRIVE_REMOTE, resolve_watch_mode

# Ctrl+C 반응 지연의 상한. 큐 대기가 이보다 길면 인터럽트가 늦게 전달될 수 있다.
LOOP_TIMEOUT_S = 0.2

STATUS_UNCHANGED = "unchanged"
STATUS_MODIFIED = "modified"
STATUS_ADDED = "added"
STATUS_DELETED = "deleted"
# 중단된 세션 전용. baseline↔final 비교를 못 했으므로 어느 상태도 주장하지 않는다.
STATUS_UNKNOWN = "unknown"

# diff 를 못 만들어 정제·요약에 넘길 것이 없는 세션.
ERROR_DIFF_FAILED = "diff_failed"
# 정제 산출물을 못 써서 스캔 통과를 주장할 수 없는 세션.
ERROR_REDACTION_FAILED = "redaction_failed"
ERROR_OPENAI_KEY_MISSING = "openai_key_missing"
# 요약 자체는 만들었지만 산출물을 못 쓴 세션. 성공으로 부르면 5단계가 없는 파일을 읽는다.
ERROR_SUMMARY_WRITE_FAILED = "summary_write_failed"
ABORTED_ERROR = "aborted_by_user"

# WatchOutcome.summary_state — cli 의 콘솔·종료 코드 매핑 기준.
SUMMARY_OK = "ok"
SUMMARY_FALLBACK = "fallback"
SUMMARY_FAILED = "failed"
SUMMARY_DRY_RUN = "dry_run"
# 요약 단계에 도달하지 못한 세션 (no_change·차단·diff/정제 실패·abort).
SUMMARY_NOT_RUN = "not_run"


@dataclass(frozen=True)
class WatchOutcome:
    statuses: Mapping[str, str]
    unstable: bool
    logical_event_count: int
    no_change: bool
    aborted: bool
    # 비밀값 탐지로 외부 전송을 중단한 세션 (FR-036). 종료 코드는 1 그대로다.
    secrets_blocked: bool = False
    summary_state: str = SUMMARY_NOT_RUN
    discord_state: str = DISCORD_NOT_RUN


def compute_statuses(
    baseline: Mapping[str, str], final: Mapping[str, str]
) -> dict[str, str]:
    """순수 — baseline/final 해시 맵을 파일별 상태로 환원한다 (FR-017, FR-035 판정 근거)."""
    statuses: dict[str, str] = {}
    for rel_path in sorted(set(baseline) | set(final)):
        before = baseline.get(rel_path)
        after = final.get(rel_path)
        if before is None:
            statuses[rel_path] = STATUS_ADDED
        elif after is None:
            statuses[rel_path] = STATUS_DELETED
        elif before == after:
            statuses[rel_path] = STATUS_UNCHANGED
        else:
            statuses[rel_path] = STATUS_MODIFIED
    return statuses


def unknown_file_statuses(selected: Sequence[PurePosixPath]) -> list[dict[str, str]]:
    """순수 — 중단된 세션의 watched_files.

    시작 시점 doc 은 전 파일을 unchanged 로 적어 둔다. 중단되면 그 값이 그대로 굳어
    "변경 없음"이라고 잘못 말하게 되므로 판정 불가로 낮춘다.
    """
    return [{"path": str(path), "status": STATUS_UNKNOWN} for path in selected]


def is_no_change(statuses: Mapping[str, str]) -> bool:
    return all(status == STATUS_UNCHANGED for status in statuses.values())


def resolve_summary_state(
    outcome: SummarizeOutcome | None, *, attempted: bool, dry_run: bool
) -> str:
    """순수 — 요약 결과를 WatchOutcome.summary_state 로 환원한다.

    attempted 는 "정제를 통과해 요약 지점까지 왔는가"다. dry-run 은 호출을 하지 않고도
    할 일을 다 한 경우라 실패와 구분해야 한다 (PRD 10.2).
    """
    if not attempted:
        return SUMMARY_NOT_RUN
    if outcome is None:
        return SUMMARY_DRY_RUN if dry_run else SUMMARY_NOT_RUN
    if outcome.error is not None:
        return SUMMARY_FAILED
    if outcome.source == SOURCE_RULE_BASED:
        return SUMMARY_FALLBACK
    return SUMMARY_OK


def resolve_session_end(
    *,
    no_change: bool,
    secrets_blocked: bool,
    diff_failed: bool,
    redaction_failed: bool,
    summary_state: str,
    summary_error: str | None,
    no_discord: bool,
    discord: DeliveryOutcome | None = None,
) -> tuple[SessionStatus, str | None]:
    """순수 — 세션 종료 status·error 판정 (PRD 12절 표).

    분기 순서가 곧 우선순위다. 비밀값 차단이 맨 앞인 이유는 그 뒤 어떤 단계가 실패했든
    사용자에게 알려야 하는 사실이 "전송을 막았다"이기 때문이다.
    """
    if no_change:
        return SessionStatus.COMPLETED, None
    if secrets_blocked:
        return SessionStatus.FAILED, ERROR_SECRETS_DETECTED
    if diff_failed:
        return SessionStatus.PARTIAL, ERROR_DIFF_FAILED
    if redaction_failed:
        return SessionStatus.PARTIAL, ERROR_REDACTION_FAILED
    if summary_state == SUMMARY_DRY_RUN:
        return SessionStatus.COMPLETED, None
    if summary_error is not None:
        return SessionStatus.PARTIAL, summary_error
    if summary_state in (SUMMARY_OK, SUMMARY_FALLBACK) and no_discord:
        # 사용자가 전송 생략을 명시했으므로 이 설정에서 할 일이 전부 끝났다 (PRD 10.2).
        return SessionStatus.COMPLETED, None
    if discord is not None and discord.delivered:
        return SessionStatus.COMPLETED, None
    if discord is not None and discord.error is not None:
        # 4xx/5xx/timeout/연결 실패를 error 값으로 가른다. 종료 코드는 PRD 10.3 의 4종
        # 제약 때문에 전부 1이고, 구분은 session.json 이 한다 (C-10).
        return SessionStatus.PARTIAL, discord.error
    # 전송 판정 지점에 도달하지 못했다. 5갈래 생략은 위 분기가 이미 잡으므로 정상 경로에는 없다.
    return SessionStatus.PARTIAL, ERROR_DISCORD_NOT_ATTEMPTED


def _drive_type_of(drive_root: str) -> int:
    """`추정`: Windows GetDriveTypeW. 조회 실패는 판정 불가(0)로 환원해 native 로 흘린다."""
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return 0
    try:
        return int(windll.kernel32.GetDriveTypeW(drive_root))
    except (OSError, AttributeError, ValueError):
        return 0


def _make_observer(mode: str) -> BaseObserver:
    return PollingObserver() if mode == "polling" else Observer()


class _Handler(FileSystemEventHandler):
    """watchdog 스레드에서 도는 유일한 코드. 필터링과 큐 적재만 하고 판단하지 않는다."""

    def __init__(
        self,
        root: Path,
        include: tuple[str, ...],
        exclude: tuple[str, ...],
        sink: "queue.Queue[RawEvent]",
        clock: Callable[[], float],
    ) -> None:
        self._root = root
        self._include = include
        self._exclude = exclude
        self._sink = sink
        self._clock = clock

    def _offer(self, raw_path: str | bytes, kind: EventKind) -> None:
        try:
            rel = Path(os.fsdecode(raw_path)).relative_to(self._root)
        except ValueError:
            # 감시 루트 밖의 경로. watchdog 이 루트 자신의 이벤트를 줄 때도 여기로 온다.
            return
        posix = PurePosixPath(rel.as_posix())
        if not is_watched(posix, self._include, self._exclude):
            return
        self._sink.put(RawEvent(rel_path=posix.as_posix(), kind=kind, at=self._clock()))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._offer(event.src_path, "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._offer(event.src_path, "modified")

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._offer(event.src_path, "deleted")

    def on_moved(self, event: FileSystemEvent) -> None:
        # 원자적 저장(임시파일 → rename)이 여기로 온다. 두 경로를 각각 따로 본다 (FR-013).
        if event.is_directory:
            return
        self._offer(event.src_path, "deleted")
        self._offer(event.dest_path, "moved")


def _format_size(size: int | None) -> str:
    if size is None:
        return "-"
    if size < 1024:
        return f"{size} B"
    return f"{size / 1024:.1f} KB"


def _read_digest(root: Path, rel_path: str) -> tuple[str | None, int | None]:
    """이벤트 로그용 해시·크기. 이미 사라진 파일은 값 없이 지나간다."""
    try:
        data = (root / rel_path).read_bytes()
    except OSError:
        return None, None
    return hash_bytes(data), len(data)


class _Session:
    """run_session 의 지역 상태를 담는 상자. 외부에 노출하지 않는다."""

    def __init__(
        self,
        config: WatchConfig,
        paths: SessionPaths,
        selection: Selection,
        emit: Callable[[str], None],
        secrets: Secrets,
        model: str,
    ) -> None:
        self.config = config
        self.paths = paths
        self.selection = selection
        self.emit = emit
        # known-value 탐지 규칙에만 쓴다 (FR-036). 다른 용도로 쓰지 않는다.
        self.secrets = secrets
        # 비밀값이 아니라 설정값이다. 해석은 cli 가 끝내고 여기로는 확정된 이름만 온다.
        self.model = model
        self.doc: dict[str, object] = {}
        self.baseline_hashes: dict[str, str] = {}
        self.observed: set[str] = set()
        self.event_count = 0
        self.history_seq = 0

    def write_status(self, status: SessionStatus, **fields: object) -> None:
        self.doc = transition(self.doc, status, **fields)
        write_session_json(self.paths, self.doc)

    def handle(self, logical: LogicalEvent) -> None:
        self.event_count += 1
        self.observed.add(logical.rel_path)

        if logical.kind == "deleted":
            sha256, size = None, None
        else:
            sha256, size = _read_digest(self.config.watch_root, logical.rel_path)

        append_jsonl(
            self.paths.events_jsonl,
            event_row(
                logical,
                wall_time=datetime.now().astimezone(),
                sha256=sha256,
                size=size,
            ),
        )

        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        digest = f"{sha256[:4]}..." if sha256 else "-"
        suffix = " (신규)" if logical.rel_path not in self.baseline_hashes else ""
        label = "삭제 감지" if logical.kind == "deleted" else "변경 감지"
        self.emit(
            f"[{stamp}] {label}  {logical.rel_path}  hash={digest}  "
            f"{_format_size(size)}{suffix}"
        )

        if self.config.history and logical.kind != "deleted":
            self.history_seq += 1
            slot = self.paths.history_dir / f"{self.history_seq:04d}"
            result = snapshot_tree(
                self.config.watch_root, [PurePosixPath(logical.rel_path)], slot
            )
            write_manifest(slot, result)


def _diff_console_line(result: DiffResult) -> str:
    """PRD 10.1 의 [DIFF] 한 줄. 건너뛴 파일이 없으면 뒷부분을 붙이지 않는다."""
    line = (
        f"[DIFF] {result.files_changed}개 파일 변경 "
        f"(+{result.added_lines} / -{result.deleted_lines})"
    )
    if not result.skipped:
        return line
    reasons = sorted({item.skip_reason for item in result.skipped if item.skip_reason})
    return f"{line}, {len(result.skipped)}개 건너뜀({', '.join(reasons)})"


def _generate_diff(
    state: "_Session", statuses: Mapping[str, str], ended_at: str
) -> DiffResult | None:
    """diff 산출물 생성. 통째로 실패해도 세션을 죽이지 않는다 (PRD 12절 복구 원칙)."""
    try:
        result = generate_session_diff(
            state.paths,
            statuses,
            event_count=state.event_count,
            started_at=str(state.doc.get("started_at", "")),
            ended_at=ended_at,
        )
    except OSError as exc:
        _stage_error(state, "diff", exc)
        state.emit("[WARN] diff 생성에 실패했습니다. baseline/final 스냅샷은 그대로 남습니다.")
        return None
    state.emit(_diff_console_line(result))
    return result


def _scan_console_line(result: RedactionResult) -> str:
    """PRD 10.1 의 [SCAN] 한 줄. 규칙 ID 와 건수만 싣는다 — 위치는 redaction.json 이 안내한다."""
    found = len(result.findings)
    if found == 0:
        return "[SCAN] 비밀정보 패턴 탐지 없음"
    if result.blocked:
        counts = by_rule_counts(result.findings)
        rules = ", ".join(f"{rule} {count}" for rule, count in counts.items())
        return f"[SCAN] 비밀정보 패턴 {found}건 탐지 (규칙: {rules}) - 외부 전송을 중단합니다"
    return f"[SCAN] 비밀정보 패턴 {found}건 탐지 - --allow-secrets 로 마스킹 후 진행합니다"


def _run_redaction(state: "_Session", diff_result: DiffResult) -> RedactionResult | None:
    """외부 전송 직전의 정제 (FR-036~FR-038). 판정은 전부 redact.py 순수 함수가 한다.

    산출물 쓰기가 실패하면 None 을 돌려준다. 스캔 실패를 "통과"로 오인해 전송으로 흘리면
    안 되므로 안전한 쪽으로 실패시킨다 — 세션 자체는 죽이지 않는다 (PRD 12절 복구 원칙).
    """
    known = [
        value
        for value in (state.secrets.openai_api_key, state.secrets.discord_webhook_url)
        if value
    ]
    result = redact_diff(
        render_final_diff(diff_result),
        scanned_paths=[item.rel_path for item in diff_result.files],
        allow_secrets=state.config.allow_secrets,
        markers=build_env_markers(state.config.watch_root, os.environ),
        rules=default_rules(known),
    )
    doc = redaction_doc(
        result,
        scanned_at=datetime.now().astimezone().isoformat(),
        allow_secrets=state.config.allow_secrets,
    )
    try:
        write_redaction_json(state.paths.redaction_json, doc)
    except OSError as exc:
        _stage_error(state, "redaction", exc)
        state.emit("[WARN] 비밀정보 검사 결과를 저장하지 못했습니다. 외부 전송은 하지 않습니다.")
        return None
    state.emit(_scan_console_line(result))
    return result


def _stage_error(state: "_Session", stage: str, exc: OSError) -> None:
    """errors.jsonl 한 행. 예외 메시지 원문은 싣지 않는다 — 경로·환경이 새는 통로다 (FR-042)."""
    append_jsonl(
        state.paths.errors_jsonl,
        {
            "timestamp": datetime.now().astimezone().isoformat(),
            "stage": stage,
            "error": type(exc).__name__,
        },
    )


def _record_schema_failures(state: "_Session", outcome: SummarizeOutcome) -> None:
    """검증 실패 원본의 "제한적 보관" (FR-031, PRD 9.3).

    발췌를 mask_secrets 에 통과시키는 것이 여기다 — 응답에 키가 되비쳐 나와도
    errors.jsonl 에는 남지 않는다 (FR-042).
    """
    for failure in outcome.schema_failures:
        masked = replace(
            failure, raw_excerpt=mask_secrets(failure.raw_excerpt, state.secrets)
        )
        append_jsonl(
            state.paths.errors_jsonl,
            schema_error_row(masked, timestamp=datetime.now().astimezone().isoformat()),
        )


def _flag_llm_sensitive(state: "_Session") -> None:
    """모델의 sensitive_data_detected 를 redaction.json 에 사후 신호로 더한다 (PRD 11.3).

    전송 차단이 아니다 — 스캐너 규칙 개선 후보를 모으는 기록일 뿐이라는 PRD 원칙 그대로다.
    """
    state.emit("[WARN] 모델이 비밀정보 의심을 신고했습니다. redaction.json 을 확인하세요.")
    try:
        doc = json.loads(state.paths.redaction_json.read_text(encoding="utf-8"))
        doc["llm_sensitive_flag"] = True
        write_redaction_json(state.paths.redaction_json, doc)
    except (OSError, ValueError):
        state.emit("[WARN] redaction.json 에 모델 신고를 기록하지 못했습니다.")


def _failed_summary(error: str) -> SummarizeOutcome:
    """호출 없이 끝난 실패. calls=0 이 session.json 에 그대로 남는다 (FR-035 준수 근거)."""
    return SummarizeOutcome(
        source=None,
        doc=None,
        calls=0,
        retries=0,
        request_id=None,
        model=None,
        error=error,
        http_status=None,
        llm_sensitive_flag=False,
        schema_failures=(),
    )


def _run_summarize(
    state: "_Session", redacted_diff: str, diff_result: DiffResult, ended_at: str
) -> SummarizeOutcome | None:
    """요약 호출 지점 (FR-030~FR-032, FR-039). dry-run 성공일 때만 None 을 돌려준다.

    정제를 통과한 본문만 인자로 받는다 — final.diff 를 다시 읽거나 스캔을 다시 도는 경로를
    만들지 않는다 (FR-036). watch_root·session.json 은 PromptInput 에 들어갈 자리가 아예
    없어 프롬프트로 새지 않는다 (FR-037).
    """
    inp = PromptInput(
        title=state.config.title,
        started_at=str(state.doc.get("started_at", "")),
        ended_at=ended_at,
        files=prompt_file_stats(diff_result),
        redacted_diff=redacted_diff,
    )

    if state.config.dry_run:
        prompt = build_prompt(inp)
        target = state.paths.prompt_json
        doc = prompt_doc(
            prompt,
            generated_at=datetime.now().astimezone().isoformat(),
            model=state.model,
        )
        try:
            write_prompt_json(target, doc)
        except OSError as exc:
            _stage_error(state, "summarize", exc)
            state.emit("[WARN] 프롬프트 산출물을 저장하지 못했습니다.")
            return _failed_summary(ERROR_SUMMARY_WRITE_FAILED)
        state.emit(f"[DRY-RUN] 외부 호출 없이 프롬프트까지 검증했습니다: {target}")
        return None

    api_key = state.secrets.openai_api_key
    if not api_key:
        state.emit("[FAILED] OPENAI_API_KEY 가 없습니다. .env 를 확인하세요.")
        return _failed_summary(ERROR_OPENAI_KEY_MISSING)

    outcome = run_summarize(
        inp,
        make_openai_caller(api_key, state.model),
        now=lambda: datetime.now().astimezone().isoformat(),
        emit=state.emit,
    )
    _record_schema_failures(state, outcome)
    if outcome.llm_sensitive_flag:
        _flag_llm_sensitive(state)
    if outcome.doc is None:
        return outcome

    target = state.paths.summary_json
    try:
        write_summary_json(target, outcome.doc)
    except OSError as exc:
        _stage_error(state, "summarize", exc)
        state.emit("[WARN] 요약 산출물을 저장하지 못했습니다.")
        return replace(outcome, error=ERROR_SUMMARY_WRITE_FAILED)
    state.emit(f"[AI] 요약 저장: {target}")
    return outcome


def _change_stats_of(
    diff_result: DiffResult | None, statuses: Mapping[str, str], event_count: int
) -> dict[str, object]:
    """session.json 과 콘솔이 같은 수치를 말하도록 계산을 한 곳에 모은다.

    diff 를 못 돌린 세션(no_change 또는 생성 실패)은 라인 수를 주장하지 않는다.
    """
    if diff_result is None:
        changed = sum(1 for status in statuses.values() if status != STATUS_UNCHANGED)
        return {"files_changed": changed, "events": event_count}
    return change_stats_fields(diff_result, event_count)


def _stat_int(stats: Mapping[str, object], key: str) -> int:
    value = stats.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _run_notify(
    state: "_Session",
    outcome: SummarizeOutcome | None,
    *,
    change_stats: Mapping[str, object],
    ended_at: str,
    no_change: bool,
    secrets_blocked: bool,
) -> DeliveryOutcome:
    """Discord 전송 지점 (FR-033/034/050~052). 판정은 전부 notify.py 순수 함수가 한다.

    렌더러에는 summary.json 의 doc 만 넘어간다 — final.diff·정제본을 다시 읽는 경로를
    만들지 않는다 (FR-051). 전송이 아예 일어나지 않는 5갈래는 여기서 갈리고, 그중 넷은
    콘솔 출력도 기존 문구를 그대로 쓴다.
    """
    if no_change:
        return skipped_delivery(SKIP_NO_CHANGE)
    if secrets_blocked:
        return skipped_delivery(SKIP_SECRETS_BLOCKED)
    if state.config.dry_run:
        return skipped_delivery(SKIP_DRY_RUN)

    started_at = str(state.doc.get("started_at", ""))
    doc = outcome.doc if outcome is not None else None
    render_input = (
        build_render_input(
            doc, title_fallback=state.config.title, started_at=started_at, ended_at=ended_at
        )
        if doc is not None
        else None
    )
    if render_input is None:
        # 보낼 내용이 없는데 알림을 쓰는 것은 FR-052 와 어긋난다. 실행자에게는 통계만 보인다.
        state.emit(
            render_stats_only(
                title=state.config.title,
                started_at=started_at,
                ended_at=ended_at,
                files_changed=_stat_int(change_stats, "files_changed"),
                added_lines=_stat_int(change_stats, "added_lines"),
                deleted_lines=_stat_int(change_stats, "deleted_lines"),
                session_root=str(state.paths.root),
                reason=(outcome.error if outcome is not None and outcome.error else "요약 없음"),
            )
        )
        return skipped_delivery(SKIP_NO_SUMMARY)

    plan = plan_message(render_input)
    # 전송 성공·실패·--no-discord 를 가리지 않고 항상 낸다. 분기가 하나면 실행자가 요약을
    # 볼 경로도, E2E 가 webhook 없이 렌더링을 검증할 경로도 같아진다.
    state.emit(plan.text)
    try:
        write_payload_json(
            state.paths.discord_payload_json,
            payload_doc(plan, generated_at=datetime.now().astimezone().isoformat()),
        )
    except OSError as exc:
        _stage_error(state, "notify", exc)
        # 로컬 보존이 성립하지 않는 상태로 외부에 나가지 않는다 (PRD 12절 복구 원칙).
        state.emit("[WARN] 전송할 내용을 저장하지 못했습니다. 외부 전송은 하지 않습니다.")
        return failed_delivery(ERROR_DISCORD_PAYLOAD_FAILED)

    if state.config.no_discord:
        return skipped_delivery(SKIP_NO_DISCORD)
    webhook_url = state.secrets.discord_webhook_url
    if not webhook_url:
        # --no-discord 를 안 준 사용자는 전송을 기대했다. 생략이 아니라 실패다.
        state.emit("[FAILED] DISCORD_WEBHOOK_URL 이 없습니다. .env 를 확인하세요.")
        return failed_delivery(ERROR_DISCORD_URL_MISSING)

    state.emit(f"[DISCORD] 요약을 전송합니다 ({len(plan.chunks)}개 메시지)")
    result = deliver(plan, make_discord_sender(webhook_url))
    if result.delivered:
        state.emit(f"[DISCORD] 전송 완료 (HTTP {result.http_status})")
    else:
        # 메시지에 URL 을 넣지 않는 것이 1차, emit 뒤의 mask_secrets 가 2차다.
        state.emit(f"[WARN] Discord 전송에 실패했습니다 (사유: {result.error}).")
        state.emit(f"       전송할 내용은 남아 있습니다: {state.paths.discord_payload_json}")
    return result


def _drain_queue(sink: "queue.Queue[RawEvent]", debouncer: Debouncer) -> None:
    """큐를 짧게 기다렸다가 쌓인 것을 한 번에 흡수한다."""
    deadline = debouncer.next_deadline()
    timeout = LOOP_TIMEOUT_S
    if deadline is not None:
        timeout = max(0.0, min(LOOP_TIMEOUT_S, deadline - time.monotonic()))
    try:
        if timeout > 0:
            debouncer.observe(sink.get(timeout=timeout))
        while True:
            debouncer.observe(sink.get_nowait())
    except queue.Empty:
        return


def run_session(
    config: WatchConfig,
    paths: SessionPaths,
    selection: Selection,
    emit: Callable[[str], None],
    secrets: Secrets,
    model: str = DEFAULT_OPENAI_MODEL,
) -> WatchOutcome:
    """감시 세션 전체. session.json 의 상태 전이도 여기서 밟는다 (FR-040).

    model 은 `.env` 병합까지 끝낸 cli 가 넘긴다. 기본값을 둔 것은 이 함수를 직접 부르는
    호출부가 env 해석을 몰라도 되게 하기 위해서다.
    """
    state = _Session(config, paths, selection, emit, secrets, model)
    state.doc = json.loads(paths.session_json.read_text(encoding="utf-8"))

    decision = resolve_watch_mode(config.watch_root, config.polling, os.environ, _drive_type_of)
    state.doc["watch_mode"] = decision.mode
    if decision.reason is not None:
        state.doc["watch_mode_reason"] = decision.reason
        if not config.polling:
            emit(f"[WATCH] 폴링 모드로 자동 전환합니다: {decision.reason}")
    write_session_json(paths, state.doc)

    baseline = snapshot_tree(config.watch_root, selection.selected, paths.baseline_dir)
    write_manifest(paths.baseline_dir, baseline)
    state.baseline_hashes = hash_map(baseline)
    emit(f"[OK] baseline 저장: {paths.baseline_dir}")

    state.write_status(SessionStatus.WATCHING)
    emit("[WATCHING] 변경 감시 중... 종료하려면 Ctrl+C")

    sink: queue.Queue[RawEvent] = queue.Queue()
    debouncer = Debouncer(config.debounce_ms, known_paths=state.baseline_hashes)
    handler = _Handler(config.watch_root, config.include, config.exclude, sink, time.monotonic)
    observer = _make_observer(decision.mode)
    observer.schedule(handler, str(config.watch_root), recursive=True)
    observer.start()

    try:
        while True:
            _drain_queue(sink, debouncer)
            for logical in debouncer.due(time.monotonic()):
                state.handle(logical)
    except KeyboardInterrupt:
        emit("")
    finally:
        observer.stop()
        observer.join(timeout=FINALIZE_ENTER_BUDGET_MS / 1000.0)

    return _finalize(state, sink, debouncer)


def _finalize(
    state: _Session, sink: "queue.Queue[RawEvent]", debouncer: Debouncer
) -> WatchOutcome:
    """FR-014 의 종료 흐름. 두 번째 Ctrl+C 는 여기서 잡아 산출물을 남긴 채 끝낸다."""
    state.write_status(SessionStatus.FINALIZING)
    state.emit("[FINALIZING] 감시 중지 · debounce flush · 파일 안정화 확인")

    aborted = False
    unstable = False
    statuses: dict[str, str] = {}
    diff_result: DiffResult | None = None
    redaction: RedactionResult | None = None
    summarize: SummarizeOutcome | None = None
    summarize_attempted = False
    discord: DeliveryOutcome | None = None
    # stats.json 과 session.json 이 서로 다른 시각을 말하면 같은 세션으로 안 보인다.
    ended_at: str | None = None
    try:
        while True:
            try:
                debouncer.observe(sink.get_nowait())
            except queue.Empty:
                break
        for logical in debouncer.flush():
            state.handle(logical)

        report = wait_for_stability(
            sorted(state.observed),
            lambda rel: _stat_of(state.config.watch_root, rel),
            time.monotonic,
            time.sleep,
            DEFAULT_STABLE_MS,
            DEFAULT_STABLE_TIMEOUT_MS,
        )
        unstable = bool(report.unstable)

        targets = sorted(set(state.baseline_hashes) | state.observed)
        final = snapshot_tree(
            state.config.watch_root,
            [PurePosixPath(rel) for rel in targets],
            state.paths.final_dir,
        )
        final = SnapshotResult(metas=final.metas, unstable=unstable)
        write_manifest(state.paths.final_dir, final)
        statuses = compute_statuses(state.baseline_hashes, hash_map(final))
        ended_at = datetime.now().astimezone().isoformat()
        # no_change 세션은 diff 산출물을 만들지 않는다 (FR-035 경로 불변).
        if not is_no_change(statuses):
            diff_result = _generate_diff(state, statuses, ended_at)
            # diff 를 못 만든 세션은 스캔할 대상 자체가 없다.
            if diff_result is not None:
                redaction = _run_redaction(state, diff_result)
                # blocked 면 text 가 None 이라 요약 인자를 만들 수 없다 — 타입과 가드 이중으로
                # 무호출을 보장한다 (FR-036). 요약은 정제 판정 직후, 이 try 안에 둔다.
                if redaction is not None and redaction.text is not None:
                    summarize_attempted = True
                    summarize = _run_summarize(
                        state, redaction.text, diff_result, ended_at
                    )
        # 전송도 같은 try 안이다 — 두 번째 Ctrl+C 는 abort 분기로 흐르고 이미 쓴
        # discord_payload.json 은 남는다 (PRD 12절).
        discord = _run_notify(
            state,
            summarize,
            change_stats=_change_stats_of(diff_result, statuses, state.event_count),
            ended_at=ended_at,
            no_change=is_no_change(statuses),
            secrets_blocked=redaction is not None and redaction.blocked,
        )
    except KeyboardInterrupt:
        aborted = True

    if ended_at is None:
        ended_at = datetime.now().astimezone().isoformat()
    if aborted:
        state.write_status(
            SessionStatus.FAILED,
            ended_at=ended_at,
            error=ABORTED_ERROR,
            watched_files=unknown_file_statuses(state.selection.selected),
            discord=session_discord_fields(discord),
        )
        state.emit("[ABORTED] 두 번째 종료 요청. 지금까지의 산출물만 남깁니다.")
        return WatchOutcome(
            statuses=statuses,
            unstable=unstable,
            logical_event_count=state.event_count,
            no_change=False,
            aborted=True,
        )

    no_change = is_no_change(statuses)
    if diff_result is None:
        watched_files: list[dict[str, str]] = [
            {"path": rel, "status": status} for rel, status in statuses.items()
        ]
    else:
        watched_files = watched_file_entries(statuses, diff_result)
    fields: dict[str, object] = {
        "ended_at": ended_at,
        "watched_files": watched_files,
        "change_stats": _change_stats_of(diff_result, statuses, state.event_count),
        "no_change": no_change,
        # 호출이 없던 세션도 calls: 0 으로 남긴다 — "호출 0회"를 사후에 증명할 수 있어야
        # 준수율 집계(PRD 15절)가 전 세션에서 성립한다 (FR-030, FR-035).
        "openai": session_openai_fields(summarize),
        # 전송 0회도 계수로 남긴다 — 같은 논리다 (FR-035, FR-052).
        "discord": session_discord_fields(discord),
    }
    if state.config.dry_run:
        fields["dry_run"] = True
    if redaction is not None:
        fields["redaction"] = session_redaction_fields(redaction)
    secrets_blocked = redaction is not None and redaction.blocked
    summary_state = resolve_summary_state(
        summarize, attempted=summarize_attempted, dry_run=state.config.dry_run
    )
    status, error = resolve_session_end(
        no_change=no_change,
        secrets_blocked=secrets_blocked,
        diff_failed=not no_change and diff_result is None,
        redaction_failed=diff_result is not None and redaction is None,
        summary_state=summary_state,
        summary_error=summarize.error if summarize is not None else None,
        no_discord=state.config.no_discord,
        discord=discord,
    )
    if error is None:
        state.write_status(status, **fields)
    else:
        state.write_status(status, **fields, error=error)

    return WatchOutcome(
        statuses=statuses,
        unstable=unstable,
        logical_event_count=state.event_count,
        no_change=no_change,
        aborted=False,
        secrets_blocked=secrets_blocked,
        summary_state=summary_state,
        discord_state=resolve_discord_state(discord),
    )


def _stat_of(root: Path, rel_path: str) -> tuple[int, float] | None:
    try:
        stat = (root / rel_path).stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime)


__all__ = [
    "DRIVE_REMOTE",
    "WatchOutcome",
    "compute_statuses",
    "is_no_change",
    "resolve_session_end",
    "resolve_summary_state",
    "run_session",
]
