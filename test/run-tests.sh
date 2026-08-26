#!/usr/bin/env bash
# orchestrate.sh 게이트 검증 스위트
#
# 검증하는 것: 게이트가 "통과시키는가"가 아니라 "막는가"
# API 호출 0회. fake-claude 를 PATH 앞에 끼워넣는다.
#
# 사용법: ./test/run-tests.sh

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(dirname "$HERE")"

PASS=0; FAIL=0

# tty 없는 실행(런처·cron·CI)을 흉내내려면 controlling terminal 을 떼야 한다.
# macOS 에 setsid 가 없어 python3 의 os.setsid 를 쓰는데, **Windows 의 python3 에는
# 그 함수 자체가 없다.** command -v 만 보면 "있다"고 판단해서 해당 케이스가 통째로
# 거짓 실패한다 — 호출 가능한지까지 확인한다.
have_detach() {
  command -v python3 >/dev/null 2>&1 && python3 -c 'import os; os.setsid' >/dev/null 2>&1
}
green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }

# sandbox_python <pytest> <ruff> <mypy> — 각 모듈을 돌렸을 때 실행할 명령을 지정한다
# (true = 통과, false = 실패). orchestrate.sh 가 $PY 로 부르는 python 을 가로챈다.
#
# npm 스크립트(package.json) 대신 셤을 쓰는 이유: 파이썬에는 "스크립트 이름 → 명령"
# 매핑을 담는 파일이 없다. 검증 명령이 `python -m <모듈>` 형태라 python 자체를
# 가로채는 것이 그 자리에 해당한다.
#
# `--version` 은 "설치돼 있는가"를 묻는 프리플라이트 질문이라 실행 결과와 분리한다.
# 그래야 "도구가 없다"와 "도구가 실패했다"를 따로 흉내낼 수 있다
# (FAKE_PYTEST_MISSING / FAKE_RUFF_MISSING / FAKE_MYPY_MISSING).
sandbox_python() {
  cat > test/python <<'SHIM'
#!/usr/bin/env bash
mod=""
[ "${1:-}" = "-m" ] && { mod="${2:-}"; shift 2; }
if [ "${1:-}" = "--version" ]; then
  mv="FAKE_$(printf '%s' "$mod" | tr 'a-z' 'A-Z')_MISSING"
  [ -n "${!mv:-}" ] && exit 1
  printf '%s (fake)\n' "$mod"; exit 0
fi
case "$mod" in
  pytest) __PYTEST__ ;;
  ruff)   __RUFF__ ;;
  mypy)   __MYPY__ ;;
  *)      exit 0 ;;
esac
SHIM
  sed -i "s|__PYTEST__|$1|; s|__RUFF__|$2|; s|__MYPY__|$3|" test/python
  chmod +x test/python
}

# ── 매 테스트마다 깨끗한 샌드박스 repo 를 만든다
setup() {
  SANDBOX="$(mktemp -d)"
  cd "$SANDBOX"
  git init -q .
  git config user.email t@t; git config user.name t
  mkdir -p prompts test
  cp "$SRC/orchestrate.sh" "$SRC/approve.sh" .
  cp "$SRC/prompts/"*.md prompts/
  cp "$HERE/fake-claude" test/claude       # ← 이름이 'claude' 여야 가로챈다
  chmod +x orchestrate.sh approve.sh test/claude
  # 보호 파일 게이트 검증용. 없으면 지문 비교가 '수정'이 아니라 '생성'을 보게 된다.
  # 프리플라이트도 이 파일의 존재를 본다 — 없으면 타입 게이트가 꺼진다.
  printf '[project]\nname = "sandbox"\nversion = "0.0.0"\n' > pyproject.toml
  mkdir -p src
  # 기본 검증 목록(pytest → ruff → mypy) 경로를 테스트하려면 셤이 있어야 한다.
  # 개별 케이스에서 sandbox_python 으로 덮어써 실패를 흉내낸다.
  sandbox_python true true true
  echo x > x.txt; git add -A; git commit -qm init
  export PATH="$SANDBOX/test:$PATH"
}

teardown() { cd /; rm -rf "$SANDBOX"; }

# expect <설명> <기대exit코드> -- <env할당들...>
expect() {
  local desc=$1 want=$2; shift 3
  setup
  local got=0
  env "$@" AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq "$want" ]; then
    green "  PASS  $desc (exit $got)"; PASS=$((PASS+1))
  else
    red   "  FAIL  $desc — 기대 exit $want, 실제 $got"; FAIL=$((FAIL+1))
  fi
  teardown
}

echo "=== 정상 경로 ==="
expect "전부 정상이면 0으로 끝난다" 0 -- FAKE_SCENARIO=ok

echo
echo "=== 게이트가 막아야 하는 것들 ==="
expect "STATUS 라인 없으면 죽는다 (설계)"        2 -- FAKE_SCENARIO_DESIGN=no_status
expect "STATUS 라인 없으면 죽는다 (구현)"        2 -- FAKE_SCENARIO_IMPL=no_status
expect "STATUS 라인 없으면 죽는다 (검증)"        2 -- FAKE_SCENARIO_VERIFY=no_status
expect "산출물 파일 없으면 죽는다"               2 -- FAKE_SCENARIO_DESIGN=no_file
expect "에이전트 에러면 죽는다"                  2 -- FAKE_SCENARIO_DESIGN=agent_error
expect "프로세스가 죽으면 죽는다"                2 -- FAKE_SCENARIO_DESIGN=crash
expect "BLOCKED 는 exit 3 (사람 호출)"           3 -- FAKE_SCENARIO_DESIGN=blocked
expect "구현 BLOCKED 도 exit 3"                  3 -- FAKE_SCENARIO_IMPL=blocked
expect "STATUS 라인 없으면 죽는다 (판단검증)"    2 -- FAKE_SCENARIO_JUDGE=no_status
expect "판단검증 카운트 라인 없으면 죽는다"      2 -- FAKE_SCENARIO_JUDGE=judge_nocount
expect "판단검증 BLOCKED 도 exit 3"              3 -- FAKE_SCENARIO_JUDGE=blocked

