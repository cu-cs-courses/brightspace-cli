# bs-yaml-quiz

A Brightspace quiz written as YAML, with each question's text in Markdown, and
the question-import CSV that puts it in a course's Question Library.

    bs-yaml-quiz.py quiz.yml             writes quiz.csv beside it
    bs-yaml-quiz.py quiz.yml --check     exit 1 if quiz.csv is not what quiz.yml makes
    bs-yaml-quiz.py quiz.csv             writes quiz.yml from a CSV made some other way
    bs-yaml-quiz.py quiz.yml -o out.csv

It needs PyYAML (`pip install pyyaml`, or `python3.withPackages (ps: [ ps.pyyaml ])`
on Nix) and nothing else.

**The YAML is the source and the CSV is generated.** Edit the one and run this
to get the other. Never edit the CSV by hand: its quoting, and its HTML squeezed
onto one line, are exactly what this tool exists to get right.

`example.yml` is a whole quiz with one question of each kind it is likely to
meet; copy it to start one.

## A quiz

```yaml
questions:

# Comments are for whoever edits the quiz next: why a question is multiple
# choice, which reading it comes from, what each wrong option stands for.
- title: Q1 What it prints
  type: SA
  text: |
    You run this:

    ```java
    int total = 0;
    for (int i = 1; i <= 3; i++) {
        total += i;
    }
    System.out.println(total);
    ```

    What does it print? *Write the number.*
  answers:
  - 6
  - six

- title: Q2 Why it compiles
  type: MC
  text: Why does `Object o = "hi";` compile?
  options:
  - 100: Because every class, String included, is a subclass of Object.
  - 0: Because the compiler converts the text to an Object at run time.
  - 0: Because Object is an interface that String implements.
```

A file is `questions:` and the list of them, in the order they import. Each
question is:

| key | what it is |
|---|---|
| `title` | The question's name in the Question Library. Students see it only if the quiz is set to show titles. |
| `type` | `SA` short answer, `MC` multiple choice, `WR` written response, `TF` true or false, `MS` multi-select, `M` matching, `O` ordering. |
| `text` | The question, in Markdown; below. |
| `points` | What it is worth. 1 when left out. |
| `answers` | `SA`: every spelling that counts, one per line. D2L compares strings, so a right answer in a spelling not listed here is marked wrong. Casing does not matter: short answers are case-insensitive by default. `- 50: half right` gives a spelling partial credit, and `regexp: true` beside it makes it a regular expression. |
| `width`, `rows` | `SA`: the input box, 40 wide and 1 row when left out. `width: null` leaves the box out. |
| `options` | `MC`, `MS`: in order, each `- <weight>: <text>`, the weight being the percent it earns. `feedback:` beside one is shown when it is chosen. Options are plain text: D2L shows them as written, so Markdown and HTML are both wrong there. |
| `correct` | `TF`: `true` or `false`. `weights: [TRUE's, FALSE's]` instead of 100 and 0, and `true_feedback` and `false_feedback`. |
| `choices`, `matches` | `M`: choices numbered from 1 in order, and matches as `- <choice's number>: <text>`. |
| `items` | `O`: in the right order, each its text, or `text:` with `format:` and `feedback:`. |
| `scoring` | `MS`, `M`, `O`: D2L's scoring rule, such as `RightAnswers` or `EquallyWeighted`. |
| `initial_text`, `answer_key` | `WR`: what the box starts with, and the grader's key. |
| `id`, `difficulty`, `image`, `hint`, `feedback` | D2L's own rows of those names. |

**A list repeated between questions is written once,** with a YAML anchor:
`answers: &compile-error` and the list on the first question that has it, and
`answers: *compile-error` on the next. An anchored list can also be one item
of another, `- *compile-error`, and its items are spliced in where it stands,
so a question can take the shared spellings and add its own. The same goes for
options, choices, matches and items. Anchors do not reach across files.

**Every value is read as the text written.** An answer of `010`, `0.10`, `yes`
or `true` is exactly that, and never a number or a boolean: the file is read
with PyYAML's `BaseLoader`. Quote a value only where YAML itself needs it, as
for text beginning with a quote or holding `: `.

## The text, in Markdown

A question's text is Markdown, rendered here rather than by a Markdown library,
so that the CSV is the same every time and the same as one written by hand:

- **Paragraphs** are separated by a blank line. Lines inside one are joined
  with a space.
