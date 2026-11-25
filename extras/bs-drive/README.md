# bs-drive: the quiz settings the API has no route for

`brightspace.py` makes a quiz, its grade item and its restrictions through the
API, and ends by printing what the API cannot do: the header and footer,
special access, LockDown Browser, the visibility switch. Each of those is a
form on the quiz's editor page, and this script fills the form the way a hand
does, in a headless Chromium driven over the DevTools protocol with the session
`brightspace.py` keeps. One verb per setting; each reads the setting back from a
fresh load of the page and says `checked on the server:`.

    bs-drive.py header   COURSE QUIZ --header FILE [--footer FILE] [--go]
    bs-drive.py visible  COURSE QUIZ [--hidden] [--go]
    bs-drive.py lockdown COURSE QUIZ --domains 'a.org, b.edu' [--off] [--go]
    bs-drive.py special-access COURSE QUIZ --user 'First Last' [--only] [--go]
    bs-drive.py import   COURSE FILE.csv --section NAME [--go]
    bs-drive.py blanks   COURSE FILE.yml --section NAME [--go]
    bs-drive.py shuffle  COURSE FILE.yml --section NAME [--go]
    bs-drive.py add      COURSE QUIZ FILE.csv --section NAME [--go]
    bs-drive.py shot     COURSE QUIZ FILE.png

`COURSE` is a label from `~/.config/brightspace/courses.ini` or an org unit id;
a label is asked of the file first, since `120` is both. `QUIZ` is a quiz id or
its exact name, and the name may be the one from before Respondus appended
`- Requires Respondus LockDown Browser` to it. **Without `--go` a verb opens the
page, prints what is there and writes nothing.** With it, the verb writes,
closes the browser, opens the page again and reads the setting back; a readback
that differs from what was asked exits 1.

It needs `chromium` on `PATH` and `websocket-client`, and `blanks` and
`shuffle` need `pyyaml` too, since it reads the quiz through `bs-yaml-quiz`:

    nix-shell -p chromium python3Packages.websocket-client python3Packages.pyyaml --run \
      './bs-drive.py visible 120 "Progress Exam 3"'

Chromium is started without the session's `LD_LIBRARY_PATH`: a Nix-built
Chromium finds its own libraries, and a desktop that exports a newer one, as
this machine's did after a reboot on 2026-11-18, kills it at load.

The session is `~/.local/state/brightspace/cookies.txt`, the jar `brightspace.py
session` fills; `BRIGHTSPACE_STATE` moves it, as it does for `brightspace.py`.
`$BRIGHTSPACE_SESSION` acts as a `[name]` in the keep-alive file instead, read
by `brightspace.py`'s own parser, and `$BRIGHTSPACE_COURSES` names another
courses file, whose `ou =` lines it reads, as `brightspace.py` does.
A dead session shows as an editor page that does not open.

**Try a verb in a sandbox first**, the way every write to Brightspace is tried:
a quiz in the Devshell course, made with `setup-quiz --go` from a YAML copy of
the real one. Each verb here was proved on `APelenitsyn_Devshell_01`, org unit
4207254, on 2026-11-18 and 2026-11-19, with every write read back.

## The verbs

**`header`** pastes the two HTML files into *Timing & Display → Manage Header
and Footer*. The files are what `exam.py header` prints for an exam seat, or
any HTML; a header for a quiz in LockDown links the seat with
`{OrgDefinedId}` in the address, which the editor keeps as written. The
readback compares the HTML with the file, newlines aside.

**`visible`** flips the switch at the foot of the editor and saves; `--hidden`
hides. `brightspace.py` cannot: the switch is not in the quiz object.

**`lockdown`** opens the quiz's *LockDown Browser* tab, which is a Respondus
page in an iframe, and in the quiz's row sets *Required*, ticks *allow access to
external domains*, and puts `--domains` in the box. **The box takes one
comma-separated line**, which is how `exam.py header` prints the list;
newlines are taken silently and then the browser opens each allowed site's
first page and nothing behind it, which is what happened on 2026-11-18 from a
list retyped one host per line. The verb joins whatever it is given with `, `
and warns when the stored value holds a newline. `--off` sets *Not Required*.
`--monitor` requires **Respondus Monitor**, the webcam, in the section below,
and `--no-monitor` does not; `--second-camera` ticks *Require students use a
second camera in addition to the webcam*, under Monitor's *Second Camera*,
and implies `--monitor`. Without any of the three, Monitor is left as it is;
the readback, and `--json`, carry it either way.

