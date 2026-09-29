"""언어 공통 1층 + 언어별 2층 규칙표로 '오늘 추가된 줄'에서 문법·함수·메소드 목록을 뽑는다.

외부 의존성 없이 겉모양만 본다 (summarize.py fallback 과 같은 원칙). 정확한 파서가 아니므로
주석·문자열만 확실히 걷어내고, 나머지는 정규식과 규칙표로 판정한다.

  1층 (공통)  주석·문자열 제거 → .메소드( / 함수( / 연산자 / 직접 정의한 이름 제외
  2층 (언어)  키워드 · 구문 패턴 · 어노테이션 · 알려진 속성

사용: python -X utf8 inventory.py <세션ID> [--json]
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SESSIONS = Path(__file__).resolve().parents[2] / "sessions"
_WS = re.compile(r"\s+")


# ------------------------------------------------------------------ 언어 규칙표
@dataclass(frozen=True)
class Lang:
    name: str
    line_comments: tuple[str, ...]
    block_comments: tuple[tuple[str, str], ...]
    strings: tuple[str, ...]  # 긴 구분자부터
    template: str | None = None  # 안쪽 ${ } 를 코드로 보는 문자열 구분자
    keywords: frozenset[str] = frozenset()
    skip_keywords: frozenset[str] = frozenset()
    # (카드 이름, 정규식) — 주석·문자열을 걷어낸 줄에 건다
    constructs: tuple[tuple[str, str], ...] = ()
    operators: tuple[str, ...] = ()
    # 직접 정의한 이름을 잡는 정규식. 그룹 1 이 이름
    declarations: tuple[str, ...] = ()
    # 메소드 호출이 아니어도 카드로 만들 속성 (.length 등)
    properties: frozenset[str] = frozenset()
    # 대문자로 시작해도 사용자 타입이 아닌 내장 수신자 (Math.max 처럼 한정 이름으로 적는다)
    static_receivers: frozenset[str] = frozenset()
    annotations: bool = False
    jsx: bool = False
    block_style: str = "brace"  # brace | indent
    not_calls: frozenset[str] = frozenset()


def _kw(*words: str) -> frozenset[str]:
    return frozenset(w for ws in words for w in ws.split())


PYTHON = Lang(
    name="Python",
    line_comments=("#",),
    block_comments=(),
    strings=('"""', "'''", '"', "'"),
    keywords=_kw("if elif else for while break continue pass match case and or not in is",
                 "try except finally raise with as lambda yield global nonlocal del assert async await"),
    skip_keywords=_kw("import from def class return True False None"),
    constructs=(
        ("컴프리헨션", r"[\[{(][^\[\]{}()]*\bfor\b[^\[\]{}()]*\bin\b"),
        ("조건식(삼항)", r"\S.*\bif\b.+\belse\b"),
        ("f-string", r"(?<![\w])[fF][rR]?[\"']"),
        ("슬라이싱", r"\w[\]\)]?\[[^\]]*:[^\]]*\]"),
        ("언패킹 할당", r"^\s*\w+\s*,\s*\w+.*=(?!=)"),
        ("for-else", r"^\s*else\s*:"),  # 아래에서 앞 블록이 for/while 인지 따로 본다
    ),
    operators=("**=", "//=", ":=", "**", "//", "+=", "-=", "*=", "/=", "%=", "%", "==", "!="),
    declarations=(r"^\s*(?:async\s+)?def\s+(\w+)", r"^\s*class\s+(\w+)"),
    block_style="indent",
    not_calls=_kw("if elif while for return not and or in print_function"),
)