echo
echo "=== 판단 검증 게이트 ==="
# 이 게이트의 존재 이유: 반박·미확인이 있는 설계가 AUTO=1 로 조용히 구현까지
# 흘러가면 안 된다. 즉 검사할 것은 "통과하는가"가 아니라 "무인이어도 멈추는가".
#
# 게이트는 /dev/tty 에서 읽으므로, 터미널이 있으면 대기하고 없으면 즉시 중단한다.
# 둘 중 어느 쪽이 되든 불변식은 하나다 — **IMPL.md 가 만들어지지 않는다.**
# 그래서 phase 가 아니라 그걸 단정한다 (tty 유무에 따라 결과가 갈리지 않게).
setup
env FAKE_SCENARIO_JUDGE=judge_flagged AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1 &
pid=$!
for _ in $(seq 1 60); do
  [ -f .pipeline/feat/JUDGE.md ] && ! kill -0 $pid 2>/dev/null && break
  grep -q 'phase: GATE' .pipeline/feat/STATE.md 2>/dev/null && break
  sleep 0.1
done
kill $pid 2>/dev/null; wait $pid 2>/dev/null
if grep -q '^UNVERIFIED: 2 REFUTED: 1' .pipeline/feat/JUDGE.md 2>/dev/null \
   && [ ! -f .pipeline/feat/IMPL.md ]; then
  green "  PASS  반박이 있으면 AUTO=1 이어도 구현으로 넘어가지 않는다"; PASS=$((PASS+1))
else
  red   "  FAIL  판단 검증 게이트가 무인 모드를 막지 못함 (IMPL.md 생성됨)"; FAIL=$((FAIL+1))
fi
teardown

# tty 없는 환경(런처 모드·cron·CI)에서 게이트가 **의도한 경로로** 멈추는지.
# 마커 없이 게이트에 걸리면 "사람이 아직 검토하지 않음" = exit 4 (승인 대기).
# "사람이 거부함"(exit 2)과 구분되어야 런처가 산출물을 보여주고 재실행할 수 있다.
# set -e 아래에서 read 실패가 exit 1 로 새는 함정은 실전 1회에서 겪었다 (2026-08-18).
#
# macOS 에 setsid 가 없어서 python3 로 세션을 떼어 controlling terminal 을 없앤다.
# 테스트 전용 의존성이고, 없으면 케이스를 건너뛴다 (조용히 통과시키지 않는다).
if have_detach; then
  setup
  got=0
  python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
    env FAKE_SCENARIO_JUDGE=judge_flagged AUTO=1 TEST_CMD=true \
    ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq 4 ] && [ ! -f .pipeline/feat/IMPL.md ] \
     && grep -q 'phase: AWAITING_APPROVAL' .pipeline/feat/STATE.md 2>/dev/null; then
    green "  PASS  tty 없는 게이트는 exit 4 승인 대기로 멈춘다"; PASS=$((PASS+1))
  else
    red   "  FAIL  tty 부재 시 종료 경로 — exit=$got (기대 4), phase=$(grep -m1 'phase:' .pipeline/feat/STATE.md 2>/dev/null)"
    FAIL=$((FAIL+1))
  fi
  teardown
else
  red "  SKIP  tty 부재 케이스 — setsid 사용 불가 (setsid 대체 불가)"
fi