**`special-access`** opens *Availability Dates & Conditions → Manage Special
Access*, and if the user is not on the list, *Add Users to Special Access*,
ticks the user's box, saves the users, saves the dialog and saves the page.
`--only` also chooses *Allow only users with special access to see this quiz*,
which is how a sitting for one student is kept out of the course's sight. A
user the page does not list stops the verb with the names it does list, and
nothing is written. The user is the name as the classlist shows it. **Do not
put a student's name in the quiz's title**, see the README above on names;
the name goes to this verb's argument and nowhere on Brightspace.

**`import`** puts a question-import CSV, the one `bs-yaml-quiz.py` writes, into
a section of the course's Question Library, which is the step before a quiz's
*Add Existing*. The section is at the library's top and is made when it is not
there. **It refuses a CSV any of whose titles the section already holds**, since
an import of the same file again adds a second copy of every question; delete
those in the library first. It uploads the file, checks that Brightspace found
as many questions as the CSV has titles, presses *Import All*, and reads the
section back from a fresh load, every title by name.

**`blanks`** finishes a quiz's multi-blank questions. The CSV cannot say which
blank an answer is for, so `bs-yaml-quiz.py --msa-as-sa` imports an `MSA`
question as a short answer with its first blank only, and prints the rest.
This verb reads the same YAML, finds each `MSA` question in the section by its
title, adds the blanks after the first, types every answer into each, saves,
and reads all the blanks back. A question that already has all its blanks is
left alone; one that has something else than the first blank as imported is
left alone too, and named. **A blank of regular expressions** is set to that
answer type first, through the button beside its answers (*Answer Type for
Blank 3, Text Selected*), whose menu offers Text, Case-Sensitive Text and
Regular Expression; every blank's type is set before any answer is typed,
since a click on a type button while an answer field has the focus only takes
the focus away, and the item is clicked where the menu draws it, since
scrolling closes the menu. The readback compares every blank's answers and
type. The points are shared among the blanks, *part marks per blank*, which is
Brightspace's default for a short answer with several; and an answer's own
weight does not survive the question having several blanks: every answer of a
blank gets the blank's share (measured 2026-11-24, `1001010` imported at 50%
read back at 33.3, as `01001010` beside it). So bs-yaml-quiz refuses part
credit in a blank, and a blank of both kinds, before anything is made.

**`shuffle`** sets each question's *Randomize answer order*, which the CSV
cannot carry and D2L's import leaves off. It reads the same YAML, where an `MC`
or `MS` question's options are shuffled unless it says `shuffle: false`, opens
each such question in the section by its title, and makes its box say what the
file says, off as well as on. One browser goes from question to question, since
the editor's *Save* and *Cancel* both land on the section's list, and a fresh
one reads back each question it changed. **A question already in a quiz is
changed there too**: *Add Existing* gives the quiz a copy, and *Save* on the
library's question then asks which copies the change goes to, the library's
ticked and each quiz's not; the verb ticks them all, names the quizzes, and
reads each quiz's copy back through the API. So a quiz made before its options
were shuffled is fixed with the same verb on its section. Three questions, one
of them changed, took a minute on 2026-11-19.

**`add`** is the quiz's *Add Existing*: it opens *Browse Question Library*,
opens the section, ticks the question of each title the CSV has, checks that
exactly those are ticked and no section is, and imports them. It reads the
quiz's questions through the API before and after, so a title the quiz holds
already is refused rather than added twice, and the readback is the quiz's own
list, each title exactly once. The quiz gets the questions themselves, not the
section as a titled part of it.

**`setup-quiz --go` runs these itself**, after it makes the quiz, so a quiz
goes in from its YAML in one call, LockDown and visibility included, from the
block's `lockdown`, `lockdown_domains` and `visible`:

    brightspace.py setup-quiz q9.yml --go

