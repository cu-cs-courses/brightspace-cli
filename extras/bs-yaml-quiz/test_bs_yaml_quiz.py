"""bs-yaml-quiz.py on invented quizzes: YAML to CSV, a CSV back to YAML, and
what it refuses.

    python3 test_bs_yaml_quiz.py

Every quiz here is invented. A real one carries its answer key, and this
repository is public.
"""
import csv, importlib.util, io, pathlib, subprocess, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
TOOL = HERE / "bs-yaml-quiz.py"
spec = importlib.util.spec_from_file_location("bq", TOOL)
bq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bq)
tmp = pathlib.Path(tempfile.mkdtemp())
PRE = '<pre style="font-family:monospace;">'
fails = 0


def check(label, good, detail=""):
    global fails
    print(("ok  " if good else "FAIL"), label)
    if not good:
        fails += 1
        if detail:
            print(detail)


def run(*args, ok=True, has=(), lacks=()):
    r = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=tmp)
    text = r.stdout + r.stderr
    good = (r.returncode == 0) == ok and all(h in text for h in has) and not any(x in text for x in lacks)
    check(" ".join(args) + f" (exit {r.returncode})", good, text)
    return text


def rows(name):
    return list(csv.reader(io.StringIO((tmp / name).read_bytes().decode("utf-8"), newline="")))


def write_csv(name, table, end="\r\n"):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator=end)
    for r in table:
        w.writerow(r + [""] * (5 - len(r)))
    (tmp / name).write_bytes(buf.getvalue().encode("utf-8"))


def yml(name, text):
    (tmp / name).write_text(text)


# --- YAML to CSV: the example ---------------------------------------------------
(tmp / "example.yml").write_text((HERE / "example.yml").read_text())
run("example.yml", has=["wrote example.csv: 5 questions"])
raw = (tmp / "example.csv").read_bytes()
r = rows("example.csv")
check("UTF-8 with no BOM, CRLF and no other line ending",
      not raw.startswith(b"\xef\xbb\xbf") and raw.count(b"\n") == raw.count(b"\r\n") and b"\r\r" not in raw)
check("five cells to every row", all(len(x) == 5 for x in r))
check("a blank row after every question, the last one included",
      sum(1 for x in r if not any(x)) == 5 and not any(r[-1]))
texts = [x for x in r if x[0] == "QuestionText"]
check("every QuestionText carries the HTML marker", all(x[2] == "HTML" for x in texts))
check("a program: &nbsp; for indentation, <br /> between lines, < > & and quotes escaped",
      PRE + "int total = 0;<br />for (int i = 1; i &lt;= 3; i++) {<br />"
      "&nbsp;&nbsp;&nbsp;&nbsp;total += i;<br />}<br />System.out.println(total);</pre>" in texts[0][1],
      texts[0][1])
check("paragraphs around it, with inline Markdown",
      texts[0][1].startswith("<p>You run this:</p>" + PRE)
      and texts[0][1].endswith("</pre><p>What does it print? <em>Write the number.</em></p>"), texts[0][1])
check("a one-line text is one paragraph; code spans escape < > & and leave quotes",
      texts[1][1] == '<p>Why does <code>Object o = "hi";</code> compile?</p>', texts[1][1])
check("a paragraph written over two lines is one line, joined with a space",
      texts[2][1] == "<p>Write a method <code>twice(int n)</code> that gives back <strong>two times</strong> "
                     "<code>n</code>, and show it called once, with what it gives back.</p>", texts[2][1])
check("the short answer: its box, then a row per spelling",
      r[4:7] == [["InputBox", "1", "12", "", ""], ["Answer", "100", "6", "", ""], ["Answer", "100", "six", "", ""]], r[4:7])
check("options in order, weight in the second cell, feedback in the fifth",
      ["Option", "100", "Because every class, String included, is a subclass of Object.", "",
       "A String is an Object, so the assignment needs no conversion."] in r
      and ["Option", "0", "Because Object is an interface that String implements.", "", ""] in r)
check("points default to 1, and a written response's are its own",
      [x[1] for x in r if x[0] == "Points"] == ["1", "1", "10", "1", "1"])
check("a written response's starting text", ["InitialText", "public static int twice(int n) {", "", "", ""] in r)
check("true or false: TRUE earns 100 and FALSE nothing",
      ["TRUE", "100", "", "", ""] in r and ["FALSE", "0", "", "", ""] in r)
check("multi-select: its scoring rule, and options weighted 1 and 0",
      ["Scoring", "RightAnswers", "", "", ""] in r and ["Option", "0", 'int y = "3";', "", ""] in r)

# --- --check ----------------------------------------------------------------------
run("example.yml", "--check", lacks=["missing", "not what"])
(tmp / "example.csv").write_bytes(raw.replace(b"six", b"seven"))
run("example.yml", "--check", ok=False, has=["example.csv: not what example.yml makes"])
(tmp / "example.csv").unlink()
run("example.yml", "--check", ok=False, has=["example.csv: missing"])
run("example.yml")

