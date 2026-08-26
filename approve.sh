#!/usr/bin/env bash
# 사람 전용 승인 도구 — 게이트 마커를 만든다.
#
# 오케스트레이터가 tty 없이(런처 모드) 게이트에 걸리면 exit 4 로 멈추고 이
# 명령을 찍어 준다. 검토를 마친 **사람이 터미널에서 직접** 실행한다.
# 클로드 세션 안이라면 `! /path/to/approve.sh ...` — ! 프리픽스는 사람 키 입력이다.
#
# 마커는 대상 파일의 sha256 을 담는다. 승인 후 파일이 한 글자라도 바뀌면 마커가
# 무효가 되므로 "무엇을 승인했는가"가 항상 내용 단위로 남는다.
#
# /dev/tty 확인은 물리적 봉쇄가 아니다 — 셸 접근이 있는 에이전트는 마커를 직접
# 쓸 수도 있다. 이 확인의 역할은 "편의로 슬쩍 자동화되는" 침식 경로를 끊는
# 것이고, 런처(메인 세션)는 이 스크립트를 절대 대신 실행하지 않는다는 계약이다.
#
# 사용법:
#   ./approve.sh <feature> <artifact>     # 예: ./approve.sh add-auth DESIGN.md
#   ./approve.sh --hash <file>            # 내부/테스트용: 해시만 출력 (마커 안 만듦)
#   승인 취소: rm .pipeline/<feature>/<artifact>.approved

set -euo pipefail

# orchestrate.sh 의 file_hash 와 결과가 같아야 한다 (run-tests 가 교차 검증한다)
file_hash() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1"
  else sha256sum "$1"; fi | awk '{print $1}'
}

if [ "${1:-}" = "--hash" ]; then
  file_hash "${2:?사용법: ./approve.sh --hash <file>}"
  exit 0
fi

# cwd 가 아니라 스크립트 위치로 저장소를 잡는다 — 런처 세션의 cwd 는 worktree
# 밖(메인 체크아웃·홈 등)일 수 있고, 승인은 항상 이 스크립트가 놓인 저장소의
# .pipeline 을 향해야 한다.
ROOT="$(cd "$(dirname "$0")" && pwd)"
FEATURE="${1:?사용법: ./approve.sh <feature> <artifact>  (예: ./approve.sh add-auth DESIGN.md)}"
NAME="${2:?사용법: ./approve.sh <feature> <artifact>  (예: ./approve.sh add-auth DESIGN.md)}"
FILE="$ROOT/.pipeline/$FEATURE/$NAME"

[ -f "$FILE" ] || { printf '없는 파일: %s\n' "$FILE" >&2; exit 2; }

printf '승인 대상: %s\n' "$FILE"
printf '  %s\n' "$(grep -m1 '^STATUS:' "$FILE" 2>/dev/null || echo '(STATUS 라인 없음)')"
printf '이 내용을 읽고 검토했는가? (y = 승인 마커 생성 / 그 외 = 취소)\n> '
if ! read -t 120 -r ans < /dev/tty; then
  printf '\ntty 없음 — 승인은 사람이 터미널에서 직접 한다 (런처가 대신 누르는 것을 막는 장치)\n' >&2
  exit 2
fi
case "$ans" in
  y|Y)
    file_hash "$FILE" > "$FILE.approved"
    printf '승인됨 → %s.approved\n재실행하면 이 게이트는 마커로 통과한다. 파일이 바뀌면 재승인이 필요하다.\n' "$FILE" ;;
  *)
    printf '취소 — 마커를 만들지 않음\n'; exit 1 ;;
esac