echo
echo "=== 승인 마커 (런처 모드) ==="
# 런처 모드의 계약 세 가지를 검사한다:
#   1) 사람이 남긴 마커는 tty 없는 게이트를 통과시킨다
#   2) 승인 후 내용이 바뀐 마커(낡은 마커)는 통과시키지 않는다
#   3) approve.sh 자체가 tty 없이는 마커를 만들지 못한다 (런처 대리 승인 차단)
# 1의 마커는 approve.sh --hash 로 만든다 — orchestrate.sh 의 file_hash 와
# 구현이 어긋나면 이 케이스가 잡는다 (교차 검증).
if have_detach; then
  detach() { python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@"; }

  setup
  mkdir -p .pipeline/feat
  printf 'STATUS: DONE\n\n(검토된 설계)\n\nALLOWED_FILES:\n- x.txt\n\n' > .pipeline/feat/DESIGN.md
  sleep 1
  printf 'STATUS: DONE\nUNVERIFIED: 0 REFUTED: 0\n' > .pipeline/feat/JUDGE.md
  ./approve.sh --hash .pipeline/feat/DESIGN.md > .pipeline/feat/DESIGN.md.approved
  got=0
  detach env FAKE_SCENARIO=ok AUTO=0 TEST_CMD=true ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq 0 ] && [ -f .pipeline/feat/IMPL.md ]; then
    green "  PASS  유효한 승인 마커는 tty 없는 게이트를 통과시킨다"; PASS=$((PASS+1))
  else
    red   "  FAIL  마커 통과 실패 — exit=$got (기대 0)"; FAIL=$((FAIL+1))
  fi
  teardown

  setup
  mkdir -p .pipeline/feat
  printf 'STATUS: DONE\n\n(검토된 설계)\n\nALLOWED_FILES:\n- x.txt\n\n' > .pipeline/feat/DESIGN.md
  sleep 1
  printf 'STATUS: DONE\nUNVERIFIED: 0 REFUTED: 0\n' > .pipeline/feat/JUDGE.md
  echo "stale-hash-of-previously-approved-content" > .pipeline/feat/DESIGN.md.approved
  got=0
  detach env FAKE_SCENARIO=ok AUTO=0 TEST_CMD=true ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq 4 ] && [ ! -f .pipeline/feat/IMPL.md ]; then
    green "  PASS  낡은 마커는 통과시키지 않는다 (재승인 요구)"; PASS=$((PASS+1))
  else
    red   "  FAIL  낡은 마커 — exit=$got (기대 4)$([ -f .pipeline/feat/IMPL.md ] && echo ', IMPL.md 생성됨')"; FAIL=$((FAIL+1))
  fi
  teardown

  setup
  mkdir -p .pipeline/feat
  printf 'STATUS: DONE\n' > .pipeline/feat/DESIGN.md
  got=0
  detach ./approve.sh feat DESIGN.md >/dev/null 2>&1 || got=$?
  if [ "$got" -ne 0 ] && [ ! -f .pipeline/feat/DESIGN.md.approved ]; then
    green "  PASS  approve.sh 는 tty 없이 마커를 만들지 않는다"; PASS=$((PASS+1))
  else
    red   "  FAIL  tty 없는 approve — exit=$got$([ -f .pipeline/feat/DESIGN.md.approved ] && echo ', 마커 생성됨')"; FAIL=$((FAIL+1))
  fi
  teardown
else
  red "  SKIP  승인 마커 케이스 — setsid 사용 불가 (setsid 대체 불가)"
fi
# 대조군 — 게이트가 '항상 막는' 게 아니라는 것. 이게 없으면 위 PASS 는 무의미하다.
setup
got=0
env FAKE_SCENARIO_JUDGE=judge_clean AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ] && [ -f .pipeline/feat/IMPL.md ]; then
  green "  PASS  미확인·반박 0 이면 그대로 진행한다 (대조군)"; PASS=$((PASS+1))
else
  red   "  FAIL  깨끗한 판정인데 진행이 막혔다 — exit=$got"; FAIL=$((FAIL+1))
fi
teardown

# 설계가 새로 돌면 판정도 다시 받아야 한다 (JUDGE.md 가 DESIGN.md 보다 오래됐으면 재실행)
setup
mkdir -p .pipeline/feat
printf 'STATUS: DONE\nUNVERIFIED: 0 REFUTED: 0\n\n(지난 판정)\n' > .pipeline/feat/JUDGE.md
sleep 1
printf 'STATUS: DONE\n\n(새 설계)\n' > .pipeline/feat/DESIGN.md
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if [ -f .pipeline/feat/judge.result.json ]; then
  green "  PASS  설계가 판정보다 새로우면 판단 검증을 다시 돌린다"; PASS=$((PASS+1))
else
  red   "  FAIL  낡은 JUDGE.md 를 그대로 재사용했다"; FAIL=$((FAIL+1))
fi
teardown

setup
mkdir -p .pipeline/feat
printf 'STATUS: DONE\n\n(사람이 이미 검토한 설계)\n' > .pipeline/feat/DESIGN.md
sleep 1
printf 'STATUS: DONE\nUNVERIFIED: 0 REFUTED: 0\n\n(이미 받은 판정)\n' > .pipeline/feat/JUDGE.md
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if [ ! -f .pipeline/feat/judge.result.json ] \
   && grep -q '이미 받은 판정' .pipeline/feat/JUDGE.md; then
  green "  PASS  판정이 설계보다 새로우면 재사용한다"; PASS=$((PASS+1))
else
  red   "  FAIL  판단 검증 재사용 실패"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 프롬프트 치환 ==="
# orchestrate.sh 가 export 하지 않은 변수를 프롬프트가 참조하면 envsubst 가 빈
# 문자열로 치환한다. 에이전트는 "셸이 `` 를 실행한다" 같은 깨진 문장을 받는데,
# 파이프라인은 정상 동작하므로 아무도 모른다. 실제로 $TEST_CMD 가 그랬다.
setup
rendered_ok=1
for f in prompts/design.md prompts/judge.md prompts/impl.md prompts/verify.md; do
  out=$(FEATURE=feat WORK=/w ROOT=/r TEST_CMD="python -m pytest" PY=python envsubst < "$f")
  # 빈 백틱 = 치환됐는데 값이 없었다는 뜻
  # 정확히 백틱 2개(앞뒤가 백틱이 아닌) = 빈 인라인 코드. ``` 펜스는 제외된다.
  printf '%s' "$out" | grep -qE '(^|[^`])``([^`]|$)' && { rendered_ok=0; echo "         빈 치환: $f"; }
  # 살아남은 $VAR = export 목록에 없는 변수
  printf '%s' "$out" | grep -qE '\$[A-Z_][A-Z_0-9]*' && { rendered_ok=0; echo "         미치환: $f"; }
done
if [ "$rendered_ok" -eq 1 ]; then
  green "  PASS  모든 프롬프트가 빈 치환 없이 렌더된다"; PASS=$((PASS+1))
