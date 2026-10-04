#!/usr/bin/env python3
"""A Brightspace quiz written as YAML and Markdown, and the CSV that imports it.

    bs-yaml-quiz.py quiz.yml             writes quiz.csv beside it
    bs-yaml-quiz.py quiz.yml --check     exit 1 if quiz.csv is not what quiz.yml makes
    bs-yaml-quiz.py quiz.csv             writes quiz.yml: the other way, for a CSV made elsewhere
    bs-yaml-quiz.py quiz.yml -o out.csv

The YAML holds the structure, a question's text is Markdown inside it, and
the CSV is generated: edit the YAML and run this, and never edit the CSV by
hand. README.md beside this file is the scheme and the Markdown it reads.

The CSV is D2L's question-import format, the route into a Question Library
that works where QTI does not: a row per directive, five cells to a row, a
blank row after every question, UTF-8 with no BOM and CRLF line endings.

Every value in the YAML is read as the text written. An answer of 010, 0.10
or yes stays exactly that; nothing turns into a number or a boolean.

Going from a CSV, nothing is written that does not convert back to the same
rows, and the run says when the bytes differ as well -- in line endings, say.
A row this tool does not know stops it, rather than being dropped.
"""

import argparse
import csv
import html
import io
import json
import pathlib
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("bs-yaml-quiz.py needs PyYAML: `pip install pyyaml`, or on Nix "
             "python3.withPackages (ps: [ ps.pyyaml ])")

# The opening tag of every program. Monospace by style rather than by class,
# because a Question Library question carries no stylesheet of its own.
PRE = '<pre style="font-family:monospace;">'
CELLS = 5
TYPES = ("MC", "SA", "WR", "TF", "MS", "M", "O")
# A question's keys, in the order they are written to the YAML. A question has
# exactly one of text (Markdown), html (as it goes in) and plain_text (no HTML).
KEYS = ("title", "type", "id", "points", "difficulty", "image", "scoring",
        "text", "html", "plain_text", "initial_text", "answer_key", "answers", "width", "rows",
        "options", "correct", "weights", "true_feedback", "false_feedback", "choices",
        "matches", "items", "hint", "feedback")
TEXTS = ("text", "html", "plain_text")
OWN = {"MC": {"options"}, "MS": {"options", "scoring"}, "SA": {"answers", "width", "rows"},
       "WR": {"initial_text", "answer_key"},
       "TF": {"correct", "weights", "true_feedback", "false_feedback"},
       "M": {"choices", "matches", "scoring"}, "O": {"items", "scoring"}}
COMMON = {"title", "type", "id", "points", "difficulty", "image", "hint", "feedback", *TEXTS}
POINTS, WIDTH, ROWS = "1", "40", "1"   # what a question carries when it names none
NONE = ("null", "~", "")
NUMBER = re.compile(r"-?\d+(\.\d+)?")


class Failed(Exception):
    pass


def load(text):
    """A YAML document with every scalar left as the text written."""
    return yaml.load(text, Loader=yaml.BaseLoader)


# --- programs: real lines in the YAML, one line of HTML in the CSV -------------

def code_html(src):
    """One line of HTML: <br /> between lines and &nbsp; for each leading space.

    A CSV field may legally hold a newline, but D2L's parser is an unknown, and
    a newline it dropped would flatten every program into one run-on line. This
    form has none to drop. A tab is refused rather than passed through: D2L
    renders one at a width of its own choosing.
    """
    if "\t" in src:
        raise Failed("a program holds a tab: indent with spaces, which become &nbsp;")
    out = []
    for line in src.split("\n"):
        stripped = line.lstrip(" ")
        out.append("&nbsp;" * (len(line) - len(stripped)) + html.escape(stripped))
    return PRE + "<br />".join(out) + "</pre>"


