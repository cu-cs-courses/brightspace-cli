# brightspace-cli

`brightspace.py` reads a course's Brightspace through its API, logged in as
you: the entries students hand in to, the Journal, submitted files, quizzes,
grade items, the classlist. A few commands write — a folder, a quiz, a grade
item — and each one prints what it is about to send and reads the result back.

It is one file, standard library only, plus `lz4` to read a session out of
Firefox. Written for Commonwealth University's Brightspace, whose CU accounts
log in through single sign-on; `--base-url` or `$BRIGHTSPACE_URL` names any
other host.

    git clone https://github.com/cu-cs-courses/brightspace-cli ~/brightspace-cli
    pip install --user lz4         # or on Nix: python3.withPackages (ps: [ ps.lz4 ])
    cd ~/brightspace-cli
    ./brightspace.py session       # logged in to Brightspace in Firefox first
    ./brightspace.py init --sites ~/courses
    ./brightspace.py folders 240

`init` writes the two files the rest of this page refers to, a label for each
course you teach and your session for the keep-alive, and `--sites` is
optional. [Where its state lives](#where-its-state-lives) lists every file the
tool keeps or reads.

Python 3.11 or newer; the suite runs on 3.11 and 3.13.

## Commands

    ./brightspace.py session                reads the session out of Firefox; nothing to copy
    ./brightspace.py session --how          that, and every other way in
    ./brightspace.py session --export       that, printed for someone else's keepalive file
    ./brightspace.py init                   writes the courses file and the keepalive file
    ./brightspace.py courses                your enrollments, each with its org unit id
    ./brightspace.py folders 240            the entries: id, name, due, hidden, who has handed in
    ./brightspace.py journal 240            today's Journal entries, printed and saved
    ./brightspace.py submissions 240 'Assignment 4' --download
    ./brightspace.py quizzes 240            each quiz with how many took one attempt and two
    ./brightspace.py quiz 240 'Midterm Part 1'   one quiz's settings, IP restriction included
    ./brightspace.py classlist 240
    ./brightspace.py announcements 240
    ./brightspace.py ping                   one call, to keep the session from idling out
    ./brightspace.py keepalive              the same for every session in the keepalive file
    ./brightspace.py logout

`--json` prints what the API returned instead of a table. `--help` on any
command says the rest.

## Where its state lives

Everything the tool keeps, and everything it reads apart from Brightspace:

| Path | What it holds | Written by |
|---|---|---|
| `~/.local/state/brightspace/` | Your session. `cookies.txt` has the two cookies; `session.json` has where they came from, the write token, the API versions, and the bearer token if `--token` gave one. Mode 0600, in a 0700 directory. | `session`, and again when a dead session is re-read from Firefox. `logout` deletes both files. |
| `~/.config/brightspace/courses.ini` | [Your course labels](#courses). | `init`, then you. |
| `~/.config/brightspace/keepalive.ini` | [The sessions `keepalive` pings](#keeping-sessions-alive): yours, and any a colleague hands over. Mode 0600, or it is refused. | `init` writes yours. A colleague's is the block their `session --export` prints. |
| `~/.config/systemd/user/brightspace-keepalive.*` | The timer that runs `keepalive`, if you install it. | You, from `systemd/`. |
| A Firefox profile's `sessionstore-backups/recovery.jsonlz4` | Where `session` finds the cookies. | Firefox. Only read here. |
| A course site's `_quarto.yml`, `assignments.yml` and `config/brightspace.yml` | The org unit id, and what `setup` makes. | You. Only read here. |
| A course's `dropbox/journals/` and `dropbox/submissions/` | Downloads: student work. | `journal`, `submissions --download`. |

`$BRIGHTSPACE_STATE_DIR` names another session directory and
`$BRIGHTSPACE_COURSES` another courses file; `$XDG_STATE_HOME` and
`$XDG_CONFIG_HOME` move all three of the first files as usual. No password is
ever kept. `login`, for a local D2L account, drops its password as soon as the
request is sent.

## Courses

A course is named by a label from `~/.config/brightspace/courses.ini` (or the
file `$BRIGHTSPACE_COURSES` names), or by its bare org unit id — the number in
its `/d2l/home/` URL, which `courses` lists.

**`init` writes the file** from your enrollments: a label for each course you
teach that has not ended, the number in its name (`CMSC-240-01-Fall 2026` is
`240`), with the section added when you teach several (`115-01`). `--sites
DIR` looks under DIR, up to three directories down, for each course's website
repo. A course found there gets its `site`, and its `dropbox` when one sits
beside the site, as in a course directory holding `website/` and `dropbox/`.
`init` never touches a file that exists. Run it again next term and it prints
the sections for the courses the file does not name yet.

The file says where each course's files are:

```ini
[240]
site = ~/courses/cmsc-240/website
dropbox = ~/courses/cmsc-240/dropbox

# One site, three sections: an ou each.
[115-01]
site = ~/courses/cmsc-115/website
ou = 4100001

[115-02]
site = ~/courses/cmsc-115/website
ou = 4100002
```

- **`site`** is the course's website repo, one built from
  [course-template](https://github.com/cu-cs-courses/course-template). The org
  unit id is read off `urls.brightspace` in its `_quarto.yml`, so it is typed
  nowhere else, and `setup` reads the rest of the site's files.
- **`ou`** names the org unit id instead. It wins over the site's, which is how
  a site serving several sections names each one, and it is all a course with
  no site needs.
- **`dropbox`** is where `journal` and `submissions --download` write, under
  `journals/` and `submissions/`. Without it they want `--out`.

Comments go on lines of their own. A relative path is read from the file's own
directory, and through a symlink: a courses file kept beside the courses it
names can be linked into `~/.config/brightspace/` and still find them.

Only `urls.brightspace` itself is read, never one nested deeper. A site serving
several sections carries one per section under `meetings:`, and the first of
those used to be taken for the course's.

## Getting in

**There is no password to give this tool.** Commonwealth logs in through single
sign-on: the button on the Brightspace login page goes to
`passhe.proxy.cirrusidentity.com` and on to Microsoft Entra. The
username-and-password form on that same page is for **local** D2L accounts, and
CU credentials posted to it come back `loginFailed`. `session` is the way in:
log in in Firefox as usual, and it reads that session from the profile.
`login` is kept for a local D2L account, and says so when it is handed CU
credentials.

**Where it reads from, which is not where you would look.** Brightspace sets its
two session cookies with no expiry, and Firefox keeps cookies without an expiry
in the session store rather than in `cookies.sqlite`, so that a restarted browser
can restore them. Measured 2026-09-26: zero rows in `cookies.sqlite` for the
host, both cookies present in `sessionstore-backups/recovery.jsonlz4`. That file
is mozlz4, which is what `lz4` is for. Firefox rewrites it every few seconds, so
a login you just did can take a moment to turn up.

**An expired session is re-read on its own.** Any command that gets a 403 goes
back to the same profile once, and carries on if Firefox has a live session
there. So `session` is normally run once, and the rest of the term is just
commands. It only ever re-reads the source you chose, it says so on stderr when
it does, and it says something different when the file holds the same dead
session, which means Firefox is logged out too.

**The other ways in.** `--paste` prompts instead, which is how any other browser
gets in; [the next section](#copying-the-two-cookies-by-hand) says where the
cookies are. `--token` takes a bearer token that
lasts an hour and leaves the cookies in the browser; `session --how` prints the
console line that mints one. Chromium cannot be read from disk, because it
encrypts cookie values against the desktop keyring. `--from-firefox` is the
default said out loud: it fails instead of falling back to the prompts, which
is what a script wants. `--profile` names a profile when the most recently
written one is the wrong one.

**Brightspace sets both session cookies on a *refused* login,** so their
presence proves nothing: every way in confirms with `whoami` before saving. And
a session being replaced drops the token it left in memory first, or the check
would bless the old session while the new paste goes untested.

**This was got wrong first, and the way it was got wrong is the lesson.** The
login page was fetched and searched for a link to an identity provider; there is
none, because the SSO button is a `<button>` with a JavaScript handler. "No SSO"
was then written down, and the first real login failed. A page's links are not
its behaviour.

`session` takes the browser's route, not the documented one — that is an OAuth
app registered by the Brightspace admin, worth asking for if this becomes
routine. It works because `/d2l/api/` checks a session the way the pages do. A
Brightspace upgrade could close it: the sign is `whoami` failing on a session
that still works in the browser.

**The session is student data.** An instructor's session reaches every grade in
every course. It lives in `~/.local/state/brightspace/` (or
`$BRIGHTSPACE_STATE_DIR`), mode 0600, outside every repo, and `logout` deletes
it. Logging out in the browser ends a pasted cookie session too, which is the
cheapest way to revoke it. Prefer `--token` for a one-off: it expires in an hour
on its own.

## Copying the two cookies by hand

A session is two cookies, `d2lSessionVal` and `d2lSecureSessionVal`, and both
are needed. You need them by hand to hand a session to someone else's
[keepalive file](#keeping-sessions-alive), and for `session --paste` from a
browser other than Firefox.

**With this tool and Firefox, there is nothing to copy by hand.** Log in to
Brightspace in Firefox, then:

    ./brightspace.py session --export

It takes the session the way `session` always does, checks it, and prints it as
a keepalive entry under your first name:

```ini
[ada]
cookies = d2lSessionVal=…; d2lSecureSessionVal=…
```

Only those two lines go to standard output, so `| wl-copy`, or `| pbcopy` on a
Mac, puts exactly them on the clipboard. The steps below are for any other
browser.

**From the developer tools' cookie list,** in the browser that is logged in to
Brightspace:

1. Open any Brightspace page, logged in, and press F12 (⌥⌘I on a Mac).
2. Open the list of cookies for `https://commonwealthu.brightspace.com`:
   - **Firefox:** the **Storage** tab, then **Cookies** in the left column,
     then that address.
   - **Chrome or Edge:** the **Application** tab, then **Storage → Cookies** in
     the left column, then that address.

   A tab that is not shown is under `»`.
3. Find the row `d2lSessionVal`, double-click its **Value**, and copy it. Then
   do the same for `d2lSecureSessionVal`.

Each value is 36 characters on Commonwealth's instance, with no spaces and no
semicolons. A longer copy picked up part of the next column.

**Or both at once, from a request.** In the **Network** tab, reload the page
and click the first request, which is the page itself. Under **Request
Headers**, the value of `Cookie` is one line holding every cookie the site
sets, these two among them. Copy all of it, and the tool picks out the two.
Firefox's *Copy as cURL* is one line too and also works. Chromium's spans
several lines, and a prompt reads only the first.

**Not from the console.** Both cookies are HttpOnly, so `document.cookie` does
not show them. The cookie list and the request headers do.

**Where they go.** In a keepalive file, on one line under the session's name:

```ini
[colleague]
cookies = d2lSessionVal=<the first value>; d2lSecureSessionVal=<the second value>
```

A whole Cookie header pasted after `cookies = ` works as well. Then run
`brightspace.py keepalive` without `--quiet`. It should print
`colleague  alive:` with that person's username, which also checks that these
are the right person's cookies. For `session --paste`, answer its two prompts
with the two values, or paste the Cookie header at the first.

**Treat them as a password.** Whoever holds both is logged in as you, every
grade included, until the session ends. With a keep-alive pinging it, it never
ends by idling. Logging out of Brightspace in the browser they came from ends
it, which is also how to take them back. Closing the browser does not.

## Keeping sessions alive

**A session dies sooner than the login page's "180 minutes" suggests.** That
number is the identity provider's; a session here was dead after about fifty
minutes idle, which fits D2L's own default of thirty. `ping` is one
authenticated call, which counts as activity for the session the browser is
holding, so it keeps Firefox logged in too. A ping that finds the session dead
still runs the Firefox re-read, so it also repairs the session whenever the
browser has a live one.

**`keepalive` pings a list of sessions**, which is how one machine that is
always on keeps several people logged in. The list is
`~/.config/brightspace/keepalive.ini`, mode 0600, a `[name]` per session:

```ini
[me]
state = ~/.local/state/brightspace

[colleague]
cookies = d2lSessionVal=...; d2lSecureSessionVal=...
```

- **`state`** is a directory `session` saved a session in, and one taken from
  Firefox is re-read from there when it dies, as it would be by any command.
  `init` writes yours.
- **`cookies`** is a session handed over whole, in any one-line form `session
  --paste` takes. Its owner's `session --export` prints the entry ready to
  paste; [Copying the two cookies by hand](#copying-the-two-cookies-by-hand)
  covers other browsers.
  Nothing can repair one of these. Once it dies, every run says so, until
  fresh cookies replace it.

Every session is tried even when one fails, and the exit status is 1 if any
did. The name is only for the output, and nothing else a line holds is ever
printed, not even in an error about the file. A file anyone else can read is
refused, the way ssh refuses a private key.

**Run it from a timer.** `systemd/` has a user unit and its timer, every
fifteen minutes:

    mkdir -p ~/.config/systemd/user
    cp systemd/brightspace-keepalive.* ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now brightspace-keepalive.timer
    loginctl enable-linger "$USER"      # keep pinging while you are logged out
    journalctl --user -u brightspace-keepalive -n 20

The unit expects the clone at `~/brightspace-cli`; edit `ExecStart` in your
copy if it is elsewhere. It skips, rather than fails, until the keepalive file
exists. `--quiet` keeps the journal to failures, and a failure is a line rather
than an incident: the next run tries again. Anything that runs `keepalive
--quiet` every fifteen minutes does the same job, cron included. A NixOS system
unit doing it is
[brightspace-keepalive.nix](https://github.com/ulysses4ever/dotfiles/blob/main/machines/um690/brightspace-keepalive.nix).

**What it costs.** Sessions that do not idle out, which are also browsers that
stay logged in to Brightspace. A session ends when the machine holding it goes
down, or when `logout` or a browser logout ends it, and it exists only on that
one machine. That is why the machine stays locked, rather than trusting the idle timeout.
Whoever can read the keepalive file holds every session in it, so a
colleague's cookies belong only on a machine they would trust with their login. The permanent fix remains a registered OAuth
application, whose refresh tokens make all of this unnecessary.

## Reading

**Downloads mirror the Download button.** One directory per student,
`<user id>-<folder id> - First-Last`, holding `My journal-Sep 8, 2026 112 PM.html`
for a text entry or the files handed in, under the course's
`dropbox/journals/` or `dropbox/submissions/` — so a dump made here reads like
one made by hand, and nothing lands in a repo. `journal` takes today by default;
`--on`, `--since` and `--all` widen it, `--no-write` only prints.

**What `quiz` is for.** A quiz that carries an exam has settings that are wrong
in ways the UI does not show you at a glance: an IP restriction that does not
cover the room, a password nobody was told, attempts left at one when the design
wants two. `quiz <course> <name>` prints all of it on one screen, including
`RestrictIPAddressRange` as Brightspace actually stored it, so the configuration
can be checked the morning of the exam in one command rather than five tabs.

## Writing

    ./brightspace.py new-category 240 Midterm --weight 35
    ./brightspace.py set-item 240 Midterm --name 'Midterm Part 1' --category Midterm
    ./brightspace.py new-quiz 240 'Midterm Part 1' --start '2026-09-29 12:30' \
        --end '2026-09-29 13:25' --minutes 50 --attempts 1 \
        --ip 148.137.150.0-148.137.150.255
    ./brightspace.py set-quiz 240 'Quiz 3' --shuffle --auto-publish
    ./brightspace.py new-folder 120 'Assignment 5' --like 'Assignment 4' \
        --open '2026-09-30 00:01' --due '2026-10-06 23:59:59' \
        --link https://example.edu/cmsc-120/assignments/a5-arrays.html
    ./brightspace.py new-item 120 'Assignment 5' --like 'Assignment 4' --folder 'Assignment 5'
    ./brightspace.py setup 120 a6 [--go|--check]
    ./brightspace.py set-folder 240 'Assignment 5' --show
    ./brightspace.py delete-quiz 230 Untitled --go

Each prints the exact body it is about to send, `--dry-run` stops before
sending, and after sending **each reads its object back** and prints `checked
on the server:` with the fields that landed — or stops and names the ones that
did not. This API answers 200 and silently drops what it does not understand, so
a write is not done until it has been read back. The gradebook commands print
the weight total before and after, because the number that has to be 100 is the
one thing a gradebook write can quietly break: creating a category before
moving an item into it puts the total at 135 until the second call lands, so
run the pair back to back.

**`setup` makes an assignment's folder and column with nothing typed.** It
reads three files in the course's site:
- the assignment's entry in `assignments.yml`, for its dates;
- `_quarto.yml`'s `urls.site`, for its page;
- `config/brightspace.yml`, for what stays the same every week: a folder or
  only a grade item, what they are called, when a folder opens, and whether it
  starts hidden.

A course with a folder gets the folder, then the grade item attached from the
item's side, as below. A course that hands in elsewhere gets the item alone.
The settings come from the assignment before; `--like` names another, and A1
always needs it. Bare prints the plan, `--go` creates, and `--check` sets
Brightspace beside the files and exits 1 on any difference, so a deadline
moved in `assignments.yml` and not on Brightspace gets noticed.

**They copy a shape that already works** rather than building a payload out of
the documentation. `new-category` clones an existing category, `new-quiz` an
existing quiz and `new-folder` an existing folder (`--like` names which), and
each overrides only the fields you pass. `new-quiz` forces nothing it was not
told: shuffle and auto-publish come from the quiz it copies. `set-item`,
`set-quiz` and `set-folder` read the object, change the named fields and send
the rest back untouched, because the update route replaces the whole object.

**But a copy is taken in the read shape, and a few fields differ on the way
in.** Rich text is `{"Content", "Type"}` going in and `{"Text", "Html"}` coming
out, and a quiz's attempts go in as a flat `NumberOfAttemptsAllowed` while they
come out nested in `AttemptsAllowed`. The first mismatch is dropped without a
word — an assignment folder went out with its instructions silently empty that
way — and the second fails the whole quiz as a *JSON Binding Error*. The
dropbox's *feedback* route is the other way round, and keeps only `{Text, Html}`.

**`new-folder` is the entry students hand in to.** What differs between two
assignments in one course is the name, the two dates and the link; submission
type, what a resubmission does and how many points it is out of are course
policy and carry over untouched. The instructions it writes are the
assignment's URL and nothing else: the text lives on the course site, and a
second copy inside Brightspace is a copy that goes stale. It refuses a name
that already exists, so running it twice is safe, and it leaves the folder
hidden unless you pass `--active`.

**`new-item` is a gradebook column**, cloned from a named item — never the
newest, which is as likely to be the final exam — so its category, points and
scale carry over. Inside a category the category owns the weight: sending one
is refused (*"Cannot set grade weight directly when weight is specified by
grade category"*), and the category spreads its own over the new item, so the
weights are printed before and after. `--folder` attaches it to an assignment
folder **from the item's side**, which is the one way to link the two without
rewriting the folder: a folder update replaces the whole object, and it cannot
send back a link attachment the folder may carry.

**`set-folder` shows a folder to students or hides it, and changes nothing
else.** It sends back every setting it read, with the instructions reshaped
into the form the API accepts. A link or a file attached in the web page cannot
be sent back, so a folder holding either is refused; change that one in the
web page.

**`delete-quiz` removes only a quiz with nothing in it**: no attempts, no
questions, hidden from students, and sending its scores to no grade item.
Attempts are student work, questions are authoring no route can put back, and a
visible or graded quiz is in use, so any of those is refused with every reason
listed. What is left is the blank a stray click on *New Quiz* leaves behind.
Without `--go` it only says whether the quiz may go.

**A quiz copied from one that shows in the calendar needs a date.** `new-quiz`
carries the template's *Display in calendar* over, and Brightspace refuses such
a quiz with no `--start` or `--end`: "Cannot have schedule association without
having either a Start Date or End Date."

**Writing needs the XSRF token, and the tool reads it for itself.** A logged-in
page hands its own scripts the token inline, so `session` fetches `/d2l/home`
once the cookies are in and says `write token: read off /d2l/home` — never the
value. A write reads it again if it is refused, since a token lives exactly as
long as its session. For the rare page that does not carry it, `session --xsrf`
asks for it: F12, Console, `localStorage['XSRF.Token']`, and paste the result
without the quotes.

**Try a new kind of write in a sandbox first.** Ask for a sandbox course
offering (a "Devshell") where nothing is live; a bare org unit id works
wherever a course is named. **Match its gradebook to the course's, or the
rehearsal lies:** a sandbox that grades by *Points* drops a category's weight by
design and reads it back `null`, so a weight that is fine in a *Weighted* course
looks broken there.

## What the API does not have

*Quiz questions.* There is no route that creates one, at any version: questions
live in the course's Question Library rather than in a quiz. So a quiz is made
in two steps, `new-quiz` for the shell and its restrictions and the questions by
hand, or by D2L's CSV question import into the library. QTI import would not
import on Commonwealth's instance.

*Surveys*, *special access*, *submission views* and *shuffle per section* are
not part of the quiz object at all; what students see after an attempt is set
in the UI. *Link attachments* on a folder are read-only: the documentation lists
them in the folder it returns and nowhere in the data it accepts, and one sent
anyway is dropped.

*Announcements* cannot be posted on Commonwealth's instance. Creating one is
refused with 400 *Invalid Parameters* in every shape the documentation allows:
JSON and multipart/mixed, the body as `RichTextInput` and as `RichText`, every
documented field, published and draft, LE 1.50 to 1.99, with cookies and with a
minted bearer token. `announcements` reads them, which works.

*Per-question quiz results.* `quizzes` gives the attempt counts, which is the
denominator the Statistics page does not print.

## Tests

    python3 test_brightspace.py
    MOCK_COOKIES_OK=0 python3 test_brightspace.py     # /d2l/api/ refuses cookies: the token fallback

They run the tool as a subprocess against a fake Brightspace, which is also
where the API's JSON shapes are written down. **Everything in it is invented**
— the students, their ids, what they wrote. Never paste a real API response
into it: a classlist or a Journal is student data, and this repository is
public.

## License

MIT; see `LICENSE`.
