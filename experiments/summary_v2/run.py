r"""prompt_v2.md 로 세션 diff 를 다시 요약한다. 결과는 out/{session_id}.json/.md.

사용: .venv\Scripts\python.exe -X utf8 experiments\summary_v2\run.py <세션ID> [--title 제목]
.env 의 OPENAI_API_KEY / OPENAI_MODEL 을 쓴다. 세션 폴더에는 아무것도 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from openai import OpenAI

HOME = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SESSIONS = HOME / "sessions"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (HOME / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def schema() -> dict:
    def obj(props: dict) -> dict:
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}

    s = {"type": "string"}
    arr = lambda item: {"type": "array", "items": item}  # noqa: E731
    return obj({
        "title": s,
        "summary": s,
        "api_inventory": arr(s),
        "pitfalls": arr(obj({"title": s, "explanation": s, "code_example": s})),
        "keywords": arr(obj({"term": s, "syntax": s, "concept": s, "code_example": s, "group": s})),
        "practice": arr(obj({"question": s, "hint": s})),
        "review_questions": arr(s),
        "notes": arr(s),
    })


def user_prompt(doc: dict, diff: str, title: str) -> str:
    files = [f["path"] for f in doc.get("watched_files", []) if f.get("status") != "unchanged"]
    st = doc.get("started_at", "")[:16].replace("T", " ")
    en = doc.get("ended_at", "")[:16].replace("T", " ")
    cs = doc.get("change_stats") or {}
    lines = [
        f"세션 제목(참고용, 내용과 다를 수 있음): {title}",
        f"수업 시간: {st} ~ {en}",
        f"변경 파일 {len(files)}개, +{cs.get('added_lines', '?')} / -{cs.get('deleted_lines', '?')}",
        *(f"- {p}" for p in files),
        "",
        "<diff>",
        diff,
        "</diff>",
    ]
    return "\n".join(lines)


def render_md(d: dict, session_id: str, model: str) -> str:
    out = [f"# {d['title']}", "", f"_세션 {session_id} · 모델 {model}_", "", "## 오늘 배운 것", "", d["summary"], ""]
    if d["pitfalls"]:
        out += ["## 꼭 기억할 함정", ""]
        for i, p in enumerate(d["pitfalls"], 1):
            out += [f"### {i}. {p['title']}", "", p["explanation"], ""]
            if p["code_example"]:
                out += ["```java", p["code_example"], "```", ""]
    groups: dict[str, list[dict]] = {}
    for k in d["keywords"]:
        groups.setdefault(k["group"], []).append(k)
    out += ["## 오늘의 키워드", ""]
    for g, items in groups.items():
        out += [f"### {g}", ""]
        for k in items:
            head = f"- **{k['term']}**" + (f" `{k['syntax']}`" if k["syntax"] else "")
            out += [head, f"  {k['concept']}"]
            if k["code_example"]:
                out += [f"  `{k['code_example'].strip()}`"]
        out += [""]
    if d["practice"]:
        out += ["## 오늘의 실습 (다시 풀어보기)", ""]
        for i, q in enumerate(d["practice"], 1):
            q_lines = q["question"].splitlines()
            out += [f"{i}. {q_lines[0]}"] + [f"   {l}" for l in q_lines[1:]]
            if q["hint"]:
                out += [f"   - 힌트: {q['hint']}"]
        out += [""]
    out += ["## 복습 질문", ""] + [f"- {q}" for q in d["review_questions"]] + [""]
    if d["notes"]:
        out += ["## 참고", ""] + [f"- {n}" for n in d["notes"]] + [""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id")
    ap.add_argument("--title", default=None)
    ap.add_argument("--model", default=None)
    a = ap.parse_args()

    root = SESSIONS / a.session_id
    doc = json.loads((root / "session.json").read_text(encoding="utf-8"))
    diff = (root / "final.diff").read_text(encoding="utf-8")
    red = json.loads((root / "redaction.json").read_text(encoding="utf-8")) if (root / "redaction.json").exists() else {}
    if red.get("secrets_found"):
        sys.exit("redaction.json 에 비밀정보 탐지 기록이 있어 원본 diff 를 보내지 않는다.")

    env = load_env()
    model = a.model or env.get("OPENAI_MODEL") or "gpt-4o"
    title = a.title or doc.get("title", "")
    system = (HERE / "prompt_v2.md").read_text(encoding="utf-8")
    user = user_prompt(doc, diff, title)

    client = OpenAI(api_key=env["OPENAI_API_KEY"], timeout=120, max_retries=2)
    t0 = datetime.now()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": {"name": "lesson_note_v2", "strict": True, "schema": schema()}},
        temperature=0.2,
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    usage = resp.usage
    out_dir = HERE / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{a.session_id}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_md(data, a.session_id, resp.model)
    (out_dir / f"{a.session_id}.md").write_text(md, encoding="utf-8")
    print(f"model={resp.model} in={usage.prompt_tokens} out={usage.completion_tokens} "
          f"elapsed={(datetime.now()-t0).seconds}s -> {out_dir / (a.session_id + '.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