It prints the `bs-drive` commands as it runs them, stops at the first that does
not read back as asked, and says what is left. Bare, it prints them as the
plan. Run by hand, they are:

    bs-yaml-quiz.py q9.yml --msa-as-sa
    bs-drive.py import   120 q9.csv --section 'Quiz 9' --go
    bs-drive.py blanks   120 q9.yml --section 'Quiz 9' --go
    bs-drive.py shuffle  120 q9.yml --section 'Quiz 9' --go
    bs-drive.py add      120 'Quiz 9' q9.csv --section 'Quiz 9' --go
    bs-drive.py lockdown 120 'Quiz 9' --domains 'cu-cs-courses.site, docs.oracle.com' --go

`setup-quiz --check` reads LockDown back through `lockdown --json`, and each
question's shuffle through the API. The web page runs both as jobs, since
five minutes is longer than a request should wait: the button sends the
browser to the job's page, which shows each step as it goes (until
2026-11-24 it set `BRIGHTSPACE_BY_HAND` and left them printed).

Proved on 2026-11-19 in the 120 Devshell with 115's Quiz 12 in-class YAML,
seven questions of which two have two blanks: the quiz-questions API read back
seven questions at their points and both blanks of each, `6` and `4`, `0` and
`0`. **In a course taught from a shell, as 115 is, a test does not go in the
shell**: `copy-quiz` copies every quiz the shell holds and refuses to run while
it holds more than one, and `delete-quiz` will not remove a quiz that has
questions, so a test quiz left there blocks the next copy until it is deleted
by hand.

**`shot`** saves a screenshot of the editor, for a look at what a verb saw.

## What the pages are made of, for the next verb

Written down because it took an evening to find, and the next verb, a CSV
import into the Question Library or a blank added to a short answer, will meet
the same things.

- **Everything is web components behind shadow roots.** `document.querySelector`
  finds nothing; the helper `deepAll(selector)` walks every shadow root. A
  button is a `d2l-button` or `d2l-button-subtle` as often as a `button`, and
  is found by the text it shows, not by an id.
- **A dialog loads same-origin iframes, nested.** *Manage Special Access* is a
  `d2l-dialog-fullscreen` whose body is an iframe holding another; the users
  page is a third. `findAll(pred)` walks every reachable frame, and `absOf(e)`
  turns an element's position into the top window's coordinates, which is what
  a DevTools mouse event takes. A click is a mouse event at a point, never
  `element.click()`, since a component's handler may listen for the pointer.
- **Search the innermost frame last so a dialog's button wins over the page's.**
  The first special-access run clicked the page's *Save* instead of the frame's
  and saved nothing.
- **The header dialog's button says *Add*, not *Save*.** Its editors are
  `d2l-htmleditor` elements wrapping TinyMCE; setting the content means
  `tinymce.get(id).setContent(html)`, then `editor.html = html`, then a
  `d2l-htmleditor-update` event, or the dialog keeps the old text.
- **A checkbox is 1 pixel wide and its label is what is clicked.** On the users
  page the box is found by its `aria-label`, *Select First Last*; matching on
  `value` ticked the wrong student once.
- **The Respondus dashboard is an iframe from `smc-service-cloud.respondus2.com`**
  and loads for twenty seconds or more; the verb reads its URL off the LockDown
  tab and opens it as the page, which is faster than waiting inside the frame.
  Its *Settings* link is under a caret on the quiz's row, and *Advanced
  Settings* hides the domains box until clicked.
- **The API's readback of a header substitutes `{OrgDefinedId}` with the
  caller's own id**, so the editor, reloaded, is the only readback that shows
  what students get.
- **The library's upload dialog keeps its file input in a closed shadow root**,
  which no script in the page reaches. The DevTools DOM domain does:
  `DOM.getDocument` with `pierce`, then `DOM.performSearch` for
  `input[type="file"]`, then `DOM.setFileInputFiles` on the node. A node id
  lasts until the next `getDocument`, so it is used at once and found again
  after any other search; the first `import` crashed on a stale one.
