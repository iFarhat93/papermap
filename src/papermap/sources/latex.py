"""Parse a LaTeX source tree (e.g. an arXiv e-print) into sections, exact
equations, structured tables, figures (with their image files) and references.

The output is plain readable text with math kept as LaTeX (`$...$`, `$$...$$`),
references and citations resolved to readable form, tables rendered as
Markdown-style blocks and floats replaced by short markers. It is defensive:
unknown macros are dropped rather than failing the parse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_EXTS = (".pdf", ".png", ".jpg", ".jpeg")
FLOAT_ENVS = ("figure*", "figure", "wrapfigure", "table*", "table", "wraptable", "SCfigure")
DISPLAY_MATH_ENVS = ("equation*", "equation", "align*", "align", "gather*", "gather", "multline*", "multline",
                     "eqnarray*", "eqnarray", "displaymath", "flalign*", "flalign")
KEEP_ARG_CMDS = ("textbf", "textit", "emph", "texttt", "textsc", "textrm", "textsf", "underline", "mbox", "text",
                 "textup", "textnormal", "textmd", "uline", "hbox", "fbox", "makebox", "framebox", "textsl")
DROP_ARG_CMDS = {"label": 1, "vspace": 1, "hspace": 1, "vskip": 0, "bibliographystyle": 1, "bibliography": 1,
                 "setlength": 2, "addtolength": 2, "color": 1, "pagestyle": 1, "thispagestyle": 1, "newpage": 0,
                 "includegraphics": 1, "addcontentsline": 3, "phantomsection": 0, "linespread": 1, "nocite": 1,
                 "footnotetext": 1, "captionsetup": 1, "setcounter": 2, "addtocounter": 2, "hypersetup": 1}
ACCENTS = {
    "'": {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú", "c": "ć", "n": "ń", "s": "ś", "y": "ý", "E": "É", "A": "Á", "O": "Ó"},
    "`": {"a": "à", "e": "è", "i": "ì", "o": "ò", "u": "ù", "E": "È", "A": "À"},
    '"': {"a": "ä", "e": "ë", "i": "ï", "o": "ö", "u": "ü", "A": "Ä", "O": "Ö", "U": "Ü"},
    "^": {"a": "â", "e": "ê", "i": "î", "o": "ô", "u": "û"},
    "~": {"n": "ñ", "a": "ã", "o": "õ", "N": "Ñ"},
    "c": {"c": "ç", "C": "Ç", "s": "ş"},
    "v": {"s": "š", "c": "č", "z": "ž", "r": "ř", "S": "Š", "C": "Č", "Z": "Ž"},
}


@dataclass
class LatexSection:
    heading: str
    level: int
    text: str
    number: str = ""


@dataclass
class LatexFigure:
    number: int
    caption: str
    files: list[Path]
    label: str = ""
    section_index: int = -1
    sub_captions: list[str] = field(default_factory=list)


@dataclass
class LatexTable:
    number: int
    caption: str
    columns: list[str]
    rows: list[list[str]]
    label: str = ""
    section_index: int = -1


@dataclass
class LatexPaper:
    title: str
    abstract: str
    sections: list[LatexSection]
    figures: list[LatexFigure]
    tables: list[LatexTable]
    references: list[str]
    main_file: Path


# --------------------------------------------------------------- low level


def read_group(s: str, i: int, open_ch: str = "{", close_ch: str = "}") -> tuple[str, int]:
    """s[i] must be `open_ch`; return (content, index after the matching close)."""
    assert s[i] == open_ch
    depth = 0
    j = i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return s[i + 1 : j], j + 1
        j += 1
    return s[i + 1 :], len(s)


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\n":
        i += 1
    return i


def read_args(s: str, i: int, n: int, optional: bool = True) -> tuple[list[str], int]:
    """Read up to n `{...}` arguments (skipping `[...]` options) starting at i."""
    args: list[str] = []
    while len(args) < n:
        j = _skip_ws(s, i)
        if optional and j < len(s) and s[j] == "[":
            _, j = read_group(s, j, "[", "]")
            i = j
            continue
        if j < len(s) and s[j] == "{":
            content, j = read_group(s, j)
            args.append(content)
            i = j
        else:
            break
    return args, i


def strip_comments(tex: str) -> str:
    out = []
    for line in tex.split("\n"):
        m = re.search(r"(?<!\\)%", line)
        out.append(line[: m.start()] if m else line)
    return "\n".join(out)


def find_main_file(root: Path) -> Path | None:
    candidates = []
    for p in root.rglob("*.tex"):
        try:
            t = p.read_text("utf-8", errors="replace")
        except OSError:
            continue
        if "\\documentclass" in t and "\\begin{document}" in t:
            candidates.append((len(t), p))
    if not candidates:
        return None
    return max(candidates)[1]


def expand_inputs(tex: str, base: Path, depth: int = 0) -> str:
    if depth > 6:
        return tex

    def repl(m: re.Match) -> str:
        name = m.group(2).strip()
        p = base / name
        for cand in (p, p.with_suffix(".tex"), Path(str(p) + ".tex")):
            if cand.is_file():
                try:
                    return expand_inputs(strip_comments(cand.read_text("utf-8", errors="replace")), base, depth + 1)
                except OSError:
                    break
        return ""

    return re.sub(r"\\(input|include|subfile)\s*\{([^}]+)\}", repl, tex)


def find_env(s: str, name: str, start: int = 0) -> tuple[int, int, int, int] | None:
    """Locate \\begin{name}...\\end{name} (nesting-aware). Returns
    (begin_start, body_start, body_end, end_end) or None."""
    b = s.find(f"\\begin{{{name}}}", start)
    if b == -1:
        return None
    body = b + len(f"\\begin{{{name}}}")
    depth = 1
    i = body
    pat = re.compile(r"\\(begin|end)\{" + re.escape(name) + r"\}")
    while True:
        m = pat.search(s, i)
        if not m:
            return b, body, len(s), len(s)
        depth += 1 if m.group(1) == "begin" else -1
        if depth == 0:
            return b, body, m.start(), m.end()
        i = m.end()


# ------------------------------------------------------------ bibliography


def parse_bbl(bbl: str) -> tuple[dict[str, str], list[str]]:
    """key -> short label ("Black et al., 2024"), plus readable reference entries."""
    labels: dict[str, str] = {}
    refs: list[str] = []
    parts = re.split(r"\\bibitem", bbl)[1:]
    for n, part in enumerate(parts, start=1):
        label = ""
        i = _skip_ws(part, 0)
        if i < len(part) and part[i] == "[":
            label, i = read_group(part, i, "[", "]")
        i = _skip_ws(part, i)
        key = ""
        if i < len(part) and part[i] == "{":
            key, i = read_group(part, i)
        body = part[i:].split("\\end{thebibliography}")[0]
        entry = re.sub(r"\s+", " ", clean_text(body.replace("\\newblock", " "), {}, {})).strip()
        refs.append(entry)
        short = str(n)
        if label:
            lab = clean_text(label, {}, {}).replace("\n", " ")
            m = re.match(r"\s*(.*?)\s*\((\d{4}[a-z]?)\)", lab)
            short = f"{m.group(1)}, {m.group(2)}" if m else lab.split(")")[0]
        labels[key.strip()] = short
    return labels, refs


# ---------------------------------------------------------- text cleaning


def _accent(m: re.Match) -> str:
    acc, letter = m.group(1), m.group(2)
    return ACCENTS.get(acc, {}).get(letter, letter)


def _protect_math(s: str) -> tuple[str, list[str]]:
    keep: list[str] = []

    def stash(m: re.Match) -> str:
        keep.append(m.group(0))
        return f"\x00M{len(keep) - 1}\x00"

    def stash_inline(m: re.Match) -> str:  # \( x \) -> $x$
        keep.append(f"${m.group(1)}$")
        return f"\x00M{len(keep) - 1}\x00"

    s = re.sub(r"\$\$.+?\$\$", stash, s, flags=re.S)
    s = re.sub(r"(?<!\\)\$(?:\\\$|[^$])+?(?<!\\)\$", stash, s, flags=re.S)
    s = re.sub(r"\\\((.+?)\\\)", stash_inline, s, flags=re.S)
    return s, keep


def _restore_math(s: str, keep: list[str]) -> str:
    return re.sub(r"\x00M(\d+)\x00", lambda m: keep[int(m.group(1))], s)


def _replace_cmd(s: str, name: str, fn) -> str:
    """Replace every \\name{arg1}...{argN} via fn(args) (balanced braces)."""
    pat = re.compile(r"\\" + re.escape(name) + r"\*?(?![A-Za-z])")
    out = []
    i = 0
    while True:
        m = pat.search(s, i)
        if not m:
            out.append(s[i:])
            break
        out.append(s[i : m.start()])
        repl, end = fn(s, m.end())
        out.append(repl)
        i = end
    return "".join(out)


def clean_text(s: str, refs: dict[str, str], cites: dict[str, str]) -> str:
    """LaTeX -> readable text, keeping math."""
    s, math = _protect_math(s)

    # references and citations
    def cite_fn(kind: str):
        def fn(src: str, i: int):
            args, j = read_args(src, i, 1)
            keys = [k.strip() for k in (args[0] if args else "").split(",") if k.strip()]
            names = [cites.get(k, k) for k in keys]
            if kind == "citet" and names:
                parts = []
                for nm in names:
                    m = re.match(r"(.*), (\d{4}[a-z]?)$", nm)
                    parts.append(f"{m.group(1)} ({m.group(2)})" if m else nm)
                return " and ".join(parts), j
            if kind in ("citeauthor",):
                return ", ".join(re.sub(r", \d{4}[a-z]?$", "", nm) for nm in names), j
            if kind == "citeyear":
                return ", ".join((re.search(r"\d{4}[a-z]?$", nm) or re.match(".*", nm)).group(0) for nm in names), j
            return ("[" + "; ".join(names) + "]") if names else "", j

        return fn

    for name in ("citep", "citealp", "parencite", "cite", "autocite"):
        s = _replace_cmd(s, name, cite_fn("cite"))
    for name in ("citet", "textcite"):
        s = _replace_cmd(s, name, cite_fn("citet"))
    s = _replace_cmd(s, "citeauthor", cite_fn("citeauthor"))
    s = _replace_cmd(s, "citeyear", cite_fn("citeyear"))

    def ref_fn(prefixed: bool, paren: bool = False):
        def fn(src: str, i: int):
            args, j = read_args(src, i, 1, optional=False)
            out = []
            for key in (args[0] if args else "").split(","):
                num, kind = refs.get(key.strip(), ("?", ""))
                txt = f"{kind} {num}".strip() if prefixed and kind else num
                out.append(f"({txt})" if paren else txt)
            return ", ".join(out), j

        return fn

    for name in ("autoref", "cref", "Cref", "nameref"):
        s = _replace_cmd(s, name, ref_fn(True))
    s = _replace_cmd(s, "eqref", ref_fn(False, paren=True))
    s = _replace_cmd(s, "ref", ref_fn(False))

    # structure-ish commands
    s = _replace_cmd(s, "href", lambda src, i: (lambda a, j: (a[1] if len(a) > 1 else (a[0] if a else ""), j))(*read_args(src, i, 2)))
    s = _replace_cmd(s, "url", lambda src, i: (lambda a, j: (a[0] if a else "", j))(*read_args(src, i, 1)))
    s = _replace_cmd(s, "footnote", lambda src, i: (lambda a, j: (f" ({a[0]})" if a else "", j))(*read_args(src, i, 1)))
    for name in ("resizebox",):
        s = _replace_cmd(s, name, lambda src, i: (lambda a, j: (a[-1] if a else "", j))(*read_args(src, i, 3)))
    for name in ("scalebox", "raisebox", "textcolor", "colorbox", "adjustbox"):
        s = _replace_cmd(s, name, lambda src, i: (lambda a, j: (a[-1] if a else "", j))(*read_args(src, i, 2)))
    for name, n in DROP_ARG_CMDS.items():
        s = _replace_cmd(s, name, lambda src, i, n=n: ("", read_args(src, i, n)[1]))
    for name in KEEP_ARG_CMDS:
        s = _replace_cmd(s, name, lambda src, i: (lambda a, j: (a[0] if a else "", j))(*read_args(src, i, 1)))
    s = re.sub(r"\\paragraph\*?\{([^}]*)\}", r"\n\n\1 ", s)
    s = re.sub(r"\\item\s*\[([^\]]*)\]", r"\n- \1: ", s)
    s = re.sub(r"\\item\b", "\n- ", s)
    s = re.sub(r"\\begin\{(itemize|enumerate|description)\}(\[[^\]]*\])?", "\n", s)
    s = re.sub(r"\\end\{(itemize|enumerate|description)\}", "\n", s)
    s = re.sub(r"\\begin\{minipage\}(\[[^\]]*\])?\{[^}]*\}", "", s)
    s = re.sub(r"\\begin\{subfigure\}(\[[^\]]*\])?\{[^}]*\}", "", s)
    s = re.sub(r"\\(begin|end)\{[^}]*\}", "\n", s)
    s = re.sub(r"\\looseness\s*=\s*-?\d+", "", s)
    s = s.replace("\\ding{51}", "✓").replace("\\ding{55}", "✗")

    # characters
    s = re.sub(r"\\([`'\"^~cv])\{?\\?([A-Za-z])\}?", _accent, s)
    for a, b in (("---", "—"), ("--", "–"), ("``", "“"), ("''", "”"), ("\\ldots", "…"), ("\\dots", "…"),
                 ("\\%", "%"), ("\\&", "&"), ("\\_", "_"), ("\\#", "#"), ("\\$", "$"), ("\\{", "{"), ("\\}", "}"),
                 ("\\textendash", "–"), ("\\textemdash", "—"), ("\\@", ""), ("\\,", " "), ("\\;", " "),
                 ("\\!", ""), ("\\ ", " "), ("\\/", "")):
        s = s.replace(a, b)
    s = s.replace("\\\\", "\n")
    s = s.replace("~", " ")
    s = re.sub(r"\\[A-Za-z@]+\*?(\[[^\]]*\])?", "", s)  # remaining commands
    s = s.replace("{", "").replace("}", "")
    s = _restore_math(s, math)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _join_paragraphs(s: str) -> str:
    """Single newlines inside a paragraph -> spaces (LaTeX semantics)."""
    paras = re.split(r"\n\s*\n", s)
    out = []
    for p in paras:
        lines = [ln.strip() for ln in p.split("\n") if ln.strip()]
        if not lines:
            continue
        if any(ln.startswith(("- ", "| ", "$$", "[Figure", "[Table")) for ln in lines):
            out.append("\n".join(lines))
        else:
            out.append(" ".join(lines))
    return "\n\n".join(out)


# ----------------------------------------------------------------- tables


def parse_tabular(body: str, cites: dict[str, str]) -> list[list[str]]:
    rows: list[list[str]] = []
    body = re.sub(r"\\(toprule|midrule|bottomrule|hline|endhead|endfoot|endfirsthead)\b(\[[^\]]*\])?", "", body)
    body = re.sub(r"\\(cmidrule|cline)(\([^)]*\))?\{[^}]*\}", "", body)
    body = re.sub(r"\\(rowcolor|cellcolor)(\[[^\]]*\])?\{[^}]*\}", "", body)
    for raw in re.split(r"\\\\(?:\[[^\]]*\])?", body):
        raw = raw.strip()
        if not raw:
            continue
        cells: list[str] = []
        for cell in re.split(r"(?<!\\)&", raw):
            m = re.match(r"\s*\\multicolumn\{(\d+)\}\{[^}]*\}\{(.*)\}\s*$", cell, re.S)
            span = 1
            if m:
                span, cell = int(m.group(1)), m.group(2)
            m2 = re.match(r"\s*\\multirow\{[^}]*\}\{[^}]*\}\{(.*)\}\s*$", cell, re.S)
            if m2:
                cell = m2.group(1)
            # citations inside table cells are noise for charts: drop them
            cell = re.sub(r"\\cite[a-z]*\*?(\[[^\]]*\])?\{[^}]*\}", "", cell)
            text = clean_text(cell, {}, cites).replace("\n", " ").strip()
            cells.append(text)
            cells.extend([""] * (span - 1))
        if any(c for c in cells):
            rows.append(cells)
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    return [r + [""] * (width - len(r)) for r in rows]


def table_markdown(number: int, caption: str, rows: list[list[str]]) -> str:
    lines = [f"[Table {number}: {caption}]"]
    for k, r in enumerate(rows):
        lines.append("| " + " | ".join(c.replace("|", "/") for c in r) + " |")
        if k == 0:
            lines.append("|" + "---|" * len(r))
    return "\n".join(lines)


# ------------------------------------------------------------------ parse


SECTION_RE = re.compile(r"\\(section|subsection|subsubsection)(\*?)\s*(\[[^\]]*\])?\s*\{")


def _resolve_image(base: Path, name: str) -> Path | None:
    p = base / name.strip()
    if p.suffix.lower() in IMAGE_EXTS and p.is_file():
        return p
    for ext in IMAGE_EXTS:
        cand = Path(str(p) + ext)
        if cand.is_file():
            return cand
    return None


def parse_latex(root: Path) -> LatexPaper | None:
    main = find_main_file(root)
    if main is None:
        return None
    base = main.parent
    tex = strip_comments(main.read_text("utf-8", errors="replace"))
    tex = expand_inputs(tex, base)

    # zero-argument macros (\newcommand{\foo}{bar}, \def\foo{bar})
    macros: dict[str, str] = {}
    for m in re.finditer(r"\\(?:re)?newcommand\*?\s*\{?\\([A-Za-z]+)\}?\s*\{", tex):
        body, _ = read_group(tex, m.end() - 1)
        macros[m.group(1)] = body
    for m in re.finditer(r"\\def\\([A-Za-z]+)\s*\{", tex):
        body, _ = read_group(tex, m.end() - 1)
        macros.setdefault(m.group(1), body)

    def expand_macros(text: str) -> str:
        for name, val in sorted(macros.items(), key=lambda kv: -len(kv[0])):
            if "#" not in val and len(val) < 200:
                text = re.sub(r"\\" + name + r"(?![A-Za-z])(\{\})?", lambda _m, v=val: v, text)
        return text

    title_m = re.search(r"\\title\s*(\[[^\]]*\])?\s*\{", tex)
    title = ""
    if title_m:
        raw_title, _ = read_group(tex, title_m.end() - 1)
        title = re.sub(r"\s+", " ", clean_text(expand_macros(raw_title), {}, {})).strip()

    doc = find_env(tex, "document")
    body = expand_macros(tex[doc[1] : doc[2]] if doc else tex)

    # bibliography
    cites: dict[str, str] = {}
    references: list[str] = []
    bbls = sorted(base.glob("*.bbl")) or sorted(root.rglob("*.bbl"))
    bib_inline = find_env(body, "thebibliography")
    if bib_inline:
        cites, references = parse_bbl(body[bib_inline[0] : bib_inline[3]])
        body = body[: bib_inline[0]] + body[bib_inline[3] :]
    elif bbls:
        cites, references = parse_bbl(bbls[0].read_text("utf-8", errors="replace"))

    abstract = ""
    ab = find_env(body, "abstract")
    if ab:
        abstract_raw = body[ab[1] : ab[2]]
        body = body[: ab[0]] + body[ab[3] :]
    else:
        abstract_raw = ""

    # ---- pass 1: number sections, floats and equations so \ref can resolve
    refs: dict[str, tuple[str, str]] = {}
    appendix_at = body.find("\\appendix")
    counters = [0, 0, 0]
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    in_appendix = False
    heads: list[tuple[int, int, str, int, str]] = []  # (start, after, title, level, number)
    for m in SECTION_RE.finditer(body):
        level = {"section": 1, "subsection": 2, "subsubsection": 3}[m.group(1)]
        title_raw, after = read_group(body, m.end() - 1)
        if appendix_at != -1 and m.start() > appendix_at and not in_appendix:
            in_appendix = True
            counters = [0, 0, 0]  # appendix sections restart at A
        number = ""
        if not m.group(2):  # unstarred: numbered
            counters[level - 1] += 1
            for k in range(level, 3):
                counters[k] = 0
            head = letters[(counters[0] - 1) % 26] if in_appendix else str(counters[0])
            number = ".".join([head] + [str(c) for c in counters[1:level]])
        lab = re.match(r"\s*\\label\{([^}]+)\}", body[after:])
        if lab:
            refs[lab.group(1)] = (number, "Appendix" if in_appendix else "Section")
        heads.append((m.start(), after, title_raw, level, number))

    # floats
    figures: list[LatexFigure] = []
    tables: list[LatexTable] = []
    float_spans: list[tuple[int, int, str]] = []  # (start, end, replacement)
    float_blocks: list[str] = []
    fig_n = 0
    tab_n = 0
    pos = 0
    while True:
        nxt = None
        for env in FLOAT_ENVS:
            f = find_env(body, env, pos)
            if f and (nxt is None or f[0] < nxt[1][0]):
                nxt = (env, f)
        if nxt is None:
            break
        env, (b0, b1, b2, b3) = nxt
        block = body[b1:b2]
        pos = b3
        section_index = sum(1 for h in heads if h[0] < b0) - 1
        # subfigure captions are kept as sub-captions, not as figure captions
        subs: list[str] = []
        sub_spans: list[tuple[int, int]] = []
        for sub_env in ("subfigure", "subtable"):
            sp = 0
            while True:
                f = find_env(block, sub_env, sp)
                if not f:
                    break
                sub_spans.append((f[0], f[3]))
                cm = re.search(r"\\caption\*?\s*(\[[^\]]*\])?\s*\{", block[f[1] : f[2]])
                if cm:
                    cap, _ = read_group(block[f[1] : f[2]], cm.end() - 1)
                    subs.append(clean_text(cap, refs, cites).replace("\n", " "))
                sp = f[3]
        captions: list[tuple[str, str, str, int]] = []  # (kind, caption, label, position)
        for cm in re.finditer(r"\\caption(?:of\s*\{(figure|table)\})?\*?\s*(\[[^\]]*\])?\s*\{", block):
            if any(a <= cm.start() < b for a, b in sub_spans):
                continue
            cap, after = read_group(block, cm.end() - 1)
            kind = cm.group(1) or ("table" if env.startswith(("table", "wraptable")) else "figure")
            lab = re.search(r"\\label\{([^}]+)\}", block[after : after + 400])
            captions.append((kind, clean_text(cap, refs, cites).replace("\n", " "), lab.group(1) if lab else "", cm.start()))
        if not captions:
            lab = re.search(r"\\label\{([^}]+)\}", block)
            captions.append(("table" if env.startswith(("table", "wraptable")) else "figure", "", lab.group(1) if lab else "", len(block)))
        image_hits = [(m.start(), _resolve_image(base, m.group(1)))
                      for m in re.finditer(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}", block)]
        image_hits = [(pos_, p) for pos_, p in image_hits if p]
        fig_caps = [c for c in captions if c[0] == "figure"]

        def images_for(cap_pos: int) -> list[Path]:
            """Images of one caption when a float holds several (side-by-side minipages):
            those between the previous figure caption and this one, else all."""
            if len(fig_caps) <= 1:
                return [p for _, p in image_hits]
            prev = max((c[3] for c in fig_caps if c[3] < cap_pos), default=-1)
            mine = [p for pos_, p in image_hits if prev < pos_ < cap_pos]
            if not mine:  # caption written above its image
                nxt = min((c[3] for c in fig_caps if c[3] > cap_pos), default=len(block))
                mine = [p for pos_, p in image_hits if cap_pos < pos_ < nxt]
            return mine
        tabulars = []
        for tab_env in ("tabular", "tabular*", "tabularx", "longtable"):
            tp = 0
            while True:
                f = find_env(block, tab_env, tp)
                if not f:
                    break
                inner = block[f[1] : f[2]]
                # skip the column spec (and the width argument of tabular*/tabularx)
                n_args = 2 if tab_env in ("tabular*", "tabularx") else 1
                _, k = read_args(inner, 0, n_args, optional=True)
                tabulars.append(parse_tabular(inner[k:], cites))
                tp = f[3]
        replacement = []
        tab_iter = iter(tabulars)
        for kind, cap, lab, cap_pos in captions:
            if kind == "figure":
                fig_n += 1
                if lab:
                    refs[lab] = (str(fig_n), "Figure")
                figures.append(LatexFigure(number=fig_n, caption=cap, files=images_for(cap_pos), label=lab,
                                           section_index=section_index, sub_captions=subs if len(fig_caps) == 1 else []))
                replacement.append(f"[Figure {fig_n}: {cap}]")
            else:
                rows = next(tab_iter, [])
                tab_n += 1
                if lab:
                    refs[lab] = (str(tab_n), "Table")
                if rows:
                    tables.append(LatexTable(number=tab_n, caption=cap, columns=rows[0], rows=rows[1:], label=lab, section_index=section_index))
                    replacement.append(table_markdown(tab_n, cap, rows))
                else:
                    replacement.append(f"[Table {tab_n}: {cap}]")
        # already-clean text: stash it so the section cleaner leaves it untouched
        float_blocks.append("\n\n".join(replacement))
        float_spans.append((b0, b3, f"\n\n\x00F{len(float_blocks) - 1}\x00\n\n"))

    # equations: numbered environments get numbers for \eqref
    eq_n = 0
    for m in re.finditer(r"\\begin\{(" + "|".join(re.escape(e) for e in DISPLAY_MATH_ENVS) + r")\}", body):
        env = m.group(1)
        f = find_env(body, env, m.start())
        if not f:
            continue
        inner = body[f[1] : f[2]]
        if not env.endswith("*") and env != "displaymath":
            labels = re.findall(r"\\label\{([^}]+)\}", inner)
            n_lines = max(1, len(re.findall(r"\\\\", inner)) + 1) if env.startswith(("align", "eqnarray", "gather", "multline", "flalign")) else 1
            if labels:
                for lab in labels:
                    eq_n += 1
                    refs[lab] = (str(eq_n), "Equation")
                eq_n += max(0, n_lines - len(labels)) if env.startswith(("align", "eqnarray", "gather")) else 0
            else:
                eq_n += n_lines

    # ---- pass 2: rebuild body with floats replaced, math normalized
    pieces = []
    last = 0
    for b0, b3, rep in float_spans:
        pieces.append(body[last:b0])
        pieces.append(rep)
        last = b3
    pieces.append(body[last:])
    body2 = "".join(pieces)

    def display_math(src: str) -> str:
        out = src
        for env in DISPLAY_MATH_ENVS:
            while True:
                f = find_env(out, env)
                if not f:
                    break
                inner = out[f[1] : f[2]]
                inner = re.sub(r"\\(label|tag)\{[^}]*\}|\\(nonumber|notag)\b", "", inner).strip()
                out = out[: f[0]] + f"\n\n$$ {inner} $$\n\n" + out[f[3] :]
        out = re.sub(r"\\\[(.+?)\\\]", lambda m: f"\n\n$$ {m.group(1).strip()} $$\n\n", out, flags=re.S)
        return out

    body2 = display_math(body2)

    def restore_floats(text: str) -> str:
        return re.sub(r"\x00F(\d+)\x00", lambda m: float_blocks[int(m.group(1))], text)

    # split into sections (positions shift after float replacement: re-scan)
    sections: list[LatexSection] = []
    marks = list(SECTION_RE.finditer(body2))
    numbers = [h[4] for h in heads]
    if marks and marks[0].start() > 0:
        intro = body2[: marks[0].start()]
    else:
        intro = ""
    for k, m in enumerate(marks):
        level = {"section": 1, "subsection": 2, "subsubsection": 3}[m.group(1)]
        title_raw, after = read_group(body2, m.end() - 1)
        end = marks[k + 1].start() if k + 1 < len(marks) else len(body2)
        number = numbers[k] if k < len(numbers) else ""
        heading_text = clean_text(title_raw, refs, cites).replace("\n", " ").strip()
        text = restore_floats(_join_paragraphs(clean_text(body2[after:end], refs, cites)))
        heading = f"{number} {heading_text}".strip()
        sections.append(LatexSection(heading=heading, level=level, text=text, number=number))
    intro_text = restore_floats(_join_paragraphs(clean_text(intro.replace("\\maketitle", ""), refs, cites)))
    if intro_text and len(intro_text) > 200:
        sections.insert(0, LatexSection(heading="Preamble", level=1, text=intro_text))
        for f in figures:
            f.section_index += 1
        for t in tables:
            t.section_index += 1

    abstract = _join_paragraphs(clean_text(abstract_raw, refs, cites)) if abstract_raw else ""
    return LatexPaper(title=title, abstract=abstract, sections=sections, figures=figures, tables=tables,
                      references=references, main_file=main)