# --- CSV back to YAML ---------------------------------------------------------------
run("example.csv", ok=False, has=["example.yml exists", "--force"])
run("example.csv", "-o", "back.yml", has=["wrote back.yml: 5 questions", "byte for byte"])
run("back.yml", "-o", "back.csv")
check("the example's CSV, to YAML and back, is the same bytes", (tmp / "back.csv").read_bytes() == raw)
back = (tmp / "back.yml").read_text()
check("a program comes back as a fenced block of real lines",
      "    ```\n    int total = 0;\n    for (int i = 1; i <= 3; i++) {\n        total += i;\n" in back, back)

# Every kind of row D2L's format has, invented, with its quirks: a // comment, a
# regular expression, partial credit, an ID, text without the HTML marker.
everything = [
    ["// questions for a test of every row"],
    ["NewQuestion", "WR"], ["ID", "T-1"], ["Title", "Written"], ["QuestionText", "<p>Explain.</p>", "HTML"],
    ["Points", "5"], ["Difficulty", "3"], ["Image", "images/a.png"], ["InitialText", "Start here"],
    ["AnswerKey", "The key"], ["Hint", "A hint"], ["Feedback", "Afterwards"], [],
    ["NewQuestion", "SA"], ["Title", "Short"], ["QuestionText", "Plain text, no HTML marker"], ["Points", "2"],
    ["InputBox", "3", "60"], ["Answer", "100", "^[0-9]+$", "regexp"], ["Answer", "50", "almost"], [],
    ["NewQuestion", "M"], ["Title", "Matching"], ["QuestionText", "<p>Match them.</p>", "HTML"], ["Points", "2"],
    ["Scoring", "EquallyWeighted"], ["Choice", "1", "one"], ["Choice", "2", "two"],
    ["Match", "2", "deux"], ["Match", "1", "un"], [],
    ["NewQuestion", "O"], ["Title", "Ordering"], ["QuestionText", "<p>In order.</p>", "HTML"], ["Points", "1"],
    ["Scoring", "RightMinusWrong"], ["Item", "first", "NOT HTML", "it comes first"], ["Item", "second"], [],
    ["NewQuestion", "TF"], ["Title", "Weighted"], ["QuestionText", "<p>Is it?</p>", "HTML"], ["Points", "1"],
    ["TRUE", "25", "a quarter"], ["FALSE", "75"], [],
    ["NewQuestion", "MC"], ["Title", "No title box"], ["QuestionText", "<p>Pick.</p>", "HTML"], ["Points", "1"],
    ["Option", "100", "this"], ["Option", "0", "that", "", "not that"], [],
]
write_csv("every.csv", everything)
run("every.csv", has=["wrote every.yml: 6 questions", "row for row", "// comment rows, which are YAML comments now"])
run("every.yml", "-o", "every-back.csv")
check("every kind of row, to YAML and back, is the same rows",
      bq.data_rows((tmp / "every-back.csv").read_text()) == bq.data_rows((tmp / "every.csv").read_text()))
check("the comment row is a YAML comment", "# questions for a test of every row" in (tmp / "every.yml").read_text())
write_csv("every-nc.csv", everything[1:])
run("every-nc.csv", has=["byte for byte"])
write_csv("doubled.csv", everything[1:], end="\r\r\n")
run("doubled.csv", has=["row for row", "lines that end \\r\\r\\n"])

# A program that would not come back the same, a &nbsp; inside a line, stays HTML.
odd = PRE + "a&nbsp;b<br />c</pre>"
write_csv("odd.csv", [["NewQuestion", "SA"], ["QuestionText", "<p>What?</p>" + odd, "HTML"], ["Points", "1"],
                      ["InputBox", "1", "40"], ["Answer", "100", "x"], []])
run("odd.csv", has=["byte for byte"])
check("a program that cannot be a fenced block exactly is a raw HTML block",
      "```{=html}\n    " + odd + "\n    ```" in (tmp / "odd.yml").read_text(), (tmp / "odd.yml").read_text())

# --- every value is the text written --------------------------------------------------
yml("text.yml", "questions:\n- type: SA\n  text: Spell it.\n  points: 25\n  answers:\n"
    "  - 010\n  - 0.10\n  - yes\n  - true\n  - null\n  - ~\n")
run("text.yml")
check("010, 0.10, yes, true, null and ~ stay as written, and points as 25",
      [x[2] for x in rows("text.csv") if x[0] == "Answer"] == ["010", "0.10", "yes", "true", "null", "~"]
      and ["Points", "25", "", "", ""] in rows("text.csv"), rows("text.csv"))