_JS_KEYWORDS = _kw(
    "if else for while do break continue switch case default return",
    "const let var function class extends new this super typeof instanceof in of delete void",
    "try catch finally throw async await yield import export from",
)
JAVASCRIPT = Lang(
    name="JavaScript",
    line_comments=("//",),
    block_comments=(("/*", "*/"),),
    strings=('"', "'"),
    template="`",
    keywords=_JS_KEYWORDS,
    skip_keywords=_kw("import export from return function class this void"),
    constructs=(
        ("화살표 함수", r"=>"),
        ("템플릿 리터럴", r"`"),
        ("for...of", r"\bfor\s*\(\s*(?:const|let|var)?\s*[\w\[\]{},\s]+\s+of\b"),
        ("for...in", r"\bfor\s*\(\s*(?:const|let|var)?\s*\w+\s+in\b"),
        ("구조 분해 할당", r"\b(?:const|let|var)\s*[\[{]"),
        ("전개 연산자", r"\.\.\.\w"),
        ("옵셔널 체이닝", r"\?\.(?!\d)"),
        ("삼항 연산자", r"[^?]\?[^.?:][^:]*:"),
        ("단축 평가(&&, ||)", r"(?:&&|\|\|)\s*[\w<(]"),
    ),
    operators=("===", "!==", "??=", "??", "**", "&&=", "||=", "+=", "-=", "*=", "/=", "%=", "++", "--", "%"),
    declarations=(
        r"\bfunction\s*\*?\s*(\w+)",
        r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|\w+\s*=>)",
        r"\bclass\s+(\w+)",
        r"^\s*(?:async\s+|static\s+|get\s+|set\s+)*(\w+)\s*\([^)]*\)\s*\{",  # 클래스 메소드
    ),
    properties=_kw(
        "length innerHTML innerText textContent value style classList dataset id className",
        "parentElement parentNode children firstElementChild lastElementChild nextElementSibling previousElementSibling",
        "target currentTarget key code checked disabled href src files body head title",
        "PI MAX_VALUE MIN_VALUE MAX_SAFE_INTEGER EPSILON NaN",
        "localStorage sessionStorage location history navigator",
    ),
    static_receivers=_kw(
        "Math Object Array JSON Number String Date Promise Symbol Reflect Intl Boolean",
        "document window console localStorage sessionStorage navigator location history crypto",
    ),
    not_calls=_kw("if for while switch catch function return typeof await"),
)

TYPESCRIPT = Lang(**{**JAVASCRIPT.__dict__, "name": "TypeScript",
                     "keywords": JAVASCRIPT.keywords | _kw("interface type enum implements readonly as keyof"),
                     "constructs": JAVASCRIPT.constructs + (("타입 주석", r"\w\s*:\s*[A-Z]?\w+(?:\[\])?\s*[=,)]"),
                                                          ("제네릭", r"\w<[A-Z]\w*(?:,\s*\w+)*>\(")),
                     })

REACT_CONSTRUCTS = (
    ("JSX", r"<[A-Za-z][\w.]*(?:\s|>|/>)"),
    ("JSX 표현식 { }", r">[^<]*\{[^}]+\}"),
    ("JSX 이벤트 속성", r"\bon[A-Z]\w*\s*=\s*\{"),
    ("JSX className", r"\bclassName\s*="),
    ("리스트 렌더링 (map + key)", r"\bkey\s*=\s*\{"),
    ("조건부 렌더링 (&&)", r"&&\s*\(?\s*<"),
    ("조건부 렌더링 (삼항)", r"\?\s*\(?\s*<[^:]*:"),
    ("props 구조 분해", r"function\s+[A-Z]\w*\s*\(\s*\{"),
    ("Fragment", r"<>|</>"),
)
JSX = Lang(**{**JAVASCRIPT.__dict__, "name": "JavaScript (JSX)", "jsx": True,
              "constructs": JAVASCRIPT.constructs + REACT_CONSTRUCTS})
TSX = Lang(**{**TYPESCRIPT.__dict__, "name": "TypeScript (TSX)", "jsx": True,
              "constructs": TYPESCRIPT.constructs + REACT_CONSTRUCTS})

