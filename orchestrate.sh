#!/usr/bin/env bash
# 실행 역할 터미널 (터미널 1)
#
# 역할 분리:
#   이 스크립트  = 오케스트레이터. 진행 결정권을 독점한다.
#   advisor.sh   = 상담역. 읽기 전용. 진행 권한 없음.
#   사람         = 유일하게 게이트 버튼을 누르는 주체.
#
# 사용법:
#   ./orchestrate.sh <feature-name>
#   AUTO=1 ./orchestrate.sh <feature-name>     # 사람 게이트 건너뜀 (무인)
#   MAX_RETRY=3 ./orchestrate.sh <feature-name>

set -euo pipefail

# ─────────────────────────────────────────── 설정
ROOT="$(git rev-parse --show-toplevel)"
FEATURE="${1:?사용법: ./orchestrate.sh <feature-name>}"
WORK="$ROOT/.pipeline/$FEATURE"
PROMPTS="$ROOT/prompts"

MAX_RETRY="${MAX_RETRY:-2}"
AUTO="${AUTO:-0}"
# 이미 STATUS: DONE 인 DESIGN.md 가 있으면 설계 단계를 건너뛰고 재사용한다.
# (중단 후 재실행에서 비싼 설계를 다시 만들지 않기 위한 것 — 설계 게이트는 그대로 거친다)
# 설계를 새로 뽑고 싶으면 FRESH_DESIGN=1
FRESH_DESIGN="${FRESH_DESIGN:-0}"
# pytest. 수집된 테스트가 0개면 exit 5 로 실패한다 — 검증 단계가 테스트를 안 쓰고
# 넘어간 것을 게이트가 통과시키면 안 되기 때문이다. (DMS 의 passWithNoTests:false
# 와 같은 자리인데, pytest 는 기본 동작이 이미 그래서 설정이 따로 필요 없다.)
#
# 파이썬에는 빌드 산출물이 없다. DMS 에서 `npm run build` 가 겸하던 "타입이 맞는가"
# 는 mypy 가 대신하고, 게이트 이름도 BUILD_GATE 가 아니라 TYPE_GATE 다.
# mypy·ruff 가 안 깔린 체크아웃이 있을 수 있으므로 무조건 돌리지 않고,
# **프리플라이트에서 환경을 먼저 판정한 뒤** 켤지 정한다 (아래 preflight).
# 환경 실패와 코드 실패를 구분하지 않으면, 도구 없는 체크아웃에서 impl+verify
# 사이클이 단계당 $5~$8 예산을 태우며 3회 헛돈다.
#
# 인터프리터는 PY 로 바꿔 낀다. venv 를 쓰면 그 안의 python 을 주면 된다:
#   PY=.venv/Scripts/python ./orchestrate.sh <feature>
PY="${PY:-python}"
#
# TEST_CMD 를 직접 주면 기본 검증 목록 대신 그것만 쓴다 (하위 호환):
#   TEST_CMD="$PY -m pytest && $PY -m mypy src" ./orchestrate.sh <feature>
TEST_CMD_OVERRIDE="${TEST_CMD:-}"
TEST_CMD="${TEST_CMD:-$PY -m pytest}"

# ── 검증 게이트 상태 ─────────────────────────────────
# state() 가 첫 호출부터 참조하므로 여기서 초기화한다 (set -u).
TYPE_GATE=0                        # 1 = 기준선 mypy 가 녹색이라 타입 검사를 검증에 포함
TYPE_GATE_REASON="프리플라이트 전"
LINT_GATE=0                        # 1 = ruff 가 있어 린트를 검증에 포함
LINT_GATE_REASON="프리플라이트 전"
VERIFY_LIST_DESC="(프리플라이트 전)"
VERIFY_LAST=""
VERIFY_PASSED=""
VERIFY_FAILED=""

# ── 모델 티어링 ──────────────────────────────────────
# 별칭 대신 풀 ID를 박는다. 별칭은 어느 날 조용히 다른 모델을 가리킨다.
#
#   설계  : 최상위. 여기가 틀리면 뒤가 전부 낭비다.
#   구현  : 설계가 확정돼 있으면 난이도가 내려간다. 중간 티어로 충분.
#   검증  : 다시 최상위. "설계에서 벗어난 지점 찾기"는 적대적 추론이라 구현보다 어렵다.
#
# FALLBACK_* 은 가용성 폴백(529 과부하 등) 전용이다.
# 안전 분류기에 의한 모델 교체는 이걸로 막을 수 없다 — MODEL_LOG.md 로 감시한다.
MODEL_DESIGN="${MODEL_DESIGN:-claude-fable-5}"
# 판단 검증은 설계를 반박하는 일이라 verify 와 같은 적대적 추론이다. 상위 모델.
MODEL_JUDGE="${MODEL_JUDGE:-claude-fable-5}"
MODEL_IMPL="${MODEL_IMPL:-claude-opus-5}"
MODEL_VERIFY="${MODEL_VERIFY:-claude-fable-5}"

FALLBACK_DESIGN="${FALLBACK_DESIGN:-claude-opus-5,claude-sonnet-5}"
FALLBACK_JUDGE="${FALLBACK_JUDGE:-claude-opus-5,claude-sonnet-5}"
FALLBACK_IMPL="${FALLBACK_IMPL:-claude-sonnet-5}"
FALLBACK_VERIFY="${FALLBACK_VERIFY:-claude-opus-5,claude-sonnet-5}"

# ── 단계별 상한 ──────────────────────────────────────
# 하나의 세트(40턴/$5)를 전 단계에 쓰던 것이 틀렸다. **병목 축이 단계마다 다르다** —
# judge 는 읽고 대조하느라 턴당 토큰이 커서 돈이 먼저 닿고, impl 은 파일을 많이 써서
# 턴이 먼저 닿는다. 2026-08-24 실측: judge 가 $5.08 에서, impl 이 41턴에서 죽었다.
#
# 실질 브레이크는 예산이다. 턴 상한은 무한루프 탈출용으로만 둔다 —
# 턴으로 조이면 "일은 잘 하는데 상한에 걸려 죽는" 낭비가 생긴다.
# 적용된 값은 STATE.md 의 RUNNING note 에 찍힌다 (다음 주행에서 근거가 쌓이도록).
TURNS_DESIGN="${TURNS_DESIGN:-40}"
TURNS_JUDGE="${TURNS_JUDGE:-40}"
TURNS_IMPL="${TURNS_IMPL:-80}"
TURNS_VERIFY="${TURNS_VERIFY:-40}"

