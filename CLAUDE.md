# Class Code Watcher

## 문서 지도 — 세션 시작 시 읽는 순서

| 문서 | 무엇 |
|---|---|
| `HANDOFF.md` | **정본.** 지금 상태 · 다음 할 일 · 지뢰 · 이미 내려진 결정 |
| `PRD.md` | 요구사항 정본 (FR-xxx · 수용 기준 · C-xx 개정 이력) |
| `docs/JOURNAL.md` | 완료된 주행 기록과 해결된 결함. 지금 상태를 물을 때는 안 읽는다 |

`HANDOFF.md` 5절 **(차) 미해결 항목**이 "무엇이 아직 안 닫혔나"의 한 장짜리 목록이다.

## 파이프라인 런처 프로토콜 (serial-agent-pipeline)

이 리포에는 셸 오케스트레이터(`orchestrate.sh`)가 있다. 세션(LLM)은 **런처**다 — 실행·전달·중계만 하고 판단하지 않는다.

- 실행: worktree 에서 `./orchestrate.sh <feature>` 를 백그라운드로. 진행 중에는 `.pipeline/<feature>/STATE.md` 만 읽는다 (`*.stream.jsonl` tail 금지 — 컨텍스트 오염)
- 멈추면 STATE.md 의 `## 다음 행동` 블록을 그대로 따른다. exit 4 = 사람 승인 대기(실패 아님), 2 = 게이트 위반·프로세스 사망(사인은 FAIL_LOG.md 마지막 항목), 3 = BLOCKED
- **게이트 승인은 y 중계다. 판단 금지.** exit 4 에서 세션이 하는 일은 셋뿐이다 — ① 검토 대상 파일을 **그대로** 보여준다 (요약·추천·"괜찮아 보인다" 금지) ② AskUserQuestion 으로 "승인? (y/n)" 하나만 묻는다 ③ 사람의 답이 **정확히 y** 일 때만 STATE.md 가 준 승인 명령(`approve.sh … --relayed y` 또는 `mv …`)을 실행하고 재실행한다. "알아서", "괜찮으면 해" 는 y 가 아니다 — 다시 묻는다. n 이면 중단을 보고하고 기다린다
- 사람이 묻지 않았는데 승인 명령을 실행하거나, `.approved` 파일을 직접 쓰거나, 사람의 답을 바꿔 전달하는 것은 금지. 승인 기록은 `.pipeline/<feature>/APPROVALS.md` 에 남는다
- 사람에게 터미널을 더 열라고 안내하지 마라 — advisor.sh 는 선택지일 뿐, 상담은 이 세션이 STATE.md·산출물 읽기로 대신한다

### 이 저장소에서 주행할 때

```bash
./pipeline-worktree.sh <feature>          # 메인 체크아웃에서는 시작이 거부된다
cd ../class-code-watcher-pipeline-<feature>
# PY 는 먼저 export 한다. 같은 줄에 두면 $PY 가 현재 셸 값으로 먼저 확장돼 빈 문자열이 된다.
export PY=.venv/bin/python
PREFLIGHT_CMD="$PY -m mypy src && $PY -m ruff check ." ./orchestrate.sh <feature>
```

`PREFLIGHT_CMD` 는 기본값이 비어 있다. **안 넘기면 mypy·ruff 없이 verify 가 녹색을 준다** — 게이트 3종 중 둘이 빠진다.