else
  red   "  FAIL  프롬프트 치환이 깨짐 (orchestrate.sh 의 export 목록 확인)"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 보호 파일 게이트 (프로젝트 전용) ==="
# 프롬프트가 못박은 파일(pyproject.toml 등)을 구현 단계가 건드리면 검증으로
# 넘어가기 전에 죽어야 한다. 확인할 것은 "죽는가"와 "VERIFY 로 안 넘어갔는가" 둘 다다.
setup
got=0
env FAKE_SCENARIO_IMPL=impl_protected AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/VERIFY.md ]; then
  green "  PASS  구현이 pyproject.toml 을 건드리면 검증 전에 죽는다"; PASS=$((PASS+1))
else
  red   "  FAIL  보호 파일 게이트 — exit=$got (기대 2), VERIFY.md=$([ -f .pipeline/feat/VERIFY.md ] && echo 생성됨 || echo 없음)"
  FAIL=$((FAIL+1))
fi
teardown

# 대조군 — 보호 파일을 안 건드리면 통과해야 한다. 이게 없으면 위 PASS 는
# "게이트가 항상 막는다"와 구분되지 않는다.
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ] && [ -f .pipeline/feat/VERIFY.md ]; then
  green "  PASS  보호 파일을 안 건드리면 게이트가 안 뜬다 (대조군)"; PASS=$((PASS+1))
else
  red   "  FAIL  보호 파일 게이트가 정상 경로를 막았다 — exit=$got"; FAIL=$((FAIL+1))
fi
teardown

echo
setup
got=0
env FAKE_SCENARIO_VERIFY=verify_protected AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ]; then
  green "  PASS  검증이 pytest 설정을 고치면 테스트 실행 전에 죽는다"; PASS=$((PASS+1))
else
  red   "  FAIL  검증 단계 보호 파일 게이트 — exit=$got (기대 2)"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 프리플라이트 / 타입 게이트 ==="
# 배경(DMS 실측): 이전 실행에서 phase:DONE 이 떴는데 타입 검사가 한 번도 안 돌았다.
# 셸은 테스트만 돌렸고 아무도 그 사실을 몰랐다. 아래 넷이 그 재발을 막는다.

# ① 도구가 갖춰져 있으면 기준선 mypy 가 돌고 타입 검사가 검증 목록에 들어간다
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ] \
   && grep -q '타입 게이트(mypy): 켜짐' .pipeline/feat/STATE.md 2>/dev/null \
   && grep -q 'python -m mypy src' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  도구가 갖춰지면 타입 검사가 검증 목록에 포함된다"; PASS=$((PASS+1))
else
  red   "  FAIL  타입 게이트가 안 켜짐 — exit=$got"
  sed -n '/검증 게이트/,/산출물/p' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'
  FAIL=$((FAIL+1))
fi
teardown

# ② 기준선 타입 검사가 실패하면 에이전트를 한 번도 안 띄우고 죽는다.
#    이게 이 단계의 존재 이유다 — 환경 문제로 $5짜리 사이클을 3회 태우지 않는다.
setup
sandbox_python true true false
got=0
env FAKE_SCENARIO=ok AUTO=1 ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/design.result.json ] \
   && [ ! -f .pipeline/feat/DESIGN.md ]; then
  green "  PASS  기준선 타입 검사가 깨지면 에이전트를 띄우기 전에 죽는다 (비용 0)"; PASS=$((PASS+1))
else
  red   "  FAIL  프리플라이트가 에이전트를 막지 못함 — exit=$got, DESIGN.md=$([ -f .pipeline/feat/DESIGN.md ] && echo 생성됨 || echo 없음)"
  FAIL=$((FAIL+1))
fi
teardown

# ③-a 런처 모드에서 이 게이트를 **통과할 수 있어야** 한다.
#     승인 대상이 STATE.md 였을 때는 state() 가 매 호출마다 updated:·pid: 를 새로 찍어
#     마커가 즉시 낡았고, 승인해도 재실행이 계속 exit 4 였다 (2026-08-24 재현 확정).
#     도구가 안 깔린 새 체크아웃에서 바로 밟히는 경로다.
if have_detach; then
  detach_pf() { python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@"; }
  setup; rm -f pyproject.toml   # 프리플라이트가 타입 게이트를 끄고 승인 게이트를 띄운다
  # 단언은 "exit 0" 이 아니라 "**프리플라이트를 지나갔는가**" 여야 한다. 그 뒤에도
  # 설계 검토 게이트가 있어서 AUTO=0 런은 어차피 거기서 다시 멈춘다 — exit 코드로
  # 단언하면 어느 게이트에서 멈췄는지 구분하지 못하고 통과·실패가 뒤바뀐다.
  # 수정 전 코드로 대조 확인함: 승인 후에도 같은 프리플라이트 게이트에 멈추고
  # DESIGN.md 가 끝내 안 생겼다 (2026-08-24).
  got=0
  detach_pf env FAKE_SCENARIO=ok AUTO=0 ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  first=$got
  first_stopped_at_preflight=1
  grep -q '타입 검사 없이 진행' .pipeline/feat/STATE.md 2>/dev/null || first_stopped_at_preflight=0
  [ -f .pipeline/feat/DESIGN.md ] && first_stopped_at_preflight=0
  ./approve.sh --hash .pipeline/feat/PREFLIGHT.md > .pipeline/feat/PREFLIGHT.md.approved 2>/dev/null
  detach_pf env FAKE_SCENARIO=ok AUTO=0 ./orchestrate.sh feat >/dev/null 2>&1 || true
  if [ "$first" -eq 4 ] && [ "$first_stopped_at_preflight" -eq 1 ] \
     && [ -f .pipeline/feat/DESIGN.md ] \
     && ! grep -q '타입 검사 없이 진행' .pipeline/feat/STATE.md 2>/dev/null; then
    green "  PASS  프리플라이트 게이트는 승인 마커로 통과한다 (런처 모드)"; PASS=$((PASS+1))
  else
    red   "  FAIL  프리플라이트 승인이 안 먹음 — 1차=$first (기대 4, 프리플라이트에서 멈춤=$first_stopped_at_preflight), 2차 DESIGN.md=$([ -f .pipeline/feat/DESIGN.md ] && echo 생성 || echo 없음)"
    FAIL=$((FAIL+1))
  fi
  teardown
else
  red "  SKIP  프리플라이트 승인 케이스 — setsid 사용 불가"
fi

# ③ 도구가 없으면 타입 검사를 끄되 그 사실이 STATE.md 에 남는다 (조용히 넘어가지 않는다)
setup; rm -f pyproject.toml
got=0
env FAKE_SCENARIO=ok AUTO=1 ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 0 ] \
   && grep -q '타입 게이트(mypy): 꺼짐 — 누락:' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  도구가 없으면 타입 게이트가 꺼지고 사유가 STATE.md 에 남는다"; PASS=$((PASS+1))
