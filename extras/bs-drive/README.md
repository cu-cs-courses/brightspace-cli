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
    bs-drive.py shot     COURSE QUIZ FILE.png

`COURSE` is a label from `~/.config/brightspace/courses.ini` or an org unit id;
a label is asked of the file first, since `120` is both. `QUIZ` is a quiz id or
its exact name, and the name may be the one from before Respondus appended
`- Requires Respondus LockDown Browser` to it. **Without `--go` a verb opens the
page, prints what is there and writes nothing.** With it, the verb writes,
closes the browser, opens the page again and reads the setting back; a readback
that differs from what was asked exits 1.

It needs `chromium` on `PATH` and `websocket-client`, and `blanks` needs
`pyyaml` too, since it reads the quiz through `bs-yaml-quiz`:

    nix-shell -p chromium python3Packages.websocket-client python3Packages.pyyaml --run \
      './bs-drive.py visible 120 "Progress Exam 3"'

Chromium is started without the session's `LD_LIBRARY_PATH`: a Nix-built
Chromium finds its own libraries, and a desktop that exports a newer one, as
this machine's did after a reboot on 2026-11-18, kills it at load.

The session is `~/.local/state/brightspace/cookies.txt`, the jar `brightspace.py
session` fills; `BRIGHTSPACE_STATE` moves it, as it does for `brightspace.py`.
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
left alone too, and named. A weighted answer, `- 50: half right`, or a
`regexp:` one stops it, since the blank's answer field has no place for
either; that blank is added by hand. The points are shared among the blanks,
*part marks per blank*, which is Brightspace's default for a short answer with
several.

The two together are the questions of a quiz, from its YAML, with no hand step:

    bs-yaml-quiz.py q9.yml --msa-as-sa
    bs-drive.py import 120 q9.csv --section 'Quiz 9' --go
    bs-drive.py blanks 120 q9.yml --section 'Quiz 9' --go

and then the quiz's *Add Existing*, which is still by hand.

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
- **Deleting in the library** is the row's *Select* box, the list's *Delete*,
  and the *Delete* inside the confirmation, which says only *delete the
  selected items?*. No verb deletes; the Devshell's probes were cleared this way
  by hand-written script.
- **A throwaway profile per run, removed with the browser.** Fifteen of them
  left behind filled `/tmp` and then the root filesystem on 2026-11-18.
