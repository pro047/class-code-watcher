### 테스트 환경

pytest. 셸이 `$TEST_CMD` 로 돌린다.

- 테스트 파일 위치: `tests/test_*.py`
- **수집된 테스트가 0개면 pytest 는 exit 5 로 실패한다.** 테스트를 안 쓰고 넘어가는
  경로가 없다는 뜻이다
- 임시 파일·디렉터리는 `tmp_path` fixture 를 쓴다. 저장소 안에 쓰지 마라
- 외부 호출은 `monkeypatch` 또는 `unittest.mock` 으로 막는다
- 테스트에도 타입 힌트를 붙인다 (mypy 가 검증 목록에 있다)

**돌릴 수 없는 것** — OpenAI·Discord 에 실제로 붙는 테스트는 쓰지 마라. `.env` 는
없거나 더미 값이다. 네트워크를 타는 경로는 전부 mock 대상이다.

**불안정한 것** — watchdog 의 실제 파일시스템 이벤트는 OS·타이밍·백신에 의존한다.
`sleep` 을 넣어 이벤트를 기다리는 테스트는 CI 와 다른 PC 에서 무작위로 깨진다.
**이벤트 수신 자체를 테스트하지 말고, 이벤트를 받은 뒤의 판정 로직**(debounce 병합,
경로 필터, 스냅샷 비교, 안정화 판정)을 순수 함수로 테스트해라. 실제 감지는
사람 확인 체크리스트로 넘긴다.

### 불변식 회귀 테스트

이번 변경이 아래를 깨뜨릴 수 있는 자리에 있으면 그걸 고정하는 케이스를 넣는다.
해당 없으면 "해당 없음"이라고 쓴다.
- LLM 호출은 세션당 정상 1회 / 상한 2회, 저장 이벤트 중 0회 (FR-030)
- 외부 전송 전 secret scan 통과 필수, 탐지 시 기본 전송 중단 (FR-036)
- Discord payload 에 코드 원문·diff 라인이 없다 (FR-051)
- 변경 없음이면 OpenAI·Discord 모두 0회 (FR-035)
- git 없이도 diff 가 생성된다 (FR-020)

VERIFY.md 에 **불변식 회귀 테스트 결과 (위 5개 각각: 넣음 / 해당 없음)** 절을 더한다.

### 손대면 안 되는 설정
pytest·ruff·mypy 설정 — `pyproject.toml`, `pytest.ini`, `ruff.toml`, `mypy.ini`,
`setup.cfg`, `conftest.py` 포함. fixture 가 필요하면 테스트 파일 안에 둬라.