else
  red   "  FAIL  타입 게이트 OFF 사유가 기록되지 않음 — exit=$got"; FAIL=$((FAIL+1))
fi
teardown


# ④ pytest 자체가 없으면 타입 게이트를 끄고 진행하는 게 아니라 **죽는다**.
#    DMS 의 `npm test` 자리라 이게 없으면 셸이 아무것도 판정할 수 없다 —
#    재시도 3회가 전부 같은 이유로 죽고, 에이전트가 고칠 수 있는 실패도 아니다.
setup
got=0
env FAKE_PYTEST_MISSING=1 FAKE_SCENARIO=ok AUTO=1 ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && [ ! -f .pipeline/feat/DESIGN.md ]; then
  green "  PASS  pytest 가 없으면 에이전트를 띄우기 전에 죽는다 (비용 0)"; PASS=$((PASS+1))
else
  red   "  FAIL  pytest 부재를 프리플라이트가 못 막음 — exit=$got, DESIGN.md=$([ -f .pipeline/feat/DESIGN.md ] && echo 생성됨 || echo 없음)"
  FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 검증 목록 ==="
# ④ 어느 명령이 실패했는지가 FAIL_LOG 한 줄로 보여야 한다.
#    출력만 있고 명령 이름이 없으면 다음 구현 시도가 무엇을 고칠지 추측하게 된다.
setup
sandbox_python true false true
got=0
# MAX_RETRY=0 — 첫 실패가 곧 마지막이다. 그 마지막 실패도 기록돼야 한다.
env FAKE_SCENARIO=ok AUTO=1 MAX_RETRY=0 ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] \
   && grep -q '실패한 명령: `python -m ruff check .`' .pipeline/feat/FAIL_LOG.md 2>/dev/null \
   && grep -q '그 앞까지 통과: python -m pytest' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
  green "  PASS  실패한 명령 이름이 FAIL_LOG 에 남는다"; PASS=$((PASS+1))
else
  red   "  FAIL  FAIL_LOG 에 실패 명령이 없음 — exit=$got"
  sed -n '1,6p' .pipeline/feat/FAIL_LOG.md 2>/dev/null | sed 's/^/         /'
  FAIL=$((FAIL+1))
fi
teardown

# ⑤ DONE 이 무엇을 뜻하는지 STATE.md 만 읽어도 나와야 한다
setup
env FAKE_SCENARIO=ok AUTO=1 ./orchestrate.sh feat >/dev/null 2>&1
if grep -q 'phase: DONE' .pipeline/feat/STATE.md 2>/dev/null \
   && grep -q '마지막 결과: 통과: python -m pytest, python -m ruff check ., python -m mypy src' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  DONE 이면 무엇을 통과했는지가 STATE.md 에 남는다"; PASS=$((PASS+1))
else
  red   "  FAIL  DONE 에 검증 증거가 없음"
  sed -n '/검증 게이트/,/산출물/p' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'
  FAIL=$((FAIL+1))
fi
teardown

# ⑥ TEST_CMD 오버라이드는 그대로 동작하고, 그 사실이 STATE.md 에 보인다 (하위 호환)
setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if grep -q '검증 명령: true' .pipeline/feat/STATE.md 2>/dev/null \
   && grep -q 'TEST_CMD 오버라이드' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  TEST_CMD 오버라이드가 STATE.md 에 드러난다"; PASS=$((PASS+1))
else
  red   "  FAIL  오버라이드가 STATE.md 에 안 보임"; FAIL=$((FAIL+1))
fi
teardown

# ⑦ smoke.sh 가 있으면 검증 목록 마지막에 붙는다 (없으면 조용히 건너뜀은 위 케이스들이 커버)
setup
mkdir -p .pipeline/feat
printf '#!/usr/bin/env bash\nexit 0\n' > .pipeline/feat/smoke.sh
env FAKE_SCENARIO=ok AUTO=1 ./orchestrate.sh feat >/dev/null 2>&1
if grep -q 'smoke.sh' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  기능 폴더의 smoke.sh 가 검증 목록에 붙는다"; PASS=$((PASS+1))
else
  red   "  FAIL  smoke.sh 훅이 안 붙음"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 재시도 루프 ==="