- **A program** is a fenced block, three backticks or more, with a language
  after the opening fence if you like; it is ignored. The program is kept
  exactly: each leading space becomes `&nbsp;`, each line ends in `<br />`,
  and `<`, `>`, `&` and quotes are escaped, inside
  `<pre style="font-family:monospace;">`. A tab is refused, since D2L renders
  one at a width of its own; indent with spaces.
- **Inline:** `` `code` ``, `*emphasis*`, `**strong**`, `[a link](https://…)`,
  and a backslash before any punctuation to write it literally: `\*`.
- **HTML** and entities such as `&rarr;` pass through as written. A block
  fenced as ```` ```{=html} ```` goes in as it stands, for whatever Markdown
  cannot say.
- **Not read:** underscores for emphasis (`_` is just an underscore), lists,
  headings, tables and images. Write those as HTML.

`html:` in place of `text:` takes the question's HTML as it goes into the CSV,
and `plain_text:` takes text that D2L is to show without reading HTML at all.

## Where it goes on Brightspace

A file can also carry a `brightspace:` block, above its questions, saying
which course the quiz goes in and when it runs. `brightspace.py setup-quiz`
makes the quiz and its grade item from it, and brightspace-cli's web page takes
the same file. The CSV carries none of it, so a quiz made by hand leaves the
block out.

```yaml
brightspace:
  course: 240
  name: Quiz 5
  like: Quiz 4
  start: 2026-10-06 12:30
  end: 2026-10-06 14:00
  minutes: 8
  attempts: 2
  points: 8
  description: |
    Eight questions on this week's reading.

    **You get two attempts**, one now and one near the end of class.
```

| key | what it is |
|---|---|
| `course` | A label from your courses file, or an org unit id. |
| `name` | The quiz's name, and its grade item's. |
| `like` | The quiz it copies its settings from: shuffle, auto-publish, paging and the rest. The course's last quiz when left out. |
| `start`, `end` | Local time, as `2026-10-06 12:30`; the end is its due date too. No dates when left out. |
| `minutes` | A time limit, enforced. The copied quiz's when left out. |
| `attempts` | How many. 1 when left out. |
| `ip`, `password` | The addresses it can be taken from, as `148.137.150.0-148.137.150.255`, and a password. |
| `points` | What its grade item is out of, when one is made. The copied item's when left out. |
| `grade_item` | `none` for no grade item. Left out, the quiz goes to an item of its own name, made like the one `like` sends its scores to if there is none yet. |
| `description` | What students read before they start, in Markdown, as a question's text is. |

The quiz is made hidden, to be shown once its questions are in. Those go in by
hand, from the CSV, since no API route creates a question; `setup-quiz` ends
by saying how.

## What the CSV has to get right

D2L's question-import CSV is the route into a Question Library that works where
QTI does not. A row per directive, five cells to a row, a blank row after each
question, UTF-8 with no BOM and CRLF line endings. The traps, all of which this
tool handles and one of which it cannot:

- **`QuestionText` needs `HTML` in its third cell**, or every program shows its
  `<pre>` and `<br />` tags as text.
- **Each program is one line of HTML.** A CSV cell may hold a newline, but
  D2L's parser is an unknown, and a newline it dropped would run a program's
  lines together.
- **Options cannot carry HTML.** The option's text sits where the marker would
  have to go.
- **Do not open the CSV in Excel and save it.** Its "CSV UTF-8" adds a BOM and
  mangles the HTML.
- **Importing puts questions in the Question Library only.** The quiz itself
  is made in the web page or with `brightspace.py setup-quiz`, from the block
  above, and its questions added with *Add Existing*. **Importing again after a fix adds a second copy**
  of every question; delete the first section before re-importing.

## From a CSV

A `.csv` given to it becomes a `.yml` beside it, for quizzes written some other
way. Nothing is written unless the YAML converts back to the same rows, and the
run says whether the bytes are the same too or what separates them, such as
lines that end `\r\r\n`. A program is written back as a fenced block only when
it would come out the same, and anything else as raw HTML, so nothing is lost
either way. A `//` comment row becomes a YAML comment. A row it does not know
stops it rather than being dropped. It will not overwrite a `.yml` that exists,
since that is the file with the comments in it, without `--force`.

## Tests

    python3 test_bs_yaml_quiz.py

The quizzes in them, and in `example.yml`, are invented. A real quiz carries its
answer key, and this repository is public.