BUDGET_DESIGN="${BUDGET_DESIGN:-5}"
BUDGET_JUDGE="${BUDGET_JUDGE:-5}"
BUDGET_IMPL="${BUDGET_IMPL:-8}"
BUDGET_VERIFY="${BUDGET_VERIFY:-5}"

MODEL_LOG=""   # WORK 확정 후 아래에서 설정

mkdir -p "$WORK"
FAIL_LOG="$WORK/FAIL_LOG.md"     # append-only
STATE="$WORK/STATE.md"           # 상담역이 읽는 유일한 실시간 창구
MODEL_LOG="$WORK/MODEL_LOG.md"   # 요청 모델 vs 실제 실행 모델
touch "$FAIL_LOG" "$MODEL_LOG"

# TEST_CMD 도 내보낸다. prompts/verify.md 가 본문에서 참조하는데, export 하지 않으면
# envsubst 가 빈 문자열로 치환해서 "셸이 `` 를 실행해서 판정한다" 라는 깨진 문장이
# 에이전트에게 전달된다. 실제 명령을 보여주는 편이 리터럴보다 유용하다.
export FEATURE WORK ROOT TEST_CMD PY

log() { printf '\033[1;36m[orch]\033[0m %s\n' "$*" >&2; }
die() {
  state "DIED" "$*" "실패했다. $FAIL_LOG 와 위 note 를 읽고 원인을 사람에게 보고해라. 재실행 여부는 사람이 정한다 — 런처가 임의로 재실행하지 마라."
  printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 2
}

# 실패를 append-only 로 남기는 유일한 창구.
# prompts/impl.md 는 FAIL_LOG 를 "이전 시도가 왜 실패했나"의 유일한 입력으로 쓰는데,
# 예전엔 검증 실패 경로 한 곳에서만 여기에 썼다. 단계가 죽는 경로에는 아무것도 안 남아
# 사람이 매번 *.stream.jsonl 을 jq 로 파야 했다 (2026-08-24 하루에 세 번).
fail_log() {   # fail_log <제목> ; 본문은 stdin
  { echo "## $1 — $(date -Iseconds)"; cat; echo; } >> "$FAIL_LOG"
}