# 테스트가 항상 실패하면 MAX_RETRY 만큼 돌고 죽어야 한다.
# 기록은 시도 횟수와 같다 — 마지막 시도도 die 전에 FAIL_LOG 에 남는다.
setup
got=0
env FAKE_SCENARIO=ok AUTO=1 MAX_RETRY=2 TEST_CMD="false" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
attempts=$(grep -c '^## attempt' .pipeline/feat/FAIL_LOG.md 2>/dev/null | head -1)
attempts=${attempts:-0}
if [ "$got" -eq 2 ] && [ "$attempts" -eq 3 ]; then
  green "  PASS  테스트 계속 실패 → 3회 전부 기록 후 포기 (exit 2)"; PASS=$((PASS+1))
else
  red   "  FAIL  재시도 루프 — exit=$got, FAIL_LOG 기록=$attempts (기대: 2, 3)"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 설계 재사용 ==="
# 이미 DONE 인 DESIGN.md 가 있으면 설계 단계를 아예 호출하지 않아야 한다
setup
mkdir -p .pipeline/feat
printf 'STATUS: DONE\n\n(사람이 이미 검토한 설계)\n' > .pipeline/feat/DESIGN.md
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if [ ! -f .pipeline/feat/design.result.json ] \
   && grep -q '사람이 이미 검토한 설계' .pipeline/feat/DESIGN.md \
   && [ -f .pipeline/feat/IMPL.md ]; then
  green "  PASS  기존 DESIGN.md 는 재사용되고 덮어쓰이지 않는다"; PASS=$((PASS+1))
else
  red   "  FAIL  설계 재사용 실패 — design 단계가 다시 돌았거나 산출물이 덮어써짐"; FAIL=$((FAIL+1))
fi
teardown

setup
mkdir -p .pipeline/feat
printf 'STATUS: DONE\n\n(사람이 이미 검토한 설계)\n' > .pipeline/feat/DESIGN.md
env FAKE_SCENARIO=ok AUTO=1 FRESH_DESIGN=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if [ -f .pipeline/feat/design.result.json ]; then
  green "  PASS  FRESH_DESIGN=1 이면 설계를 다시 뽑는다"; PASS=$((PASS+1))
else
  red   "  FAIL  FRESH_DESIGN=1 인데 설계 단계가 안 돌았다"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 진행 스트림 ==="
# tee 가 원본 스트림을 보존해야 result 추출이 가능하다.
# 스트림이 비면 진행 표시도 죽고 게이트 판정 근거도 사라진다.
setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if grep -q '"type":"assistant"' .pipeline/feat/design.stream.jsonl 2>/dev/null \
   && [ "$(jq -r '.is_error' .pipeline/feat/design.result.json 2>/dev/null)" = "false" ]; then
  green "  PASS  스트림이 보존되고 마지막 result 만 추출된다"; PASS=$((PASS+1))
else
  red   "  FAIL  스트림 보존/추출 실패"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 모델 교체 감시 ==="
setup
env FAKE_SCENARIO=model_swap AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1
if grep -q '요청 claude-fable-5 → 실제 claude-opus-4-8' .pipeline/feat/MODEL_LOG.md 2>/dev/null; then
  green "  PASS  다른 모델이 돌면 MODEL_LOG 에 기록된다"; PASS=$((PASS+1))
else
  red   "  FAIL  모델 교체가 기록되지 않음"
  echo "         MODEL_LOG 내용:"; sed 's/^/         /' .pipeline/feat/MODEL_LOG.md 2>/dev/null
  FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 상담역·런처 상태 창구 ==="
setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if grep -q 'phase: DONE' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  STATE.md 가 최종 상태를 반영한다"; PASS=$((PASS+1))
else
  red   "  FAIL  STATE.md 미갱신"; FAIL=$((FAIL+1))
fi
# 런처 계약은 문서가 아니라 런처가 실제로 읽는 파일에 있어야 한다.
# 문서에만 적힌 계약은 안 지켜졌다 (2026-08-24 실전 2회).
if grep -q '## 다음 행동' .pipeline/feat/STATE.md 2>/dev/null \
   && grep -q '완주다' .pipeline/feat/STATE.md 2>/dev/null; then
  green "  PASS  DONE 상태에 런처용 다음 행동 블록이 있다"; PASS=$((PASS+1))
else
  red   "  FAIL  다음 행동 블록 없음"
  sed -n '/다음 행동/,+3p' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'
  FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 단계별 상한 ==="
# 하나의 세트(40턴/$5)를 전 단계에 쓰던 것이 틀렸다 — 병목 축이 단계마다 다르다.
# 확인할 것은 "단계마다 다른 값이 실제로 CLI 에 전달되는가"다.
setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
d="$(cat .pipeline/feat/DESIGN.args 2>/dev/null)"
i="$(cat .pipeline/feat/IMPL.args 2>/dev/null)"
if [[ "$d" == *"turns=40 budget=5"* ]] && [[ "$i" == *"turns=80 budget=8"* ]]; then
  green "  PASS  단계마다 다른 상한이 CLI 로 전달된다"; PASS=$((PASS+1))
else
  red   "  FAIL  단계별 상한 — design=[$d] impl=[$i]"; FAIL=$((FAIL+1))
fi
teardown

