# 다른 세션에 넘길 프롬프트 — DMS 하네스 결함 4건 이식

아래 `---` 사이를 통째로 복사해서 새 세션에 붙여 넣으면 된다.
`class-code-watcher` 에서 찾아 고친 결함 4건을 나머지 두 사본에 옮기는 작업이다.

**왜 별도 세션인가**: 대상이 다른 저장소 두 곳이고, 스택도 다르다(여기는 Python, 저쪽은 Node/TypeScript).
②번 수정은 그대로 복사하면 **안 되고** 스택에 맞게 다시 써야 한다. 파일 통째 복사는 금물이다.

---

## 작업

이 머신에 DMS 개발 파이프라인 하네스(`orchestrate.sh`)의 사본이 3개 있다.

```
C:/Users/ksmart/class-code-watcher/orchestrate.sh      ← 기준 구현 (Python 프로젝트). 3건 모두 수정됨
C:/Users/ksmart/dms-auto-classify/orchestrate.sh       756줄 (Node/TypeScript). 미수정
C:/Users/ksmart/Nyangmeong_care_dms/orchestrate.sh     756줄 (Node/TypeScript). 미수정
```

뒤 두 사본은 **서로 완전히 동일하다**(확인됨). `class-code-watcher` 에서 결함 3건을 찾아 고쳤는데,
고침이 역류할 경로가 없어 나머지 두 곳에 그대로 남아 있다.

**이 세션의 일**: 결함 4건이 두 사본에 실제로 존재하는지 **먼저 검증하고**, 존재하면 이식한다.

### 먼저 읽을 것

- `C:/Users/ksmart/class-code-watcher/HANDOFF.md` 7절 — 결함 3건의 배경과 실측 증거
- `C:/Users/ksmart/class-code-watcher/orchestrate.sh` — 기준 구현

기준 저장소의 관련 커밋:

| 커밋 | 결함 |
|---|---|
| `bbcc316` | ① 레이트 리밋 |
| `1e7fe58` | ② 게이트 명령 권한 |
| `f475c9a` | ③ 비-JSON 줄 |
| `962f532` | ④ 검증 명령 무한 대기 |

`git -C C:/Users/ksmart/class-code-watcher show <해시>` 로 diff 를 볼 수 있다.

---

## 결함 ① — `FALLBACK_*` 이 레이트 리밋을 못 받는다

**증상**: 주간·5시간 사용 창이 소진되면 단계가 그냥 죽는다. `FALLBACK_VERIFY` 에 다른 모델이
설정돼 있어도 **한 번도 시도하지 않는다.**

**원인**: `--fallback-model` 의 문서화된 범위는 "overloaded or not available" 뿐이다
(`claude --help` 로 확인 가능). 레이트 리밋 거부는 그 경로를 타지 않는다. CLI 버그가 아니라
하네스가 이 플래그를 유일한 안전망으로 삼은 것이 문제다.

**실측 증거** (2026-08-26 16:10, `class-code-watcher`):
`FALLBACK_VERIFY="claude-opus-5,claude-sonnet-5"` 였는데도 fable-5 에서 죽었고 폴백 시도 0회.
verify 한 단계가 통째로 날아갔다.

**이식 방법**: `bbcc316` 을 거의 그대로 옮길 수 있다. **스택 무관**이다 (모델·스트림 처리 로직).

1. `rate_limited()` 헬퍼를 `run_stage()` 정의 앞에 추가. 신호 두 개 중 하나면 참:
   - `rate_limit_event.rate_limit_info.status == "rejected"`
   - assistant 메시지의 `error == "rate_limit"`
   `terminal_reason` 은 보조로만 — 없는 버전이 있을 수 있다.
2. `run_stage` 안의 `claude -p` 호출(대상 파일 343행 부근)을 모델 체인 루프로 감싼다.
   `chain="$model${fallback:+,$fallback}"` 에서 앞부터 하나씩 꺼내 쓰고, 리밋 거부로 죽었고
   아직 안 써본 모델이 남아 있을 때만 갈아탄다.
3. **리밋이 아닌 실패로는 갈아타지 않는다.** 예산·턴 초과나 에이전트 에러는 모델을 바꿔서
   나아지는 실패가 아니다. 게다가 조용히 다른 모델로 재주행하면 `MODEL_LOG` 가 감시하려던
   "다른 모델이 돌았다"를 셸이 스스로 만들어내는 꼴이 된다.
4. 갈아타기 전 증거는 `<name>.ratelimit<n>.*` 로 보관하고 `fail_log` 에 남긴다.
5. `check_model_swap` 이 `$model` 이 아니라 **실제로 돌린 모델**(`$try_model`)과 대조하도록 고친다.

**검증 방법 — 진짜 증거 파일이 있다.** 이 판정 함수는 실제 스트림으로 테스트할 수 있다:

```bash
# 🔴 리밋으로 죽은 실제 스트림 (감지돼야 함)
C:/Users/ksmart/class-code-watcher/.pipeline/bootstrap-cli/verify.attempt1.stream.jsonl

# 🟢 정상 완료한 스트림 4개 (감지되면 안 됨)
C:/Users/ksmart/class-code-watcher/.pipeline/bootstrap-cli/{design,judge,impl,verify}.stream.jsonl
```

기준 저장소에서 이 5개로 검증했고 전부 올바르게 판정했다. **이식한 쪽에서도 같은 파일로 검증할 것.**

---

## 결함 ② — 에이전트가 게이트 명령을 실행하지 못한다

> **⚠ 이 건은 그대로 복사하면 안 된다. 스택이 다르다.**

**증상**: 에이전트가 테스트·린트·타입체크를 **한 번도 실행하지 못한 채** "통과할 것"이라고
추측만 하고 단계를 끝낸다.

**원인**: `--permission-mode acceptEdits` 는 Write/Edit 만 자동 승인한다. Bash·PowerShell 은
승인을 요구하는데 `-p` 는 비대화형이라 승인할 사람이 없다 → 전부 거부.

**실측 증거** (`class-code-watcher`, `*.result.json` 의 `permission_denials`):

| 단계 | 거부된 호출 |
|---|---|
| judge | 실행 요청 전량 거부 |
| impl | 5건 전부 |
| verify | 3건 전부 |

그 결과 impl 이 린트 오류 한 줄(103자 주석)을 **두 주행 내내 못 고쳤다.** 자기가 뭘 어겼는지
볼 수 없었기 때문이다.

**왜 조용하고 비싼가**: ①③은 죽어서 눈에 보인다. ②는 안 보인다. 셸의 `run_verify` 가 최종
판정을 하니 틀린 코드가 통과하지는 않지만, 에이전트가 스스로 못 고쳐서 **실패 → 재시도 →
또 실패**로 돌고 그게 전부 돈이다.

**이식 방법**: `claude -p` 호출에 `--allowedTools "$GATE_TOOLS"` 를 추가하고, `GATE_TOOLS` 를
**그 저장소의 게이트 명령에 맞게** 만든다.

- `class-code-watcher`(Python)는 `$PY` 에서 `pytest`·`ruff`·`mypy` 패턴을 만든다 — **이건 여기 전용이다.**
- 대상 두 저장소는 **Node/TypeScript** 다. `TEST_CMD="npm test"`(vitest), 프리플라이트는
  `npm run build`, 린트는 eslint(`eslint.config.mjs` 존재). 그 저장소의 `orchestrate.sh` 를
  읽고 실제로 무엇을 게이트로 쓰는지 확인한 뒤 패턴을 짤 것. 추측하지 말고 파일에서 확인할 것.
- 형식은 `Bash(npm test*)` 처럼 글로브다 (`claude --help` 의 `--allowedTools` 참조).

**지켜야 할 선**:
- **게이트 명령만 연다.** `Bash(npm *)` 처럼 넓게 열면 임의 스크립트 실행이 된다.
  `Bash(npm run *)` 도 위험하다 — `package.json` 의 아무 스크립트나 돌 수 있다.
- **판정권은 셸에 유지한다.** `run_verify` 가 직접 돌려 판정하는 구조를 건드리지 말 것.
  에이전트가 "통과했다"고 쓴 문장은 여전히 읽지 않는다.
- 통째 교체용 탈출구(`GATE_TOOLS_OVERRIDE` 같은 환경변수)를 하나 남겨 둘 것.

---

## 결함 ③ — 스트림의 비-JSON 줄 하나에 단계가 통째로 죽는다

**증상**: 멀쩡히 일하던 단계가 갑자기 죽고, 사인조차 안 남는다.

**원인**: `stream-json` 은 NDJSON 인데 claude 가 JSON 이 아닌 줄을 stdout 으로 흘릴 때가 있다.
실측된 사례: MCP 서버의 `Client.listTools() called but server does not advertise tools capability`
경고가 6번째 줄에 섞였다. jq 의 기본 파서가 그 줄에서 죽고, `tee` 와 `claude` 가 SIGPIPE 로
연달아 죽는다.

**대상 파일에서 고칠 자리 두 곳** (dms-auto-classify 기준 행 번호):

| 행 | 현재 | 문제 |
|---|---|---|
| 353 | `jq --unbuffered -r '` | 진행 표시용. 여기서 죽으면 파이프 전체가 무너진다 |
| 369 | `jq -s '[.[] \| select(.type? == "result")] \| last'` | 사인 추출. 여기서도 죽어 사인이 안 남는다 |

**이식 방법**: 두 곳 다 `-R` + `fromjson?` 으로 관용 파싱한다.

```
353행:  jq --unbuffered -Rr 'fromjson? // empty | select(...) ...'
369행:  jq -Rn '[inputs | fromjson? | select(.type? == "result")] | last'
```