# --- anchors: a list written once ------------------------------------------------------
yml("anchors.yml", "questions:\n"
    "- type: SA\n  text: First.\n  answers: &ce\n  - COMPILE ERROR\n  - COMPILE-ERROR\n"
    "- type: SA\n  text: Second.\n  answers: *ce\n"
    "- type: SA\n  text: Third.\n  answers:\n  - *ce\n  - it does not compile\n"
    "- type: MC\n  text: Fourth.\n  options:\n  - 100: right\n  - &wrong\n    - 0: wrong one\n    - 0: wrong two\n"
    "- type: MC\n  text: Fifth.\n  options:\n  - *wrong\n  - 100: right again\n")
run("anchors.yml")
got = [(x[0], x[1], x[2]) for x in rows("anchors.csv") if x and x[0] in ("Answer", "Option")]
check("an anchored list, whole or as one item among others, is spliced in where it stands",
      got == [("Answer", "100", "COMPILE ERROR"), ("Answer", "100", "COMPILE-ERROR"),
              ("Answer", "100", "COMPILE ERROR"), ("Answer", "100", "COMPILE-ERROR"),
              ("Answer", "100", "COMPILE ERROR"), ("Answer", "100", "COMPILE-ERROR"),
              ("Answer", "100", "it does not compile"),
              ("Option", "100", "right"), ("Option", "0", "wrong one"), ("Option", "0", "wrong two"),
              ("Option", "0", "wrong one"), ("Option", "0", "wrong two"), ("Option", "100", "right again")], got)

# --- the Markdown it reads -------------------------------------------------------------
md = lambda s: bq.md_html(s, "test")
check("a backslash makes punctuation literal", md(r"\*not emphasis\* and \`no code\`") ==
      "<p>*not emphasis* and `no code`</p>")
check("a longer run of backticks holds a backtick", md("``a`b``") == "<p><code>a`b</code></p>")
check("emphasis inside strong", md("**bold *and em* inside**") == "<p><strong>bold <em>and em</em> inside</strong></p>")
check("a lone asterisk between spaces is an asterisk", md("2 * 3 * 4") == "<p>2 * 3 * 4</p>")
check("an underscore is an underscore", md("snake_case_name") == "<p>snake_case_name</p>")
check("a link", md("[the site](https://example.edu/x)") == '<p><a href="https://example.edu/x">the site</a></p>')
check("HTML and entities pass through", md("a &rarr; <sup>2</sup>") == "<p>a &rarr; <sup>2</sup></p>")
check("a language after the fence is ignored", md("```c\nint x;\n```") == md("```\nint x;\n```") == PRE + "int x;</pre>")
check("a fence of four holds a line of three", md("````\n```\n````") == PRE + "```</pre>")
check("a raw block goes in as it stands", md("```{=html}\n<ul><li>one</li></ul>\n```") == "<ul><li>one</li></ul>")
for h in ["<p>Use <code>a &lt; b</code>, <em>not</em> <strong>this</strong>.</p>",
          "<p><a href=\"https://example.edu\">here</a> &mdash; and *stars* and [brackets]</p>",
          PRE + "if (x) {<br />&nbsp;&nbsp;y();<br />}</pre><p>Then?</p>"]:
    back_md = bq.html_md(h)
    check(f"HTML to Markdown and back is the same: {h[:40]}...", back_md is not None and md(back_md) == h, back_md)

# --- what it refuses ----------------------------------------------------------------------
def refuses(name, text, says):
    yml(name, text)
    run(name, ok=False, has=[says])

refuses("tab.yml", "questions:\n- type: SA\n  text: |\n    ```\n    \tx\n    ```\n  answers: [x]\n", "holds a tab")
refuses("key.yml", "questions:\n- type: SA\n  text: Hi.\n  answer: x\n", "a SA question has no answer")
refuses("type.yml", "questions:\n- text: Hi.\n  answers: [x]\n", "type is one of")
refuses("none.yml", "questions:\n- type: SA\n  text: Hi.\n", "needs answers")
refuses("weight.yml", "questions:\n- type: MC\n  text: Hi.\n  options:\n  - just text\n", "is `- <weight>: <text>`")
refuses("both.yml", "questions:\n- type: SA\n  text: Hi.\n  html: <p>Hi.</p>\n  answers: [x]\n",
        "one of text, html and plain_text")
refuses("fence.yml", "questions:\n- type: SA\n  text: |\n    ```\n    never closed\n  answers: [x]\n", "never closed")
refuses("lines.yml", "questions:\n- type: SA\n  text: Hi.\n  answers:\n  - |\n    two\n    lines\n", "is one line")
refuses("top.yml", "- type: SA\n", "a quiz file is `questions:`")
write_csv("unknown.csv", [["NewQuestion", "SA"], ["QuestionText", "<p>Hi.</p>", "HTML"], ["Bogus", "1"], []])
run("unknown.csv", ok=False, has=["a 'Bogus' row is not one this tool knows"])

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