JAVA = Lang(
    name="Java",
    line_comments=("//",),
    block_comments=(("/*", "*/"),),
    strings=('"""', '"', "'"),
    keywords=_kw(
        "if else for while do break continue switch case default return",
        "new this super extends implements instanceof static final abstract interface enum record",
        "try catch finally throw throws synchronized var yield",
        "public private protected",
    ),
    skip_keywords=_kw("import package return class public private protected void"),
    constructs=(
        ("향상된 for 문", r"\bfor\s*\([^;:]+:[^:)]+\)"),
        ("람다식", r"\([^()]*\)\s*->|\b\w+\s*->"),
        ("메소드 참조", r"\w::\w"),
        ("제네릭", r"\b[A-Z]\w*<[\w<>,?\s\[\]]*>"),
        ("switch 화살표", r"\bcase\b[^:]*->"),
        ("try-with-resources", r"\btry\s*\("),
        ("삼항 연산자", r"\?[^:;]+:"),
        ("배열 선언", r"\b\w+\s*\[\]\s*\w+\s*=|new\s+\w+\s*\[\d*\]"),
        ("익명 클래스", r"new\s+[A-Z]\w*(?:<[^>]*>)?\s*\([^)]*\)\s*\{"),
    ),
    operators=("==", "!=", "&&", "||", "+=", "-=", "*=", "/=", "%=", "++", "--", "%", "instanceof"),
    declarations=(
        r"\b(?:class|interface|enum|record)\s+(\w+)",
        r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|default)\s+)*"
        r"(?:<[^>]+>\s+)?[\w<>\[\],.?]+\s+(\w+)\s*\([^;]*\)\s*(?:throws\s+[\w.,\s]+)?(?:\{.*)?$",
        r"^\s*(?:(?:public|private|protected)\s+)?([A-Z]\w*)\s*\([^;]*\)\s*(?:throws\s+[\w.,\s]+)?(?:\{.*)?$",
    ),
    properties=_kw("length out err in"),
    static_receivers=_kw(
        "Math System Arrays Collections List Set Map Objects Optional String Integer Double Long Boolean",
        "Character Stream Collectors LocalDate LocalDateTime Duration Files Paths Path Thread Executors",
        "ResponseEntity HttpStatus",
    ),
    annotations=True,
    not_calls=_kw("if for while switch catch synchronized return new super this"),
)

BY_EXT = {".py": PYTHON, ".js": JAVASCRIPT, ".mjs": JAVASCRIPT, ".cjs": JAVASCRIPT,
          ".ts": TYPESCRIPT, ".jsx": JSX, ".tsx": TSX, ".java": JAVA, ".html": JAVASCRIPT, ".htm": JAVASCRIPT}


# ------------------------------------------------------------------ 1층: 주석·문자열 제거
def strip_code(text: str, lang: Lang) -> list[str]:
    """주석과 문자열 내용을 공백으로 바꾼다. 줄 수·열 위치를 보존한다. 따옴표 자체는 남긴다."""
    out = []
    i, n = 0, len(text)
    buf: list[str] = []
    mode: tuple[str, str] | None = None  # (kind, closer)
    brace_depth: list[int] = []  # 템플릿 ${ } 중첩
    while i < n:
        ch = text[i]
        if ch == "\n":
            # 줄 주석과 한 줄짜리 문자열은 줄 끝에서 닫는다 (따옴표 짝이 틀려도 다음 줄로 번지지 않게)
            if mode and (mode[0] == "line" or (mode[0] == "str" and len(mode[1]) == 1)):
                mode = None
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        if mode is None:
            if brace_depth and ch == "}" and brace_depth[-1] == 0:
                brace_depth.pop()
                buf.append(ch)
                mode = ("tmpl", lang.template or "`")
                i += 1
                continue
            if brace_depth:
                if ch == "{":
                    brace_depth[-1] += 1
                elif ch == "}":
                    brace_depth[-1] -= 1
            matched = False
            for lc in lang.line_comments:
                if text.startswith(lc, i):
                    mode = ("line", "\n")
                    buf.append(" " * len(lc))
                    i += len(lc)
                    matched = True
                    break
            if matched:
                continue
            for op, cl in lang.block_comments:
                if text.startswith(op, i):
                    mode = ("block", cl)
                    buf.append(" " * len(op))
                    i += len(op)
                    matched = True
                    break
            if matched:
                continue
            if lang.template and text.startswith(lang.template, i):
                mode = ("tmpl", lang.template)
                buf.append(ch)
                i += 1
                continue
            for q in lang.strings:
                if text.startswith(q, i):
                    mode = ("str", q)
                    buf.append(q)
                    i += len(q)
                    matched = True
                    break
            if matched:
                continue
            buf.append(ch)
            i += 1
            continue
        kind, closer = mode
        if kind in ("str", "tmpl") and ch == "\\":
            buf.append("  " if i + 1 < n and text[i + 1] != "\n" else " ")
            i += 2 if i + 1 < n and text[i + 1] != "\n" else 1
            continue
        if kind == "tmpl" and text.startswith("${", i):
            buf.append("${")
            i += 2
            brace_depth.append(0)
            mode = None
            continue
        if text.startswith(closer, i) and kind != "line":
            buf.append(closer if kind in ("str", "tmpl") else " " * len(closer))
            i += len(closer)
            mode = None
            continue
        buf.append(" ")
        i += 1
        # 한 줄 문자열이 줄바꿈 없이 닫히지 않으면 줄 끝에서 끊는다 (따옴표 짝 오류 방어)
    out.append("".join(buf))
    return out