- **An upload stages its questions in a section of its own**,
  `TemporaryQuestionsImportSection_` and a GUID, inside the section that was
  open, and only *Import All* or the dialog's *Cancel* takes them out of it. An
  upload left at the dialog leaves that section behind with the questions in
  it; `import` cancels on every failure after the upload for this reason.
- **An import lands in the section that is open.** The list's link enters a
  section, the tree's does not; the list frame's address then carries the
  section's id as `poid`.
- **The question editor's Save takes Enter, not a click.** A mouse click on it
  sent nothing at all, with the button shown as pressed, on 2026-11-18; the
  same button focused and Enter pressed submitted the form, a `PUT` answered
  200. Its answers are tags: typed into the blank's field, each ended with
  Enter. Every blank is added before any is typed into, since a click on *Add
  Blank* after typing landed where the button had been.
- **Add Existing's dialog draws its rows in closed shadow roots too.** A text
  search finds a row's words but not the row, and a title like *Assignment* is
  found elsewhere on the page as well; so a box is found as a box, and its row
  read from the box upward: a question's reads `<title> <type> <n> points`, a
  section's `<name> Section`. Ticking every question of a section ticks the
  dialog's *select all* box by itself, which is the dialog's own bookkeeping
  and not a section ticked. A section opens with the button labeled *Expand
  <name> section*.
- **Respondus Monitor is the same settings page, below LockDown**: one radio
  group, `monitorEnabled`, whose ids start `monitorEnabledTrue_` (required),
  `monitorEnabledFalse_` and `liveProctoringEnabled_` (an instructor
  proctoring over Zoom, Teams and the like); the startup sequence's boxes
  (`FACE_PIC_CHECK_ENABLED`, `ID_CHECK_ENABLED`, `ENV_CHECK_ENABLED` and the
  rest); and, folded under the *Second Camera* section after Monitor's own
  *Advanced Settings*, `secondCameraEnabled`. **Choosing Monitor for the
  first time on a course opens Respondus's introduction over the page**, and
  every click lands on it until its *Continue to Respondus Monitor >>* closes
  it; its *Don't show this message again* is left alone. **Respondus renames
  the quiz again**: *Quiz 9- Requires Respondus LockDown Browser* with
  LockDown alone, *Quiz 9 - Requires Respondus LockDown Browser + Webcam* with
  Monitor, and the first again without it; a quiz and its dashboard row are
  found by the name with either taken off, the row's next word being its
  LockDown column, so Quiz 1 is never Quiz 10's row. All read off 100's
  Progress Exam 1 and proved on the 120 Devshell, 2026-11-24.
- **On a quiz never required before, LockDown's Advanced Settings appears only
  once Require is chosen, folded,** and its boxes are in the page but not on the
  screen. A click on one there lands on nothing: the first one-call trial saved
  LockDown required with no sites. The verb opens the section until the box is
  on the screen, by its size and `offsetParent`, and checks the box and the
  typed line before Save.
- **Randomize answer order is the same box on an MC and an MS question**,
  `input[name="question.choiceInteraction.shuffle"]`, under the options. The
  quiz-questions API reads it on a quiz's copy as `QuestionInfo`'s `Randomize`,
  on a multiple choice (`QuestionTypeId` 1) and a multi-select (4).
- **Save on a question that is in a quiz goes to *Question In Use***,
  `shared_question.d2l` in a frame of its own: a box per place the question is
  used, `gridObjects_cb`, labelled *Select <place>*, the library's ticked and
  each quiz's not, and a *Save* of its own, which a click works. The editor's
  *Cancel* goes back to the section's list as its *Save* does.
- **The quiz-questions API reads a short answer's blanks**: `QuestionInfo`'s
  `Blanks`, each with its `Answers` and an `EvaluationType`. It covers only
  questions already in a quiz; the library has no read route.
- **Deleting in the library** is the row's *Select* box, the list's *Delete*,
  and the *Delete* inside the confirmation, which says only *delete the
  selected items?*. No verb deletes; the Devshell's probes were cleared this way
  by hand-written script.
- **A throwaway profile per run, removed with the browser.** Fifteen of them
  left behind filled `/tmp` and then the root filesystem on 2026-11-18.