**버린 줄은 반드시 세어서 보고할 것.** 조용히 넘기면 다음에 같은 일이 나도 원인을 못 찾는다:

```bash
junk=$(grep -cv '^{' "$stream" 2>/dev/null || true)
[ "${junk:-0}" -gt 0 ] && log "  ⚠ 스트림에 JSON 아닌 줄 ${junk}개 — 무시하고 진행 (원문: $stream)"
```

---

## 결함 ④ — 검증 명령이 안 돌아오면 파이프라인이 조용히 매달린다

**증상**: `run_verify` 가 게이트 명령을 돌리다 영원히 서 있는다. 로그도 `FAIL_LOG` 도 남지
않는다. 사람이 알아채기 전까지 아무 일도 일어나지 않는다.

**원인**: `run_verify` 의 `eval "$cmd"` 에 시간 상한이 없다. 테스트가 블로킹하면 그대로 멈춘다.

**실측 증거** (2026-08-27, `class-code-watcher` 1단계): impl 이 `run_watch` 를 진짜 감시
루프로 바꾸자, 0단계가 남긴 테스트 3개가 Ctrl+C 를 영원히 기다리게 됐다. `pytest` 전체가
멈췄다. **죽는 것보다 나쁘다** — 죽으면 `FAIL_LOG` 라도 남는다.

**이식 방법**: `962f532` 를 거의 그대로 옮길 수 있다. **스택 무관**이다.

1. `VERIFY_TIMEOUT` 설정 추가 (기본 300초, `0` 이면 상한 없음).
2. `run_verify` 안에서 각 명령을 `timeout -k 10 "$VERIFY_TIMEOUT" bash -c "$cmd"` 로 감싼다.
3. **`timeout(1)` 이 없는 환경(coreutils 없는 macOS 등)에서도 돌아야 한다.** 상한을 못 걸면
   조용히 넘기지 말고 그 사실을 `log` 로 남긴다 — 나중에 매달렸을 때 이유를 찾을 수 있게.
4. **`exit 124`(시간 초과)를 일반 실패와 구분해 보고한다.** 뭉개면 다음 시도의 impl 이
   `FAIL_LOG` 를 읽고 "테스트가 틀렸구나"로 오해한다 — 실제로는 안 돌아온 것이다.
5. `STATE.md` 의 검증 게이트 블록에 상한을 노출한다.

**주의**: 대상 저장소는 vitest 다. vitest 는 자체 `testTimeout` 이 있지만 그것은 **케이스
단위**이고, 여기서 막으려는 것은 **명령 전체가 안 돌아오는 것**이다 (수집 단계 무한 루프,
watch 모드로 잘못 들어간 실행 등). 둘은 다른 층이므로 vitest 설정이 있다고 건너뛰지 말 것.
`npm test` 가 watch 모드로 도는 설정이면 그것부터 확인할 것.

## 진행 규칙

1. **검증 먼저.** 3건이 실제로 존재하는지 각 사본에서 확인하고 결과를 보고할 것.
   이미 고쳐져 있으면 건드리지 말 것. 두 사본이 여전히 동일한지도 확인할 것
   (한쪽만 갈라졌다면 각각 다뤄야 한다).
2. **파일을 통째로 복사하지 말 것.** 기준 구현은 Python 프로젝트용이라
   `PY`, `pytest`, `ruff`, `mypy`, 프리플라이트가 전부 다르다. **3건만 정확히 이식한다.**
3. **각 수정 후 `bash -n orchestrate.sh` 로 문법 검증.** 이 스크립트는 `set -euo pipefail`
   이라 문법 오류가 조용히 지나가지 않는다.
4. `orchestrate.sh` 외의 파일은 건드리지 말 것. 특히 각 저장소의 `PROTECTED` 목록에 있는
   파일들(`package.json` 등)은 손대면 안 된다.
5. **커밋 전에 사람에게 diff 를 보여주고 확인받을 것.** 두 저장소가 지금 활발히 돌고 있을 수
   있다. 커밋 메시지는 그 저장소의 기존 스타일(한국어, `fix:` 접두사, 근거 서술)을 따를 것.
6. 셸을 고치는 작업이라 파이프라인을 돌릴 필요는 없다. 에이전트 호출 0회로 끝나야 정상이다.

## 보고할 것

- 사본별 결함 4건의 존재 여부 (검증 근거와 함께)
- 이식한 내용과 스택에 맞춰 바꾼 부분 (특히 ②의 `GATE_TOOLS`)
- `bash -n` 결과
- ① 판정 함수를 실제 스트림 5개로 검증한 결과
- 손대지 않기로 한 것이 있다면 그 이유

---

> **근본 문제 (이번 작업 범위 밖, 기록용)**
> 같은 하네스가 세 번 복사됐고 고침이 역류할 경로가 없다. 이번 이식은 대증요법이다.
> 네 번째 복사가 생기기 전에 별도 리포로 뽑고 각 프로젝트가 참조하게 해야 한다.