setup
env FAKE_SCENARIO=ok AUTO=1 TURNS_IMPL=7 BUDGET_IMPL=2 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1
if grep -q 'turns=7 budget=2' .pipeline/feat/IMPL.args 2>/dev/null; then
  green "  PASS  TURNS_IMPL/BUDGET_IMPL 이 기본값을 덮는다"; PASS=$((PASS+1))
else
  red   "  FAIL  상한 오버라이드가 안 먹음 — $(cat .pipeline/feat/IMPL.args 2>/dev/null)"; FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 죽은 단계 부검 ==="
# 2026-08-24 하루에 세 번 죽었는데 세 번 다 로그에 사인이 한 줄도 안 남았다.
# 사람이 매번 *.stream.jsonl 을 jq 로 파서 원인을 알아냈다. 그게 이 절이 막는 것이다.

# ① 사인이 없어도 "없다"를 정직하게 남긴다 (침묵 금지)
setup
got=0
env FAKE_SCENARIO_DESIGN=crash_no_result AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
if [ "$got" -eq 2 ] && grep -q '사인 확인 불가' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
  green "  PASS  result 이벤트가 없으면 그 사실을 FAIL_LOG 에 남기고 죽는다"; PASS=$((PASS+1))
else
  red   "  FAIL  사인 부재가 기록되지 않음 — exit=$got"
  sed 's/^/         /' .pipeline/feat/FAIL_LOG.md 2>/dev/null | head -6
  FAIL=$((FAIL+1))
fi
teardown

if have_detach; then
  detach_pm() { python3 -c 'import os,sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@"; }

  # ② 산출물이 온전해도 자동 통과는 없다. 사인은 FAIL_LOG 로, 산출물은 파킹으로.
  #    AUTO=1 인데도 멈추는 것이 핵심이다 (gate_human force=1).
  setup
  got=0
  detach_pm env FAKE_SCENARIO_DESIGN=crash_with_artifact AUTO=1 TEST_CMD=true \
    ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq 4 ] \
     && grep -q 'error_max_turns' .pipeline/feat/FAIL_LOG.md 2>/dev/null \
     && grep -q 'turns_exhausted' .pipeline/feat/FAIL_LOG.md 2>/dev/null \
     && [ -f .pipeline/feat/DESIGN.md.crashed ] && [ ! -f .pipeline/feat/DESIGN.md ] \
     && [ ! -f .pipeline/feat/IMPL.md ]; then
    green "  PASS  죽었는데 산출물이 온전하면 AUTO=1 이어도 멈추고 사인·파킹을 남긴다"; PASS=$((PASS+1))
  else
    red   "  FAIL  부검 게이트 — exit=$got (기대 4), 파킹=$([ -f .pipeline/feat/DESIGN.md.crashed ] && echo O || echo X)"
    sed 's/^/         /' .pipeline/feat/FAIL_LOG.md 2>/dev/null | head -6
    FAIL=$((FAIL+1))
  fi
  # ③ 그 정지 지점에도 런처용 안내가 붙어야 한다
  if grep -q 'phase: AWAITING_APPROVAL' .pipeline/feat/STATE.md 2>/dev/null \
     && grep -q 'mv ' .pipeline/feat/STATE.md 2>/dev/null; then
    green "  PASS  exit 4 정지 지점에 살리는 방법(mv)이 STATE.md 에 찍힌다"; PASS=$((PASS+1))
  else
    red   "  FAIL  파킹 승인 안내가 STATE.md 에 없음"
    sed -n '/다음 행동/,+3p' .pipeline/feat/STATE.md 2>/dev/null | sed 's/^/         /'
    FAIL=$((FAIL+1))
  fi

  # ④ 미승인 산출물이 재사용 로직에 걸리면 안 된다.
  #    제자리에 둔 채 게이트만 띄우면 다음 실행이 **게이트 없이** 되살린다.
  #    (2026-08-24 document-detail 의 JUDGE.md 가 정확히 그 상태였다)
  env FAKE_SCENARIO=ok AUTO=1 TEST_CMD=true ./orchestrate.sh feat >/dev/null 2>&1
  if [ -f .pipeline/feat/design.result.json ] \
     && [ -f .pipeline/feat/DESIGN.md.crashed ] \
     && ! grep -q 'crash_with_artifact' .pipeline/feat/DESIGN.md 2>/dev/null; then
    green "  PASS  파킹된 산출물은 다음 실행의 재사용 로직에 안 걸린다"; PASS=$((PASS+1))
  else
    red   "  FAIL  미승인 산출물이 게이트 없이 되살아났다"; FAIL=$((FAIL+1))
  fi
  teardown

  # ⑤ 이전 주행이 남긴 산출물을 "이번 주행 것"으로 오인하면 안 된다.
  #    FRESH_DESIGN=1 은 사람이 그 설계를 **버리라고 명시한** 것인데, 파일 존재만 보면
  #    셸이 그걸 되살리라고 게이트에 내민다 (2026-08-24 코드리뷰 발견 · 재현 확정).
  setup
  env FAKE_SCENARIO=ok AUTO=1 TEST_CMD=true ./orchestrate.sh feat >/dev/null 2>&1
  got=0
  detach_pm env FRESH_DESIGN=1 FAKE_SCENARIO_DESIGN=crash_no_result AUTO=1 TEST_CMD=true \
    ./orchestrate.sh feat >/dev/null 2>&1 || got=$?
  if [ "$got" -eq 2 ] && [ ! -e .pipeline/feat/DESIGN.md.crashed ] \
     && grep -q '이전 주행 것' .pipeline/feat/FAIL_LOG.md 2>/dev/null; then
    green "  PASS  이번 주행이 안 쓴 산출물은 살리지 않는다 (die, 파킹 없음)"; PASS=$((PASS+1))
  else
    red   "  FAIL  이전 주행 산출물이 승격됨 — exit=$got (기대 2), 파킹=$([ -e .pipeline/feat/DESIGN.md.crashed ] && echo O || echo X)"
    FAIL=$((FAIL+1))
  fi
  teardown

  # ⑥ 파킹본도 덮어쓰지 않는다. 스트림을 attempt 번호로 보존하면서 정작 가장 비싼
  #    산출물만 덮어쓰는 것은 앞뒤가 안 맞는다.
  setup
  detach_pm env FAKE_SCENARIO_DESIGN=crash_with_artifact AUTO=1 TEST_CMD=true \
    ./orchestrate.sh feat >/dev/null 2>&1 || true
  detach_pm env FAKE_SCENARIO_DESIGN=crash_with_artifact AUTO=1 TEST_CMD=true \
    ./orchestrate.sh feat >/dev/null 2>&1 || true
  if [ -f .pipeline/feat/DESIGN.md.crashed ] && [ -f .pipeline/feat/DESIGN.md.crashed2 ]; then
    green "  PASS  두 번째 파킹본이 첫 번째를 덮지 않는다"; PASS=$((PASS+1))
  else
    red   "  FAIL  파킹본이 덮어써짐"; ls -1 .pipeline/feat/ | sed 's/^/         /'; FAIL=$((FAIL+1))
  fi
  teardown

  # ⑦ 죽은 경로에서도 모델 교체가 기록돼야 한다. 다른 모델이 돌다 상한에 닿은 것이라면
  #    사람이 "산출물을 신뢰할까"를 판단할 때 그 사실을 알아야 한다.
  setup
  detach_pm env FAKE_SCENARIO_DESIGN=crash_swapped AUTO=1 TEST_CMD=true \
    ./orchestrate.sh feat >/dev/null 2>&1 || true
  if grep -q '요청 claude-fable-5 → 실제 claude-opus-4-8' .pipeline/feat/MODEL_LOG.md 2>/dev/null; then
    green "  PASS  크래시 경로에서도 모델 교체가 MODEL_LOG 에 남는다"; PASS=$((PASS+1))
  else
    red   "  FAIL  크래시 시 모델 교체 미기록"; sed 's/^/         /' .pipeline/feat/MODEL_LOG.md 2>/dev/null; FAIL=$((FAIL+1))
  fi
  teardown