def script_rows(text: str) -> set[int]:
    """HTML 에서 <script> 안쪽 행만 JS 로 본다 (src 가 있는 script 는 제외)."""
    rows: set[int] = set()
    inside = False
    for r, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        if not inside and re.search(r"<script(?![^>]*\bsrc=)[^>]*>", low):
            inside = True
            after = low.split(">", 1)[1] if ">" in low else ""
            if "</script" in after:
                inside = False
            continue
        if inside and "</script" in low:
            inside = False
            continue
        if inside:
            rows.add(r)
    return rows


# ------------------------------------------------------------------ 행 단위 추출
@dataclass
class Item:
    id: str
    name: str
    group: str
    where: str
    example: str
    variants: list[str] = field(default_factory=list)
    context: str = ""  # 메소드 수신자의 선언·대입 줄


def added_rows(before: str, after: str) -> set[int]:
    a = [_WS.sub("", x) for x in before.splitlines()]
    b = [_WS.sub("", x) for x in after.splitlines()]
    rows: set[int] = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            rows.update(r + 1 for r in range(j1, j2) if b[r])
    return rows


def user_names(clean: list[str], lang: Lang) -> set[str]:
    names: set[str] = set()
    for line in clean:
        for pat in lang.declarations:
            for m in re.finditer(pat, line):
                names.add(m.group(1))
        if lang.jsx:  # <MyComponent 처럼 쓰인 사용자 컴포넌트
            names.update(re.findall(r"<([A-Z]\w*)", line))
        if lang is not PYTHON and lang is not JAVA:
            # 매개변수와 구조 분해로 생긴 이름 (callback(), onToggle(), setTodos() 처럼 호출된다)
            lists = re.findall(r"\bfunction\s*\w*\s*\(([^)]*)\)|\(([^()]*)\)\s*(?::\s*[^=]+)?=>", line)
            lists += [(m, "") for m in re.findall(r"\b(?:const|let|var)\s*([\[{][^=]*)=", line)]
            for plist in lists:
                text = re.sub(r":\s*[^,{}()\[\]]+", "", " ".join(plist))
                names.update(re.findall(r"[A-Za-z_$][\w$]*", text))
            names.update(re.findall(r"([A-Za-z_$][\w$]*)\s*=>", line))
    return names - lang.not_calls - lang.keywords


_IDENT = r"[A-Za-z_$][\w$]*"


