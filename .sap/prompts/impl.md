### 추가 입력
- `$ROOT/PRD.md` — 요구사항 원문. DESIGN.md 가 인용한 FR 의 수용 기준을 확인할 때만 본다

### 이 저장소의 코딩 규칙
- **콘솔 출력 문구와 코드 주석은 한국어.** 사용자가 한국인이다
- 주석은 "왜"만 적는다. 코드를 읽으면 아는 "무엇"은 적지 않는다
- 주석 밀도는 주변 코드에 맞춘다
- **타입 힌트를 붙인다.** mypy 가 검증 목록에 있다
- **부작용과 판정 로직을 분리한다.** 파일 읽기·네트워크·시계는 얇게, 판정은 순수
  함수로. 검증 단계가 쓴 테스트가 부르는 자리가 여기다
- **비밀값을 로그·콘솔·예외 메시지에 흘리지 않는다** (FR-003, FR-042).
  예외를 그대로 올리기 전에 메시지에 키·Webhook URL 이 섞였는지 본다.
  마지막 4자도 기본적으로 출력하지 않는 것이 이 저장소의 기준이다

### 손대면 안 되는 것 — 필요하면 STATUS: BLOCKED
- `pyproject.toml` 의 의존성 (패키지 추가/제거는 사람 승인 사항)
- pytest·ruff·mypy 설정 — `pyproject.toml` 의 `[tool.*]`, `pytest.ini`, `ruff.toml`,
  `mypy.ini`, `setup.cfg`, `conftest.py`. **새로 만드는 것도 포함이다.**
  검증 단계의 게이트 설정이라 셸이 지문으로 감시한다
- `.env` / `.env.example` / `.gitignore`
- `AGENTS.md`
- `PRD.md` (파이프라인 완주 후 사람이 갱신한다)
- `pip install` 을 돌리지 않는다. 의존성이 필요하면 BLOCKED.

### 실행 확인
소스 검사는 `.venv/bin/python -m ruff check src` 와 `.venv/bin/python -m mypy src` 다.
못 돌렸으면 그 이유를 쓴다 (예: mypy 미설치 → 타입 검사 불가).