# ─────────────────────────────────────────── 상담역·런처용 상태 브로드캐스트
# 셸은 대화를 못 한다. 대신 상태를 파일로 흘려서 상담역·런처 세션이 읽게 한다.
#
# 3번째 인자가 "## 다음 행동" 블록이 된다 — 런처 계약은 문서(CLAUDE.md·SKILL.md)가
# 아니라 런처가 실제로 읽는 이 파일에 박는다. 문서에만 적힌 계약은 안 지켜졌다
# (2026-08-24: 런처 세션이 스크립트 stderr 의 터미널 안내를 그대로 사용자에게 전달했고,
#  단계가 끝난 뒤 갈 길을 잃었다).
state() {
  local phase=$1 note=${2:-} next=${3:-}
  local bg lg
  if [ "${TYPE_GATE:-0}" = "1" ]; then
    bg="켜짐"
  elif [ -n "${TEST_CMD_OVERRIDE:-}" ]; then
    # 오버라이드가 타입 검사를 포함할 수도 있다. 셸은 모르므로 단정하지 않는다.
    bg="해당 없음 — ${TYPE_GATE_REASON:-TEST_CMD 오버라이드}"
  else
    bg="꺼짐 — ${TYPE_GATE_REASON:-사유 미기록}"
  fi
  if [ "${LINT_GATE:-0}" = "1" ]; then
    lg="켜짐"
  elif [ -n "${TEST_CMD_OVERRIDE:-}" ]; then
    lg="해당 없음 — ${LINT_GATE_REASON:-TEST_CMD 오버라이드}"
  else
    lg="꺼짐 — ${LINT_GATE_REASON:-사유 미기록}"
  fi
  cat > "$STATE" <<EOF
# 파이프라인 상태 (셸이 자동 생성 — 사람이 편집하지 말 것)

- feature: $FEATURE
- phase: $phase
- attempt: ${ATTEMPT:-0} / $((MAX_RETRY + 1))
- pid: $$
- updated: $(date -Iseconds)
- note: $note

## 다음 행동 (런처 세션은 이 블록만 따르면 된다)
${next:-진행 중 — 개입 불필요. 이 파일을 다시 읽으면 최신 상태가 보인다.}

## 검증 게이트

셸이 실제로 무엇을 돌렸는지. "DONE" 이 무엇을 뜻하는지는 여기를 봐야 안다.

- 타입 게이트(mypy): $bg
- 린트 게이트(ruff): $lg
- 검증 명령: $VERIFY_LIST_DESC
- 마지막 결과: ${VERIFY_LAST:-(아직 실행 안 함)}

## 지금까지 생성된 산출물
$(ls -1 "$WORK"/*.md 2>/dev/null | sed 's|.*/|- |' || echo "- (없음)")

## 마지막 테스트 출력 (tail 20)
\`\`\`
$(tail -20 "$WORK/test_out.txt" 2>/dev/null || echo "(아직 없음)")
\`\`\`
EOF
}

# ─────────────────────────────────────────── BLOCKED 종료
# 정상 종료든 크래시든 산출물이 BLOCKED 면 사람은 **막힌 이유**를 받아야 한다.
# 크래시 경로가 이걸 안 부르면 "그냥 죽었다"만 보이고 BLOCKED_NEEDS 가 묻힌다
# (예산 상한 직전에 BLOCKED 를 쓰고 죽는 것은 흔한 조합이다).
emit_blocked() {   # emit_blocked <이름> <산출물> [덧붙일 사인]
  local name=$1 artifact=$2 extra=${3:-}
  state "BLOCKED:$name" "사람 판단 필요${extra:+ ($extra)}" \
    "$artifact 의 BLOCKED_REASON·BLOCKED_NEEDS 를 사람에게 보고하고 결정을 받아라. 결정 전에는 재실행하지 마라 — 같은 곳에서 또 막힌다."
  log "  ⛔ $name BLOCKED${extra:+ — $extra}"
  sed -n '/^BLOCKED_REASON:/,$p' "$artifact" >&2
  printf '\n\033[1;33m→ 상담역(advisor.sh 또는 런처 세션)에게:\033[0m\n  "%s BLOCKED 났어. 원인 뭐야?"\n\n' "$name" >&2
  exit 3
}

# ─────────────────────────────────────────── 모델 교체 감시
# 안전 분류기가 걸리면 --model 로 지정한 모델이 아닌 다른 모델이 돈다.
# --fallback-model 로 막을 수 없으므로, 막는 대신 기록해서 눈에 띄게 한다.
# ※ 필드명은 버전마다 다를 수 있다. `jq 'keys' result.json` 으로 확인할 것.
#
# allow_gate=0 이면 기록만 한다 — 크래시 경로가 그렇다. 바로 뒤 부검 게이트가
# 같은 판단("이 산출물을 신뢰할까")을 묻는데 게이트를 두 번 띄울 이유가 없다.
check_model_swap() {   # check_model_swap <이름> <result.json> <요청모델> <allow_gate>
  local name=$1 out=$2 model=$3 allow_gate=$4 actual
  actual=$(jq -r '(.modelUsage // {} | keys | join(",")) // empty' "$out" 2>/dev/null || true)
  [ -z "$actual" ] && actual=$(jq -r '.model // empty' "$out" 2>/dev/null || true)

  if [ -n "$actual" ] && [[ "$actual" != *"$model"* ]]; then
    log "  ⚠ 모델 교체 감지: 요청=$model 실제=$actual"
    echo "- $(date -Iseconds) | $name | 요청 $model → 실제 $actual" >> "$MODEL_LOG"
    if [ "$allow_gate" = "1" ] && [ "$AUTO" != "1" ]; then
      gate_human "요청한 모델이 안 돌았다. 결과를 신뢰할지 판단해라" "$MODEL_LOG"
    fi
  elif [ -z "$actual" ]; then
    echo "- $(date -Iseconds) | $name | 실제 모델 확인 불가 (필드명 점검 필요)" >> "$MODEL_LOG"
  fi
}

# ─────────────────────────────────────────── 죽은 단계 부검
# claude 가 0 이 아닌 코드로 죽어도 스트림 마지막 result 이벤트에는 이유가 들어 있다
# (2026-08-24 실측: subtype=error_max_budget_usd / $5.08 / 34턴). 예전엔 exit code
# 숫자 하나만 보고 die 해서 그 파일을 손에 쥐고도 버렸다.
#
# 반환 0 = 사람이 산출물을 신뢰하기로 했다(호출자는 그대로 진행).
# 그 밖의 경로는 이 함수 안에서 끝난다 — die(2) 또는 승인 대기(4).
stage_postmortem() {
  local name=$1 out=$2 stream=$3 code=$4 artifact=$5 art_before=$6
  local reason

  if [ "$(jq -r 'type' "$out" 2>/dev/null)" = "object" ]; then
    local subtype errors turns cost term
    subtype="$(jq -r '.subtype // "?"' "$out")"
    errors="$(jq -r '(.errors // []) | join("; ")' "$out")"
    turns="$(jq -r '.num_turns // "?"' "$out")"
    cost="$(jq -r '.total_cost_usd // "?"' "$out")"
    term="$(jq -r '.terminal_reason // "?"' "$out")"
    reason="$subtype${errors:+ — $errors} (턴 $turns, \$$cost, terminal_reason=$term)"
  else
    # 침묵하지 않는다. "확인 불가"도 정보다 — 스트림이 중간에 끊겼다는 뜻이고,
    # 그건 예산·턴 초과와 다른 사건이다 (프로세스 강제 종료·디스크·파이프 파손).
    reason="사인 확인 불가 — 스트림에 result 이벤트가 없다 (프로세스가 중간에 끊김)"
  fi

  # 산출물이 **이번 주행 것인지**를 실행 전 지문과 비교해 판정한다.
  # 파일 존재만 보면 이전 주행이 남긴 것을 이번 것으로 오인한다 — 실측: 1차 정상 주행
  # 뒤 `FRESH_DESIGN=1` 로 설계를 버리라고 명시하고 2차가 산출물 없이 죽었는데,
  # 셸이 **1차의 DESIGN.md** 를 "온전해 보인다"며 되살리라고 사람에게 내밀었다.
  # 같은 구멍이 재시도 루프에도 있다 — 검증에 실패한 attempt N 의 IMPL.md 가
  # attempt N+1 의 사망으로 "이번 주행의 온전한 산출물"로 승격된다.
  local fresh=0 artifact_state="없음"
  if [ -f "$artifact" ]; then
    if [ "$(file_hash "$artifact")" != "$art_before" ]; then
      fresh=1; artifact_state="이번 주행이 씀"
    else
      artifact_state="있으나 이전 주행 것 (이번 주행은 건드리지 않음)"
    fi
  fi

  log "  ✖ $name: 프로세스 사망 (exit $code) — $reason"
  fail_log "$name 단계 프로세스 사망 (exit $code)" <<EOF
사인: $reason
스트림: $stream
산출물: $artifact ($artifact_state)
EOF

  local verdict=""
  if [ "$fresh" = "1" ]; then
    verdict="$(grep -m1 '^STATUS:' "$artifact" | awk '{print $2}' || true)"
  fi

  # 이번 주행이 BLOCKED 를 쓰고 죽었으면 "그냥 죽었다"가 아니라 막힌 이유를 넘긴다.
  if [ "$verdict" = "BLOCKED" ]; then
    emit_blocked "$name" "$artifact" "$reason"
  fi

  if [ "$verdict" != "DONE" ]; then
    die "$name: 프로세스 사망 (exit $code) — $reason → $stream"
  fi

  # 여기부터: 프로세스는 죽었는데 산출물은 온전해 보인다 (2026-08-24 judge 가 그랬다 —
  # JUDGE.md 는 완성본이었는데 셸이 버려서 $4.72 를 다시 냈다).
  #
  # 그래도 자동 통과는 안 된다. 에이전트가 파일을 쓴 **뒤** 더 검증하려다 죽었다면
  # 내용이 의도보다 덜 검증된 상태다 — 마지막 확인들이 파일에 반영되지 않았다.
  # 그래서 gate_human 의 force=1 이다: AUTO=1 에서도 반드시 사람이 본다.
  #
  # 게이트를 띄우기 **전에** 파킹한다. 제자리에 둔 채 게이트만 띄우면, 사람이 n 을
  # 누르든 tty 없이 exit 4 로 멈추든, 다음 실행의 재사용 로직이 이 미승인 산출물을
  # **게이트 없이** 되살린다. 가정이 아니다 — document-detail 의 JUDGE.md 가 죽은 뒤
  # DESIGN.md 보다 최신이라 재사용 조건을 그대로 통과하는 상태였다 (2026-08-24 실측).
  # 파킹본도 번호를 매긴다. 같은 단계가 두 번 연속 이 경로로 끝나면 1차 파킹본이
  # 사라지는데, 스트림·result 를 attempt 번호로 보존하면서 정작 가장 비싼 산출물만
  # 덮어쓰는 것은 앞뒤가 안 맞는다.
  local parked="$artifact.crashed"
  if [ -e "$parked" ]; then
    local m=2
    while [ -e "$artifact.crashed$m" ]; do m=$((m + 1)); done
    parked="$artifact.crashed$m"
  fi
  mv "$artifact" "$parked"
  log "  ⚠ 프로세스는 죽었으나 산출물은 STATUS: DONE — $parked 로 파킹"

  gate_human \
    "죽은 이유: $reason. 산출물이 온전해 보이는데 신뢰할까? (y = 제자리로 되돌리고 진행)" \
    "$parked" 1 \
    "검토 후 mv '$parked' '$artifact' 하고 재실행 — mv 라는 행위 자체가 승인이다 (design·judge 는 재사용 로직이 집고, impl·verify 는 단계가 다시 돈다). 되살리지 않으면 파킹된 채로 남는다"

  # 여기 도달 = 사람이 y 를 눌렀거나 유효한 승인 마커가 있었다.
  # n·tty없음은 gate_human 안에서 끝난다.
  mv "$parked" "$artifact"
  log "  ✔ 사람이 산출물을 신뢰하기로 했다 — 제자리로 되돌리고 진행"
  return 0
}

# ─────────────────────────────────────────── 단계 실행기
# run_stage <이름> <모델> <폴백체인> <프롬프트파일> <산출물경로>
#
# stream-json 으로 받아 진행 상황(도구 호출·중간 텍스트)을 실시간으로 흘리고,
# 스트림 마지막의 result 이벤트만 뽑아 게이트 판정에 쓴다.
# (--output-format json 은 완료까지 무출력이라 UX가 나빠서 교체함)
run_stage() {
  local name=$1 model=$2 fallback=$3 prompt_file=$4 artifact=$5
  local out="$WORK/$name.result.json" stream="$WORK/$name.stream.jsonl" code=0

  # 상한은 단계 이름으로 끌어온다: name=impl → TURNS_IMPL / BUDGET_IMPL
  # 인자로 더 받지 않는 이유 — 이미 5개다. 7개짜리 위치 인자는 호출부에서 순서를 틀린다.
  local upper turns_var budget_var turns budget
  upper="$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')"
  turns_var="TURNS_$upper"; budget_var="BUDGET_$upper"
  turns="${!turns_var:-40}"; budget="${!budget_var:-5}"

  # 재시도가 이전 시도의 증거를 덮어쓰지 않게 한다. 고정 이름은 유지하고(사람·테스트·
  # 도구가 그 경로를 안다) 덮어쓰기 직전에 이전 것을 번호로 밀어둔다.
  # 번호를 ATTEMPT 로 매기지 않는 이유: ATTEMPT 는 검증 루프 안에서만 올라가고
  # design·judge 는 루프 밖이라 늘 0 이다 — 두 경우가 다 남아야 한다.
  # (2026-08-24: impl 1차 실패의 증거가 2차 성공 주행에 덮여 사라졌다)
  if [ -f "$stream" ] || [ -f "$out" ]; then
    local n=1
    while [ -e "$WORK/$name.attempt$n.stream.jsonl" ] || [ -e "$WORK/$name.attempt$n.result.json" ]; do
      n=$((n + 1))
    done
    if [ -f "$stream" ]; then mv "$stream" "$WORK/$name.attempt$n.stream.jsonl"; fi
    if [ -f "$out" ];    then mv "$out"    "$WORK/$name.attempt$n.result.json"; fi
    log "  ↩ 이전 $name 증거 보관 → $name.attempt$n.*"
  fi

  # 산출물의 "실행 전 지문". 부검이 "이 파일이 이번 주행 것인가"를 이걸로 판정한다.
  # mtime 비교(-nt)를 안 쓰는 이유: macOS 기본 bash 3.2 는 mtime 을 **초 단위로만**
  # 비교해서, 같은 초 안에 끝난 단계의 산출물이 전부 이전 것으로 오판된다 (실측).
  local art_before="NONE"
  if [ -f "$artifact" ]; then art_before="$(file_hash "$artifact")"; fi

  state "RUNNING:$name" "model=$model, 턴≤$turns, 예산≤\$$budget"
  log "▶ $name (model=$model, fallback=$fallback, 턴≤$turns, 예산≤\$$budget)"

  set +e
  envsubst < "$prompt_file" | claude -p \
    --model "$model" \
    --fallback-model "$fallback" \
    --output-format stream-json \
    --verbose \
    --max-turns "$turns" \
    --max-budget-usd "$budget" \
    --permission-mode acceptEdits \
    --append-system-prompt "$(cat "$PROMPTS/_contract.md")" \
    | tee "$stream" \
    | jq --unbuffered -r '
        select(.type? == "assistant") | .message.content[]? |
        if .type == "tool_use" then
          "  ⚙ \(.name)  \((.input.file_path // .input.command // .input.pattern // .input.description // "") | tostring | .[0:90])"
        elif .type == "text" and ((.text // "") | length) > 0 then
          "  💬 \(.text | gsub("\\s+"; " ") | .[0:160])"
        else empty end' >&2
  code=${PIPESTATUS[1]}   # [0]=envsubst [1]=claude [2]=tee [3]=jq — 판정 기준은 claude
  set -e

  # 사인을 먼저 확보한다 — exit code 검사보다 **앞**이다. claude 가 0 이 아닌 코드로
  # 죽어도 스트림 마지막 result 이벤트에는 이유가 들어 있다. 예전엔 순서가 반대라
  # 그 파일을 손에 쥐고도 exit code 숫자 하나만 보고 버렸다 (진단 가능성이 나머지
  # 전부의 전제다 — 한도를 올리는 것도 초과가 로그에 남아야 안전해진다).
  #
  # 스트림 마지막의 result 이벤트 = 기존 --output-format json 이 주던 것과 같은 오브젝트
  jq -s '[.[] | select(.type? == "result")] | last' "$stream" > "$out" 2>/dev/null || true

  # 모델 교체 감시는 **죽은 경로에서도** 돈다. 다른 모델이 돌다 상한에 닿은 것이라면,
  # 사람이 "이 산출물을 신뢰할까"를 판단할 때 그 사실을 알아야 한다. 예전엔 크래시가
  # 이 블록을 통째로 건너뛰어 MODEL_LOG 에 그 단계 줄이 아예 안 남았다.
  if [ "$(jq -r 'type' "$out" 2>/dev/null)" = "object" ]; then
    if [ "$code" -eq 0 ]; then check_model_swap "$name" "$out" "$model" 1
    else                       check_model_swap "$name" "$out" "$model" 0
    fi
  fi

  if [ "$code" -ne 0 ]; then
    # 돌아왔다 = 사람이 산출물을 신뢰하기로 했다. 곧장 반환한다 — 죽은 주행의 result 는
    # is_error=true 라 아래 검사에 걸려서, 계속 내려가면 사람의 승인이 무효가 된다.
    stage_postmortem "$name" "$out" "$stream" "$code" "$artifact" "$art_before"
    return 0
  fi

  [ "$(jq -r 'type' "$out" 2>/dev/null)" = "object" ] \
    || die "$name: 스트림에 result 이벤트가 없음 → $stream 확인"

  [ "$(jq -r '.is_error' "$out")" = "false" ] \
    || die "$name: 에이전트 에러 — $(jq -r '.result' "$out" | head -c 300)"

  log "  \$$(jq -r '.total_cost_usd' "$out") / 턴 $(jq -r '.num_turns' "$out")"

  # 게이트 1: 산출물 물리적 존재
  [ -f "$artifact" ] || die "$name: 산출물 없음 → $artifact"

  # 게이트 2: 종료 형식
  local verdict
  verdict="$(grep -m1 '^STATUS:' "$artifact" | awk '{print $2}' || true)"
  case "${verdict:-MISSING}" in
    DONE)
      log "  ✔ $name DONE" ;;
    BLOCKED)
      emit_blocked "$name" "$artifact" ;;
    *)
      die "$name: STATUS 라인 없음 또는 형식 위반 (DONE|BLOCKED 필수)" ;;
  esac
}

# ─────────────────────────────────────────── 사람 게이트
# 상담역은 여기에 손댈 수 없다. 오직 사람만 누른다.
# gate_human <메시지> <검토파일> [force] [승인방법]
#
# 4번째 인자는 tty 없는 경로(exit 4)에서 "사람이 무엇을 해야 승인인가"를 바꾼다.
# 기본은 approve.sh 마커지만, 파킹된 산출물처럼 마커로 되살릴 수 없는 게이트도 있다.
#
# force=1 이면 AUTO=1 이어도 멈춘다. 검증되지 않은 주장을 무인으로 통과시키면
# 이 파이프라인이 막으려는 것(근거 없는 판단이 구현까지 흘러가는 것)이 그대로
# 일어난다 — 무인 모드는 "게이트를 없앤다"가 아니라 "판정 가능한 것만 자동으로
# 넘긴다"는 뜻이다.
# 파일 내용 해시 — 승인 마커가 "무엇을 승인했는가"를 내용 단위로 기억하는 키.
# approve.sh 의 file_hash 와 결과가 같아야 한다 (run-tests 가 교차 검증한다).
file_hash() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1"
  else sha256sum "$1"; fi | awk '{print $1}'
}

gate_human() {
  local msg=$1 file=$2 force=${3:-0} approve_how=${4:-}
  [ -n "$approve_how" ] \
    || approve_how="$ROOT/approve.sh $FEATURE $(basename "$file") 실행 (승인 후 재실행하면 마커로 통과 — 내용이 바뀌면 무효)"

  # 승인 마커: 사람이 approve.sh 로 "이 내용을 검토했다"를 남긴 것.
  # 해시로 내용에 묶여 있어 승인 후 파일이 바뀌면 무효가 된다.
  # AUTO 보다 먼저 본다 — 명시적 승인은 force 게이트까지 통과시키는 유일한
  # 무인 경로다 (AUTO 는 force 를 못 넘는다).
  local marker="$file.approved"
  if [ -f "$marker" ]; then
    if [ "$(cat "$marker")" = "$(file_hash "$file")" ]; then
      log "  ✔ 승인 마커 — 게이트 통과: $msg"
      return 0
    fi
    log "  ⚠ 승인 마커가 낡음 ($(basename "$file") 이 승인 뒤에 바뀜) — 재승인 필요"
  fi

  [ "$AUTO" = "1" ] && [ "$force" != "1" ] \
    && { log "  (AUTO=1 — 게이트 통과: $msg)"; return 0; }

  state "GATE" "$msg" "tty 게이트에서 사람 응답 대기 중 — 런처 개입 불필요."
  cat >&2 <<EOF

$(printf '\033[1;33m[게이트]\033[0m') $msg
  검토 대상: $file
  상담역에게: "$(basename "$file") 봐줘"

  y = 진행   e = 열어보기   n = 중단
EOF
  printf '  > ' >&2
  # tty 가 없으면(런처 모드·cron·CI) read 가 rc=1 로 끝난다. 예전에는 n 과 같이
  # 취급해 exit 2 로 죽였는데, 그러면 호출자가 "사람이 거부함"(2)과 "사람이 아직
  # 검토하지 않음"을 구분할 수 없다. 후자는 별도 코드(4)로 내보내고 승인 방법을
  # 찍어 준다 — 사람이 approve.sh 로 마커를 만들고 재실행하면 위의 마커 검사로
  # 통과한다. `|| ans=...` 가드가 없으면 set -e 가 read 실패 지점에서 exit 1 을
  # 내 어느 경로도 타지 못한다 (2026-08-18 실전에서 밟은 함정).
  local ans; read -r ans < /dev/tty || ans=__NO_TTY__
  case "$ans" in
    y|Y) return 0 ;;
    e|E) "${EDITOR:-less}" "$file"; gate_human "$msg" "$file" "$force" "$approve_how" ;;
    __NO_TTY__)
      state "AWAITING_APPROVAL" "$msg — $(basename "$file")" \
        "1) $file 을 사람에게 보여줘라. 2) 승인은 사람만 한다 — 사람이 직접 $approve_how. 런처가 대신 실행하거나 승인 파일을 직접 쓰는 것은 금지다. 3) 승인 뒤 같은 명령으로 재실행하면 이 게이트를 통과한다."
      {
        printf '\033[1;33m[승인 대기]\033[0m tty 가 없어 게이트에서 멈춘다 (exit 4)\n'
        printf '  검토 대상: %s\n' "$file"
        printf '  승인 방법: 검토한 사람이 터미널에서 직접 %s\n' "$approve_how"
        printf '  자세한 안내는 %s 의 "다음 행동" 블록에 있다\n' "$STATE"
      } >&2
      exit 4 ;;
    *)   die "사람이 중단함" ;;
  esac
}

# ─────────────────────────────────────────── 프리플라이트: 환경 기준선
# 에이전트를 **띄우기 전에** 환경을 판정한다. 여기서 죽으면 비용이 $0 이다.
#
# 이 단계가 있는 이유(DMS 실측): 이전 실행에서 phase:DONE 이 떴는데 타입 검사가
# 한 번도 안 돌았다. 셸은 테스트만 돌렸고, 에이전트들이 각자 시도한 타입 검사는
# 전부 권한 거부됐다. 타입 검사를 검증에 넣으려면 **그 실패가 코드 탓임을 먼저
# 보장**해야 한다 — 그게 기준선의 역할이다.
preflight() {
  # 사용자가 검증 명령을 명시했으면 기본 목록을 안 쓰므로 기준선도 의미가 없다.
  # 여기서 빌드를 돌리면 .env 없는 CI 에서 쓸데없이 사람 게이트가 뜬다.
  if [ -n "$TEST_CMD_OVERRIDE" ]; then
    TYPE_GATE=0
    LINT_GATE=0
    TYPE_GATE_REASON="TEST_CMD 오버라이드 — 검증 명령을 사용자가 직접 지정함"
    LINT_GATE_REASON="$TYPE_GATE_REASON"
    return 0
  fi

  # ── 치명적 전제: pytest ─────────────────────────────────
  # DMS 는 `npm test` 가 언제나 도는 것을 전제했다. 여기서 그 자리가 pytest 다.
  # pytest 가 없으면 셸이 **아무것도 판정할 수 없다** — 타입 게이트를 끄고 진행하는
  # 것과는 급이 다르다. 재시도 루프가 3회 전부 "pytest 없음"으로 죽는데, 그건
  # 에이전트가 고칠 수 있는 실패가 아니라 순수 낭비다 ($39 규모). 여기서 죽으면 $0.
  command -v "$PY" >/dev/null 2>&1 \
    || die "인터프리터 없음: $PY — PY=<경로> 로 지정하거나 PATH 를 고쳐라 (비용 \$0)"
  "$PY" -m pytest --version >/dev/null 2>&1 \
    || die "pytest 없음 ($PY) — 셸이 검증을 판정할 수 없다. \`$PY -m pip install -e '.[dev]'\` 후 다시 실행 (비용 \$0)"

  # ── 선택적 도구: mypy / ruff ────────────────────────────
  local missing=""
  [ -f "$ROOT/pyproject.toml" ] || missing="$missing pyproject.toml"
  "$PY" -m mypy --version >/dev/null 2>&1 || missing="$missing mypy"
  if "$PY" -m ruff --version >/dev/null 2>&1; then
    LINT_GATE=1; LINT_GATE_REASON=""
  else
    missing="$missing ruff"
  fi

  if [ -n "$missing" ]; then
    TYPE_GATE=0
    TYPE_GATE_REASON="누락:$missing"
    [ "$LINT_GATE" = "1" ] || LINT_GATE_REASON="누락:$missing"
    printf '\033[1;31m[orch]\033[0m ⚠ 타입 검사 OFF — 누락:%s\n' "$missing" >&2
    log "  타입 검사 없이 진행하면 타입 오류가 검증을 그대로 통과한다."
    log "  고치려면: $PY -m pip install -e '.[dev]'"
    # 승인 대상을 STATE.md 로 두면 안 된다. state() 가 불릴 때마다 updated:·pid: 를
    # 새로 찍어 내용이 바뀌므로, 해시로 내용에 묶인 승인 마커가 다음 실행의 첫
    # state "START" 에서 즉시 무효가 된다 — 런처 모드(tty 없음)에서 이 게이트를
    # **영원히 통과할 수 없다.** 2026-08-24 재현: 승인 후 재실행 3회 전부 exit 4.
    # `.env` 는 gitignore 대상이라 새 체크아웃·worktree 에서 바로 밟힌다.
    #
    # 대신 내용이 **환경에만** 의존하는 파일을 승인 대상으로 만든다. 누락 목록이
    # 같으면 해시도 같아 마커가 살아남고, 환경이 바뀌면 내용이 달라져 재승인을 요구한다.
    local pf="$WORK/PREFLIGHT.md"
    cat > "$pf" <<EOF
# 프리플라이트 판정 (셸이 자동 생성)

- 인터프리터: $PY
- 타입 게이트(mypy): 꺼짐
- 린트 게이트(ruff): $([ "$LINT_GATE" = "1" ] && echo 켜짐 || echo 꺼짐)
- 사유: 누락:$missing

타입 검사 없이 진행하면 타입 오류가 검증을 그대로 통과한다.
고치려면: $PY -m pip install -e '.[dev]'

이 파일을 승인하면 "타입 검사 없이 진행한다"를 승인한 것이다.
누락 목록이 달라지면 이 파일의 내용이 바뀌어 승인이 자동으로 무효가 된다.
EOF

    # AUTO=1 이면 gate_human 이 알아서 통과시킨다. 사람이 볼 때는 한 번 멈춘다 —
    # "빌드가 안 돌았다"를 모르고 DONE 을 받는 것이 이번에 실제로 일어난 사고다.
    gate_human "타입 검사 없이 진행한다 (누락:$missing). 알고 넘어가는 게 맞나?" "$pf"
    return 0
  fi

  log "▶ 프리플라이트: $PY -m mypy src (기준선)"
  if (cd "$ROOT" && "$PY" -m mypy src) > "$WORK/preflight_typecheck.txt" 2>&1; then
    TYPE_GATE=1
    TYPE_GATE_REASON=""
    log "  ✔ 기준선 녹색 — 이후 타입 실패는 에이전트가 만든 것이므로 재시도 대상이다"
  else
    tail -30 "$WORK/preflight_typecheck.txt" >&2
    # 원인을 환경으로 단정하지 않는다. 이 파이프라인은 더러운 워킹트리에서 시작하는
    # 것을 전제하므로(check_protected 주석 참조), 이미 있던 미완성 코드가 원인일 수도
    # 있다. 확실한 것 하나만 말한다 — **에이전트가 만든 것은 아니다**.
    die "프리플라이트 타입 검사 실패 — 에이전트는 아직 한 번도 안 띄웠으므로(비용 \$0) **에이전트가 만든 문제가 아니다**. 환경이거나 이미 워킹트리에 있던 코드다. 고친 뒤 다시 실행해라 → $WORK/preflight_typecheck.txt"
  fi
}

# 검증 목록을 만든다. 단일 문자열 대신 순서 있는 목록인 이유: 어느 명령이 실패했는지
# test_out.txt 를 읽지 않고도 알아야 FAIL_LOG 가 다음 시도에 쓸모가 있다.
#
# 순서 — 싼 것부터. pytest → ruff(린트) → mypy(타입).
# 린트를 타입 검사 앞에 둔 이유는 린트가 훨씬 싸기 때문이다.
build_verify_list() {
  if [ -n "$TEST_CMD_OVERRIDE" ]; then
    VERIFY_CMDS=("$TEST_CMD_OVERRIDE")
  else
    VERIFY_CMDS=("$PY -m pytest")
    [ "$LINT_GATE" = "1" ] && VERIFY_CMDS+=("$PY -m ruff check .")
    [ "$TYPE_GATE" = "1" ] && VERIFY_CMDS+=("$PY -m mypy src")
    # 기능별 스모크 훅. 있으면 마지막에 돈다 — 오케스트레이터는 기능 중립이어야
    # 하므로 라우트나 포트를 여기 하드코딩하지 않는다.
    [ -f "$WORK/smoke.sh" ] && VERIFY_CMDS+=("bash '$WORK/smoke.sh'")
  fi

  # 표시용(쉼표)과 실행 가능한 형태(&&)를 나눈다. prompts/verify.md 가 본문에서
  # $TEST_CMD 를 참조하는데, 쉼표로 이어붙인 문자열을 받은 에이전트가 그걸 복사해
  # 재현하려 하면 셸 에러가 난다.
  VERIFY_LIST_DESC="$(printf '%s, ' "${VERIFY_CMDS[@]}")"
  VERIFY_LIST_DESC="${VERIFY_LIST_DESC%, }"
  TEST_CMD="$(printf '%s && ' "${VERIFY_CMDS[@]}")"
  TEST_CMD="${TEST_CMD% && }"
}

# 목록을 순서대로 돌리고 첫 실패에서 멈춘다.
#
# fail-fast 를 고른 근거: 이 목록은 의존 순서가 있다. 타입이 깨졌으면 린트 결과는
# 대개 파생 잡음이고 빌드도 같은 이유로 죽는다. 재시도 루프에 넘길 정보는 "무엇을
# 먼저 고쳐야 하는가" 하나면 충분하고, 실패 3개를 한꺼번에 주면 FAIL_LOG 가 길어져
# 다음 구현 에이전트가 우선순위를 못 잡는다. 이미 실패한 뒤에 뒤 명령을 돌리는 것은
# 재시도 횟수만큼 곱해지는 순수 낭비이기도 하다.
run_verify() {
  local cmd rc=0
  VERIFY_PASSED=""
  VERIFY_FAILED=""
  : > "$WORK/test_out.txt"

  for cmd in "${VERIFY_CMDS[@]}"; do
    log "  ▸ $cmd"
    echo "### \$ $cmd" >> "$WORK/test_out.txt"
    if (cd "$ROOT" && eval "$cmd") >> "$WORK/test_out.txt" 2>&1; then
      echo "→ 통과" >> "$WORK/test_out.txt"
      echo >> "$WORK/test_out.txt"
      VERIFY_PASSED="$VERIFY_PASSED${VERIFY_PASSED:+, }$cmd"
    else
      rc=$?
      VERIFY_FAILED="$cmd"
      echo "→ 실패 (exit $rc)" >> "$WORK/test_out.txt"
      return 1
    fi
  done
  return 0
}

# ─────────────────────────────────────────── 파이프라인
ATTEMPT=0
state "START"
log "=== $FEATURE 시작 ==="
log "상태는 $STATE 에 실시간으로 쓴다 — 런처 세션은 이 파일만 읽으면 된다"
log "대화형 상담역이 필요하면 다른 터미널에서: ./advisor.sh $FEATURE"

preflight
build_verify_list
state "PREFLIGHT" "검증 명령: $VERIFY_LIST_DESC"
log "검증 목록: $VERIFY_LIST_DESC"

if [ "$FRESH_DESIGN" != "1" ] && [ -f "$WORK/DESIGN.md" ] \
   && [ "$(grep -m1 '^STATUS:' "$WORK/DESIGN.md" | awk '{print $2}')" = "DONE" ]; then
  log "↺ 기존 DESIGN.md 재사용 ($(date -r "$WORK/DESIGN.md" '+%m-%d %H:%M') 생성) — 새로 뽑으려면 FRESH_DESIGN=1"
  state "REUSED:design" "기존 산출물 재사용"
else
  run_stage design "$MODEL_DESIGN" "$FALLBACK_DESIGN" "$PROMPTS/design.md" "$WORK/DESIGN.md"
fi

# ─────────────────────────────────────────── 판단 검증
# 설계의 '주장'을 별 프로세스가 감사한다. 구현물에는 테스트·게이트가 있는데
# 판단물(원인 판정·우선순위·"X 가 없다")은 아무 검사 없이 구현으로 흘러갔다.
# DESIGN.md 보다 새로우면 재사용한다 — 설계가 새로 돌면 판정도 다시 받아야 한다.
if [ -f "$WORK/JUDGE.md" ] && [ "$WORK/JUDGE.md" -nt "$WORK/DESIGN.md" ] \
   && [ "$(grep -m1 '^STATUS:' "$WORK/JUDGE.md" | awk '{print $2}')" = "DONE" ]; then
  log "↺ 기존 JUDGE.md 재사용 (DESIGN.md 보다 최신)"
  state "REUSED:judge" "기존 산출물 재사용"
else
  run_stage judge "$MODEL_JUDGE" "$FALLBACK_JUDGE" "$PROMPTS/judge.md" "$WORK/JUDGE.md"
fi

# ★ 판정권은 셸에 있다. 에이전트가 쓴 '판정' 문장을 읽지 않고, 자기가 신고한
#   카운트 한 줄만 파싱한다. 형식이 없으면 그것도 게이트 위반이다.
JUDGE_COUNTS="$(grep -m1 -E '^UNVERIFIED: *[0-9]+ +REFUTED: *[0-9]+' "$WORK/JUDGE.md" || true)"
if [ -z "$JUDGE_COUNTS" ]; then
  die "JUDGE.md 에 'UNVERIFIED: <n> REFUTED: <n>' 라인이 없다 → $WORK/JUDGE.md"
fi
UNVERIFIED="$(sed -E 's/^UNVERIFIED: *([0-9]+).*/\1/' <<<"$JUDGE_COUNTS")"
REFUTED="$(sed -E 's/.*REFUTED: *([0-9]+).*/\1/' <<<"$JUDGE_COUNTS")"
log "판단 검증: 미확인 $UNVERIFIED / 반박 $REFUTED"

if [ "$UNVERIFIED" -gt 0 ] || [ "$REFUTED" -gt 0 ]; then
  state "JUDGE_FLAGGED" "미확인 $UNVERIFIED / 반박 $REFUTED" \
    "$WORK/JUDGE.md 의 반박·미확인 항목을 사람에게 보여주고 판단을 받아라. 승인 없이 구현으로 넘기지 마라."
  gate_human \
    "설계의 주장 중 반박 $REFUTED 건·미확인 $UNVERIFIED 건 — 이대로 구현하면 그 위에 코드가 쌓인다" \
    "$WORK/JUDGE.md" 1
fi

gate_human "설계 검토 — 여기서 틀리면 뒤가 전부 낭비다" "$WORK/DESIGN.md"

# ─────────────────────────────────────────── 게이트 5: 보호 파일 (프로젝트 전용)
# CLAUDE.md 가 "손대면 안 되는 것"으로 못박은 파일들이다. 프롬프트에도 적혀 있지만
# 프롬프트는 게이트가 아니다 — 에이전트가 안 지켰을 때 막는 것이 없다.
#
# git diff 대신 지문 비교를 쓰는 이유: 파이프라인 시작 시점에 이미 더러운 워킹
# 트리(예: 이 파일들 자체를 커밋 안 한 상태)에서 돌리면 diff 기반 검사가 첫 시도부터
# 헛발질한다. 검사할 것은 "지금 더러운가"가 아니라 "이번 실행이 바꿨는가"다.
#
# requirements*.txt 가 포함된 이유: 에이전트가 pyproject.toml 을 안 건드리고
# pip install <pkg> 만 돌려도 의존성은 늘어난다.
#
# 존재하지 않는 설정 파일도 목록에 둔다. protected_fingerprint 는 없는 파일을
# "(없음)" 으로 찍으므로, 에이전트가 ruff.toml·mypy.ini 를 **새로 만들어** 게이트를
# 느슨하게 하는 경로도 지문 변화로 잡힌다.
PROTECTED="pyproject.toml requirements.txt requirements-dev.txt setup.cfg pytest.ini mypy.ini ruff.toml .ruff.toml conftest.py tests/conftest.py .gitignore AGENTS.md .env.example"

sha() { command -v shasum >/dev/null && shasum -a 256 "$1" || sha256sum "$1"; }

protected_fingerprint() {
  local f
  for f in $PROTECTED; do
    if [ -f "$ROOT/$f" ]; then
      printf '%s %s\n' "$f" "$(sha "$ROOT/$f" | awk '{print $1}')"
    else
      printf '%s (없음)\n' "$f"
    fi
  done
}

PROTECTED_BASELINE="$(protected_fingerprint)"

# check_protected <단계이름>
# 구현 직후·검증 직후에 각각 부른다. 늦게 볼수록 그 위에 코드와 테스트가 쌓여
# 되돌리는 비용이 올라간다.
check_protected() {
  local stage=$1 changed
  changed="$(diff <(printf '%s\n' "$PROTECTED_BASELINE") <(protected_fingerprint) \
             | grep '^[<>]' | awk '{print $2}' | sort -u | tr '\n' ' ' || true)"
  [ -z "$changed" ] \
    || die "$stage 단계가 보호 파일을 수정함: $changed — git checkout 으로 되돌린 뒤 설계부터 다시 볼 것"
}

while :; do
  ATTEMPT=$((ATTEMPT + 1))
  log "── 시도 $ATTEMPT/$((MAX_RETRY + 1))"

  run_stage impl   "$MODEL_IMPL"   "$FALLBACK_IMPL"   "$PROMPTS/impl.md"   "$WORK/IMPL.md"

  # 구현 직후에 검사한다. 검증 단계까지 흘려보내면 그 위에 테스트가 쌓여서
  # 되돌리는 비용이 올라간다.
  check_protected impl

  run_stage verify "$MODEL_VERIFY" "$FALLBACK_VERIFY" "$PROMPTS/verify.md" "$WORK/VERIFY.md"

  # 검증 단계도 같은 검사를 받는다. 이 단계는 tests/test_*.py 만 쓸 수 있는데,
  # 통과시키려고 pyproject.toml 의 [tool.pytest.ini_options] 나 ruff/mypy 설정을
  # 손대는 것이 가장 값싼 부정행위 경로다.
  check_protected verify

  # ★ 최종 판정은 셸이 한다. 에이전트에게 안 맡긴다.
  state "TESTING" "$VERIFY_LIST_DESC"
  if run_verify; then
    VERIFY_LAST="통과: $VERIFY_PASSED"
    log "✅ 검증 통과 ($VERIFY_PASSED)"
    break
  fi

  VERIFY_LAST="실패: $VERIFY_FAILED (그 앞까지 통과: ${VERIFY_PASSED:-없음})"
  log "❌ 검증 실패 — $VERIFY_FAILED"
  tail -30 "$WORK/test_out.txt" >&2
  state "TEST_FAILED" "attempt $ATTEMPT — $VERIFY_FAILED 실패" \
    "$FAIL_LOG 의 마지막 항목을 읽고 무엇이 실패했는지 사람에게 보고해라. 재시도 여부는 아래 게이트에서 사람이 정한다."

  # 기록이 die 보다 먼저다. 예전엔 순서가 반대라 **마지막 시도의 실패가 FAIL_LOG 에
  # 영영 안 남았다** — 정작 가장 알고 싶은 실패가 그것이고, 아래 die 메시지가
  # 가리키는 파일도 이것이다.
  #
  # 첫 줄에 어느 명령이 실패했는지 둔다. 다음 구현 시도가 이걸 읽는데, 출력만 있고
  # 명령 이름이 없으면 무엇을 고쳐야 하는지 추측하게 된다.
  {
    echo "실패한 명령: \`$VERIFY_FAILED\`"
    echo "그 앞까지 통과: ${VERIFY_PASSED:-없음}"
    echo '```'
    tail -60 "$WORK/test_out.txt"
    echo '```'
  } | fail_log "attempt $ATTEMPT"

  [ "$ATTEMPT" -gt "$MAX_RETRY" ] \
    && die "검증 ${MAX_RETRY}회 재시도 후에도 실패 (마지막: $VERIFY_FAILED) → $FAIL_LOG"

  gate_human "재시도 $((ATTEMPT + 1)) 진행? (상담역에게 FAIL_LOG 물어봐도 됨)" "$FAIL_LOG"
done

state "DONE" "통과: $VERIFY_PASSED" \
  "완주다. 산출물($WORK/{DESIGN,JUDGE,IMPL,VERIFY}.md)과 위 '검증 게이트' 블록이 말하는 통과 범위를 사람에게 보고해라."
log "=== $FEATURE 완료 ==="
log "검증 통과: $VERIFY_PASSED"
# 오버라이드는 빌드를 포함할 수도 있어서 셸이 판단할 수 없다. 단정하지 않는다.
if [ "$TYPE_GATE" != "1" ] && [ -z "$TEST_CMD_OVERRIDE" ]; then
  log "⚠ 타입 검사는 돌지 않았다 ($TYPE_GATE_REASON)"
fi
log "산출물: $WORK/{DESIGN,JUDGE,IMPL,VERIFY}.md"