def row_items(line: str, lang: Lang, users: set[str]) -> list[tuple[str, str, str | None]]:
    """(그룹, 이름, 수신자) 목록."""
    found: list[tuple[str, str, str | None]] = []
    stripped = line.strip()
    if lang.name.startswith(("JavaScript", "TypeScript", "Java")) and re.match(r"(import|package)\b", stripped):
        return found
    if lang is PYTHON and re.match(r"(import|from)\b", stripped):
        return found
    # 어노테이션 (Spring)
    if lang.annotations:
        for m in re.finditer(r"(?<![\w.])@([A-Z]\w*)", line):
            found.append(("어노테이션", f"@{m.group(1)}", None))
    # 메소드 호출
    for m in re.finditer(rf"({_IDENT}(?:\s*\.\s*{_IDENT})*|[\)\]])?\s*(\.|::)\s*({_IDENT})\s*(\()?", line):
        chain, sep, name, call = m.group(1), m.group(2), m.group(3), m.group(4)
        chain = re.sub(r"\s+", "", chain) if chain else None
        recv = chain.split(".")[0] if chain else None
        if name in users and (recv in (None, "this", "self") or not call):
            continue
        maybe_user = name in users
        if sep == "::":
            found.append(("메소드 참조", f"{chain}::{name}" if chain and chain[:1].isupper() else f"::{name}", recv))
            continue
        qualified = chain if recv and recv in lang.static_receivers else None
        if call:
            label = f"{qualified}.{name}()" if qualified else f".{name}()"
            found.append(("메소드?" if maybe_user and not qualified else "메소드", label, recv))
        elif name in lang.properties or (qualified and name[:1].isupper()):
            label = f"{qualified}.{name}" if qualified else f".{name}"
            found.append(("속성", label, recv))
    # 생성자 / 함수 호출
    for m in re.finditer(rf"(?<![\w$.@])(new\s+)?({_IDENT})\s*(?:<[\w\s,<>?\[\].]*>)?\s*\(", line):
        is_new, name = m.group(1), m.group(2)
        if name in lang.not_calls or name in lang.keywords or name in lang.skip_keywords:
            continue
        if is_new:
            if name not in users:
                found.append(("생성자", f"new {name}()", None))
            continue
        if name in users:
            continue
        if lang is JAVA and re.match(rf"^\s*(?:[\w<>\[\],.?]+\s+)+{re.escape(name)}\s*\(", line):
            continue  # 메소드 선언 줄
        found.append(("함수", f"{name}()", None))
    # 키워드
    for m in re.finditer(r"(?<![\w$.])([a-z]\w*)(?![\w$])", line):
        w = m.group(1)
        if w == "default" and re.search(r"\bexport\s+$", line[: m.start()]):
            continue
        if w in lang.keywords and w not in lang.skip_keywords:
            found.append(("키워드", w, None))
    # 구문
    for cname, pat in lang.constructs:
        if re.search(pat, line):
            found.append(("문법", cname, None))
    # 연산자 (긴 것부터, 겹치면 한 번만)
    rest = line
    for op in sorted(lang.operators, key=len, reverse=True):
        pat = rf"(?<![\w]){op}(?![\w])" if op.isalpha() else re.escape(op)
        if re.search(pat, rest):
            found.append(("연산자", op, None))
            rest = re.sub(pat, " ", rest)
    return found


def block_at(lines: list[str], row: int, style: str, max_lines: int = 8) -> str:
    head = lines[row - 1]
    out = [head.rstrip()]
    if style == "indent":
        if head.split("#")[0].rstrip().endswith(":"):
            indent = len(head) - len(head.lstrip())
            for nxt in lines[row:]:
                if not nxt.strip() or nxt.lstrip().startswith("#"):
                    continue
                if len(nxt) - len(nxt.lstrip()) <= indent and not re.match(r"\s*(elif|else|except|finally|case)\b", nxt):
                    break
                out.append(nxt.rstrip())
                if len(out) >= max_lines:
                    break
    else:
        depth = head.count("{") - head.count("}")
        if depth > 0:
            for nxt in lines[row:]:
                out.append(nxt.rstrip())
                depth += nxt.count("{") - nxt.count("}")
                if depth <= 0 or len(out) >= max_lines:
                    break
            if depth > 0:
                out.append("    ...")
    base = min((len(x) - len(x.lstrip()) for x in out if x.strip()), default=0)
    return "\n".join(x[base:] for x in out)


def receiver_context(raw: list[str], clean: list[str], row: int, recv: str | None) -> str:
    if not recv or not re.fullmatch(_IDENT, recv) or recv in ("this", "self"):
        return ""
    decl = re.compile(
        rf"(?:\b(?:const|let|var)\s+{re.escape(recv)}\b)"
        rf"|(?:^\s*(?:final\s+)?[\w<>\[\],.?]+\s+{re.escape(recv)}\s*[=;:])"
        rf"|(?:(?<![\w.$]){re.escape(recv)}\s*=(?!=))"
        rf"|(?:\bfor\b.*\b{re.escape(recv)}\b)"
        rf"|(?:\(\s*[\w<>\[\]]+\s+{re.escape(recv)}\s*[,)])"
    )
    for r in range(row, 0, -1):
        if decl.search(clean[r - 1]):
            text = raw[r - 1].strip()
            return text if r != row else (text if "=" in clean[r - 1] else "")
    return ""


