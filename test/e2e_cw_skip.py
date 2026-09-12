"""cw:skip E2E — 실제 cli.main 을 --dry-run 으로 돌려 산출물을 검사한다.

watchdog Observer·스냅샷·diff·정제·프롬프트 생성까지 실제 경로를 탄다. 종료는 Ctrl+C 와
같은 KeyboardInterrupt 를 메인 스레드에 넣는다. 외부 호출(OpenAI·Discord)은 없다.
"""

import _thread
import json
import shutil
import sys
import threading
import time
from pathlib import Path

from class_watcher import cli

BASE = Path(sys.argv[1]).resolve()
results: list[tuple[str, str, bool, str]] = []


def check(scenario: str, name: str, ok: bool, detail: str = "") -> None:
    results.append((scenario, name, ok, detail))


def run_session(name: str, baseline: dict[str, str], edits: list[tuple[str, str | None]]) -> Path:
    root = BASE / name / "watch"
    sessions = BASE / name / "sessions"
    shutil.rmtree(BASE / name, ignore_errors=True)
    root.mkdir(parents=True)
    for rel, body in baseline.items():
        (root / rel).write_text(body, encoding="utf-8")

    def driver() -> None:
        time.sleep(3)
        for rel, body in edits:
            target = root / rel
            if body is None:
                target.unlink()
            else:
                target.write_text(body, encoding="utf-8")
            time.sleep(0.4)
        time.sleep(3)
        _thread.interrupt_main()

    threading.Thread(target=driver, daemon=True).start()
    code = cli.main(
        [
            "watch",
            str(root),
            "--title",
            f"cw-skip {name}",
            "--session-dir",
            str(sessions),
            "--dry-run",
        ]
    )
    check(name, "종료 코드 0", code == 0, f"exit={code}")
    return next(sessions.iterdir())


# ── 시나리오 A: 섞인 세션 ─────────────────────────────────────────────
a = run_session(
    "A",
    baseline={
        "keep.py": "x = 1\n",
        "later.py": "y = 1\n",
        "unmark.py": "# cw:skip\nz = 1\n",
        "gone.py": "# cw:skip\nGONE_TOKEN = 1\n",
    },
    edits=[
        ("keep.py", "x = 1\nKEEP_TOKEN = 2\n"),
        ("scratch.py", "# 연습 cw:skip\nSCRATCH_TOKEN = 1\n"),
        ("page.html", "<!--\n  오늘 실습 CW:SKIP\n-->\n<p>HTML_TOKEN</p>\n"),
        ("Main.java", "/* cw:skip */\nclass Main { int JAVA_TOKEN; }\n"),
        ("later.py", "# cw:skip\ny = 1\nLATER_TOKEN = 2\n"),
        ("unmark.py", "z = 1\nUNMARK_TOKEN = 2\n"),
        ("mid.py", "a = 1\n# cw:skip\nMID_TOKEN = 1\n"),
        ("gone.py", None),
    ],
)
diff = (a / "final.diff").read_text(encoding="utf-8")
prompt = (a / "prompt.json").read_text(encoding="utf-8") if (a / "prompt.json").exists() else ""
stats = json.loads((a / "stats.json").read_text(encoding="utf-8"))
session = json.loads((a / "session.json").read_text(encoding="utf-8"))
reasons = {f["path"]: f["skip_reason"] for f in stats["files"]}

for rel in ("scratch.py", "page.html", "Main.java", "later.py", "gone.py"):
    check("A", f"{rel} → opt_out", reasons.get(rel) == "opt_out", str(reasons.get(rel)))
    check("A", f"final.diff 에 {rel} skipped 한 줄", f"# skipped: {rel} (opt_out)" in diff)
for rel in ("keep.py", "unmark.py", "mid.py"):
    check(
        "A",
        f"{rel} → 요약 포함",
        reasons.get(rel) is None and f"+++ b/{rel}" in diff,
        str(reasons.get(rel)),
    )
for token in ("SCRATCH_TOKEN", "HTML_TOKEN", "JAVA_TOKEN", "LATER_TOKEN", "GONE_TOKEN"):
    check(
        "A", f"{token} 가 final.diff·prompt.json 에 없음", token not in diff and token not in prompt
    )
for token in ("KEEP_TOKEN", "UNMARK_TOKEN", "MID_TOKEN"):
    check("A", f"{token} 가 prompt.json 에 있음", token in prompt)
check("A", "prompt.json 생성 (dry-run 요약 입력)", bool(prompt))
check("A", "files_changed == 3", stats["totals"]["files_changed"] == 3, str(stats["totals"]))
check("A", "skipped == 5", stats["totals"]["skipped"] == 5, str(stats["totals"]))
watched = {w["path"]: w for w in session.get("watched_files", [])}
check(
    "A",
    "session.json watched_files 에 opt_out 사유",
    watched.get("scratch.py", {}).get("reason") == "opt_out",
    str(watched.get("scratch.py")),
)
check(
    "A",
    "session.json status completed",
    session.get("status") == "completed",
    str(session.get("status")),
)

# ── 시나리오 B: 표식 파일만 바뀐 세션 → 요약·전송 생략 ────────────────────
b = run_session(
    "B",
    baseline={"keep.py": "x = 1\n"},
    edits=[("scratch.py", "# cw:skip\nONLY_TOKEN = 1\n")],
)
session_b = json.loads((b / "session.json").read_text(encoding="utf-8"))
check("B", "prompt.json 미생성", not (b / "prompt.json").exists())
check("B", "summary.json 미생성", not (b / "summary.json").exists())
check(
    "B",
    "discord.skip_reason == no_meaningful_change",
    (session_b.get("discord") or {}).get("skip_reason") == "no_meaningful_change",
    str(session_b.get("discord")),
)
check(
    "B",
    "no_change == false (해시상 변경은 있음)",
    session_b.get("no_change") is False,
    str(session_b.get("no_change")),
)

print("\n===RESULTS===")
for scenario, name, ok, detail in results:
    print(json.dumps({"s": scenario, "name": name, "ok": ok, "detail": detail}, ensure_ascii=False))
print(f"===TOTAL {sum(r[2] for r in results)}/{len(results)}===")