else
  red "  SKIP  부검 게이트 케이스 — setsid 사용 불가 (setsid 대체 불가)"
fi

# ⑧ 크래시했는데 산출물이 BLOCKED 면 "그냥 죽었다"가 아니라 막힌 이유를 넘긴다.
#    예산 상한 직전에 BLOCKED 를 쓰고 죽는 것은 흔한 조합이다.
setup
got=0
# stderr 를 잡는다. STATE.md 의 안내 문구에도 'BLOCKED_NEEDS' 라는 낱말이 들어 있어서
# 그걸로 단언하면 실제 사유가 안 나와도 통과하는 공허한 검사가 된다.
env FAKE_SCENARIO_DESIGN=crash_blocked AUTO=1 TEST_CMD="true" \
  ./orchestrate.sh feat >/dev/null 2>blocked_err.txt || got=$?
if [ "$got" -eq 3 ] && grep -q 'phase: BLOCKED:design' .pipeline/feat/STATE.md 2>/dev/null \
   && grep -q '예산 상한 직전에 막힘' blocked_err.txt 2>/dev/null \
   && grep -q '스키마를 바꿔도 되는지' blocked_err.txt 2>/dev/null; then
  green "  PASS  BLOCKED 를 쓰고 죽으면 exit 3 으로 막힌 이유가 간다"; PASS=$((PASS+1))
else
  red   "  FAIL  크래시+BLOCKED — exit=$got (기대 3), phase=$(grep -m1 'phase:' .pipeline/feat/STATE.md 2>/dev/null)"
  sed 's/^/         /' blocked_err.txt 2>/dev/null | tail -6
  FAIL=$((FAIL+1))
fi
teardown

echo
echo "=== 증거 보존 ==="
# 재시도 루프의 사인은 "1차가 왜 죽었나"인데 그 파일이 2차에 덮였다 (2026-08-24 실측).
setup
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
env FAKE_SCENARIO=ok AUTO=1 TEST_CMD="true" ./orchestrate.sh feat >/dev/null 2>&1
if [ -f .pipeline/feat/impl.attempt1.stream.jsonl ] \
   && [ -f .pipeline/feat/impl.attempt1.result.json ] \
   && [ -f .pipeline/feat/impl.stream.jsonl ]; then
  green "  PASS  다시 도는 단계는 이전 증거를 attempt 번호로 보관한다"; PASS=$((PASS+1))
else
  red   "  FAIL  이전 스트림이 덮어써짐"
  ls -1 .pipeline/feat/ 2>/dev/null | sed 's/^/         /'
  FAIL=$((FAIL+1))
fi
teardown

echo
echo "════════════════════════════"
printf "  통과 %d / 실패 %d\n" "$PASS" "$FAIL"
echo "════════════════════════════"
[ "$FAIL" -eq 0 ]