def language_of(rel: str) -> Lang | None:
    return BY_EXT.get(Path(rel).suffix.lower())


def _shape(text: str) -> str:
    return re.sub(r"\w+|\"[^\"]*\"|'[^']*'", "x", text)


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def session_users(root: Path) -> dict[str, set[str]]:
    """감시 폴더 전체(final 스냅샷)에서 언어별로 직접 정의한 이름을 모은다. 다른 파일에서 정의한 클래스·함수도 실습이다."""
    by_lang: dict[str, set[str]] = {}
    modules: set[str] = set()
    files = [p for p in (root / "final").rglob("*") if p.is_file() and language_of(p.name)]
    for p in files:
        modules.add(p.stem)
    for p in files:
        lang = language_of(p.name)
        text = read_text(p)
        if lang is None or text is None:
            continue
        clean = strip_code(text, lang)
        names = by_lang.setdefault(lang.name, set())
        names |= user_names(clean, lang)
        if lang is PYTHON:  # from seperator import seperator 처럼 로컬 모듈에서 가져온 이름
            for line in clean:
                m = re.match(r"\s*from\s+([\w.]+)\s+import\s+(.+)", line)
                if m and m.group(1).split(".")[-1] in modules:
                    names.update(re.findall(r"\w+", m.group(2).split(" as ")[-1]))
    return by_lang


def build(root: Path) -> tuple[list[Item], dict[str, int]]:
    stats = json.loads((root / "stats.json").read_text(encoding="utf-8"))
    all_users = session_users(root)
    items: dict[tuple[str, str], Item] = {}
    langs: dict[str, int] = {}
    for f in stats["files"]:
        rel = f["path"]
        lang = language_of(rel)
        if f["status"] not in ("added", "modified") or lang is None:
            continue
        before = read_text(root / "baseline" / rel) if (root / "baseline" / rel).is_file() else ""
        after = read_text(root / "final" / rel)
        if after is None or before is None:
            continue
        rows = added_rows(before, after)
        if rel.lower().endswith((".html", ".htm")):
            rows &= script_rows(after)
        if not rows:
            continue
        langs[lang.name] = langs.get(lang.name, 0) + len(rows)
        raw = after.splitlines()
        clean = strip_code(after, lang)
        users = user_names(clean, lang) | all_users.get(lang.name, set())
        for r in sorted(rows):
            if r > len(clean):
                continue
            for group, name, recv in row_items(clean[r - 1], lang, users):
                if name == "for-else" and not _is_loop_else(clean, r):
                    continue
                key = (_family(lang), name)
                item = items.get(key)
                if item is None:
                    item = Item(id=f"i{len(items) + 1}", name=name, group=group, where=f"{rel}:{r}",
                                example=block_at(raw, r, lang.block_style),
                                context=receiver_context(raw, clean, r, recv))
                    items[key] = item
                text = raw[r - 1].strip()
                if len(item.variants) < 4 and _shape(text) not in {_shape(v) for v in item.variants}:
                    item.variants.append(text)
                    if not item.context and recv:
                        item.context = receiver_context(raw, clean, r, recv)
    return list(items.values()), langs


def _family(lang: Lang) -> str:
    return "JS" if lang.template else lang.name


def _is_loop_else(clean: list[str], row: int) -> bool:
    indent = len(clean[row - 1]) - len(clean[row - 1].lstrip())
    for r in range(row - 1, 0, -1):
        line = clean[r - 1]
        if not line.strip():
            continue
        ind = len(line) - len(line.lstrip())
        if ind < indent:
            return False
        if ind == indent:
            if re.match(r"\s*(for|while)\b", line):
                return True
            if re.match(r"\s*(if|elif|try|except)\b", line):
                return False
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    items, langs = build(SESSIONS / a.session_id)
    if a.json:
        print(json.dumps([it.__dict__ for it in items], ensure_ascii=False, indent=1))
        return 0
    print(f"{a.session_id} langs={langs} items={len(items)}")
    groups: dict[str, list[str]] = {}
    for it in items:
        groups.setdefault(it.group, []).append(it.name)
    for g, names in groups.items():
        print(f"  {g} ({len(names)}): {', '.join(names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