def code_source(body):
    """The program a <pre> body was made from: code_html run backwards."""
    lines = []
    for line in body.split("<br />"):
        m = re.match(r"((?:&nbsp;)*)(.*)", line, re.S)
        lines.append(" " * (len(m.group(1)) // 6) + html.unescape(m.group(2)))
    return "\n".join(lines)


# --- the Markdown of a question's text -----------------------------------------
#
# A small Markdown, rendered here rather than by a library, because a library's
# HTML is its own: CommonMark turns &rarr; into the arrow itself and escapes a
# quote inside code. Those look the same in a browser and are not the same
# CSV, and the CSV is what has to come back exactly. README.md lists what it
# reads: paragraphs, fenced code, raw HTML blocks, `code`, *em*, **strong**,
# [links](url), backslash escapes, and HTML and entities passed through.

FENCE = re.compile(r"(`{3,})[ ]*(\{=html\}|[\w+#.-]*)[ ]*")
TAG = re.compile(r"<!--.*?-->|</?[A-Za-z][\w-]*(\s[^<>]*)?/?>", re.S)
PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
LINK = re.compile(r"\[([^\[\]]*)\]\(([^()\s]+)\)")


def code_span_end(s, i):
    """(content start, content end, after) for the code span opening at s[i],
    or None: a run of backticks closed by a run of the same length."""
    n = len(s[i:]) - len(s[i:].lstrip("`"))
    j = i + n
    while True:
        j = s.find("`" * n, j)
        if j < 0:
            return None
        k = j + n
        if s[k:k + 1] != "`" and s[j - 1:j] != "`":
            return i + n, j, k
        j = k + len(s[k:]) - len(s[k:].lstrip("`"))


def skip(s, i):
    """Past whatever at s[i] is not a delimiter: an escape, a code span, a tag."""
    if s[i] == "\\" and s[i + 1:i + 2] in PUNCT:
        return i + 2
    if s[i] == "`":
        span = code_span_end(s, i)
        return span[2] if span else i + len(s[i:]) - len(s[i:].lstrip("`"))
    if s[i] == "<":
        m = TAG.match(s, i)
        if m:
            return m.end()
    return None


def closer(s, i, delim):
    """Where delim closes an emphasis opened just before s[i], or None."""
    while i < len(s):
        past = skip(s, i)
        if past is not None:
            i = past
            continue
        if s.startswith("**", i) and delim == "*":
            inner = closer(s, i + 2, "**")
            if inner is None:
                return None
            i = inner + 2
            continue
        if s.startswith(delim, i) and not s[i - 1].isspace() and (delim == "**" or s[i + 1:i + 2] != "*"):
            return i
        i += 1
    return None


def inline(s):
    """The HTML of one paragraph's Markdown."""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and s[i + 1:i + 2] in PUNCT:
            out.append(s[i + 1])
            i += 2
        elif c == "`" and code_span_end(s, i):
            a, b, i = code_span_end(s, i)
            out.append("<code>" + html.escape(s[a:b], quote=False) + "</code>")
        elif c == "<" and TAG.match(s, i):
            m = TAG.match(s, i)
            out.append(m.group(0))
            i = m.end()
        elif s.startswith("**", i) and s[i + 2:i + 3] not in ("", " ") and closer(s, i + 2, "**"):
            j = closer(s, i + 2, "**")
            out.append("<strong>" + inline(s[i + 2:j]) + "</strong>")
            i = j + 2
        elif c == "*" and s[i + 1:i + 2] not in ("", " ", "*") and closer(s, i + 1, "*"):
            j = closer(s, i + 1, "*")
            out.append("<em>" + inline(s[i + 1:j]) + "</em>")
            i = j + 1
        elif c == "[" and LINK.match(s, i):
            m = LINK.match(s, i)
            out.append(f'<a href="{m.group(2)}">' + inline(m.group(1)) + "</a>")
            i = m.end()
        else:
            out.append(c)
            i += 1
    return "".join(out)


def md_html(md, where):
    """The one line of HTML a question's Markdown text goes into the CSV as."""
    lines, out, para, i = md.strip("\n").split("\n"), [], [], 0

    def paragraph():
        if para:
            out.append("<p>" + inline(" ".join(x.strip() for x in para)) + "</p>")
            para.clear()
    while i < len(lines):
        fence = FENCE.fullmatch(lines[i])
        if fence:
            paragraph()
            body, i = [], i + 1
            while i < len(lines) and lines[i].rstrip() != fence.group(1):
                body.append(lines[i])
                i += 1
            if i == len(lines):
                raise Failed(f"{where}: a block opened with {fence.group(1)} is never closed")
            i += 1
            if fence.group(2) == "{=html}":
                out.append(" ".join(x.strip() for x in body if x.strip()))
            else:
                out.append(code_html("\n".join(body)))
        elif lines[i].strip():
            para.append(lines[i])
            i += 1
        else:
            paragraph()
            i += 1
    paragraph()
    if not out:
        raise Failed(f"{where}: the text is empty")
    return "".join(out)


def md_escape(text):
    return "".join("\\" + c if c in "\\`*[]<" else c for c in text)


def inline_md(h):
    """Markdown for one paragraph's inner HTML, or None if it cannot be
    written so that it renders back to exactly this."""
    out, pos, links = [], 0, []
    pat = re.compile(r'<code>(.*?)</code>|<(/?)(em|strong)>|<a href="([^"()\s]+)">|</a>|<[^>]*>', re.S)
    for m in pat.finditer(h):
        out.append(md_escape(h[pos:m.start()]))
        tag = m.group(0)
        if tag.startswith("<code>"):
            src = html.unescape(m.group(1))
            if "<" in m.group(1) or html.escape(src, quote=False) != m.group(1) or not src \
                    or src[0] in "` " or src[-1] in "` ":
                return None
            run = "`" * (max((len(r) for r in re.findall(r"`+", src)), default=0) + 1)
            out.append(run + src + run)
        elif m.group(3):
            out.append("*" if m.group(3) == "em" else "**")
        elif m.group(4) is not None:
            links.append(m.group(4))
            out.append("[")
        elif tag == "</a>":
            if not links:
                return None
            out.append("](" + links.pop() + ")")
        else:
            out.append(tag)
        pos = m.end()
    out.append(md_escape(h[pos:]))
    md = "".join(out)
    return md if md and md == md.strip() and "\n" not in md and inline(md) == h else None


def wrapped(md):
    """A paragraph's Markdown over lines a screen wide. Rendering joins them
    with a space again, so it breaks only at a single space, and never before
    three backticks, which would begin a line that reads as a fence."""
    words, lines, line = md.split(" "), [], ""
    if "  " in md:
        return [md]
    for w in words:
        if line and len(line) + 1 + len(w) > 76 and not w.startswith("```"):
            lines.append(line)
            line = w
        else:
            line = f"{line} {w}" if line else w
    return lines + [line]


def html_md(field):
    """Markdown for a QuestionText's HTML, or None where Markdown cannot say it
    exactly: a <p> as a paragraph, a program's <pre> as fenced code, anything
    else as a raw ```{=html} block."""
    blocks, pos = [], 0

    def raw(chunk):
        if chunk != chunk.strip() or "\n" in chunk:
            return None
        return "```{=html}\n" + chunk + "\n```"
    for m in re.finditer(r"<p>(.*?)</p>|" + re.escape(PRE) + r"(.*?)</pre>", field, re.S):
        if m.start() > pos:
            blocks.append(raw(field[pos:m.start()]))
        if m.group(0).startswith("<p>"):
            md = inline_md(m.group(1))
            blocks.append("\n".join(wrapped(md)) if md else raw(m.group(0)))
        else:
            src = code_source(m.group(2))
            if "\t" in src or code_html(src) != m.group(0):
                blocks.append(raw(m.group(0)))
            else:
                run = "`" * max(3, max((len(r) for r in re.findall(r"^`+", src, re.M)), default=0) + 1)
                blocks.append(run + "\n" + src + "\n" + run)
        pos = m.end()
    if pos < len(field):
        blocks.append(raw(field[pos:]))
    if None in blocks or not blocks:
        return None
    md = "\n\n".join(blocks)
    try:
        return md if md_html(md, "") == field else None
    except Failed:
        return None


# --- YAML to CSV --------------------------------------------------------------

def number(value, where, what):
    """A number as the text that goes in its cell, exactly as written."""
    text = str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
    if not isinstance(text, str) or not NUMBER.fullmatch(text):
        raise Failed(f"{where}: {what} is a number, not {value!r}")
    return text


def truth(value, where, what):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "yes"):
        return True
    if isinstance(value, str) and value.lower() in ("false", "no"):
        return False
    raise Failed(f"{where}: {what} is true or false, not {value!r}")


def weighted(item, where, what):
    """(weight, text, the other keys) from `- 100: text` and any keys beside it."""
    if isinstance(item, dict):
        nums = [k for k in item if NUMBER.fullmatch(str(k))]
        if len(nums) == 1 and isinstance(item[nums[0]], str):
            return str(nums[0]), item[nums[0]], {k: v for k, v in item.items() if k != nums[0]}
    raise Failed(f"{where}: each of its {what} is `- <weight>: <text>`, not {item!r}")


def spliced(value):
    """A list with any list inside it spliced in, so that an anchored list can
    be one item among others: `- *compile-error` and then a spelling of its own.
    Anything that is not a list is left for the caller to refuse."""
    if not isinstance(value, list):
        return value
    out = []
    for x in value:
        out += spliced(x) if isinstance(x, list) else [x]
    return out


def plain(value, where, what):
    """Text for a cell that D2L shows as it is: one line, no Markdown."""
    if not isinstance(value, str):
        raise Failed(f"{where}: {what} is text, not {value!r}")
    if "\n" in value.strip("\n"):
        raise Failed(f"{where}: {what} is one line, and this one has several")
    return value.strip("\n")


def question_rows(q, where):
    """The rows of one question, in the order D2L's own sample has them."""
    if not isinstance(q, dict):
        raise Failed(f"{where}: a question is a mapping of keys, not {q!r}")
    kind = q.get("type")
    if kind not in TYPES:
        raise Failed(f"{where}: type is one of {', '.join(TYPES)}, not {kind!r}")
    wrong = [k for k in q if k not in COMMON | OWN[kind]]
    if wrong:
        raise Failed(f"{where}: a {kind} question has no {', '.join(map(str, wrong))}")
    texts = [k for k in TEXTS if k in q]
    if len(texts) != 1:
        raise Failed(f"{where}: a question has one of text, html and plain_text"
                     + (f", not {' and '.join(texts)}" if texts else ""))

    rows = [["NewQuestion", kind]]
    for key, name in (("id", "ID"), ("title", "Title")):
        if key in q:
            rows.append([name, plain(q[key], where, key)])
    if "text" in q:
        if not isinstance(q["text"], str):
            raise Failed(f"{where}: text is Markdown, one string, not {q['text']!r}")
        rows.append(["QuestionText", md_html(q["text"], where), "HTML"])
    elif "html" in q:
        rows.append(["QuestionText", plain(q["html"], where, "html"), "HTML"])
    else:
        rows.append(["QuestionText", plain(q["plain_text"], where, "plain_text"), ""])
    rows.append(["Points", number(q.get("points", POINTS), where, "points")])
    if "difficulty" in q:
        rows.append(["Difficulty", number(q["difficulty"], where, "difficulty")])
    for key, name in (("image", "Image"), ("scoring", "Scoring"),
                      ("initial_text", "InitialText"), ("answer_key", "AnswerKey")):
        if key in q:
            rows.append([name, plain(q[key], where, key)])

    if kind == "SA":
        if q.get("width", WIDTH) not in NONE:
            rows.append(["InputBox", number(q.get("rows", ROWS), where, "rows"),
                         number(q.get("width", WIDTH), where, "width")])
        answers = spliced(q.get("answers"))
        if not isinstance(answers, list) or not answers:
            raise Failed(f"{where}: a short answer needs answers, every spelling that counts")
        for a in answers:
            weight, text, rest = ("100", a, {}) if isinstance(a, str) else weighted(a, where, "answers")
            if set(rest) - {"regexp"}:
                raise Failed(f"{where}: an answer takes nothing but regexp: beside its weight")
            regexp = truth(rest["regexp"], where, "regexp") if "regexp" in rest else False
            rows.append(["Answer", weight, plain(text, where, "an answer"), "regexp" if regexp else ""])
    if kind in ("MC", "MS"):
        options = spliced(q.get("options"))
        if not isinstance(options, list) or not options:
            raise Failed(f"{where}: a {kind} question needs its options")
        for o in options:
            weight, text, rest = weighted(o, where, "options")
            if set(rest) - {"feedback"}:
                raise Failed(f"{where}: an option takes nothing but feedback: beside its weight")
            rows.append(["Option", weight, plain(text, where, "an option"), "",
                         plain(rest.get("feedback", ""), where, "feedback")])
    if kind == "TF":
        right = truth(q.get("correct"), where, "correct")
        weights = q.get("weights", ["100", "0"] if right else ["0", "100"])
        if not (isinstance(weights, list) and len(weights) == 2):
            raise Failed(f"{where}: weights is [TRUE's, FALSE's]")
        rows.append(["TRUE", number(weights[0], where, "a weight"), plain(q.get("true_feedback", ""), where, "feedback")])
        rows.append(["FALSE", number(weights[1], where, "a weight"), plain(q.get("false_feedback", ""), where, "feedback")])
    if kind == "M":
        for i, c in enumerate(spliced(q.get("choices") or []), 1):
            rows.append(["Choice", str(i), plain(c, where, "a choice")])
        for m in spliced(q.get("matches") or []):
            n, text, _ = weighted(m, where, "matches")
            rows.append(["Match", n, plain(text, where, "a match")])
    if kind == "O":
        for item in spliced(q.get("items") or []):
            item = {"text": item} if isinstance(item, str) else item
            if not isinstance(item, dict) or "text" not in item or set(item) - {"text", "format", "feedback"}:
                raise Failed(f"{where}: an item is its text, or text: with format: and feedback:")
            rows.append(["Item", plain(item["text"], where, "an item"), item.get("format", ""),
                         plain(item.get("feedback", ""), where, "feedback")])
    for key, name in (("hint", "Hint"), ("feedback", "Feedback")):
        if key in q:
            rows.append([name, plain(q[key], where, key)])
    rows.append([])
    return rows


def to_csv(data, where):
    """The CSV text, CRLF line endings, for a loaded quiz document."""
    if not isinstance(data, dict) or list(data) != ["questions"] or not isinstance(data["questions"], list):
        raise Failed(f"{where}: a quiz file is `questions:` and the list of them")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    for n, q in enumerate(data["questions"], 1):
        title = q.get("title") if isinstance(q, dict) else None
        at = f"{where}, question {n}" + (f" ({title})" if title else "")
        for row in question_rows(q, at):
            if any(not isinstance(c, str) for c in row):
                raise Failed(f"{at}: {row[0]} wants text, and has {row!r}")
            if any("\t" in c for c in row):
                raise Failed(f"{at}: a tab in {row[0]}; D2L renders one at a width of its own")
            w.writerow(row + [""] * (CELLS - len(row)))
    return buf.getvalue()


# --- CSV to YAML --------------------------------------------------------------

def data_rows(text):
    """The rows that carry something: no blank rows, no // comments, and no
    empty cells at the end. Two CSVs with the same of these import the same."""
    out = []
    for r in csv.reader(io.StringIO(text, newline="")):
        if any(c.strip() for c in r) and not r[0].strip().startswith("//"):
            while r and not r[-1]:
                r = r[:-1]
            out.append(r)
    return out


def from_csv(text, where):
    """(comments left over, [(comments before it, question)]) from a CSV."""
    questions, q, comments = [], None, []
    for n, r in enumerate(csv.reader(io.StringIO(text, newline="")), 1):
        if not any(c.strip() for c in r):
            continue
        kind, cells = r[0].strip(), r[1:] + [""] * (CELLS - len(r))
        at = f"{where}, row {n}"
        if kind.startswith("//"):
            comments.append(",".join(r).rstrip(",")[2:].strip())
            continue

        def only(k):
            if any(cells[k:]):
                raise Failed(f"{at}: {kind} has more cells filled in than it uses")

        def num(cell):
            if not NUMBER.fullmatch(cell):
                raise Failed(f"{at}: {cell!r} is not a number")
            return cell

        def put(key, value):
            if key in q:
                raise Failed(f"{at}: a second {kind} row in one question")
            q[key] = value

        if kind == "NewQuestion":
            only(1)
            if cells[0] not in TYPES:
                raise Failed(f"{at}: question type {cells[0]!r} is not one this tool knows")
            q = {"type": cells[0]}
            questions.append((comments, q))
            comments = []
            continue
        if q is None:
            raise Failed(f"{at}: a {kind} row before any NewQuestion")
        t = q["type"]
        if kind in ("ID", "Title", "Image", "Scoring", "InitialText", "AnswerKey", "Hint", "Feedback"):
            only(1)
            put({"ID": "id", "Title": "title", "Image": "image", "Scoring": "scoring",
                 "InitialText": "initial_text", "AnswerKey": "answer_key", "Hint": "hint",
                 "Feedback": "feedback"}[kind], cells[0])
        elif kind == "QuestionText":
            only(2)
            if cells[1] == "HTML":
                md = html_md(cells[0])
                put("text", md) if md is not None else put("html", cells[0])
            elif not cells[1]:
                put("plain_text", cells[0])
            else:
                raise Failed(f"{at}: QuestionText's third cell is HTML or nothing, not {cells[1]!r}")
        elif kind in ("Points", "Difficulty"):
            only(1)
            put(kind.lower(), num(cells[0]))
        elif kind == "InputBox" and t == "SA":
            only(2)
            put("rows", num(cells[0]))
            put("width", num(cells[1]))
        elif kind == "Answer" and t == "SA":
            only(3)
            if cells[2] not in ("regexp", ""):
                raise Failed(f"{at}: Answer's fourth cell is regexp or nothing, not {cells[2]!r}")
            simple = num(cells[0]) == "100" and not cells[2]
            q.setdefault("answers", []).append(
                cells[1] if simple else {cells[0]: cells[1], **({"regexp": "true"} if cells[2] else {})})
        elif kind == "Option" and t in ("MC", "MS"):
            if cells[2]:
                raise Failed(f"{at}: an option's fourth cell is meant to be empty, and holds {cells[2]!r}")
            q.setdefault("options", []).append(
                {num(cells[0]): cells[1], **({"feedback": cells[3]} if cells[3] else {})})
        elif kind in ("TRUE", "FALSE") and t == "TF":
            only(2)
            q.setdefault("_tf", {})[kind] = (num(cells[0]), cells[1])
        elif kind == "Choice" and t == "M":
            only(2)
            if cells[0] != str(len(q.get("choices", [])) + 1):
                raise Failed(f"{at}: choices are numbered 1, 2, 3, in order")
            q.setdefault("choices", []).append(cells[1])
        elif kind == "Match" and t == "M":
            only(2)
            q.setdefault("matches", []).append({num(cells[0]): cells[1]})
        elif kind == "Item" and t == "O":
            only(3)
            item = {"text": cells[0], **({"format": cells[1]} if cells[1] else {}),
                    **({"feedback": cells[2]} if cells[2] else {})}
            q.setdefault("items", []).append(item if len(item) > 1 else cells[0])
        else:
            raise Failed(f"{at}: a {kind!r} row is not one this tool knows in a {t} question")

    out = []
    for before, q in questions:
        tf = q.pop("_tf", None)
        if q["type"] == "TF":
            if not tf or set(tf) != {"TRUE", "FALSE"}:
                raise Failed(f"{where}: a true/false question needs a TRUE row and a FALSE row")
            right = float(tf["TRUE"][0]) >= float(tf["FALSE"][0])
            q["correct"] = "true" if right else "false"
            if [tf["TRUE"][0], tf["FALSE"][0]] != (["100", "0"] if right else ["0", "100"]):
                q["weights"] = [tf["TRUE"][0], tf["FALSE"][0]]
            for key, cell in (("true_feedback", tf["TRUE"][1]), ("false_feedback", tf["FALSE"][1])):
                if cell:
                    q[key] = cell
        if q["type"] == "SA":
            if "width" not in q:
                q["width"] = "null"
            else:
                if q["rows"] == ROWS:
                    del q["rows"]
                if q["width"] == WIDTH:
                    del q["width"]
        if q.get("points") == POINTS:
            del q["points"]
        if not any(k in q for k in TEXTS):
            raise Failed(f"{where}: question {len(out) + 1} has no QuestionText")
        out.append((before, {k: q[k] for k in KEYS if k in q}))
    return comments, out


# --- writing YAML a person will edit -----------------------------------------
#
# Written here rather than by yaml.dump, for what it does not do: Markdown as a
# block of real lines, a long line folded at the width of a screen, and
# comments. Whatever is written is read back and converted before it is kept,
# so a slip here stops the run rather than reaching a quiz.

def plain_ok(s, room=70):
    if not s or s != s.strip() or "\n" in s or len(s) > room:
        return False
    try:
        return load("k: " + s) == {"k": s}
    except yaml.YAMLError:
        return False


def quoted(s):
    """Quoted the way that needs no escaping: single quotes unless the text
    has one of its own, double quotes otherwise."""
    single = "'" + s.replace("'", "''") + "'"
    try:
        if "\n" not in s and "'" not in s and load("k: " + single) == {"k": s}:
            return single
    except yaml.YAMLError:
        pass
    return json.dumps(s, ensure_ascii=False)


def folded(s, indent):
    """A long line as `>-` over lines no wider than a screen, or None where
    folding could not give back exactly this string."""
    if s != s.strip() or "\n" in s or "  " in s:
        return None
    lines, line = [], ""
    for word in s.split(" "):
        if line and len(indent) + len(line) + 1 + len(word) > 78:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    lines.append(line)
    return [">-"] + [indent + x for x in lines]


def scalar(value, indent, used=4):
    """The lines a value takes after `key: ` or `- `, which already fill `used`
    columns: the first goes on that line, the rest are whole lines at indent.
    On the line if it fits in 80 columns, folded over several if it does not."""
    room = 80 - used
    if plain_ok(value, room):
        return [value]
    if "\n" not in value and len(quoted(value)) <= room:
        return [quoted(value)]
    return folded(value, indent) or [quoted(value)]


def literal(text, indent):
    """Markdown as a `|` block, or a single line where that is all it is."""
    lines = text.split("\n")
    if len(lines) == 1 and plain_ok(text):
        return [text]
    if text != text.strip("\n") or lines[0][:1] == " " or any(x != x.rstrip() for x in lines if not x.startswith(" ")):
        return [quoted(text)]
    return ["|"] + [(indent + x) if x else "" for x in lines]


def comment_lines(lines):
    """Comment lines as written, a "# " in front of any that lacks one, and a
    blank line wherever a line is None."""
    return ["" if x is None else x if x.startswith("#") else ("# " + x).rstrip() for x in lines]


def dump(questions, header=()):
    """YAML text for [(comments before it, question)], header comments on top."""
    out = comment_lines(header) + ([""] if header else []) + ["questions:"]
    for before, q in questions:
        out.append("")
        out += comment_lines(before)
        for i, (key, value) in enumerate(q.items()):
            lead = "- " if i == 0 else "  "
            if key == "text":
                v = literal(value, "    ")
                out += [f"{lead}text: " + v[0]] + v[1:]
            elif isinstance(value, list):
                out.append(f"{lead}{key}:")
                for item in value:
                    if isinstance(item, dict):
                        for j, (k, x) in enumerate(item.items()):
                            v = scalar(x, "      ", 6 + len(str(k)))
                            out += [("  - " if j == 0 else "    ") + f"{k}: " + v[0]] + v[1:]
                    else:
                        v = scalar(item, "    ", 4)
                        out += ["  - " + v[0]] + v[1:]
            else:
                v = scalar(value, "    ", 4 + len(key))
                out += [f"{lead}{key}: " + v[0]] + v[1:]
    return "\n".join(out) + "\n"


def to_yaml(csv_text, where, header=(), comments=None):
    """(YAML text, whether it makes the CSV back byte for byte) for a CSV.

    header and comments -- lines, and {question index: lines} -- are comments
    the CSV cannot carry, such as the ones a generator kept beside its code.
    Refused unless the YAML converts back to the same rows as the CSV.
    """
    left, questions = from_csv(csv_text, where)
    if comments:
        questions = [(comments.get(i, []) + before, q) for i, (before, q) in enumerate(questions)]
    text = dump(questions, list(header) + left)
    back = to_csv(load(text), where)
    if data_rows(back) != data_rows(csv_text):
        raise Failed(f"{where}: the YAML written for it does not convert back to the same rows; "
                     "nothing written")
    return text, back == csv_text


def differences(have):
    """What separates a CSV from one with the same rows made here, in words."""
    said = []
    if "\r\r\n" in have:
        said.append("lines that end \\r\\r\\n rather than \\r\\n")
    if any(r and r[0].strip().startswith("//") for r in csv.reader(io.StringIO(have, newline=""))):
        said.append("// comment rows, which are YAML comments now")
    return ", ".join(said) or "blank rows or empty cells"


# --- the command --------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", metavar="FILE", help="a quiz's .yml, or a .csv to make one from")
    p.add_argument("-o", "--output", help="where to write, for a single FILE (default: beside it)")
    p.add_argument("--check", action="store_true",
                   help="for a .yml: exit 1 if the CSV beside it is not what it makes; write nothing")
    p.add_argument("--force", action="store_true", help="for a .csv: replace a .yml that exists")
    args = p.parse_args(argv)
    if args.output and len(args.files) > 1:
        p.error("-o names one output, so it takes one FILE")
    stale = 0
    try:
        for name in args.files:
            src = pathlib.Path(name)
            if src.suffix in (".yml", ".yaml"):
                dest = pathlib.Path(args.output) if args.output else src.with_suffix(".csv")
                try:
                    data = load(src.read_text(encoding="utf-8"))
                except yaml.YAMLError as e:
                    raise Failed(f"{src}: not YAML: {e}")
                text = to_csv(data, str(src))
                if args.check:
                    have = dest.read_bytes().decode("utf-8") if dest.exists() else None
                    if have != text:
                        stale += 1
                        print(f"{dest}: " + ("missing" if have is None else f"not what {src.name} makes")
                              + "; run without --check to write it")
                    continue
                with open(dest, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                print(f"wrote {dest}: {len(data['questions'])} questions")
            elif src.suffix == ".csv":
                dest = pathlib.Path(args.output) if args.output else src.with_suffix(".yml")
                if dest.exists() and not args.force:
                    raise Failed(f"{dest} exists, and is the source its CSV is made from; "
                                 "--force replaces it, comments and all")
                have = src.read_bytes().decode("utf-8")
                text, same = to_yaml(have, str(src))
                dest.write_text(text, encoding="utf-8")
                n = len(load(text)["questions"])
                print(f"wrote {dest}: {n} questions, and it makes {src.name} back "
                      + ("byte for byte" if same else f"row for row; the bytes differ in {differences(have)}"))
            else:
                raise Failed(f"{src}: a .yml to make a CSV from, or a .csv to make a .yml from")
    except Failed as e:
        sys.exit(f"bs-yaml-quiz.py: {e}")
    except OSError as e:
        sys.exit(f"bs-yaml-quiz.py: {e}")
    if stale:
        sys.exit(1)


if __name__ == "__main__":
    main()
