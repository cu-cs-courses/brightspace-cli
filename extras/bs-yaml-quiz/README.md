# bs-yaml-quiz

A Brightspace quiz written as YAML, with each question's text in Markdown, and
the question-import CSV that puts it in a course's Question Library.

    bs-yaml-quiz.py quiz.yml             writes quiz.csv beside it
    bs-yaml-quiz.py quiz.yml --check     exit 1 if quiz.csv is not what quiz.yml makes
    bs-yaml-quiz.py quiz.csv             writes quiz.yml from a CSV made some other way
    bs-yaml-quiz.py quiz.yml -o out.csv
    bs-yaml-quiz.py quiz.yml --msa-as-sa an MSA question as a short answer with one blank; below

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
| `type` | `SA` short answer, `MC` multiple choice, `WR` written response, `TF` true or false, `MS` multi-select, `M` matching, `O` ordering, and `MSA`, a short answer with several blanks, which the CSV cannot carry whole: see [Several blanks](#several-blanks). |
| `text` | The question, in Markdown; below. |
| `points` | What it is worth. 1 when left out. |
| `answers` | `SA`: every spelling that counts, one per line. D2L compares strings, so a right answer in a spelling not listed here is marked wrong. Casing does not matter: short answers are case-insensitive by default. `- 50: half right` gives a spelling partial credit, and `regexp: true` beside it makes it a regular expression. |
| `blanks` | `MSA`: two or more, in order, each a `prompt:` and its `answers:`, which are as `SA`'s. |
| `width`, `rows` | `SA`, `MSA`: the input box, 40 wide and 1 row when left out. `width: null` leaves the box out. |
| `options` | `MC`, `MS`: in order, each `- <weight>: <text>`, the weight being the percent it earns. `feedback:` beside one is shown when it is chosen. An option is one line of inline Markdown: see [Options, choices and matches](#options-choices-and-matches). |
| `shuffle` | `MC`, `MS`: `false` keeps the options in the order written. Left out, they are shuffled for each student: see [Shuffled options](#shuffled-options). |
| `correct` | `TF`: `true` or `false`. `weights: [TRUE's, FALSE's]` instead of 100 and 0, and `true_feedback` and `false_feedback`. |
| `choices`, `matches` | `M`: choices numbered from 1 in order, and matches as `- <choice's number>: <text>`. Each is one line of inline Markdown, as an option is. |
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

## Options, choices and matches

An option of an `MC` or `MS` question, and a choice or a match of an `M`
question, is one line of inline Markdown, read as a paragraph's inline is:
`` `code` ``, `*emphasis*`, `**strong**`, a link, and a backslash before
punctuation to write it literally. Where the Markdown makes no markup, the
text goes into the CSV as written with nothing in its fourth cell, exactly as
it always has; where it does, the cell holds the HTML and the fourth cell says
`HTML`, D2L's marker for an option, a choice or a match written as HTML. So an
existing quiz makes the same CSV as before, unless an option of its happened
to read as Markdown: `x*y*z` is `x`, then `y` in italics, then `z`, and wants
`x\*y\*z`.

Unlike a question's text, a cell does not take HTML: `<b>x</b>` written in an
option is shown as written, as it always was, because `List<Shape>` cannot be
told from a tag and has to stay text. In a cell with Markdown, D2L reads the
whole cell as HTML, so put a `List<Shape>` there in a code span.

A program never goes in an option. One line of code is a code span; a program
goes in the question's text, labelled, and the options name the labels.

Going back from a CSV, an `HTML` cell is written as the Markdown that renders
to it, and refused where Markdown cannot say it; a plain cell that would read
as Markdown is escaped, `\*stars\*`, so that it comes out as written.

## Several blanks

Brightspace's short answer can have several blanks, each with answers of its
own, and the CSV cannot say which blank an answer is for (below, under what the
CSV has to get right). So a question with several blanks is written as `MSA`,
D2L's own name for it, and goes in by halves: the CSV carries the question and
its first blank, and the other blanks are added by hand in the Question
Library.

```yaml
- title: Q3 Two names after the loop
  type: MSA
  points: 2
  text: |
    You run this:

    ```java
    int x = 1, y = 2;
    x += y;
    ```

    What do the names hold now?
  blanks:
  - prompt: "`x` holds"
    answers: [3, three]
  - prompt: "`y` holds"
    answers:
    - 2
    - two
```

**It is refused unless the run says `--msa-as-sa`**, so that a question does not
lose its blanks without anyone deciding it should. With the flag:

- **The question's text keeps every prompt**, in order, as a numbered list after
  the text, so a student can tell the boxes apart: the second box is for the
  second item. A prompt is one line of Markdown, as a paragraph's inline is.
- **Its one box takes the first blank's answers.** The others are dropped from
  the CSV, and nothing else is.
- **The run prints the rest**: each blank after the first, its prompt, and every
  answer it takes, one to a line, with a weight or `regexp` beside it in
  brackets. Add them in the Question Library after the import.

```
wrote quiz.csv: 8 questions
Then by hand, in the Question Library after the import, since the CSV carries one blank of each MSA question:
question 3 (Q3 Two names after the loop): blank 1 of 2 imported
  blank 2, for: `y` holds
    2
    two
```

**`bs-drive.py blanks` adds them for you**, from the same YAML, once the CSV is
imported: [`../bs-drive/`](../bs-drive/README.md), whose `import` verb does the
import as well.

**A blank holds answers of one kind, at full marks.** Every answer of a blank is
worth the blank's share of the question's points, whatever weight it came in
with, so `- 50: 1001010` in a blank would count in full: it is refused, with
the blank named, when the CSV is made. And a blank's answer type is one, Text
or Regular Expression, for all its answers, so a blank may be all `regexp:
true` answers, which bs-drive sets as such, but not some of each. Measured on
a sandbox, 2026-11-24, after 100's Progress Exam 1 make-up met both.

`--check` takes the flag too, and refuses an `MSA` question without it, as the
run that writes does. A blank is a mapping, never a bare list of answers:
`- [3, three]` under `blanks:` is refused, since `- 3` and `- three` written one
under the other would otherwise be two blanks where one blank and two spellings
were meant. One blank is a short answer: write it as `SA`. The prompts need
HTML, so `MSA` takes `text:` or `html:`, not `plain_text:`.

Going back, a CSV holds no `MSA`: what one made comes back as the `SA` it went
in as, its prompts a raw HTML block.

`brightspace.py setup-quiz` reads the quiz through this, and takes an `MSA`
question as `--msa-as-sa` writes it, since it compares the CSV beside the file
and never writes one. The blanks after the first are among the steps by hand it
ends with, between the import and *Add Existing*, so a quiz gets its questions
whole; and where the CSV is missing or stale, the command it names to make it
says `--msa-as-sa`. The web page's CSV to download is made the same way.

## Shuffled options

An `MC` or `MS` question's options are shuffled for each student, unless the
question says `shuffle: false`, which keeps them in the order written: options
that are numbers in order, say, or one that only makes sense last. Shuffled is
the default because a quiz is written with its right option first more often
than not, 71 of the 96 `MC` and `MS` questions in three courses' quizzes on
2026-11-19, and in the order written that is option A in three questions of
four.

```yaml
- title: Q4 How many times
  type: MC
  shuffle: false
  text: How many times does the loop run?
  options:
  - 0: '2'
  - 100: '3'
  - 0: '4'
```

The CSV has no row for it, and D2L's import leaves every question's
*Randomize answer order* off, so the CSV is the same with the key or without
it, and a run ends by listing the questions whose box is to be ticked in the
Question Library. **`bs-drive.py shuffle` ticks them** from the same YAML,
before *Add Existing*, or afterwards in the quiz's copies too:
[`../bs-drive/`](../bs-drive/README.md). `setup-quiz --go` runs it between the
import and *Add Existing*, and `setup-quiz --check` reads each quiz question's
setting back.

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
| `date` | The day it runs, when `start` and `end` are times of day, as `14:00`. |
| `like` | The quiz it copies its settings from: shuffle, auto-publish, paging and the rest. The course's last quiz when left out. |
| `start`, `end` | Local time, as `2026-10-06 12:30`; the end is its due date too. No dates when left out. |
| `minutes` | A time limit, enforced, or `none` for a quiz with no timer, an exam. The copied quiz's when left out, unless the course's defaults name one. |
| `attempts` | How many. 1 when left out. |
| `ip`, `password` | The addresses it can be taken from, as `148.137.150.0-148.137.150.255`, and a password. |
| `points` | What its grade item is out of, when one is made. The copied item's when left out. |
| `grade_item` | `none` for no grade item. Left out, the quiz goes to an item of its own name, made like the one `like` sends its scores to if there is none yet. |
| `description` | What students read before they start, in Markdown, as a question's text is. |
| `calendar` | `false` keeps its dates out of the course calendar. The copied quiz's when left out. |
| `special_access_only` | `true` shows it only to users given special access, which the UI adds. For a sitting meant for one student: made this way, there is no moment at which the course sees it. |
| `shell` | Make it in this empty shell, to copy into the course's sections afterwards. |
| `lockdown` | `true` requires Respondus LockDown Browser, `false` turns it off. The copied quiz's setting when left out. |
| `lockdown_domains` | The sites LockDown Browser lets a student open, as a list, one to a line, or one line with commas: host names, `cu-cs-courses.site`, never an address with `https://`. Gives `lockdown: true` when that is left out, and is refused beside `lockdown: false`. Without it, a required LockDown lets no other site open. |
| `visible` | `true` shows the quiz to students once its questions are in; `false` leaves it hidden. Left out, it is left hidden and the steps end by saying to show it. |

Whatever the block leaves out comes from the course's quiz defaults, a file of
blocks in these same keys, and `setup-quiz` takes each key as a flag too;
brightspace-cli's README has both.

**No student's name in a name, a description or a header.** Brightspace once emailed a hidden quiz's name the moment it was made, for a reason not pinned down, so names stay off activities altogether: `setup-quiz`, `new-quiz`, `set-quiz`, `new-item` and `new-folder` refuse any classlist student's first or last name, and a sitting for one student is theirs by `special_access_only`, never by its name.

The quiz is made hidden, to be shown once its questions are in. No API route
creates a question, nor sets LockDown Browser, so **`setup-quiz --go` hands
those to [`bs-drive`](../bs-drive/README.md)** when it can drive a browser: it
writes the CSV, imports it into a library section named for the quiz, adds the
blanks of each `MSA` question, shuffles the options of each `MC` and `MS`
question that does not say otherwise, puts the questions in the quiz, sets LockDown
and its sites, and then shows the quiz if `visible: true`, each step read back
before the next. One call, about five and a half minutes for a two-question
quiz with one `MSA` question, LockDown and two sites, on 2026-11-19. Where it
cannot, `chromium` not on `PATH` or `--by-hand`, the same steps are printed for
a person, the sites as one line with commas, which is how Respondus's box takes
them.

```yaml
brightspace:
  course: 120
  name: Quiz 9
  lockdown: true
  lockdown_domains:
  - cu-cs-courses.site
  - docs.oracle.com
  visible: true
```

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
- **An option written as HTML needs `HTML` in its fourth cell**, as a choice
  or a match does and an ordering item in its third, or D2L shows the tags
  as text. A plain option leaves the cell empty. Measured on a sandbox,
  2026-11-18: code spans, emphasis, links and a bare `<` beside them all
  render, in options, choices and matches alike.
- **A short answer has one box.** Brightspace's own questions can have several
  blanks, each with answers of its own, but the CSV cannot say which blank an
  answer belongs to: two `InputBox` rows import as a single box that takes
  every answer listed, and an `MSA` question is not imported at all. Measured
  on a sandbox, 2026-10-04. A question that wants two values is two questions
  over the same program, or an `MSA` in the YAML, written with `--msa-as-sa`
  as a short answer with every prompt and its first blank, the other blanks
  added by hand: see [Several blanks](#several-blanks).
- **Options import in the order written.** The CSV has no row for a
  question's *Randomize answer order*, and the import leaves it off: see
  [Shuffled options](#shuffled-options).
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
