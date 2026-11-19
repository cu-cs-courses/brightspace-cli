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
    bs-drive.py shot     COURSE QUIZ FILE.png

`COURSE` is a label from `~/.config/brightspace/courses.ini` or an org unit id;
a label is asked of the file first, since `120` is both. `QUIZ` is a quiz id or
its exact name, and the name may be the one from before Respondus appended
`- Requires Respondus LockDown Browser` to it. **Without `--go` a verb opens the
page, prints what is there and writes nothing.** With it, the verb writes,
closes the browser, opens the page again and reads the setting back; a readback
that differs from what was asked exits 1.

It needs `chromium` on `PATH` and `websocket-client`:

    nix-shell -p chromium python3Packages.websocket-client --run \
      './bs-drive.py visible 120 "Progress Exam 3"'

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
- **A throwaway profile per run, removed with the browser.** Fifteen of them
  left behind filled `/tmp` and then the root filesystem on 2026-11-18.
