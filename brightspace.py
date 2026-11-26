#!/usr/bin/env python3
"""Read a course's Brightspace through its API, logged in as yourself.

    ./brightspace.py session                     reads the session out of Firefox; no devtools, no typing
    ./brightspace.py whoami
    ./brightspace.py courses                     what you are enrolled in, with each org unit id
    ./brightspace.py folders 240                 the entries students hand in to: id, name, due, hidden, handed in
    ./brightspace.py journal 240                 today's Journal entries, printed, and written the way
                                                 Brightspace's own Download button writes them
    ./brightspace.py submissions 240 'Assignment 4' [--download]
    ./brightspace.py quizzes 240                 each quiz, and how many students took one attempt and two
    ./brightspace.py new-quiz 120 'Quiz 3' --start ... --attempts 2
    ./brightspace.py new-folder 120 'Assignment 5' --due ... --link ...
    ./brightspace.py new-item 120 'Assignment 5' --like 'Assignment 4' --folder 'Assignment 5'
    ./brightspace.py setup 120 a6 [--go|--check] an assignment's folder and grade item, every value
                                                 read off assignments.yml and config/brightspace.yml
    ./brightspace.py setup-quiz q5.yml [--go|--check]  a quiz and its grade item, from the
                                                 brightspace: block in the quiz's own YAML
    ./brightspace.py setup-quiz --course 120 --name 'Quiz 6' --date 2026-10-12
                                                 the same from flags, the rest from the course's defaults
    ./brightspace.py quiz-defaults [120 --set minutes=8]   each course's quiz defaults
    ./brightspace.py set-folder 240 'Assignment 5' --show [--submissions keep-all]
    ./brightspace.py delete-folder 240 'Assignment 9' --go   a folder nobody handed in to, to make it again
    ./brightspace.py set-quiz 120 'Quiz 3' --shuffle --auto-publish
    ./brightspace.py announcements 120
    ./brightspace.py checklists 115-01 ['Week 7']  the checklists, or one's items as new-checklist takes them
    ./brightspace.py new-checklist 115-01 115-02 --name 'Week 7' --item 'Read chapter 3' --due ... [--go]
                                                 one checklist, the same in several courses at once
    ./brightspace.py classlist 240 [--emails]
    ./brightspace.py ping                        keeps the session alive; run it from a timer
    ./brightspace.py keepalive                   pings every session in ~/.config/brightspace/keepalive.ini
    ./brightspace.py logout

Almost everything here reads. The commands that write are new-category,
set-item, new-item, new-quiz, set-quiz, new-folder, set-folder, grade,
copy-quiz, delete-quiz, delete-folder and new-checklist, setup through new-folder and
new-item, and setup-quiz through new-item and new-quiz. They print what they are about to send,
--dry-run stops before sending, and each reads its object back afterwards and
says what Brightspace kept. Nothing else changes anything.

**Commonwealth logs in through single sign-on, so there is no password to give
this tool.** The Brightspace login page's own form is for local D2L accounts;
a CU account goes out to passhe.proxy.cirrusidentity.com and on to Microsoft
Entra, and posting CU credentials to the form returns loginFailed. `session` is
therefore the way in: log in normally in Firefox, and it reads the session from
there — including again by itself when that session later expires. `--paste`
takes the cookies from the terminal instead, for any other browser. `login` is
kept for a local D2L account and says so when it is handed CU credentials.

What a session is: the two cookies the browser sends to /d2l/api/ with every
call, or a bearer token the pages mint for themselves and which lasts an hour.
Either is enough. They are read from the terminal without echo and kept in
~/.local/state/brightspace/ (or $BRIGHTSPACE_STATE_DIR), mode 0600, outside
every repo — and an instructor session reaches every grade in every course, so
that directory gets the care a marks file gets. `logout` deletes it.

This is the browser's route into the API, not the documented one — that one is
an OAuth app registered by the Brightspace admin, which is worth asking CATS
for if this becomes routine. It works because /d2l/api/ checks a session the
same way the pages do. A Brightspace upgrade can change that without notice, in
which case `whoami` fails on a session that is demonstrably good in the browser.

A course is a label in ~/.config/brightspace/courses.ini (or the file
$BRIGHTSPACE_COURSES names), or a bare org unit id. The file says where each
course's website repo is, and its org unit id is read off `urls.brightspace`
in that site's _quarto.yml, so it is typed nowhere else; `ou =` names it when
the site does not, as with one site serving three sections:

    [240]
    site = ~/courses/cmsc-240/website
    dropbox = ~/courses/cmsc-240/dropbox

    [115-01]
    site = ~/courses/cmsc-115/website
    ou = 4100001

Downloads mirror Brightspace's Download button: one directory per student,
`<user id>-<folder id> - First-Last`, holding `<Folder name>-<Sep 8, 2026 112
PM>.html` for a text entry or the files handed in, so a dump made here reads
like one made by hand. Files go under the course's `dropbox =` directory, or
--out, and never under a repo.
"""

import argparse
import base64
import collections
import datetime as dt
import functools
import getpass
import html
import html.parser
import http.client
import http.cookiejar
import importlib.util
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request

STATE = pathlib.Path(
    os.environ.get("BRIGHTSPACE_STATE_DIR")
    or pathlib.Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser() / "brightspace"
)
CONFIG = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "brightspace"
COURSES_FILE = pathlib.Path(os.environ.get("BRIGHTSPACE_COURSES") or CONFIG / "courses.ini").expanduser()
KEEPALIVE_FILE = CONFIG / "keepalive.ini"
QUIZ_DEFAULTS_FILE = pathlib.Path(os.environ.get("BRIGHTSPACE_QUIZ_DEFAULTS")
                                  or CONFIG / "quiz-defaults.yml").expanduser()
# Commonwealth's. A course's site names its own host, and --base-url or
# $BRIGHTSPACE_URL any other; this is what `session` and `courses` use before
# there is a courses file to read one from.
DEFAULT_HOST = "https://commonwealthu.brightspace.com"
USER_AGENT = "brightspace-cli/1 (github.com/cu-cs-courses/brightspace-cli)"
TIMEOUT = 60
# ENTITYDROPBOXSTATUS_T
STATUS = {0: "unsubmitted", 1: "submitted", 2: "draft", 3: "published"}
# The pair the browser sends to /d2l/api/ with every call.
SESSION_COOKIES = ("d2lSessionVal", "d2lSecureSessionVal")


HOW = """`session` reads Firefox by itself and needs nothing from you:

  ./brightspace.py session

It takes the two session cookies out of that profile's session store,
sessionstore-backups/recovery.jsonlz4 — not cookies.sqlite, which never holds
them. Brightspace sets them with no expiry, and Firefox keeps those in the
session store for session restore rather than in the cookie database. That file
is rewritten every few seconds, so a login you just did can take a moment to
turn up.

When the session later expires, any command re-reads that same profile and
carries on, so this is normally a one-time step. If Firefox has been logged out
too, log in there and the next command picks it up.

The profiles are in ~/.mozilla/firefox, or for the snap (Ubuntu's Firefox) in
~/snap/firefox/common/.mozilla/firefox, or for the Flatpak in
~/.var/app/org.mozilla.firefox/.mozilla/firefox. It reads all of them and takes
the most recently written store. --profile takes a name, or a path, when that
is the wrong one.

From another browser, or to leave that file alone, paste them instead:

  ./brightspace.py session --paste

  Two pastes.  F12 -> Storage (Firefox) or Application (Chrome, Edge) ->
               Cookies -> the Brightspace host -> the Value of d2lSessionVal,
               then of d2lSecureSessionVal. 36 characters each, here.
  One paste.   F12 -> Network -> reload -> click the first request, the page
               itself -> Request Headers -> the value of Cookie: one line
               holding both. Firefox's Copy as cURL is one line and works
               too; Chromium's spans several, and a prompt reads only the first.

Both are HttpOnly, so document.cookie in the console does not show them. The
same two values, on one line, are what a keepalive file's `cookies =` takes.

Chromium cannot be read from disk the way Firefox can: it encrypts cookie values
against the desktop keyring.

Or a token that expires in an hour and leaves the cookies in the browser. Run
this in that tab's devtools console:

  copy((await (await fetch('/d2l/lp/auth/oauth2/token', {method: 'POST',
    headers: {'X-Csrf-Token': localStorage['XSRF.Token'],
              'Content-Type': 'application/x-www-form-urlencoded'},
    body: 'scope=*:*:*'})).json()).access_token)

`copy()` puts it on the clipboard, and devtools keeps the line under Sources ->
Snippets. Then paste into `./brightspace.py session --token`.

XSRF.Token is a cross-site-request-forgery token, not a login. Every write
needs it, and `session` reads it off /d2l/home by itself, so there is nothing to
find. `session --xsrf` is the manual way in for the rare page that does not
carry it: F12 -> Console, `localStorage['XSRF.Token']`, and paste the result.
"""

class NotLoggedIn(Exception):
    pass


class Failed(Exception):
    pass


class HTTPFailed(Failed):
    """A refused request, with its status kept for a caller that can recover."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# --- where a course lives -------------------------------------------------

# host: the Brightspace its site names, or None for the default. site: the
# course's website repo, read by `setup`. dropbox: where downloads go.
Course = collections.namedtuple("Course", "host ou site dropbox")
COURSE_KEYS = ("site", "ou", "dropbox")


def read_ini(path):
    """[(section, {key: value})] from a file of `[name]` headers and `key = value`
    lines, with comments on lines of their own.

    Parsed here rather than by configparser so that an error can name a line
    without printing it. The keepalive file holds sessions, and the line a
    parser chokes on there is as likely as not half a cookie.
    """
    out = []
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        m = re.fullmatch(r"\[\s*([^\]]+?)\s*\]", line)
        if m:
            if any(name == m.group(1) for name, _ in out):
                raise Failed(f"{path}, line {n}: [{m.group(1)}] is there twice")
            out.append((m.group(1), {}))
            continue
        m = re.fullmatch(r"([\w-]+)\s*=\s*(.*)", line)
        if not m or not out:
            raise Failed(f"{path}, line {n}: not a [name] or a key = value under one")
        key = m.group(1).lower()
        if key in out[-1][1]:
            raise Failed(f"{path}, line {n}: {key} is set twice in [{out[-1][0]}]")
        out[-1][1][key] = m.group(2).strip()
    return out


def file_path(path, value):
    """A path written in one of these files: ~ expanded, and a relative one taken
    from the file's own directory. Through a symlink, so a courses file kept
    beside the courses it names can be linked into ~/.config and still find them."""
    p = pathlib.Path(value).expanduser()
    return p if p.is_absolute() else path.resolve().parent / p


def course_entries(path=None):
    """label -> its keys, from the courses file (or path); empty when there is none."""
    path = path or COURSES_FILE
    if not path.exists():
        return {}
    out = {}
    for label, keys in read_ini(path):
        wrong = sorted(set(keys) - set(COURSE_KEYS))
        if wrong:
            raise Failed(f"{path}, [{label}]: no such key {', '.join(wrong)}; "
                         f"a course takes {', '.join(COURSE_KEYS)}")
        if not (keys.get("site") or keys.get("ou")):
            raise Failed(f"{path}, [{label}]: needs a site, an ou, or both")
        if keys.get("ou") and not keys["ou"].isdigit():
            raise Failed(f"{path}, [{label}]: ou is a number, the org unit id in its /d2l/home/ URL")
        out[label] = keys
    return out


def quarto_urls(site):
    """The scalars directly under `urls:` in a site's _quarto.yml.

    Only that level: a site serving several sections carries a brightspace: per
    section deeper down, under meetings: or urls.sections, and the first of those
    is not the course's. It used to be taken for it, by a search for the first
    brightspace: anywhere in the file.
    """
    q = site / "_quarto.yml"
    try:
        text = q.read_text()
    except OSError as e:
        raise Failed(f"{q}: {e.strerror}")
    out, inside, indent = {}, False, None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            inside, indent = line.split(":", 1)[0].strip() == "urls", None
            continue
        here = len(line) - len(line.lstrip())
        if not inside or here != (indent if indent is not None else here):
            continue
        indent = here
        m = re.match(r"\s*([\w-]+):\s*(?:&\S+\s+)?(\S.*)?$", line)
        if m and m.group(2):
            out[m.group(1)] = str(yaml_value(m.group(2)))
    return out


def course_site(label, path=None):
    """The Course a label names in the courses file (or path), or a bare org unit id's.

    An ou written in the file wins over the site's, which is how a site shared
    by several sections names each one.
    """
    path = path or COURSES_FILE
    entries = course_entries(path)
    if label in entries:
        keys = entries[label]
        site = file_path(path, keys["site"]) if keys.get("site") else None
        dropbox = file_path(path, keys["dropbox"]) if keys.get("dropbox") else None
        host, ou = None, int(keys["ou"]) if keys.get("ou") else None
        if site:
            url = quarto_urls(site).get("brightspace", "")
            m = re.match(r"(https://[^/\s]+)(?:/d2l/home/(\d+))?", url)
            host = m and m.group(1)
            if ou is None and m and m.group(2):
                ou = int(m.group(2))
            if ou is None:
                raise Failed(f"{site / '_quarto.yml'}: no urls.brightspace of the form "
                             f"https://host/d2l/home/<id>, so [{label}] in {path} needs an ou")
        return Course(host, ou, site, dropbox)
    if label.isdigit():
        return Course(None, int(label), None, None)
    if entries:
        raise Failed(f"unknown course {label!r}: one of {', '.join(entries)} "
                     f"from {path}, or an org unit id")
    raise Failed(f"unknown course {label!r}: an org unit id, or a label in {path}, "
                 "which does not exist. `brightspace.py courses` lists your org unit ids.")


def known_courses():
    """org unit id -> label, for every course in the courses file that resolves."""
    out = {}
    for label in course_entries():
        try:
            # The first label wins where two name one org unit.
            out.setdefault(course_site(label).ou, label)
        except (OSError, Failed):
            pass
    return out


def base_url(args, host=None):
    url = args.base_url or os.environ.get("BRIGHTSPACE_URL") or host
    if not url:
        try:
            labels = list(course_entries())
        except Failed:
            # Said by whatever command needs a course. Not here, where it would stop
            # `session` and the keep-alive, which need none.
            labels = []
        for label in labels:
            try:
                url = course_site(label).host
            except (OSError, Failed):
                continue
            if url:
                break
    return (url or DEFAULT_HOST).rstrip("/")


# --- the session ----------------------------------------------------------

class _Answer:
    """What CookieJar.extract_cookies wants from a response, for one that
    http.client answered rather than urllib."""

    def __init__(self, response, url):
        self.response, self.url = response, url

    def info(self):
        return self.response.msg

    def geturl(self):
        return self.url


class Session:
    """Cookies plus a little metadata, loaded from and saved to a state directory:
    STATE unless another is named, and none at all for a session the keepalive
    file hands over whole, which lives in memory and is never written."""

    # GETs over connections kept open, one set per thread. Off unless a caller
    # turns it on -- one that makes many small reads at once. A status report
    # reading about sixty facts in parallel, a connection each, went from one
    # second to ten or twenty once a few runs came close together: requests
    # stalled for 3, 7, 11, 20 s on the way in, while one request alone took
    # 0.1 s. Kept open, the same sixty took half a second, run after run.
    # Writes never take this path. 2026-10-02.
    keepalive = False

    def __init__(self, base, state=STATE):
        self.base = base
        self.state = state
        self.jar = http.cookiejar.LWPCookieJar(str(state / "cookies.txt") if state else None)
        self.meta = {}
        if state and (state / "cookies.txt").exists():
            self.jar.load(ignore_discard=True, ignore_expires=True)
        if state and (state / "session.json").exists():
            self.meta = json.loads((state / "session.json").read_text())
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        # A minted token is saved only when it is the whole session — pasted in by
        # `session --token`, with no cookies behind it to mint another from.
        self.bearer = self.meta.get("bearer")

    def save(self):
        if not self.state:
            return
        self.state.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        old = os.umask(0o077)
        try:
            self.jar.save(ignore_discard=True, ignore_expires=True)
            (self.state / "session.json").write_text(json.dumps(self.meta, indent=1) + "\n")
        finally:
            os.umask(old)
        for p in (self.state / "cookies.txt", self.state / "session.json"):
            os.chmod(p, 0o600)

    def forget_in_memory(self):
        """Drop whatever was loaded, before taking a new session. The bearer goes with
        it: left behind, it confirms the old session while the new paste is untested."""
        self.jar.clear()
        self.meta = {}
        self.bearer = None

    def forget(self):
        for p in (self.state / "cookies.txt", self.state / "session.json"):
            if p.exists():
                p.unlink()

    def request(self, method, path, data=None, headers=None):
        """(status, headers, body, final url); 4xx and 5xx come back rather than raise."""
        url = path if path.startswith("http") else self.base + path
        h = {"User-Agent": USER_AGENT, **(headers or {})}
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        if self.meta.get("xsrf"):
            h.setdefault("X-Csrf-Token", self.meta["xsrf"])
        if self.bearer:
            h.setdefault("Authorization", "Bearer " + self.bearer)
        if self.keepalive and method == "GET" and body is None:
            return self.kept_get(url, h)
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=TIMEOUT) as r:
                return r.status, dict(r.headers), r.read(), r.geturl()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read(), e.geturl()
        except urllib.error.URLError as e:
            raise Failed(f"{url}: {e.reason}")

    def kept_get(self, url, headers, hops=5):
        """request() for a GET on a connection this thread keeps open.

        Cookies go through the jar both ways, as urllib's opener does, and a
        redirect is followed, as it would be. A connection the server has
        closed in the meantime is replaced once before giving up.
        """
        parts = urllib.parse.urlsplit(url)
        local = self.__dict__.setdefault("_kept", threading.local())
        conns = local.__dict__.setdefault("conns", {})
        req = urllib.request.Request(url, headers=headers, method="GET")
        self.jar.add_cookie_header(req)
        path = parts.path + ("?" + parts.query if parts.query else "")
        for attempt in (1, 2):
            conn = conns.get(parts.netloc)
            if conn is None:
                kind = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
                conn = conns[parts.netloc] = kind(parts.netloc, timeout=TIMEOUT)
            try:
                conn.request("GET", path, headers=dict(req.header_items()))
                r = conn.getresponse()
                body = r.read()
                break
            except (http.client.HTTPException, OSError) as e:
                conn.close()
                conns.pop(parts.netloc, None)
                if attempt == 2:
                    raise Failed(f"{url}: {e}")
        self.jar.extract_cookies(_Answer(r, url), req)
        where = r.getheader("Location")
        if r.status in (301, 302, 303, 307, 308) and where and hops:
            return self.kept_get(urllib.parse.urljoin(url, where), headers, hops - 1)
        return r.status, dict(r.getheaders()), body, url

    def api(self, path, params=None, raw=False, _minted=False, _reread=False):
        """GET one API resource as JSON (or bytes).

        401 and 403 mean the session is gone, and two recoveries are tried once
        each before giving up: minting a token from the cookies, and re-reading
        the browser this session came from.
        """
        if params:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
        status, headers, body, url = self.request("GET", path)
        if status in (401, 403):
            if not _minted and self.meta.get("xsrf") and not self.bearer and self.mint():
                return self.api(path, None, raw, True, _reread)
            if not _reread and self.reread_browser():
                return self.api(path, None, raw, _minted, True)
            what = "the token has expired" if self.meta.get("bearer") else "the session has expired"
            raise NotLoggedIn(f"{status} from {path}: not logged in, or {what}"
                              " — run `brightspace.py session` again")
        if status != 200:
            raise Failed(f"{status} from {path}: {body[:300]!r}")
        if raw:
            return body
        try:
            return json.loads(body)
        except ValueError:
            raise Failed(f"{path}: not JSON: {body[:300]!r}")

    def cookies_now(self):
        return {c.name: c.value for c in self.jar if c.name in SESSION_COOKIES}

    def take_cookies(self, got):
        self.jar.clear()
        self.bearer = None
        self.meta.pop("bearer", None)
        for name in SESSION_COOKIES:
            put_cookie(self.jar, self.base, name, got[name])

    def reread_browser(self):
        """The browser has probably logged in again since this session died, so read
        it once more. Only ever the source the user already chose, and only when the
        values have actually changed — a stale file would otherwise buy a second 403
        and a misleading line saying it was refreshed."""
        if self.meta.get("source") != "firefox":
            return False
        try:
            path, age, got = firefox_session(self.base, self.meta.get("profile"))
        except Failed as e:
            print(f"the session expired and Firefox could not be re-read: {e}", file=sys.stderr)
            return False
        if got == self.cookies_now():
            print(f"the session expired and Firefox still holds the same one "
                  f"({store_label(path)}, {age_str(age)}) — log in there again", file=sys.stderr)
            return False
        self.take_cookies(got)
        # The XSRF token belongs to the session it was issued with, so the old one
        # goes with the old cookies. The next write reads a fresh one.
        self.meta.pop("xsrf", None)
        self.save()
        print(f"session expired; re-read from Firefox ({store_label(path)}, {age_str(age)})",
              file=sys.stderr)
        return True

    def fetch_xsrf(self):
        """Read the XSRF token off /d2l/home, the way `login` already does.

        A logged-in page hands its own scripts the token inline, so a session that
        has the cookies can read it for itself. Until 2026-09-27 only the password
        login did this, and a session taken from Firefox -- the only way in at CU --
        was refused on its first write and sent the user into devtools to find the
        token by hand, which is exactly what this request does. Stored with the
        rest of the session, mode 0600, and never printed.
        """
        status, _, body, _ = self.request("GET", "/d2l/home")
        token = xsrf_from(body) if status == 200 else None
        if token:
            self.meta["xsrf"] = token
            self.save()
        return bool(token)

    def send(self, method, path, payload, _refetched=False):
        """POST or PUT a JSON body, or DELETE with none. The only writing this tool does.

        D2L guards state-changing requests with the XSRF token, which is not in the
        cookies. It is read off /d2l/home before the first write, and read again
        once if a write is refused, since a token outlives nothing but its own
        session. Only when the page carries none does a refusal reach the user.
        """
        if not self.meta.get("xsrf"):
            self.fetch_xsrf()
        body = None if payload is None else json.dumps(payload).encode()
        h = {"User-Agent": USER_AGENT}
        if body is not None:
            h["Content-Type"] = "application/json"
        if self.meta.get("xsrf"):
            h["X-Csrf-Token"] = self.meta["xsrf"]
        if self.bearer:
            h["Authorization"] = "Bearer " + self.bearer
        url = self.base + path
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=TIMEOUT) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode("utf-8", "replace")
            if e.code in (401, 403) and not _refetched and self.fetch_xsrf():
                return self.send(method, path, payload, True)
            if e.code in (401, 403) and not self.meta.get("xsrf"):
                raise HTTPFailed(e.code, f"{e.code} on {method} {path}. Writing needs the XSRF token, and "
                             "/d2l/home did not carry one.\n"
                             "       In a logged-in Brightspace tab: F12 -> Console, type "
                             "localStorage['XSRF.Token'] and press Enter.\n"
                             "       Then `brightspace.py session --xsrf` and paste what it "
                             "printed, without the quotes.")
            raise HTTPFailed(e.code, f"{e.code} on {method} {path}: {detail}")
        except urllib.error.URLError as e:
            raise Failed(f"{url}: {e.reason}")

    def mint(self):
        """A bearer token the way the pages get theirs: the session cookies plus the
        XSRF token, posted to the internal token endpoint. Unofficial, and only a
        fallback for when the cookies alone stop being accepted by /d2l/api/."""
        status, _, body, _ = self.request(
            "POST", "/d2l/lp/auth/oauth2/token", data={"scope": "*:*:*"},
            headers={"X-Csrf-Token": self.meta.get("xsrf", "")})
        try:
            self.bearer = json.loads(body)["access_token"]
        except (ValueError, KeyError, TypeError):
            return False
        return bool(self.bearer)

    def versions(self):
        """Latest LE and LP API versions this instance serves; read once at login."""
        if "le" not in self.meta or "lp" not in self.meta:
            v = {p["ProductCode"]: p["LatestVersion"] for p in self.api("/d2l/api/versions/")}
            self.meta["le"], self.meta["lp"] = v["le"], v["lp"]
        return self.meta["le"], self.meta["lp"]

    def paged(self, path, params=None):
        """A PagedResultSet: Items, then the bookmark, until HasMoreItems is false."""
        params = dict(params or {})
        while True:
            page = self.api(path, params)
            yield from page.get("Items") or []
            info = page.get("PagingInfo") or {}
            if not info.get("HasMoreItems") or not info.get("Bookmark"):
                return
            params["bookmark"] = info["Bookmark"]

    def objects(self, path, params=None):
        """An ObjectListPage: Objects, then follow Next until it is null."""
        page = self.api(path, params)
        while True:
            yield from page.get("Objects") or []
            nxt = page.get("Next")
            if not nxt:
                return
            page = self.api(nxt)


def xsrf_from(page):
    """The XSRF token a logged-in page hands its own scripts — written inline as
    localStorage.setItem('XSRF.Token', …) and again as the second argument of
    D2L.LP.Web.Authentication.Xsrf.Init. Empty on the login page, real after."""
    text = page.decode("utf-8", "replace")
    for pat in (r"localStorage\.setItem\('XSRF\.Token','([^']+)'\)",
                r'Xsrf\.Init\\?",\\?"P\\?":\[\\?"d2l_referrer\\?",\\?"([^"\\]+)'):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def whoami(s):
    _, lp = s.versions()
    return s.api(f"/d2l/api/lp/{lp}/users/whoami")


def open_session(args, host=None, session=None):
    """The session a command acts as: the one `session` saved, or the one named
    [session] in the keepalive file -- the argument, or $BRIGHTSPACE_SESSION --
    which is how a server acts for whichever of its people asked."""
    named = session or os.environ.get("BRIGHTSPACE_SESSION")
    if not named:
        s = Session(base_url(args, host))
    else:
        hits = [(state, cookies) for name, state, cookies in keepalive_entries(KEEPALIVE_FILE) if name == named]
        if not hits:
            raise NotLoggedIn(f"no session [{named}] in {KEEPALIVE_FILE}")
        state, cookies = hits[0]
        s = Session(base_url(args, host), state)
        if cookies:
            s.take_cookies(cookies)
            return s
    if not (s.state / "cookies.txt").exists() and not s.bearer:
        raise NotLoggedIn(f"no session in {s.state}: run `brightspace.py session`")
    return s


def put_cookie(jar, base, name, value):
    """One session cookie, flagged Secure exactly when the host is https — a Secure
    cookie is not sent over http, so hardcoding it makes a plain-http host (a test
    double) silently send nothing."""
    parts = urllib.parse.urlsplit(base)
    jar.set_cookie(http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=parts.hostname, domain_specified=True, domain_initial_dot=False,
        path="/", path_specified=True, secure=parts.scheme == "https",
        expires=None, discard=False,
        comment=None, comment_url=None, rest={"HttpOnly": None}))


# Firefox keeps cookies with an expiry in cookies.sqlite and cookies *without* one
# in the session store, for session restore. Brightspace's two are set with no
# expiry, so they are only ever in the session store: measured 2026-09-26, zero
# rows in cookies.sqlite against four entries in recovery.jsonlz4.
SESSION_STORE = ("sessionstore-backups/recovery.jsonlz4",
                 "sessionstore-backups/recovery.baklz4",
                 "sessionstore.jsonlz4",
                 "sessionstore-backups/previous.jsonlz4")
# Where Firefox keeps its profiles: its own package, the snap (Ubuntu's Firefox),
# the Flatpak. All of them are read and the most recently written store wins,
# because moving to the snap copies the profiles over, names and all, and leaves
# the originals to go stale. Found 2026-10-03: a snap Firefox with its live session
# under ~/snap, and `session` reading only ~/.mozilla/firefox, last written in 2022.
FIREFOX_DIRS = tuple(pathlib.Path.home() / d for d in (
    ".mozilla/firefox",
    "snap/firefox/common/.mozilla/firefox",
    ".var/app/org.mozilla.firefox/.mozilla/firefox"))


def mozlz4(path):
    """Firefox's own container: the magic, a little-endian uncompressed size, then
    one raw LZ4 block."""
    raw = path.read_bytes()
    if not raw.startswith(b"mozLz40\0"):
        raise Failed(f"{path}: not a mozlz4 file")
    try:
        import lz4.block
    except ImportError:
        raise Failed("the session store is LZ4-compressed and python-lz4 is missing: "
                     "`pip install lz4`, or on Nix `python3.withPackages (ps: [ ps.lz4 ])`")
    try:
        return lz4.block.decompress(raw[12:], uncompressed_size=struct.unpack("<I", raw[8:12])[0])
    except Exception as e:
        raise Failed(f"{path}: {e}")


def firefox_stores(named=None):
    """Candidate session-store files, most recently written first. Firefox rewrites
    recovery.jsonlz4 every so often and only writes sessionstore.jsonlz4 on a clean
    shutdown, so which one is current depends on whether it is running."""
    if named:
        prof = pathlib.Path(named).expanduser()
        # A name can be in more than one of them; the newest store settles it.
        profiles = [prof] if prof.is_dir() else [d / named for d in FIREFOX_DIRS if (d / named).is_dir()]
        if not profiles:
            raise Failed(f"no such Firefox profile: {named}")
    else:
        profiles = [p for d in FIREFOX_DIRS if d.is_dir() for p in d.glob("*") if p.is_dir()]
        if not profiles:
            raise Failed(f"no Firefox profile under {' or '.join(tilde(d) for d in FIREFOX_DIRS)}")
    found = [prof / rel for prof in profiles for rel in SESSION_STORE if (prof / rel).exists()]
    if not found:
        raise Failed(f"no session store in {', '.join(tilde(p) for p in profiles)}"
                     " — is this the profile you browse Brightspace in?")
    return sorted(found, key=lambda f: f.stat().st_mtime, reverse=True)


def firefox_session(base, named=None):
    """The Brightspace session cookies Firefox is holding: (where, how old, values).

    Only the two named cookies are taken, and only for this host. The file holds
    every open tab's session cookies, so everything else in it is left alone.
    """
    host = urllib.parse.urlsplit(base).hostname
    tried = {}
    for path in firefox_stores(named):
        store = json.loads(mozlz4(path))
        got = {}
        for c in store.get("cookies") or []:
            if c.get("host") == host and c.get("name") in SESSION_COOKIES and c.get("value"):
                got.setdefault(c["name"], c["value"])
        age = dt.datetime.now().timestamp() - path.stat().st_mtime
        if all(got.get(name) for name in SESSION_COOKIES):
            return path, age, got
        tried.setdefault(store_profile(path), []).append(f"{path.name} {age_str(age)}")
    # Every file with its folder and its age: file names alone made a profile
    # Firefox stopped writing years ago look like the one it is using.
    raise Failed(f"no {host} session in Firefox — log in there first, and give it a moment: "
                 "the session store is written every few seconds, not on every click. Read:\n"
                 + "\n".join(f"  {tilde(prof)}: {', '.join(files)}" for prof, files in tried.items()))


def store_profile(path):
    """The profile directory a session-store file belongs to."""
    return path.parents[1] if path.parent.name == "sessionstore-backups" else path.parent


def store_label(path):
    """The whole folder, since the same profile name can be in more than one."""
    return f"{tilde(store_profile(path))}, {path.name}"


def age_str(seconds):
    if seconds < 90:
        return f"{int(seconds)}s old"
    if seconds < 5400:
        return f"{int(seconds / 60)}m old"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h old"
    return f"{int(seconds / 86400)}d old"


def interactive():
    """Whether there is a person at the terminal to answer a question."""
    return sys.stdin.isatty()


def ask(prompt, hidden=True):
    """One prompt. EOF means there is no terminal to read from, which is what happens
    under a timer or a pipe, so it says that rather than showing a traceback."""
    try:
        return (getpass.getpass(prompt) if hidden else input(prompt)).strip()
    except EOFError:
        raise Failed(f"nothing to read from the terminal for: {prompt.strip()}")


def pasted_cookies(text):
    """The session cookies out of whatever was pasted: a Cookie header, a
    `Copy as cURL`, or a bare value. Lets one paste answer both prompts."""
    out = {}
    for name in SESSION_COOKIES:
        m = re.search(name + r"=([^;,\s'\"]+)", text)
        if m:
            out[name] = m.group(1)
    return out


def confirm(s):
    """Prove the session works before saving it, so a bad one fails here rather
    than in the middle of a download. Returns the whoami block."""
    me = whoami(s)
    if not me.get("Identifier"):
        raise Failed(f"the session was accepted but whoami returned {me!r}")
    return me


def course(args):
    """(session, org unit id, Course) for the course argument."""
    c = course_site(args.course)
    return open_session(args, c.host), c.ou, c


# --- little formatters ----------------------------------------------------

def local_dt(iso):
    if not iso:
        return None
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()


def local(iso):
    d = local_dt(iso)
    return d.strftime("%Y-%m-%d %H:%M") if d else ""


def yes_no(flag):
    return "yes" if flag else "no"


def d2l_stamp(d):
    """`Sep 8, 2026 112 PM` — the Download button's way of writing 1:12 PM."""
    return f"{d:%b} {d.day}, {d.year} {int(d.strftime('%I'))}{d:%M} {d:%p}"


def dashed(name):
    return re.sub(r"[^\w.-]+", "-", name.strip()).strip("-")


# The index.html the Download button puts beside the students' folders, which
# no route of the API returns: a grey row per student, "Last, First", in the
# order Brightspace lists them -- alphabetical with punctuation ignored, so an
# apostrophe in a name does not move it, as all seventeen web downloads of the
# Journal read on 2026-10-14 have it -- and under it a row per entry, newest first: its
# name, its text under Comments:, and when under Submitted:. An assignment's
# has a row per file instead, its name where an entry's goes. Artem,
# 2026-10-14, of the downloads made through the API, which had none: "I found
# index.html that lists every answer extremely helpful."
INDEX_HEAD = ("<html>\r\n<body bgcolor=white>\r\n<table width=440 cellpadding=1 cellspacing=0 border=0>\r\n"
              "<tr><td bgcolor=black>\r\n<table WIDTH=100% cellpadding=3 cellspacing=0 border=0 bgcolor=white>\r\n")
INDEX_TAIL = "</table></td></tr>\r\n</table>\r\n</body>\r\n</html>\r\n"


def last_first(name):
    """\"Rita Solberg\" as the index writes it, \"Solberg, Rita\": for a student
    the classlist does not name."""
    first, _, last = name.strip().rpartition(" ")
    return f"{last}, {first}" if first else last


def classlist_names(s, ou):
    """Each student's user id and name as the index writes it, \"Last, First\";
    nothing when the classlist cannot be read."""
    try:
        return {str(u.get("Identifier")): f"{u.get('LastName', '')}, {u.get('FirstName', '')}".strip(", ")
                for u in classlist_students(s, ou)}
    except Failed:
        return {}


def write_index(out, students):
    """index.html in a download. `students` maps a user id to the student's
    name, "Last, First", and their rows: (entry or file name, text as HTML,
    when submitted)."""
    rows = []
    for name, entries in sorted(students.values(), key=lambda st: re.sub(r"[^a-z0-9, ]", "", st[0].lower())):
        rows.append(f"<tr bgcolor=#AAAAAA><td colspan=2><font color=white><b>{html.escape(name, False)}</b></td></tr>")
        for entry, text, when in sorted(entries, key=lambda e: e[2], reverse=True):
            stamp = f"{when:%b} {when.day}, {when.year} {int(when.strftime('%I'))}:{when:%M} {when:%p}"
            rows.append(f"<tr bgcolor=white><td width=80% valign=top>{html.escape(entry, False)}"
                        f"<p style='margin-top:3px;'><b>Comments:</b><br>{text}</td>"
                        f"<td valign=top><b>Submitted:</b><br>{stamp}</td></tr>")
    # In ASCII, every other character a reference: the page names no charset,
    # and a browser opening it from disk would guess one and garble the rest.
    (out / "index.html").write_bytes((INDEX_HEAD + "".join(rows) + INDEX_TAIL).encode("ascii", "xmlcharrefreplace"))


def strip_html(h):
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", h, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()


def availability(f):
    a = f.get("Availability") or {}
    start, end = local(a.get("StartDate")), local(a.get("EndDate"))
    if start and end:
        return f"{start} – {end}"
    return f"from {start}" if start else (f"until {end}" if end else "")


def table(headers, rows):
    rows = [[str(c) for c in r] for r in rows]
    widths = [max(len(x) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]
    line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
    print(line(headers))
    print(line(["-" * w for w in widths]))
    for r in rows:
        print(line(r))


def emit(args, rows, headers, raw):
    if args.json:
        print(json.dumps(raw, indent=1))
    else:
        table(headers, rows)


# --- the dropbox ----------------------------------------------------------

def folders(s, ou):
    le, _ = s.versions()
    return s.api(f"/d2l/api/le/{le}/{ou}/dropbox/folders/", {"onlyCurrentStudentsAndGroups": "true"})


def find_folder(s, ou, want):
    fs = folders(s, ou)
    if want.isdigit():
        hits = [f for f in fs if f["Id"] == int(want)]
    else:
        hits = [f for f in fs if f["Name"].strip().lower() == want.strip().lower()]
        if not hits:
            hits = [f for f in fs if want.strip().lower() in f["Name"].lower()]
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(f"{f['Id']} {f['Name']!r}" for f in (hits or fs))
    raise Failed(f"{'no' if not hits else 'several'} entries match {want!r}: {names}")


def submissions(s, ou, fid, active_only=True):
    """A folder's submissions by entity. `active_only` leaves out anyone not an
    active learner — an instructor's own submission made while viewing the
    course as a learner, among them; pass False to see those too."""
    le, _ = s.versions()
    return s.api(f"/d2l/api/le/{le}/{ou}/dropbox/folders/{fid}/submissions/",
                 {"activeOnly": "true"} if active_only else None)


def entries(s, ou, fid, active_only=True):
    """Every submission in a folder, flat: (when, name, user id, submission)."""
    out = []
    for e in submissions(s, ou, fid, active_only):
        ent = e.get("Entity") or {}
        for sub in e.get("Submissions") or []:
            out.append((local_dt(sub.get("SubmissionDate")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                        ent.get("DisplayName", "?"), ent.get("EntityId"), sub))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def student_dir(out, uid, fid, name):
    return out / f"{uid}-{fid} - {dashed(name)}"


def download(s, ou, fid, sub, into):
    le, _ = s.versions()
    into.mkdir(parents=True, exist_ok=True)
    got = []
    for f in sub.get("Files") or []:
        data = s.api(f"/d2l/api/le/{le}/{ou}/dropbox/folders/{fid}/submissions/{sub['Id']}/files/{f['FileId']}", raw=True)
        path = into / pathlib.PurePath(f["FileName"]).name
        path.write_bytes(data)
        got.append(path)
    return got


# --- commands -------------------------------------------------------------

def cmd_login(args):
    """A local D2L account. A CU account cannot come in this way — see `session`."""
    s = Session(base_url(args))
    s.forget_in_memory()
    page = s.request("GET", "/d2l/login")[2].decode("utf-8", "replace")
    sso = "d2l-button-sso" in page
    if sso and not args.force:
        raise Failed("this Brightspace offers single sign-on, and a CU account goes through it "
                     "(passhe.proxy.cirrusidentity.com, then Microsoft Entra) — the form on that page "
                     "is for local D2L accounts only, and CU credentials posted to it are refused.\n"
                     "       Log in in your browser and run `brightspace.py session` instead. "
                     "Pass --force to try the form anyway.")
    user = args.user or os.environ.get("BRIGHTSPACE_USER") or ask("Brightspace username: ", hidden=False)
    password = os.environ.get("BRIGHTSPACE_PASSWORD") or ask("Password (never stored): ")
    status, _, body, final = s.request(
        "POST", "/d2l/lp/auth/login/login.d2l",
        data={"loginPath": "/d2l/login", "userName": user, "password": password, "d2l_referrer": ""},
        headers={"Referer": s.base + "/d2l/login"})
    del password
    # The cookies come back even when the login is refused, so they prove nothing.
    # What answers it is where the POST sent us, and then whoami.
    if "loginFailed" in final or "loginError" in final:
        raise Failed(f"the login was refused (landed on {final})"
                     + (" — a CU account has to come in through `session`" if sso else ""))
    s.meta["xsrf"] = xsrf_from(body) or xsrf_from(s.request("GET", "/d2l/home")[2]) or ""
    s.meta["username"] = user
    s.meta["logged_in"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    s.versions()
    me = confirm(s)
    s.save()
    print(f"logged in as {me.get('FirstName')} {me.get('LastName')} ({me.get('UniqueName')}); "
          f"API le {s.meta['le']}, lp {s.meta['lp']}; session kept in {STATE}")


def cmd_session(args):
    """Take over the session a logged-in browser already holds.

    By default it reads Firefox, which needs no devtools and no typing. What it
    reads is the session store, `sessionstore-backups/recovery.jsonlz4`, because
    Brightspace's cookies are set without an expiry and Firefox keeps those there
    rather than in cookies.sqlite. Only the two named cookies are taken, and only
    for this host.

    The token that writing needs is read off /d2l/home as soon as the cookies are
    in, and printed as found or not -- never its value. `--xsrf` asks for it by
    hand instead, for the case where that page does not carry it: F12 -> Console,
    type `localStorage['XSRF.Token']`, paste the result.

    `--paste` takes them from the terminal instead, which is the way in from any
    other browser:

      Firefox   F12 -> Storage -> Cookies -> the Brightspace host
      Chromium  F12 -> Application -> Storage -> Cookies -> that host

    or, in one paste, the Network tab's "Copy as cURL". Chromium cannot be read
    from disk the way Firefox can: it encrypts cookie values against the desktop
    keyring, and as of 2026-09-26 it was holding no Brightspace session at all.
    """
    if args.how:
        print(HOW)
        return
    if args.export and args.token:
        raise Failed("--export hands over the two cookies, and a token is not them")
    # Exporting reads Firefox or fails, rather than asking for the very cookies
    # it is about to print; --paste still works, for another browser.
    if args.export and not args.paste:
        args.from_firefox = True
    # With --export stdout carries the entry and nothing else, so that it can go
    # straight to a clipboard; everything said along the way goes to stderr.
    say = (lambda *a: print(*a, file=sys.stderr)) if args.export else print
    s = Session(base_url(args))
    s.forget_in_memory()
    took = None
    if args.token:
        tok = ask("Bearer token (pasted, not echoed): ")
        if not tok:
            raise Failed("nothing pasted")
        s.bearer = s.meta["bearer"] = tok
        s.meta["source"] = "token"
        took = "token"
    elif not args.paste:
        # Firefox is the default, and --from-firefox is the same thing said out loud:
        # the difference is that the plain form falls back to the prompts and the
        # explicit one fails, which is what a timer or a script wants.
        try:
            path, age, got = firefox_session(s.base, args.profile)
        except Failed as e:
            if args.from_firefox:
                raise
            print(f"Firefox: {e}\n", file=sys.stderr)
        else:
            s.take_cookies(got)
            s.meta["source"] = "firefox"
            s.meta["profile"] = str(store_profile(path))
            took = f"Firefox ({store_label(path)}, {age_str(age)})"
    if not took:
        first = ask("d2lSessionVal, or a paste holding both cookies (not echoed): ")
        got = pasted_cookies(first)
        if len(got) < 2:
            got = {"d2lSessionVal": got.get("d2lSessionVal") or first}
            got["d2lSecureSessionVal"] = ask("d2lSecureSessionVal (not echoed): ")
        if not all(got.get(c) for c in SESSION_COOKIES):
            raise Failed("both cookies are needed. Paste them one each, or paste one line "
                         "holding both: the value of the Cookie request header is one. "
                         "`--token` takes a bearer token instead.")
        s.take_cookies(got)
        s.meta["source"] = "paste"
        took = "the paste"
    # Asked for whichever way the cookies arrived. It used to hang off the paste
    # branch alone, which meant a Firefox session could not be given a token
    # without re-pasting both cookies by hand -- three values typed to add one,
    # on the day a write is refused and you are in a hurry.
    if args.xsrf:
        s.meta["xsrf"] = ask("XSRF.Token from Local Storage (not echoed): ")
    s.meta["taken"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    s.versions()
    me = confirm(s)
    # A bearer token has no cookies to fetch a page with, and a pasted XSRF
    # value is the user's own choice; everything else reads it here, once.
    if not s.meta.get("xsrf") and s.meta["source"] != "token":
        s.fetch_xsrf()
    s.save()
    say(f"read from {took}")
    say(f"accepted: {me.get('FirstName')} {me.get('LastName')} ({me.get('UniqueName')}), "
        f"user id {me.get('Identifier')}; API le {s.meta['le']}, lp {s.meta['lp']}; kept in {STATE}")
    if args.xsrf:
        say("write token: pasted")
    elif s.meta["source"] != "token":
        say("write token: " + ("read off /d2l/home" if s.meta.get("xsrf") else
                               "not found on /d2l/home, so a write will say how to add one"))
    if s.meta["source"] == "token":
        say("A minted token lasts about an hour. When it expires, run this again.")
    elif s.meta["source"] == "firefox":
        say("Expired sessions are re-read from that profile automatically, so this should "
            "be the last time you run this.")
    else:
        say("This is the browser's own session: logging out there ends it here too.")
    if not (os.path.lexists(COURSES_FILE) or os.path.lexists(KEEPALIVE_FILE)):
        say("Next, `brightspace.py init` writes your courses file, and asks whether this machine "
            "keeps your session alive.")
    if args.export:
        got = s.cookies_now()
        say("\nBelow is this session as an entry for someone else's keepalive file. It is\n"
            "your login: send it the way you would send a password. Logging out of\n"
            "Brightspace in this browser ends it, for them too; closing the browser does not.\n")
        print(f"[{entry_name(me)}]\ncookies = " + "; ".join(f"{c}={got[c]}" for c in SESSION_COOKIES))


def entry_name(me):
    """A session's [name] in a keepalive file: its owner's first name, lower case."""
    return re.sub(r"[^\w-]+", "", (me.get("FirstName") or "").lower()) or "me"


def cmd_logout(args):
    Session(base_url(args)).forget()
    print(f"forgot the session in {STATE}; Brightspace's own copy expires on its own")


def cmd_ping(args):
    """One cheap authenticated call, to keep the session from idling out.

    D2L expires a session after a stretch with no activity, and an API call is
    activity — the same session the browser holds, so this keeps that logged in
    too. A 403 here still runs the Firefox re-read, so a ping both holds a live
    session open and repairs a dead one whenever the browser has a good one.
    """
    s = open_session(args)
    start = dt.datetime.now()
    try:
        me = whoami(s)
    except NotLoggedIn as e:
        if not args.quiet:
            print(f"dead: {e}", file=sys.stderr)
        sys.exit(1)
    if not args.quiet:
        ms = (dt.datetime.now() - start).total_seconds() * 1000
        print(f"alive: {me.get('UniqueName')} via {s.meta.get('source', '?')}, {ms:.0f}ms")


def keepalive_entries(path):
    """[(name, state directory or None, cookies or None)], one per [name] in the
    keepalive file.

    A file anyone else can read is refused, the way ssh refuses a private key:
    each entry reaches every grade its owner can. Nothing a line holds is ever
    printed, only its name and line number.
    """
    if not path.exists():
        raise Failed(f"no {path}: write one, a [name] per session. "
                     "`brightspace.py keepalive --help` shows the shape.")
    if path.stat().st_mode & 0o077:
        raise Failed(f"{path} can be read by others, and it holds sessions: chmod 600 {path}")
    out = []
    for name, keys in read_ini(path):
        wrong = sorted(set(keys) - {"state", "cookies"})
        if wrong:
            raise Failed(f"{path}, [{name}]: no such key {', '.join(wrong)}; "
                         "a session is a state or its cookies")
        if ("state" in keys) == ("cookies" in keys):
            raise Failed(f"{path}, [{name}]: a state or cookies, not {'both' if keys else 'neither'}")
        if "cookies" in keys:
            got = pasted_cookies(keys["cookies"])
            missing = [c for c in SESSION_COOKIES if not got.get(c)]
            if missing:
                raise Failed(f"{path}, [{name}]: its cookies have no {' and no '.join(missing)}; "
                             "`brightspace.py session --how` says where a browser keeps them")
            out.append((name, None, got))
        else:
            out.append((name, file_path(path, keys["state"]), None))
    if not out:
        raise Failed(f"{path} names no sessions")
    return out


def cmd_keepalive(args):
    """Ping every session in the keepalive file, so that none of them idles out.

    The file is ~/.config/brightspace/keepalive.ini, mode 0600, a [name] per
    session:

        [me]
        state = ~/.local/state/brightspace

        [colleague]
        cookies = d2lSessionVal=...; d2lSecureSessionVal=...

    The name only says which session a line is about. `state` is a directory
    `brightspace.py session` saved a session in, and a session taken from
    Firefox is re-read from there when it dies, as it would be by any command.
    `cookies` is a session handed over whole, in any one-line form `session
    --paste` takes, a Cookie header included; `session --how` says where a
    browser keeps the two. Nothing can repair one of those, so once it dies
    every run says so until fresh cookies replace it.

    Every session is tried even when one fails, and the exit status is 1 if
    any did. --quiet prints only the failures, which is what a timer wants.
    """
    path = pathlib.Path(args.file).expanduser() if args.file else KEEPALIVE_FILE
    entries = keepalive_entries(path)
    base = base_url(args)
    width = max(len(name) for name, _, _ in entries)
    failed = 0
    for name, state, cookies in entries:
        start = dt.datetime.now()
        if cookies:
            s = Session(base, None)
            s.take_cookies(cookies)
            fix = f"put fresh cookies under [{name}] in {path}"
        else:
            try:
                s = Session(base, state)
            except (OSError, ValueError) as e:
                # Only the kind of error: the cookie jar's own message quotes the
                # line it could not parse, and that line is a cookie.
                failed += 1
                print(f"{name:<{width}}  unreadable: the session in {state} ({type(e).__name__})",
                      file=sys.stderr)
                continue
            fix = ("log in to Brightspace in Firefox again" if s.meta.get("source") == "firefox" else
                   "run `brightspace.py session`" + ("" if state == STATE else f" with BRIGHTSPACE_STATE_DIR={state}"))
        try:
            if state and not ((state / "cookies.txt").exists() or s.bearer):
                raise NotLoggedIn(f"no session in {state}")
            me = whoami(s)
        except NotLoggedIn as e:
            failed += 1
            why = str(e) if str(e).startswith("no session") else "Brightspace refused it"
            print(f"{name:<{width}}  dead: {why}; {fix}", file=sys.stderr)
            continue
        except Failed as e:
            failed += 1
            print(f"{name:<{width}}  not reached: {e}", file=sys.stderr)
            continue
        if not args.quiet:
            ms = (dt.datetime.now() - start).total_seconds() * 1000
            print(f"{name:<{width}}  alive: {me.get('UniqueName')}, {ms:.0f}ms")
    if failed:
        sys.exit(1)


def cmd_whoami(args):
    s = open_session(args)
    me = whoami(s)
    src = s.meta.get("source", "?")
    if src == "firefox" and s.meta.get("profile"):
        src = f"Firefox ({tilde(s.meta['profile'])})"
    print(f"{me.get('FirstName')} {me.get('LastName')} ({me.get('UniqueName')}), user id {me.get('Identifier')}")
    print(f"session from {src}, taken {s.meta.get('taken') or s.meta.get('logged_in', '?')}")


def cmd_courses(args):
    s = open_session(args)
    _, lp = s.versions()
    known = known_courses()
    rows, raw = [], []
    for item in s.paged(f"/d2l/api/lp/{lp}/enrollments/myenrollments/", {"orgUnitTypeId": "3"}):
        ou, acc = item.get("OrgUnit") or {}, item.get("Access") or {}
        raw.append(item)
        rows.append([ou.get("Id"), known.get(ou.get("Id"), ""), ou.get("Code", ""), ou.get("Name", ""),
                     acc.get("ClasslistRoleName", ""), "active" if acc.get("IsActive") else ""])
    emit(args, rows, ["id", "course", "code", "name", "role", ""], raw)
    if not COURSES_FILE.exists() and not args.json:
        print(f"\nNo {COURSES_FILE} yet. `brightspace.py init` writes one, a label for each "
              "course you teach, so that every command can take the label instead of the id.",
              file=sys.stderr)


# --- the two files, written for you ---------------------------------------

def taught(s):
    """[(org unit id, name)] for each course this session teaches and that has
    not ended, in Brightspace's order. A sandbox is Staff rather than
    Instructor, and so is left out with the rest."""
    _, lp = s.versions()
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for item in s.paged(f"/d2l/api/lp/{lp}/enrollments/myenrollments/", {"orgUnitTypeId": "3"}):
        ou, acc = item.get("OrgUnit") or {}, item.get("Access") or {}
        end = local_dt(acc.get("EndDate"))
        if "instructor" in (acc.get("ClasslistRoleName") or "").lower() and not (end and end < now):
            out.append((ou.get("Id"), " ".join((ou.get("Name") or "").split())))
    return out


def course_labels(courses):
    """org unit id -> label: the number in a name like CMSC-120-01-Fall 2026,
    with its section added when one person teaches several, and the id itself
    for a name that carries no number."""
    parsed = {ou: re.match(r"[A-Za-z]{2,5}[- ]?(\d{3,4})(?:[- ](\d{2,3}))?", name) for ou, name in courses}
    numbers = collections.Counter(m.group(1) for m in parsed.values() if m)
    out = {}
    for ou, m in parsed.items():
        label = str(ou) if not m else m.group(1) if numbers[m.group(1)] == 1 or not m.group(2) \
            else f"{m.group(1)}-{m.group(2)}"
        out[ou] = label if label not in out.values() else str(ou)
    return out


def find_sites(roots):
    """org unit id -> (site, whether its urls.brightspace names that id), for
    every _quarto.yml no more than three directories below one of roots.

    A site that names an id only deeper down, for one of its sections, is found
    too, and its entry then needs an ou of its own. Symlinks are not followed,
    since a course's dropbox is often one and student files are no business of
    this search; hidden directories and _site and its kind are skipped. Where
    two sites name one id, as a worktree beside its clone does, the shorter path
    wins.
    """
    found = {}
    for root in roots:
        root = pathlib.Path(root).expanduser()
        for here, dirs, files in os.walk(root):
            depth = len(pathlib.Path(here).relative_to(root).parts)
            dirs[:] = sorted(d for d in dirs if not d.startswith((".", "_")) and d != "node_modules") \
                if depth < 3 else []
            if "_quarto.yml" not in files:
                continue
            site = pathlib.Path(here)
            try:
                text = (site / "_quarto.yml").read_text()
                own = quarto_urls(site).get("brightspace", "")
            except (OSError, Failed):
                continue
            for m in re.finditer(r'brightspace:\s*"?https://[^/\s"]+/d2l/home/(\d+)', text):
                ou = int(m.group(1))
                if ou not in found or len(str(site)) < len(str(found[ou][0])):
                    found[ou] = (site, own.rstrip("/").endswith(f"/d2l/home/{ou}"))
    return found


def tilde(path):
    """A path as a person would write it in one of these files: ~ for home,
    and symlinks left as they are."""
    path = pathlib.Path(os.path.abspath(path))
    try:
        return "~/" + str(path.relative_to(pathlib.Path.home()))
    except ValueError:
        return str(path)


def course_sections(courses, labels, sites):
    """The courses file's sections, one list of lines per course."""
    out = {}
    for ou, name in courses:
        lines = [f"# {name}", f"[{labels[ou]}]"]
        site, own = sites.get(ou, (None, False))
        if site:
            lines.append(f"site = {tilde(site)}")
            # The layout this tool grew up in: a course directory holding
            # website/ and dropbox/ side by side.
            if (site.parent / "dropbox").is_dir():
                lines.append(f"dropbox = {tilde(site.parent / 'dropbox')}")
        if not own:
            lines.append(f"ou = {ou}")
        out[ou] = lines
    return out


def private_write(path, text):
    """A new file only readable by its owner, in a directory only its owner can
    enter. Never over an existing one: O_EXCL makes that the kernel's check."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def cmd_init(args):
    """Write the courses file and the keepalive file, whichever is missing.

    The courses file gets a [label] for each course you teach that has not
    ended, read off your enrollments: the course's number, or its number and
    section when you teach several. --sites names directories to look in for
    each course's website repo, a _quarto.yml no more than three levels down,
    and a course found there gets its site, and its dropbox if one sits beside
    the site.

    The keepalive file is only for a machine that keeps sessions alive, with
    a timer running `keepalive`. When someone else's keep-alive pings yours,
    you hand it over with `session --export` and keep no file. So init asks
    which it is, and --keepalive or --no-keepalive answers in advance; with
    no terminal to ask at and neither given, it writes none. The file it
    writes holds this session, under your first name.

    A file that exists is never touched. For one that does, init prints what
    it lacks instead: a course you teach that no label names yet, and this
    session if it is not in the keepalive file. --dry-run prints both files
    and writes nothing.
    """
    s = open_session(args)
    me = whoami(s)
    today = dt.date.today().isoformat()
    courses = taught(s)
    labels = course_labels(courses)
    sites = find_sites(args.sites) if args.sites else {}
    sections = course_sections(courses, labels, sites)
    name = entry_name(me)
    entry = f"[{name}]\nstate = {tilde(s.state)}\n"

    courses_text = "\n\n".join(
        [f"# Courses for brightspace.py, a [label] each, written by `brightspace.py init`\n"
         f"# on {today} from the courses {me.get('FirstName')} teaches. Edit freely.\n"
         "# site = the course's website repo, whose urls.brightspace names its org unit\n"
         "# id; ou = the id, where the site does not name it; dropbox = where journal\n"
         "# and submissions --download write."]
        + ["\n".join(lines) for lines in sections.values()]) + "\n"
    keepalive_text = (
        f"# Sessions `brightspace.py keepalive` pings, a [name] each, written by\n"
        f"# `brightspace.py init` on {today}. Mode 0600: each one reaches every grade its\n"
        "# owner can. state = a directory `brightspace.py session` saved a session in;\n"
        "# cookies = a session handed over whole, the block `brightspace.py session\n"
        "# --export` prints on its owner's machine.\n\n"
        + entry + "\n# [colleague]\n# cookies = d2lSessionVal=...; d2lSecureSessionVal=...\n")

    if args.dry_run:
        for path, text, when in ((COURSES_FILE, courses_text, ""),
                                 (KEEPALIVE_FILE, keepalive_text, " (only if this machine keeps your session alive)")):
            print(f"--- {path}{' (exists, would be left alone)' if os.path.lexists(path) else when}\n{text}")
        return

    if not os.path.lexists(COURSES_FILE):
        # 0700 whichever file comes first: the keepalive file shares the directory.
        COURSES_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        COURSES_FILE.write_text(courses_text)
        found = sum(1 for ou, _ in courses if ou in sites)
        print(f"wrote {COURSES_FILE}: {', '.join(labels[ou] for ou, _ in courses) or 'no courses'}"
              + (f", {found} with a site" if args.sites else "; --sites DIR finds their sites"))
    else:
        missing = [ou for ou, _ in courses if ou not in known_courses()]
        print(f"{COURSES_FILE} exists, left alone; "
              + ("it names every course you teach" if not missing else "not in it yet:"))
        for ou in missing:
            print("\n" + "\n".join(sections[ou]))
    if not os.path.lexists(KEEPALIVE_FILE):
        mine = args.keepalive
        if mine is None and interactive():
            mine = ask("\nWill this machine keep your session alive? That takes a timer here running\n"
                       "`brightspace.py keepalive` every 15 minutes; systemd/ has one. Say no if\n"
                       "someone else's keep-alive pings your session. [y/N] ",
                       hidden=False).lower() in ("y", "yes")
        if mine:
            private_write(KEEPALIVE_FILE, keepalive_text)
            print(f"wrote {KEEPALIVE_FILE}, mode 0600: this session, as [{name}]. "
                  "The README's \"Keeping sessions alive\" says how to install the timer.")
        elif mine is None:
            print(f"no {KEEPALIVE_FILE} written: there was nobody to ask whether this machine keeps "
                  "your session alive. --keepalive says it does.")
        else:
            print("No keepalive file, then. Hand your session to whoever keeps it alive: "
                  "`brightspace.py session --export` prints the entry for their file.")
    else:
        try:
            states = [st.resolve() for _, st, _ in keepalive_entries(KEEPALIVE_FILE) if st]
        except Failed as e:
            print(f"{KEEPALIVE_FILE}: {e}")
        else:
            print(f"{KEEPALIVE_FILE} exists, left alone; "
                  + ("it has this session" if s.state.resolve() in states else
                     f"this session is not in it:\n\n{entry}"))


def cmd_folders(args):
    s, ou, _ = course(args)
    fs = folders(s, ou)
    rows = [[f["Id"], f["Name"], local(f.get("DueDate")), availability(f), "hidden" if f.get("IsHidden") else "",
             f"{f.get('TotalUsersWithSubmissions', 0)}/{f.get('TotalUsers', 0)}"] for f in fs]
    emit(args, rows, ["id", "name", "due", "available", "", "handed in"], fs)


def cmd_submissions(args):
    s, ou, crs = course(args)
    f = find_folder(s, ou, args.folder)
    subs = submissions(s, ou, f["Id"])
    if args.json:
        print(json.dumps(subs, indent=1))
        return
    rows = []
    for e in sorted(subs, key=lambda e: (e.get("Entity") or {}).get("DisplayName", "")):
        ent, ss = e.get("Entity") or {}, e.get("Submissions") or []
        latest = max(ss, key=lambda x: x.get("SubmissionDate") or "") if ss else None
        fb = e.get("Feedback") or {}
        rows.append([ent.get("DisplayName", "?"), STATUS.get(e.get("Status"), e.get("Status")), len(ss),
                     local(latest.get("SubmissionDate")) if latest else "",
                     ", ".join(x["FileName"] for x in (latest or {}).get("Files") or []),
                     "" if fb.get("Score") is None else fb["Score"]])
    print(f"{f['Name']} (entry {f['Id']}), due {local(f.get('DueDate')) or 'no date'}")
    table(["student", "status", "n", "latest", "files", "score"], rows)
    if not (args.download or args.out):
        return
    if args.out:
        out = pathlib.Path(args.out)
    elif crs.dropbox:
        out = crs.dropbox / "submissions" / f"{f['Name']} API {d2l_stamp(dt.datetime.now())}"
    else:
        raise Failed(f"{args.course} has no dropbox in {COURSES_FILE} to write under; pass --out")
    n, names, students = 0, classlist_names(s, ou), {}
    for e in subs:
        ent, ss = e.get("Entity") or {}, e.get("Submissions") or []
        picked = ss if args.every else ([max(ss, key=lambda x: x.get("SubmissionDate") or "")] if ss else [])
        uid = str(ent.get("EntityId"))
        for sub in picked:
            got = download(s, ou, f["Id"], sub, student_dir(out, ent.get("EntityId"), f["Id"], ent.get("DisplayName", "?")))
            n += len(got)
            when = local_dt(sub.get("SubmissionDate")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
            text = (sub.get("Comment") or {}).get("Html") or ""
            rows = students.setdefault(uid, (names.get(uid) or last_first(ent.get("DisplayName", "?")), []))[1]
            rows.extend((path.name, text, when) for path in got)
    print(f"{n} files under {out}")
    if students:
        write_index(out, students)
        print(f"index.html beside them, {len(students)} students")


def cmd_journal(args):
    s, ou, crs = course(args)
    f = find_folder(s, ou, args.folder)
    today = dt.date.today()
    if args.all:
        since, until = None, None
    elif args.on:
        since = until = dt.date.fromisoformat(args.on)
    else:
        since, until = dt.date.fromisoformat(args.since) if args.since else today, None
    picked = [t for t in entries(s, ou, f["Id"])
              if (since is None or t[0].date() >= since) and (until is None or t[0].date() <= until)]
    if args.json:
        print(json.dumps([t[3] | {"Who": t[1], "UserId": t[2]} for t in picked], indent=1))
        return
    for when, name, _, sub in picked:
        c = sub.get("Comment") or {}
        text = (c.get("Text") or "").strip() or strip_html(c.get("Html") or "")
        files = ", ".join(x["FileName"] for x in sub.get("Files") or [])
        print(f"{when:%Y-%m-%d %H:%M}  {name}" + (f"  [{files}]" if files else ""))
        print("    " + text.replace("\n", "\n    ") if text else "    (no text)")
        print()
    span = "all dates" if since is None else (f"on {since}" if until else f"since {since}")
    print(f"{len(picked)} entries from {len({t[2] for t in picked})} students, {span}, in {f['Name']!r} (entry {f['Id']})")
    if args.no_write or not picked:
        return
    if args.out:
        out = pathlib.Path(args.out)
    elif crs.dropbox:
        out = crs.dropbox / "journals" / f"{f['Name']} API {d2l_stamp(dt.datetime.now())}"
    else:
        raise Failed(f"{args.course} has no dropbox in {COURSES_FILE} to write under; "
                     "pass --out or --no-write")
    n, names, students = 0, classlist_names(s, ou), {}
    for when, name, uid, sub in picked:
        into = student_dir(out, uid, f["Id"], name)
        into.mkdir(parents=True, exist_ok=True)
        text = (sub.get("Comment") or {}).get("Html") or ""
        page = into / f"{f['Name']}-{d2l_stamp(when)}.html"
        page.write_text(f"<html><body>{text}</body></html>")
        n += 1 + len(download(s, ou, f["Id"], sub, into))
        students.setdefault(str(uid), (names.get(str(uid)) or last_first(name), []))[1].append((page.stem, text, when))
    write_index(out, students)
    # This line's form is read by fetch.py, which takes the path off its end.
    print(f"{n} files under {out}")
    print(f"index.html beside them, {len(students)} students")


def cmd_index(args):
    """index.html for Journal downloads made before brightspace.py wrote one,
    built from their files: each student's folder, each entry's page, its time
    off the page's name, which is the minute the index shows."""
    s, ou, _ = course(args)
    names = classlist_names(s, ou)
    for d in args.dirs:
        out = pathlib.Path(d)
        if (out / "index.html").exists():
            print(f"{out}: has an index.html already, left alone")
            continue
        students = {}
        for folder in sorted(p for p in out.iterdir() if p.is_dir()) if out.is_dir() else []:
            m = re.fullmatch(r"(\d+)-\d+ - (.+)", folder.name)
            if not m:
                continue
            for page in folder.glob("*.html"):
                when = re.fullmatch(r".+-([A-Z][a-z]{2} \d{1,2}, \d{4}) (\d{1,2})(\d{2}) ([AP]M)", page.stem)
                if not when:
                    continue
                at = dt.datetime.strptime(f"{when.group(1)} {when.group(2)}:{when.group(3)} {when.group(4)}",
                                          "%b %d, %Y %I:%M %p")
                body = re.search(r"<body>(.*)</body>", page.read_text(errors="replace"), re.S)
                students.setdefault(m.group(1), (names.get(m.group(1)) or last_first(m.group(2).replace("-", " ")), []))[1] \
                    .append((page.stem, body.group(1) if body else "", at))
        if not students:
            print(f"{out}: no Journal entries in it to index")
            continue
        write_index(out, students)
        print(f"{out}: index.html, {sum(len(r) for _, r in students.values())} entries from {len(students)} students")


def cmd_quizzes(args):
    s, ou, _ = course(args)
    le, _ = s.versions()
    rows, raw = [], []
    for q in s.objects(f"/d2l/api/le/{le}/{ou}/quizzes/"):
        per_user, open_ = {}, 0
        for a in s.objects(f"/d2l/api/le/{le}/{ou}/quizzes/{q['QuizId']}/attempts/"):
            if a.get("Completed"):
                per_user[a.get("UserId")] = per_user.get(a.get("UserId"), 0) + 1
            else:
                open_ += 1
        raw.append(q | {"AttemptsPerUser": per_user})
        rows.append([q["QuizId"], q.get("Name", ""), "active" if q.get("IsActive") else "",
                     local(q.get("DueDate") or q.get("EndDate")), len(per_user),
                     sum(1 for n in per_user.values() if n >= 2), sum(per_user.values()), open_ or ""])
    emit(args, rows, ["id", "quiz", "", "due/ends", "students", "twice", "attempts", "unfinished"], raw)


def quiz_detail(s, ou, want):
    le, _ = s.versions()
    quizzes = list(s.objects(f"/d2l/api/le/{le}/{ou}/quizzes/"))
    if want.isdigit():
        hits = [q for q in quizzes if q.get("QuizId") == int(want)]
    else:
        hits = [q for q in quizzes if q.get("Name", "").strip().lower() == want.strip().lower()]
        if not hits:
            hits = [q for q in quizzes if want.strip().lower() in q.get("Name", "").lower()]
    if len(hits) != 1:
        names = ", ".join(f"{q.get('QuizId')} {q.get('Name')!r}" for q in (hits or quizzes))
        raise Failed(f"{'no' if not hits else 'several'} quizzes match {want!r}: {names}")
    return hits[0]


def ip_ranges(q):
    got = q.get("RestrictIPAddressRange")
    if not got:
        return "none — anyone with the link and the dates can start it"
    return "; ".join(r.get("IPRangeStart", "?") + (f" – {r['IPRangeEnd']}" if r.get("IPRangeEnd") else "")
                     for r in got)


def cmd_quiz(args):
    """One quiz's settings, including the things a mis-set exam gets wrong."""
    s, ou, _ = course(args)
    q = quiz_detail(s, ou, args.quiz)
    if args.json:
        print(json.dumps(q, indent=1))
        return
    rows = [
        ("id", q.get("QuizId")),
        ("visible to students", yes_no(q.get("IsActive"))),
        ("IP restriction", ip_ranges(q)),
        ("password", "set" if q.get("Password") else "none"),
        ("attempts allowed", attempts_of(q)),
        ("starts", local(q.get("StartDate")) or "no start date"),
        ("ends", local(q.get("EndDate")) or "no end date"),
        ("due", local(q.get("DueDate")) or "no due date"),
        ("time limit", limit_text(q.get("SubmissionTimeLimit") if isinstance(q.get("SubmissionTimeLimit"), dict) else None)),
        ("grade item", q.get("GradeItemId") or "not attached to the gradebook"),
        ("category", q.get("CategoryId") or "none"),
        ("shuffled", yes_no(q.get("Shuffle"))),
        ("paging", PAGING_LABELS.get(q.get("PagingTypeId"), str(q.get("PagingTypeId")))),
        ("in the calendar", yes_no(q.get("DisplayInCalendar"))),
    ]
    print(q.get("Name", "?"))
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"  {key.ljust(width)}  {'—' if value in (None, '') else value}")
    questions = s.api(f"/d2l/api/le/{s.versions()[0]}/{ou}/quizzes/{q['QuizId']}/questions/")
    got = questions.get("Objects") if isinstance(questions, dict) else questions
    print(f"  {'questions'.ljust(width)}  {len(got or [])}")
    for item in got or []:
        print(f"      {item.get('QuestionTypeId')}  {strip_html(str((item.get('QuestionText') or {}).get('Text') or ''))[:60]!r}")


def categories(s, ou):
    return s.api(f"/d2l/api/le/{s.versions()[0]}/{ou}/grades/categories/")


def grade_items(s, ou):
    return s.api(f"/d2l/api/le/{s.versions()[0]}/{ou}/grades/")


def weight_total(s, ou):
    """What the gradebook's weights add up to. D2L counts a category once and an
    item only when it sits outside one, so this is the number that has to be 100."""
    cats = {c["Id"]: c for c in categories(s, ou)}
    total = sum(c.get("Weight") or 0 for c in cats.values())
    loose = [g for g in grade_items(s, ou) if not (g.get("CategoryId") or 0)]
    return total + sum(g.get("Weight") or 0 for g in loose), cats, loose


def find_category(s, ou, want):
    if want is None:
        return None
    for c in categories(s, ou):
        if str(c["Id"]) == str(want) or c["Name"].strip().lower() == str(want).strip().lower():
            return c
    raise Failed(f"no grade category called {want!r}")


def find_item(s, ou, want):
    hits = [g for g in grade_items(s, ou)
            if str(g["Id"]) == str(want) or g["Name"].strip().lower() == str(want).strip().lower()]
    if len(hits) != 1:
        names = ", ".join(f"{g['Id']} {g['Name']!r}" for g in (hits or grade_items(s, ou)))
        raise Failed(f"{'no' if not hits else 'several'} grade items match {want!r}: {names}")
    return hits[0]


def show_weights(s, ou, label):
    total, cats, loose = weight_total(s, ou)
    print(f"  {label}: weights total {total:g}" + ("" if total == 100 else "  <-- not 100"))
    for c in sorted(cats.values(), key=lambda c: c["Name"]):
        print(f"      category {c['Name']!r} {c.get('Weight'):g}")
    for g in sorted(loose, key=lambda g: g["Name"]):
        print(f"      item     {g['Name']!r} {g.get('Weight'):g}  (in no category)")


def category_payload(template, name, weight, max_points):
    """A category's input, cloned from one that works: the read shape minus its id
    and its item list, the description reshaped, and the three values overridden.
    A function so a rehearsal can send exactly this to a sandbox."""
    payload = {k: v for k, v in template.items() if k not in ("Id", "Grades")}
    payload["Description"] = rich_input(payload.get("Description"))
    if isinstance(payload["Description"].get("Type"), int):
        # Read back as 1 or 2; the input takes the name.
        payload["Description"]["Type"] = "Html" if payload["Description"]["Type"] == 2 else "Text"
    return payload | {"Name": name, "Weight": float(weight), "MaxPoints": float(max_points)}


def cmd_new_category(args):
    """A grade category, shaped like one that already works in this course."""
    s, ou, _ = course(args)
    le, _ = s.versions()
    existing = categories(s, ou)
    if any(c["Name"].strip().lower() == args.name.strip().lower() for c in existing):
        raise Failed(f"a category called {args.name!r} already exists")
    if not existing:
        raise Failed("no existing category to copy the shape from; make the first one by hand")
    payload = category_payload(existing[0], args.name, args.weight, args.max_points)
    print(json.dumps(payload, indent=1))
    print(f"\n  modelled on the existing category {existing[0]['Name']!r}")
    show_weights(s, ou, "now")
    print(f"  after: {weight_total(s, ou)[0] + float(args.weight):g}")
    if args.dry_run:
        print("  --dry-run: nothing sent")
        return
    made = s.send("POST", f"/d2l/api/le/{le}/{ou}/grades/categories/", payload)
    back = next((c for c in categories(s, ou) if c["Id"] == made.get("Id")), {})
    print(f"\ncreated category {back.get('Name')!r}, id {made.get('Id')}")
    check_landed([("name", args.name, back.get("Name")),
                  ("weight", float(args.weight), back.get("Weight")),
                  ("points", float(args.max_points), back.get("MaxPoints")),
                  ("distribution", payload.get("WeightDistributionType"),
                   back.get("WeightDistributionType"))])
    show_weights(s, ou, "after")

def item_payload(full, changed):
    """An item's update, built from its own read-back so nothing unnamed changes.

    Three things the read shape gets wrong as input, all measured live on
    2026-09-27. The description has to be reshaped or it is dropped. The tool the
    item is attached to has to be sent back as read: the update replaces the whole
    object, so leaving it out detaches the item from its folder or quiz. And an
    item in a category that spreads its weight must be sent with no weight at all,
    or the update is refused with "Cannot set grade weight directly when weight is
    specified by grade category".
    """
    payload = {k: v for k, v in full.items() if k not in ("Id", "GradeSchemeUrl")}
    payload["Description"] = rich_input(payload.get("Description"))
    payload |= changed
    if (payload.get("CategoryId") or 0) and "Weight" not in changed:
        payload.pop("Weight", None)
    return payload


def cmd_set_item(args):
    """Rename a grade item, move it into a category, change its weight, or what it is out of.

    The route replaces the whole object, so this reads it first and sends it back
    with only the named fields changed -- including the tool it is attached to,
    which is what keeps a folder's grade item attached through a rename.
    """
    s, ou, _ = course(args)
    le, _ = s.versions()
    item = find_item(s, ou, args.item)
    full = s.api(f"/d2l/api/le/{le}/{ou}/grades/{item['Id']}")
    changed = {}
    if args.name:
        changed["Name"] = args.name
    if args.category is not None:
        changed["CategoryId"] = find_category(s, ou, args.category)["Id"]
    if args.weight is not None:
        changed["Weight"] = float(args.weight)
    if getattr(args, "points", None) is not None:
        changed["MaxPoints"] = float(args.points)
    folder = None
    if getattr(args, "folder", None):
        # The same attachment new-item makes, for an item that already exists:
        # setup reaches for it when a folder is made again under a name whose
        # column is already in the gradebook, as 240's A9 was on 2026-11-12.
        folder = find_folder(s, ou, args.folder)
        if folder.get("GradeItemId") and folder["GradeItemId"] != item["Id"]:
            raise Failed(f"{folder['Name']!r} already has grade item {folder['GradeItemId']}")
        was = full.get("AssociatedTool") or {}
        if was and was.get("ToolItemId") != folder["Id"]:
            raise Failed(f"{full['Name']!r} is attached to {was.get('ToolItemId')} already; "
                         "detach it in the web page first")
        changed["AssociatedTool"] = {"ToolId": DROPBOX_TOOL, "ToolItemId": folder["Id"]}
    if not changed:
        raise Failed("nothing to change: pass --name, --category, --weight, --points or --folder")
    new_max = changed.get("MaxPoints")
    if new_max is not None and full.get("MaxPoints") and new_max < full["MaxPoints"]:
        # Brightspace does not rescale the marks when an item's maximum goes
        # down: it caps each one above the new maximum at it. 120's Attendance,
        # 25 to 5 on 2026-11-24, turned a 15 and a 20 into 5 of 5 each.
        over = []
        for u in s.api(f"/d2l/api/le/{le}/{ou}/classlist/"):
            try:
                v = s.api(f"/d2l/api/le/{le}/{ou}/grades/{item['Id']}/values/{u['Identifier']}")
            except Exception:
                continue
            got = (v or {}).get("PointsNumerator")
            if got is not None and got > new_max:
                over.append(f"{u.get('DisplayName')} {got:g}")
        if over and not getattr(args, "clip", False):
            raise Failed(f"{full['Name']!r} out of {full['MaxPoints']:g} to {new_max:g}: Brightspace caps "
                         f"every mark above {new_max:g} at {new_max:g} rather than rescaling it, and "
                         f"{len(over)} are above it: {', '.join(over)}. Pass --clip to go ahead, then "
                         f"set those marks again with `grade` on the new scale")
    payload = item_payload(full, changed)
    if folder:
        payload["AssociatedTool"] = changed["AssociatedTool"]
    tool = changed.get("AssociatedTool") or full.get("AssociatedTool") or {}
    print(f"{full['Name']!r} (id {full['Id']}), attached to "
          f"{tool.get('ToolItemId') or 'nothing'}")
    for key, value in changed.items():
        print(f"  {key}: {full.get(key)!r} -> {value!r}")
    show_weights(s, ou, "now")
    if args.dry_run:
        print(json.dumps(payload, indent=1))
        print("  --dry-run: nothing sent")
        return
    s.send("PUT", f"/d2l/api/le/{le}/{ou}/grades/{item['Id']}", payload)
    after = s.api(f"/d2l/api/le/{le}/{ou}/grades/{item['Id']}")
    print(f"\nnow {after['Name']!r}, category {after.get('CategoryId')}, weight {after.get('Weight')}")
    checks = [(k.lower(), v, (after.get(k) or 0) if k == "CategoryId" else after.get(k))
              for k, v in changed.items()]
    checks.append(("attached to" if folder else "still attached to", tool or None,
                   after.get("AssociatedTool") or None))
    if folder:
        checks.append(("folder's grade item", item["Id"],
                       find_folder(s, ou, str(folder["Id"])).get("GradeItemId")))
    check_landed(checks)
    show_weights(s, ou, "after")

# --- what the API accepts, against what it returns --------------------------

# The dropbox's tool id, as every assignment's grade item reports it in
# AssociatedTool. A quiz's grade item is linked from the quiz instead.
DROPBOX_TOOL = 2000
# A folder's fields that are facts about it rather than settings: its ids and
# its counters. Neither a create nor an update sends them.
FOLDER_FACTS = ("Id", "ActivityId", "TotalFiles", "UnreadFiles", "FlaggedFiles",
                "TotalUsers", "TotalUsersWithSubmissions", "TotalUsersWithFeedback")
# A folder's file-type restriction, which the API reads and cannot write.
# Measured on a sandbox, 2026-10-03: AllowableFileType is ignored on a create,
# whatever its value, and CustomAllowableFileTypes is taken and ignored as a
# string or null but refused as any list -- "JSON Binding Error", for an empty
# one too -- which is the very shape it is read in, [".arr"]. So cloning a
# folder restricted to some file types failed outright. Neither is ever sent:
# a new folder says the restriction did not come with it, and set-folder
# leaves a restricted folder to the web page rather than risk lifting it.
FOLDER_UNWRITABLE = ("AllowableFileType", "CustomAllowableFileTypes")


def file_types(folder):
    """What a folder lets students hand in, in words, or None for anything."""
    custom = [t for t in folder.get("CustomAllowableFileTypes") or [] if t]
    if custom:
        return ", ".join(custom) + " files only"
    if folder.get("AllowableFileType"):
        return f"a restricted set of file types (setting {folder['AllowableFileType']})"
    return None


def rich_input(value):
    """Rich text as the API returns it, reshaped into what it accepts.

    D2L reads rich text back as {"Text", "Html"} and, on the folder and quiz
    routes, takes it only as {"Content", "Type"}. Sent in the first shape it is
    dropped without an error: that is how A4's folder went out with empty
    instructions on 2026-09-27, and why every write here ends by reading its
    object back. The dropbox *feedback* route is the other way round -- it
    keeps {"Text", "Html"} and drops {"Content", "Type"}, measured 2026-09-29
    -- so the shape is per route, and only the read-back settles it.
    """
    if isinstance(value, dict) and "Content" in value:
        return value
    if isinstance(value, dict):
        if value.get("Html"):
            return {"Content": value["Html"], "Type": "Html"}
        return {"Content": value.get("Text") or "", "Type": "Text"}
    return {"Content": value or "", "Type": "Text"}


# SUBMISSIONRULE_T in D2L's dropbox reference: what a folder does with a second
# submission. Assignment 4's folder, the template most folders here were copied
# from, is 3 -- a resubmission replaces the first -- which nobody had looked at
# until 2026-10-04, when "hand in as you go" was about to go into an exam's.
SUBMISSIONS_RULE = {"keep-all": 2, "overwrite": 3, "one": 4}


def rule_name(value):
    return {v: k for k, v in SUBMISSIONS_RULE.items()}.get(value, f"unknown ({value!r})")


def letters(rich):
    """Only the letters and digits a reader would see, for comparing what was
    written into rich text with what Brightspace keeps: markup, entities,
    whitespace and bullets all fall away, and a lost paragraph does not."""
    text = text_of(rich) if isinstance(rich, dict) else re.sub(r"<[^>]+>", "", rich or "")
    return re.sub(r"[^0-9A-Za-z]", "", html.unescape(text))


def link_html(url):
    """The one line an assignment folder carries: its page on the course site."""
    return {"Content": f'<p><a rel="noopener" href="{html.escape(url, quote=True)}">'
                       f'{html.escape(url)}</a></p>', "Type": "Html"}


def text_of(rich):
    """What a reader sees, from rich text in either shape."""
    if not isinstance(rich, dict):
        return rich or ""
    return (rich.get("Text")
            or re.sub(r"<[^>]+>", "", rich.get("Html") or rich.get("Content") or ""))


def words(text):
    """What rich text says, as one line of words."""
    return " ".join(strip_html(text or "").split())


def text_pair(sent, kept):
    """Rich text sent and the text kept, as words to show side by side, and the
    same string when they say the same thing. D2L keeps a plain text of its own
    beside the HTML, its tags taken out with nothing in their place, so two
    paragraphs come back run together: the spacing is not compared."""
    want, got = words(sent), words(kept)
    return (want, want) if "".join(want.split()) == "".join(got.split()) else (want, got)


def check_landed(checks, verb="created"):
    """Compare what was sent with what the server kept, and say which it was.

    This API drops what it does not understand and still answers 200 -- A4's
    instructions in the read shape, and every link attachment ever sent, since
    those are read-only. So a write is not done until it has been read back.
    """
    bad = [f"{label}: sent {want!r}, kept {got!r}" for label, want, got in checks if want != got]
    if bad:
        raise Failed(f"{verb}, but Brightspace did not keep all of it:\n  " + "\n  ".join(bad))
    print("  checked on the server: " + ", ".join(label for label, _, _ in checks))


def show_category(s, ou, cat_id, label):
    """The items in one category with their weights, which a new item redistributes."""
    if not cat_id:
        return
    cat = next((c for c in categories(s, ou) if c["Id"] == cat_id), None)
    if not cat:
        return
    items = sorted((g for g in grade_items(s, ou) if (g.get("CategoryId") or 0) == cat_id),
                   key=lambda g: g["Name"])
    print(f"  {label}: {cat['Name']!r} ({cat.get('Weight'):g}) holds "
          + ", ".join(f"{g['Name']!r} {(g.get('Weight') or 0):.3g}" for g in items))


def clone_item_payload(full, name):
    """A new grade item's body, shaped like the item full as the API returns it:
    its category, points and scale, under a new name, attached to nothing.

    In a category the category owns the weight: sending one is refused with
    "Cannot set grade weight directly when weight is specified by grade
    category" (live, 2026-09-27), and the category spreads its own over the new
    item. Outside one, the weight is a share of the final grade and is never the
    template's, so the total cannot drift: it starts at 0.
    """
    payload = {k: v for k, v in full.items() if k not in ("Id", "GradeSchemeUrl", "AssociatedTool", "Weight")}
    payload |= {"Name": name, "ShortName": "", "AssociatedTool": None,
                "Description": {"Content": "", "Type": "Text"}}
    if not (payload.get("CategoryId") or 0):
        payload["Weight"] = 0.0
    return payload


def cmd_new_item(args):
    """A grade item, shaped like a named one that already works in this course.

    The template is always named: an item carries a category, a scale and a
    number of points, and the newest item in a gradebook is as likely to be the
    final exam as an assignment. A category that spreads its weight over its
    items recomputes all of them when one is added -- which is the reason to put
    the item there -- so the weights are printed before and after.

    --folder attaches it to an assignment folder from the item's side. That is
    the one way to link the two that never rewrites the folder: an update
    replaces the whole object, and the API cannot write back the link
    attachments a folder made in the UI may carry, because it has no such field.
    Measured in the 120 Devshell on 2026-09-27: the folder's GradeItemId follows.
    """
    s, ou, _ = course(args)
    refuse_student_names(s, ou, f"the grade item {args.name!r}", args.name)
    le, _ = s.versions()
    if any(g["Name"].strip().lower() == args.name.strip().lower() for g in grade_items(s, ou)):
        raise Failed(f"a grade item called {args.name!r} already exists")
    ref = find_item(s, ou, args.like)
    full = s.api(f"/d2l/api/le/{le}/{ou}/grades/{ref['Id']}")
    payload = clone_item_payload(full, args.name)
    if args.points is not None:
        payload["MaxPoints"] = float(args.points)
    if args.category is not None:
        payload["CategoryId"] = find_category(s, ou, args.category)["Id"]
    if args.weight is not None:
        payload["Weight"] = float(args.weight)
    elif not (payload.get("CategoryId") or 0):
        payload["Weight"] = 0.0
    folder = None
    if args.folder:
        folder = find_folder(s, ou, args.folder)
        if folder.get("GradeItemId"):
            raise Failed(f"{folder['Name']!r} already has grade item {folder['GradeItemId']}")
        payload["AssociatedTool"] = {"ToolId": DROPBOX_TOOL, "ToolItemId": folder["Id"]}
    cat = payload.get("CategoryId") or 0
    print(json.dumps(payload, indent=1))
    print(f"\n  modelled on {full['Name']!r}"
          + (f", attached to the folder {folder['Name']!r}" if folder else ""))
    show_weights(s, ou, "now")
    show_category(s, ou, cat, "now")
    if args.dry_run:
        print("  --dry-run: nothing sent")
        return
    made = s.send("POST", f"/d2l/api/le/{le}/{ou}/grades/", payload)
    back = s.api(f"/d2l/api/le/{le}/{ou}/grades/{made['Id']}")
    print(f"\ncreated grade item {back['Name']!r}, id {back['Id']}")
    checks = [("name", args.name, back.get("Name")),
              ("points", payload["MaxPoints"], back.get("MaxPoints")),
              ("category", cat, back.get("CategoryId") or 0)]
    if folder:
        checks.append(("folder's grade item", back["Id"],
                       find_folder(s, ou, str(folder["Id"])).get("GradeItemId")))
    check_landed(checks)
    show_weights(s, ou, "after")
    show_category(s, ou, cat, "after")


# --- a grade item's values -----------------------------------------------------
#
# A mark for named students in one grade item: First Last as the classlist has
# them, or their user ids. Plan by default; --go writes each and reads it back.
# Asked for on 2026-10-05, to give each logged presenter the Presentation
# item's full marks. The route replaces a value whole, so whatever it carries
# beside the points -- the comment a student reads, the private one -- is read
# first and sent back as it was.

def classlist_students(s, ou):
    le, _ = s.versions()
    return [u for u in s.api(f"/d2l/api/le/{le}/{ou}/classlist/")
            if re.search(r"student|learner", u.get("ClasslistRoleDisplayName") or "", re.I)]


NAME_WORD = re.compile(r"[A-Za-z][A-Za-z'\u2019-]*[A-Za-z]")


def name_words(text):
    """The words a name check compares: each word, and each part of a hyphened
    or an apostrophed one, lowercased, three letters or more."""
    out = set()
    for w in NAME_WORD.findall(str(text or "").lower()):
        out.add(w)
        out.update(p for p in re.split(r"[-'\u2019]", w) if p)
    return {w for w in out if len(w) >= 3}


def refuse_student_names(s, ou, what, *texts):
    """Stop before a student's name reaches anything this makes or renames.

    A quiz's, a grade item's or a folder's name, description and header go
    wherever Brightspace shows the course its activities, and once, unasked,
    into a notification email: a make-up quiz named for its one student was
    emailed the moment it was made, hidden and inactive, where an earlier quiz
    named the same way sent nothing. Which setting did it is not known, so names
    stay off activities altogether, and the question does not arise. One
    student's sitting is kept theirs by special access, never by its name. Any
    classlist student's first or last name, as a word, stops the command before
    anything is sent.
    """
    words = set()
    for u in classlist_students(s, ou):
        for key in ("FirstName", "LastName"):
            words |= name_words(u.get(key))
    for text in texts:
        found = sorted(name_words(text) & words)
        if found:
            raise Failed(f"{what} would carry a student's name ({', '.join(found)}): no names on activities, "
                         "since Brightspace has emailed one. Name the thing, not the student; a sitting for "
                         "one student is theirs by special access")


def student_named(students, who):
    """The one student a name or a user id means, or a reason there is none."""
    who = who.strip()
    if who.isdigit():
        hits = [u for u in students if str(u.get("Identifier")) == who]
    else:
        want = " ".join(who.lower().split())
        hits = [u for u in students
                if " ".join(f"{u.get('FirstName', '')} {u.get('LastName', '')}".lower().split()) == want
                or " ".join((u.get("DisplayName") or "").lower().split()) == want]
    if len(hits) == 1:
        return hits[0], None
    return None, "not on the classlist" if not hits else f"{len(hits)} students of that name"


def grade_value(s, ou, item_id, user_id):
    """A student's value in a grade item, or None where there is none yet."""
    le, _ = s.versions()
    try:
        return s.api(f"/d2l/api/le/{le}/{ou}/grades/{item_id}/values/{user_id}")
    except Failed as e:
        if str(e).startswith("404 "):
            return None
        raise


def points_of(value):
    got = (value or {}).get("PointsNumerator")
    return None if got is None else float(got)


def cmd_grade(args):
    """Give named students a mark in one grade item: its full marks, or --points.

    The students are First Last as the classlist has them, or their user ids;
    one not on the classlist, a student who withdrew say, is named and left
    out. Without --go it prints each student's mark now and the one it would
    set, and sends nothing; a student who already has it is left alone. --go
    writes the rest and reads each back. The comments on a value are kept, since
    its route replaces a value whole.
    """
    if args.max == (args.points is not None):
        raise Failed("--max or --points N: the mark to give")
    s, ou, _ = course(args)
    le, _ = s.versions()
    item = find_item(s, ou, args.item)
    if item.get("GradeType") != "Numeric":
        raise Failed(f"{item['Name']!r} is a {item.get('GradeType')} item; grade sets numeric ones only")
    most = float(item.get("MaxPoints") or 0)
    mark = most if args.max else float(args.points)
    if mark < 0 or (mark > most and not item.get("CanExceedMaxPoints")):
        raise Failed(f"{item['Name']!r} is out of {most:g}, so {mark:g} cannot go in it")
    students = classlist_students(s, ou)
    plan, missing, seen = [], [], set()
    for who in args.students:
        u, why = student_named(students, who)
        if not u:
            missing.append(f"{who} ({why})")
        elif u["Identifier"] not in seen:
            seen.add(u["Identifier"])
            plan.append((u, grade_value(s, ou, item["Id"], u["Identifier"])))
    print(f"{item['Name']!r} in {args.course}, out of {most:g}: {mark:g} for {len(plan)} "
          f"student{'' if len(plan) == 1 else 's'}")
    todo = []
    for u, value in plan:
        now = points_of(value)
        name = f"{u.get('FirstName', '')} {u.get('LastName', '')}".strip()
        print(f"  {name:28} " + (f"has {now:g} already" if now == mark else
                                  f"{'none' if now is None else f'{now:g}'} -> {mark:g}"))
        if now != mark:
            todo.append((u, value, name))
    if missing:
        print("  not graded: " + "; ".join(missing))
    if not args.go:
        print(f"\n  nothing sent; --go sets the {len(todo)} that differ" if todo else "\n  nothing to change")
        return
    for u, value, name in todo:
        path = f"/d2l/api/le/{le}/{ou}/grades/{item['Id']}/values/{u['Identifier']}"
        s.send("PUT", path, {"GradeObjectType": 1, "PointsNumerator": mark,
                             "Comments": rich_input((value or {}).get("Comments")),
                             "PrivateComments": rich_input((value or {}).get("PrivateComments"))})
        back = grade_value(s, ou, item["Id"], u["Identifier"])
        check_landed([(f"{name}'s mark", mark, points_of(back)),
                      (f"{name}'s comment", text_of((value or {}).get("Comments")),
                       text_of((back or {}).get("Comments")))], "set")
    print(f"\n{len(todo)} set" if todo else "\nnothing to change")


# --- announcements ----------------------------------------------------------
#
# Reading only. Creating one -- how Artem posts each Journal prompt -- is refused
# with 400 "Invalid Parameters" in every shape the documentation allows: JSON and
# multipart/mixed, the body as RichTextInput and as RichText, every documented
# field including IsPinned, published and draft, LE 1.50 to 1.99, cookies and a
# minted bearer token. Measured in the 120 Devshell on 2026-09-27. The next step
# is the request Brightspace's own page sends: devtools, Network, Copy as cURL,
# the next time a prompt is posted by hand.

def cmd_announcements(args):
    """The latest announcements: when each appears, and whether it is a draft."""
    s, ou, _ = course(args)
    le, _ = s.versions()
    items = sorted(s.api(f"/d2l/api/le/{le}/{ou}/news/"), key=lambda n: n.get("StartDate") or "")
    shown = items[-args.last:]
    rows = [[n["Id"], local(n.get("StartDate")), "" if n.get("IsPublished") else "draft",
             n.get("Title", "")] for n in shown]
    emit(args, rows, ["id", "appears", "", "title"], shown)


def local_to_utc(text):
    """'2026-09-29 12:30' in this machine's timezone -> what D2L wants."""
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        raise Failed(f"{text!r} is not a date and time like '2026-09-29 12:30'")
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def quiz_questions(s, ou, qid):
    got = s.api(f"/d2l/api/le/{s.versions()[0]}/{ou}/quizzes/{qid}/questions/")
    return (got.get("Objects") if isinstance(got, dict) else got) or []


def attempts_allowed(text):
    """A quiz's attempts as the API takes them: a whole number, or None for
    unlimited, which reads back as AttemptsAllowed's IsUnlimited (2026-11-25)."""
    text = str(text).strip()
    if text.lower() == "unlimited":
        return None
    if not re.fullmatch(r"[1-9]\d*", text):
        raise Failed(f"attempts: is a whole number, or unlimited, not {text!r}")
    return int(text)


def paging_code(name):
    """A paging name, all, 1, 5, 10 or sections, as its PagingTypeId."""
    key = str(name).strip().lower()
    if key not in PAGING:
        raise Failed(f"paging: is all, 1, 5, 10 or sections (questions per page, or a page per section), not {name!r}")
    return PAGING[key]


def attempts_of(q):
    """A quiz's attempts as a person reads them: the number, or unlimited."""
    a = q.get("AttemptsAllowed")
    if not isinstance(a, dict):
        return str(q.get("NumberOfAttemptsAllowed"))
    return "unlimited" if a.get("IsUnlimited") else str(a.get("NumberOfAttemptsAllowed"))


def quiz_update_payload(q):
    """A quiz as the API returns it, reshaped into what its PUT accepts.

    The update replaces every property, so nothing may be left behind: the
    attempts go from the nested read shape to the flat input field, and the four
    rich-text blocks keep their words but change their shape.
    """
    payload = {k: v for k, v in q.items() if k not in ("QuizId", "ActivityId", "AttemptsAllowed")}
    payload["NumberOfAttemptsAllowed"] = (q.get("AttemptsAllowed") or {}).get("NumberOfAttemptsAllowed")
    for key in ("Instructions", "Description", "Header", "Footer"):
        if isinstance(q.get(key), dict):
            payload[key] = {"Text": rich_input(q[key].get("Text")),
                            "IsDisplayed": bool(q[key].get("IsDisplayed"))}
    return payload


def cmd_set_quiz(args):
    """Change a quiz's name, shuffle or auto-publish, and nothing else.

    The route replaces the whole quiz, so this reads it, reshapes it into the
    input form, changes only the named fields, and reads it back -- checking the
    named fields, every setting it was not asked to touch, and the number of
    questions, since an update that lost any of those would say 200 regardless.
    """
    s, ou, _ = course(args)
    le, _ = s.versions()
    before = quiz_detail(s, ou, args.quiz)
    payload = quiz_update_payload(before)
    changed = {}
    if args.name is not None:
        changed["Name"] = args.name
    if args.shuffle is not None:
        changed["Shuffle"] = args.shuffle
    if args.auto_publish is not None:
        changed["IsAutoSetGraded"] = args.auto_publish
    if args.password is not None:
        changed["Password"] = args.password
    # A quiz's due date and its end are one moment, as new-quiz makes them: the
    # end of the class it runs in. Artem, 2026-10-19: "the due date for quizzes
    # should be end of class."
    if args.start is not None:
        changed["StartDate"] = local_to_utc(args.start)
    if args.end is not None:
        changed["EndDate"] = changed["DueDate"] = local_to_utc(args.end)
    if args.calendar is not None:
        changed["DisplayInCalendar"] = args.calendar
    if args.special_access_only is not None:
        changed["AllowOnlyUsersWithSpecialAccess"] = args.special_access_only
    if args.minutes is not None:
        # The timer, enforced and with its clock shown, as new-quiz makes it;
        # `none` takes it off. A quiz that gained a question needs a minute
        # more, which the input form only takes as the whole limit.
        had = before.get("SubmissionTimeLimit") if isinstance(before.get("SubmissionTimeLimit"), dict) else {}
        if no_limit(args.minutes):
            limit = dict(NO_LIMIT)
        else:
            limit = {"IsEnforced": True, "ShowClock": True, "TimeLimitValue": int(args.minutes)}
        if limit_of(had) != limit_of(limit):
            changed["SubmissionTimeLimit"] = limit
    if args.description is not None:
        # The description a student reads before starting, written as markdown,
        # the way a quiz's YAML holds it and setup-quiz sends it. A quiz that
        # gained a question kept "Seven questions" in it until this existed.
        if not args.description.strip():
            # an empty one clears it, as the quiz had none
            changed["Description"] = {"Text": {"Content": "", "Type": "Html"}, "IsDisplayed": False}
        else:
            try:
                html_text = quiz_format().md_html(args.description, "--description")
            except Exception as e:
                raise Failed(str(e))
            changed["Description"] = {"Text": {"Content": html_text, "Type": "Html"}, "IsDisplayed": True}
    if args.header is not None:
        # The header shows above every page of the quiz: the resources allowed
        # and the mechanics, which is where E1 put them. An HTML file, kept in
        # the repo beside the quiz so setup.sh can apply it.
        html_text = pathlib.Path(args.header).read_text(encoding="utf-8").strip()
        # D2L substitutes a replace string for the caller at save time when the
        # text arrives through the API: a header with {OrgDefinedId} written this
        # way carries the instructor's own ID for every student. 230's midterm,
        # 2026-10-07: ten students in one seat. The quiz editor keeps the string.
        strings = sorted(set(re.findall(r"\{[A-Za-z]+\}", html_text)))
        if strings:
            raise Failed(f"the header holds the replace string{'s' if len(strings) > 1 else ''} "
                         f"{', '.join(strings)}; saved through the API it would carry your own value "
                         "for every student. Write it in the quiz editor, and check the link as a student.")
        changed["Header"] = {"Text": {"Content": html_text, "Type": "Html"}, "IsDisplayed": True}
    changed = {k: v for k, v in changed.items()
               if k in ("Header", "Description", "SubmissionTimeLimit") or before.get(k) != v}
    if not changed:
        raise Failed("nothing to change: the quiz already has those values, or none were named")
    refuse_student_names(s, ou, f"quiz {before.get('Name')!r}'s new name, header or password", changed.get("Name"),
                         pathlib.Path(args.header).read_text(encoding="utf-8") if args.header else None,
                         changed.get("Password"))
    payload |= changed
    labels = {"IsAutoSetGraded": "auto-publish attempt results", "Header": "header",
              "Description": "description", "SubmissionTimeLimit": "time limit"}
    print(f"{before['Name']!r} (id {before['QuizId']})")
    for key, value in changed.items():
        if key == "SubmissionTimeLimit":
            print(f"  time limit: {limit_text(before.get(key))} -> {limit_text(value)}")
        elif key == "Description":
            print(f"  description: {text_of((before.get(key) or {}).get('Text'))[:60]!r}… -> "
                  f"{text_of(value['Text'])[:60]!r}…")
        elif key == "Header":
            print(f"  header: {len(text_of((before.get('Header') or {}).get('Text')))} chars -> "
                  f"{len(text_of(value['Text']))} chars, displayed")
        else:
            print(f"  {labels.get(key, key)}: {before.get(key)!r} -> {value!r}")
    questions = len(quiz_questions(s, ou, before["QuizId"]))
    if args.dry_run:
        print("  --dry-run: nothing sent")
        return
    s.send("PUT", f"/d2l/api/le/{le}/{ou}/quizzes/{before['QuizId']}", payload)
    after = quiz_detail(s, ou, str(before["QuizId"]))
    kept = ("StartDate", "EndDate", "DueDate", "IsActive", "Password", "GradeItemId",
            "AutoExportToGrades", "SubmissionTimeLimit", "LateSubmissionInfo",
            "RestrictIPAddressRange", "AttemptsAllowed")
    def landed(k, v):
        if k == "SubmissionTimeLimit":
            return ("time limit", limit_of(v), limit_of(after.get(k)))
        if k == "Description":
            return ("description", *text_pair(v["Text"]["Content"], text_of((after.get(k) or {}).get("Text"))))
        if k == "Header":
            return ("header", text_of(v["Text"]).strip(), text_of((after.get("Header") or {}).get("Text")).strip())
        return (labels.get(k, k), v, after.get(k))
    check_landed([landed(k, v) for k, v in changed.items()]
                 + [(k, before.get(k), after.get(k)) for k in kept if k not in changed]
                 + [("questions", questions, len(quiz_questions(s, ou, before["QuizId"])))])


def no_limit(minutes):
    """Whether --minutes, or a file's `minutes:`, says none: a quiz with no time
    limit, which a course's quiz defaults would otherwise put one on. An exam
    is the case: E1 ran with none, and its 120 defaults say 8 minutes."""
    return minutes is not None and str(minutes).strip().lower() == "none"


# No time limit, as the server holds one: not enforced, and 0 minutes. Not
# enforced with minutes above 0 is a recommended time limit, which students are
# shown: what Julia's Practice Final Exam Quiz got on 2026-11-25 from minutes:
# none, which kept the 12 of the quiz it copied. The editor's own API says
# notimelimit for 0 and recommended for 12; 120's Progress Exam 1 has the 0.
NO_LIMIT = {"IsEnforced": False, "ShowClock": False, "TimeLimitValue": 0}


def limit_of(limit):
    """A quiz's time limit as (enforced, minutes), whichever shape it comes in:
    minutes is None for no limit, and a number not enforced is recommended."""
    limit = limit or {}
    return (bool(limit.get("IsEnforced")), limit.get("TimeLimitValue") or None)


def limit_text(limit):
    """A time limit as a person reads it: 12 minutes, 12 minutes recommended, none."""
    on, minutes = limit_of(limit)
    return f"{minutes} minutes" + ("" if on else " recommended") if minutes else "none"


def cmd_new_quiz(args):
    """A quiz, shaped like one that already works in this course.

    Its questions cannot come this way: the API has no route that creates one, so
    they are imported or written by hand afterwards.
    """
    template = template_from = None
    if getattr(args, "like_from", None):
        # The shape from a quiz in another course: a sandbox has nothing to copy
        # and the tool will not invent a quiz, so the live course's is read
        # instead. Its category, grade item and IP range belong to that course
        # and are dropped by make_quiz.
        if not args.like:
            raise Failed("--like-from needs --like, the quiz to copy there")
        c_from = course_site(args.like_from)
        template = quiz_detail(open_session(args, c_from.host), c_from.ou, args.like)
        template_from = args.like_from
    if make_quiz(args, template=template, template_from=template_from):
        print("Left to do by hand, because no API route creates a question:")
        print("  its questions -- imported from a CSV, or one Written Response question with")
        print("  'Enable inserted images and attachments' ticked for files -- then make it visible.")


def make_quiz(args, description=None, item_hint="--grade-item attaches one", template=None,
              template_from=None):
    """new-quiz's work, for setup-quiz as well: the quiz made and read back, or
    with --dry-run the payload printed. description is HTML for the quiz's own;
    item_hint is what is said when it goes to no grade item; template is the
    quiz to copy, read in the course template_from names when that is another.
    The quiz, or None."""
    s, ou, _ = course(args)
    refuse_student_names(s, ou, f"the quiz {args.name!r}", args.name, description, args.password)
    le, _ = s.versions()
    quizzes = list(s.objects(f"/d2l/api/le/{le}/{ou}/quizzes/"))
    if any(q["Name"].strip().lower() == args.name.strip().lower() for q in quizzes):
        raise Failed(f"a quiz called {args.name!r} already exists")
    if template is None:
        template = quiz_detail(s, ou, args.like) if args.like else (quizzes[-1] if quizzes else None)
    if not template:
        raise Failed("no existing quiz to copy the shape from; make the first one by hand")
    payload = dict(template)
    if template_from:
        # A quiz category belongs to its course, so one read elsewhere means
        # nothing here; the grade item and the IP range are set below either way.
        payload["CategoryId"] = None
    # QuizReadData is QuizData plus these three: the two ids, and the attempts as
    # a nested object where the input wants a flat NumberOfAttemptsAllowed. Sent
    # back as read, the whole quiz is refused as a "JSON Binding Error" --
    # measured in the 120 Devshell on 2026-09-27, the night before Quiz 3.
    for key in ("QuizId", "ActivityId", "AttemptsAllowed"):
        payload.pop(key, None)
    # Shuffle and IsAutoSetGraded -- the UI's "Auto-publish attempt results
    # immediately upon completion" -- are the template's. Both were forced off
    # here until 2026-09-27, which is how Quiz 3 came out without either while
    # Quiz 2, the quiz it copied, has both on.
    payload |= {
        "Name": args.name,
        "IsActive": bool(args.active),
        "StartDate": local_to_utc(args.start) if args.start else None,
        "EndDate": local_to_utc(args.end) if args.end else None,
        "DueDate": local_to_utc(args.end) if args.end else None,
        "NumberOfAttemptsAllowed": attempts_allowed(args.attempts),
        "Password": args.password,
        "AutoExportToGrades": False,
        "GradeItemId": None,
        "RestrictIPAddressRange": [],
    }
    # The order of the questions and how many to a page: the template's unless asked.
    if getattr(args, "shuffle_questions", None) is not None:
        payload["Shuffle"] = as_flag(args.shuffle_questions)
    if getattr(args, "paging", None) is not None:
        payload["PagingTypeId"] = paging_code(args.paging)
    if getattr(args, "auto_publish", None) is not None:
        payload["IsAutoSetGraded"] = as_flag(args.auto_publish)
    # A timer only when --minutes asks for one; otherwise the template's limit,
    # enforced or not, as read. --minutes defaulted to 50 until 2026-10-01, so a
    # quiz copied from Midterm Part 1, which has no timer, came out with an
    # enforced fifty minutes and a clock nobody asked for.
    # Out of the calendar, and seen only by those given special access in the
    # UI: a sitting for one student is made that way, so that there is no moment
    # at which the course can see it.
    for flag, field in (("calendar", "DisplayInCalendar"), ("special_access_only", "AllowOnlyUsersWithSpecialAccess")):
        if as_flag(getattr(args, flag, None)) is not None:
            payload[field] = as_flag(getattr(args, flag))
    if no_limit(args.minutes):
        # No timer, whatever the template had: 0 minutes, not enforced. The
        # template's value kept with IsEnforced off is a recommended limit.
        payload["SubmissionTimeLimit"] = dict(NO_LIMIT)
    elif args.minutes is not None:
        payload["SubmissionTimeLimit"] = {"IsEnforced": True, "ShowClock": True,
                                          "TimeLimitValue": int(args.minutes)}
    if args.ip:
        try:
            start, end = args.ip.split("-", 1)
        except ValueError:
            raise Failed("--ip wants a range like 148.137.150.0-148.137.150.255")
        payload["RestrictIPAddressRange"] = [{"IPRangeStart": start.strip(), "IPRangeEnd": end.strip()}]
    # Each of these reads back as {"Text": {"Text", "Html"}, "IsDisplayed"} and is
    # accepted only with the inner part as {"Content", "Type"}. They are emptied,
    # since the template's wording belongs to the template.
    for key in ("Instructions", "Description", "Header", "Footer"):
        if isinstance(payload.get(key), dict):
            payload[key] = {"Text": {"Content": "", "Type": "Text"},
                            "IsDisplayed": bool(payload[key].get("IsDisplayed"))}
    # The quiz's own words, as HTML: setup-quiz's, from the Markdown in its file.
    if description:
        payload["Description"] = {"Text": {"Content": description, "Type": "Html"}, "IsDisplayed": True}
    if args.grade_item:
        # Linked from the quiz's side, the way Quiz 1 and 2 are, with scores sent
        # to it as they come in.
        payload["GradeItemId"] = find_item(s, ou, args.grade_item)["Id"]
        payload["AutoExportToGrades"] = True
    shown = dict(payload)
    print(json.dumps(shown, indent=1))
    print(f"\n  modelled on {template['Name']!r}" + (f" in {template_from}" if template_from else "")
          + ", with its instructions and header emptied"
          + (" and its description written" if description else ""))
    if not payload["GradeItemId"]:
        print("  no grade item: " + item_hint)
    if args.dry_run:
        print("  --dry-run: nothing sent")
        return None
    made = s.send("POST", f"/d2l/api/le/{le}/{ou}/quizzes/", payload)
    print(f"\ncreated quiz {made.get('Name')!r}, id {made.get('QuizId')}, "
          f"visible to students: {yes_no(made.get('IsActive'))}")
    back = quiz_detail(s, ou, str(made.get("QuizId")))
    ips = lambda rs: [(r.get("IPRangeStart"), r.get("IPRangeEnd")) for r in rs or []]
    check_landed([
        ("name", args.name, back.get("Name")),
        ("start", payload["StartDate"], back.get("StartDate")),
        ("end", payload["EndDate"], back.get("EndDate")),
        ("attempts", "unlimited" if payload["NumberOfAttemptsAllowed"] is None
         else str(payload["NumberOfAttemptsAllowed"]), attempts_of(back)),
        ("questions shuffled", bool(payload.get("Shuffle")), bool(back.get("Shuffle"))),
        ("paging", payload.get("PagingTypeId"), back.get("PagingTypeId")),
        ("auto-publish", bool(payload.get("IsAutoSetGraded")), bool(back.get("IsAutoSetGraded"))),
        ("time limit", limit_of(payload.get("SubmissionTimeLimit")), limit_of(back.get("SubmissionTimeLimit"))),
        ("IP range", ips(payload["RestrictIPAddressRange"]), ips(back.get("RestrictIPAddressRange"))),
        ("password", bool(args.password), bool(back.get("Password"))),
        ("calendar", payload.get("DisplayInCalendar"), back.get("DisplayInCalendar")),
        ("special access only", payload.get("AllowOnlyUsersWithSpecialAccess"),
         back.get("AllowOnlyUsersWithSpecialAccess")),
        ("grade item", payload["GradeItemId"], back.get("GradeItemId"))]
        + ([("description", *text_pair(description, text_of((back.get("Description") or {}).get("Text"))))]
           if description else []))
    return back


def cmd_delete_quiz(args):
    """Delete a quiz that holds nothing: no attempts, no questions, hidden from
    students, and attached to no grade item.

    That is all it will delete, on purpose. Attempts are student work; questions
    are authoring that no route can put back; a visible or graded quiz is in use.
    Any of those is deleted in the web interface, by someone looking at it. What
    is left is the blank a stray click on New Quiz leaves behind -- 230's
    "Untitled", found by the status report's first read on 2026-09-27, which is
    the case this exists for.

    Without --go it only says whether the quiz may go. With it, it deletes, then
    reads the quiz list back to see that it has gone.
    """
    s, ou, _ = course(args)
    le, _ = s.versions()
    q = quiz_detail(s, ou, args.quiz)
    qid = q["QuizId"]
    attempts = list(s.objects(f"/d2l/api/le/{le}/{ou}/quizzes/{qid}/attempts/"))
    got = s.api(f"/d2l/api/le/{le}/{ou}/quizzes/{qid}/questions/")
    questions = (got.get("Objects") if isinstance(got, dict) else got) or []
    print(f"{q['Name']!r} (id {qid}): {len(attempts)} attempts, {len(questions)} questions, "
          f"visible to students: {yes_no(q.get('IsActive'))}, "
          f"grade item: {q.get('GradeItemId') or 'none'}")
    refuse = []
    if attempts:
        refuse.append(f"{len(attempts)} attempts, which are student work")
    if questions:
        refuse.append(f"{len(questions)} questions")
    if q.get("IsActive"):
        refuse.append("it is visible to students")
    if q.get("GradeItemId"):
        refuse.append(f"it sends its scores to grade item {q['GradeItemId']}")
    if refuse:
        raise Failed("not deleting: " + "; ".join(refuse) + ".\n"
                     "       A quiz in use is deleted in the web interface, by someone looking at it.")
    if not args.go:
        print("  empty and unused, so it may go; --go deletes it")
        return
    s.send("DELETE", f"/d2l/api/le/{le}/{ou}/quizzes/{qid}", None)
    left = [x["QuizId"] for x in s.objects(f"/d2l/api/le/{le}/{ou}/quizzes/")]
    check_landed([("gone from the quiz list", False, qid in left)])
    print(f"deleted {q['Name']!r}")


def cmd_delete_folder(args):
    """Delete an assignment folder nobody has handed in to.

    A folder with a submission in it holds student work, and is never deleted
    here: that one goes in the web interface, by someone looking at it. What
    this is for is a folder made wrong -- the dates, or hidden when it should
    not have been -- so that setup can make it again: 240's A9, made hidden on
    2026-11-03 and never shown, was found with 0 of 8 hand-ins the morning
    after its deadline, 2026-11-12, and remade with new dates, visible.

    The grade item the folder sends its scores to is left alone, and setup
    attaches the remade folder to it. Without --go it only says whether the
    folder may go; with it, it deletes and reads the folder list back.
    """
    s, ou, _ = course(args)
    le, _ = s.versions()
    f = find_folder(s, ou, args.folder)
    fid = f["Id"]
    handed = f.get("TotalUsersWithSubmissions") or 0
    files = f.get("TotalFiles") or 0
    print(f"{f['Name']!r} (id {fid}): {handed} students handed in, {files} files, "
          f"hidden from students: {yes_no(f.get('IsHidden'))}, "
          f"due {local(f.get('DueDate')) or 'no date'}, grade item: {f.get('GradeItemId') or 'none'}")
    if handed or files:
        raise Failed(f"not deleting: {handed} students handed in {files} files, which are student work.\n"
                     "       A folder with work in it is deleted in the web interface, by someone looking at it.")
    if not args.go:
        print("  nothing handed in, so it may go; --go deletes it"
              + (f", and leaves grade item {f['GradeItemId']} in the gradebook" if f.get("GradeItemId") else ""))
        return
    s.send("DELETE", f"/d2l/api/le/{le}/{ou}/dropbox/folders/{fid}", None)
    left = [x["Id"] for x in folders(s, ou)]
    check_landed([("gone from the folder list", False, fid in left)])
    print(f"deleted {f['Name']!r}"
          + (f"; grade item {f['GradeItemId']} is still in the gradebook" if f.get("GradeItemId") else ""))


# --- one quiz into several sections -------------------------------------------
#
# The API copies a tool, never one item: a copy job asked for Quizzes copies
# every quiz in its source. So a quiz meant for several sections is made in a
# shell course that holds it and nothing else, and copied from there. Measured
# on the sandboxes on 2026-10-04: a job takes a few seconds, and the copy keeps
# the quiz's name, questions, dates and whether it is shown, and drops its link
# to a grade item without making one. Attempts are not copied.

COPY_POLL = 3      # seconds between reads of a copy job's status
COPY_WAIT = 600    # how long to wait for one before saying so


def copy_job(s, ou, source, components):
    """Copy the named components of the course source into ou, and wait for the
    job to finish: (its final status, its token)."""
    le, _ = s.versions()
    job = s.send("POST", f"/d2l/api/le/{le}/import/{ou}/copy/",
                 {"SourceOrgUnitId": source, "Components": components})
    token = job.get("JobToken")
    if not token:
        raise Failed(f"the copy into {ou} was not queued: {job!r}")
    start = time.time()
    while True:
        status = s.api(f"/d2l/api/le/{le}/import/{ou}/copy/{token}").get("Status")
        if status not in ("PENDING", "PROCESSING"):
            return status, token
        if time.time() - start > COPY_WAIT:
            raise Failed(f"copy job {token} into {ou} is still {status} after {COPY_WAIT}s. It may "
                         "finish on its own, so look in the course before running this again.")
        time.sleep(COPY_POLL)


def students_in(s, ou):
    le, _ = s.versions()
    return [u for u in s.api(f"/d2l/api/le/{le}/{ou}/classlist/")
            if re.search(r"student|learner", u.get("ClasslistRoleDisplayName") or "", re.I)]


def same_name(a, b):
    return a.strip().lower() == b.strip().lower()


# --- submission views ---------------------------------------------------------
# What a student sees of an attempt once it is published: the primary view, and
# additional views that each come into effect at a date, for one attempt, a range
# of grades, or so many minutes after submission. Valence has no route for them at
# any version. The quiz editor reads and writes them through the quizzes API at the
# tenant's own host, in Siren, each setting an action of its own; this does the
# same, with a bearer token minted from the session as the editor mints its own.
# Unofficial. Read off the editor's requests and tried in the 120 Devshell,
# 2026-11-26: a PATCH form-encoded lands at once, with no working copy to save.
VIEW_KEYS = ("primary_view", "additional_views")
# Every field of a view and what it is when a file leaves it out: what Brightspace
# gives a view it has just made, so that a file names only what it changes.
VIEW_FIELDS = {"grade": True, "questions": "none", "answers": False, "responses": True,
               "question_grades": False, "standards": False, "class_average": False,
               "distribution": False, "message": ""}
VIEW_LIMITS = ("from", "attempt", "grades", "minutes")
QUESTION_SHOWS = ("none", "all", "incorrect", "correct")


def view_spec(raw, at, additional=False, day=None):
    """One view from a quiz file, checked, with every field; an additional view's
    restrictions are off unless named. from: may be a time on the quiz's date."""
    if not isinstance(raw, dict):
        raise Failed(f"{at} is a block of keys such as grade: and questions:, not {raw!r}")
    known = tuple(VIEW_FIELDS) + (VIEW_LIMITS if additional else ())
    bad = [k for k in raw if k not in known]
    if bad:
        raise Failed(f"{at} has {', '.join(bad)}; {'an additional' if additional else 'the primary'} view "
                     f"takes {', '.join(known)}")
    spec = {}
    for key, default in VIEW_FIELDS.items():
        value = raw.get(key)
        if value is None or (not str(value).strip() and key != "message"):
            spec[key] = default
            continue
        if not isinstance(value, str):
            raise Failed(f"{at} {key}: is text, not {value!r}")
        word = value.strip().lower()
        if key == "questions":
            if word not in QUESTION_SHOWS:
                raise Failed(f"{at} questions: is none, all, incorrect or correct, not {value!r}")
            spec[key] = word
        elif key == "message":
            spec[key] = value
        elif word in TRUTH | FALSITY:
            spec[key] = word in TRUTH
        else:
            raise Failed(f"{at} {key}: is true or false, not {value!r}")
    spec["message_html"] = quiz_format().md_html(spec["message"], f"{at} message") if spec["message"].strip() else ""
    if not additional:
        return spec
    when = str(raw.get("from") or "").strip()
    if not when:
        raise Failed(f"{at} has no from:, the date and time it comes into effect, which Brightspace requires")
    clock = time_of_day(when)
    if clock:
        if not day:
            raise Failed(f"{at} from: {when} is a time of day, and the quiz has no date: to put it on")
        when = f"{day} {clock}"
    try:
        spec["from"] = dt.datetime.fromisoformat(when).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        raise Failed(f"{at} from: is a date and time like 2026-11-18 00:01, or a time on the quiz's date, "
                     f"not {when!r}")
    for key in ("attempt", "minutes"):
        value = str(raw.get(key) or "").strip()
        if value and not re.fullmatch(r"[1-9]\d*", value):
            raise Failed(f"{at} {key}: is a whole number, not {value!r}")
        spec[key] = int(value) if value else None
    grades = str(raw.get("grades") or "").strip()
    spec["grades"] = None
    if grades:
        m = re.fullmatch(r"(\d{1,3})\s*%?\s*-\s*(\d{1,3})\s*%?", grades)
        if not m or not int(m[1]) <= int(m[2]) <= 100:
            raise Failed(f"{at} grades: is a range of percents like 0-50, not {grades!r}")
        if spec["attempt"] is None:
            raise Failed(f"{at} grades: narrows attempt:, which Brightspace offers only together; name the attempt")
        spec["grades"] = (int(m[1]), int(m[2]))
    return spec


def view_line(spec, additional=False):
    """A view in one line, as a person reads it."""
    shown = ([] if spec["questions"] == "none" else
             [x for x, on in (("answers", spec["answers"]), ("responses", spec["responses"]),
                              ("question grades", spec["question_grades"])) if on])
    what = ["grade" if spec["grade"] else "no grade",
            ("no questions" if spec["questions"] == "none" else
             f"{spec['questions']} questions" + (" with " + ", ".join(shown) if shown else ""))]
    what += [x for x, on in (("standards", spec["standards"]), ("class average", spec["class_average"]),
                              ("grade distribution", spec["distribution"])) if on]
    if spec.get("message_html"):
        what.append("a message")
    if not additional:
        return "; ".join(what)
    when = [f"from {spec['from']}"]
    if spec["attempt"]:
        when.append(f"attempt {spec['attempt']}")
    if spec["grades"]:
        when.append(f"graded {spec['grades'][0]}-{spec['grades'][1]}%")
    if spec["minutes"]:
        when.append(f"for {spec['minutes']} minutes")
    return ", ".join(when) + ": " + "; ".join(what)


def same_view(a, b):
    """Two views alike: what a hidden question list would show does not count."""
    def key(v):
        k = {f: v.get(f) for f in VIEW_FIELDS if f != "message"} | {f: v.get(f) for f in VIEW_LIMITS}
        if k["questions"] == "none":
            k |= {"answers": None, "responses": None, "question_grades": None}
        return k
    return key(a) == key(b) and same_html(a.get("message_html") or "", b.get("message_html") or "")


class QuizViews:
    """A quiz's submission views through the quizzes API, as the quiz editor reaches them."""

    def __init__(self, s, ou, qid):
        if not s.meta.get("xsrf"):
            s.fetch_xsrf()
        status, _, body, _ = s.request("POST", "/d2l/lp/auth/oauth2/token", data={"scope": "*:*:*"},
                                       headers={"X-Csrf-Token": s.meta.get("xsrf", "")})
        try:
            self.token = json.loads(body)["access_token"]
            part = self.token.split(".")[1]
            tenant = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["tenantid"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise Failed(f"no token for the quizzes API ({status}): the session's XSRF token was refused; "
                         "`brightspace.py session` again")
        base = os.environ.get("BRIGHTSPACE_QUIZZES_API") or f"https://{tenant}.quizzes.api.brightspace.com"
        self.collection = f"{base.rstrip('/')}/{ou}/quizzes/{qid}/submissionviews"

    def call(self, method, url, form=None):
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        h = {"Authorization": "Bearer " + self.token, "User-Agent": USER_AGENT}
        if data is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method=method),
                                        timeout=TIMEOUT) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raise HTTPFailed(e.code, f"{e.code} on {method} {url}: {e.read()[:300].decode('utf-8', 'replace')}")
        except urllib.error.URLError as e:
            raise Failed(f"{url}: {e.reason}")

    @staticmethod
    def find(entity, name):
        for a in entity.get("actions") or []:
            if a.get("name") == name:
                return a
        for sub in entity.get("entities") or []:
            hit = QuizViews.find(sub, name)
            if hit:
                return hit
        return None

    @staticmethod
    def part(entity, cls):
        return next((e for e in entity.get("entities") or [] if cls in (e.get("class") or [])), None)

    def act(self, href, name, values=None):
        """One of a view's actions, its fields as they stand but for values."""
        a = self.find(self.call("GET", href), name)
        if not a:
            raise Failed(f"Brightspace offers no {name} on this view"
                         + (": a quiz with one attempt has no attempt to restrict a view to"
                            if "attempt" in name else ""))
        form = {}
        for f in a.get("fields") or []:
            v = (values or {}).get(f["name"], f.get("value"))
            if isinstance(v, list):
                v = next((x.get("value") for x in v if x.get("selected")), None)
            if isinstance(v, bool):
                v = "true" if v else "false"
            if v is not None:
                form[f["name"]] = v
        return self.call(a.get("method", "PATCH"), a["href"], form if a.get("method", "PATCH") != "DELETE" else None)

    def views(self):
        """[(href, additional, entity)], the primary view first."""
        out = []
        for e in self.call("GET", self.collection).get("entities") or []:
            out.append((e["href"], "secondary" in (e.get("class") or []), self.call("GET", e["href"])))
        return sorted(out, key=lambda v: v[1])

    def read(self, entity, additional):
        """A view as view_spec gives one, read off its entity."""
        def flag(name):
            a = self.find(entity, name)
            return bool(a and a["fields"] and a["fields"][0].get("value"))
        hs = self.part(entity, "hide-show-questions") or {}
        shown = (hs.get("entities") or [{}])[0].get("class") or []
        spec = {"grade": flag("update-show-attempt-score"),
                "questions": ("none" if "hide-questions" in (hs.get("class") or []) else
                              next((q for q in ("all", "incorrect", "correct") if f"{q}-questions" in shown), "all")),
                "answers": "show-correct-answers" in shown, "responses": "show-learner-responses" in shown,
                "question_grades": "show-question-score" in shown, "standards": flag("update-show-standards"),
                "class_average": flag("update-show-stats-class-average"),
                "distribution": flag("update-show-stats-score-distribution")}
        spec["message_html"] = ((self.part(entity, "message") or {}).get("properties") or {}).get("html") or ""
        spec["message"] = spec["message_html"]
        if not additional:
            return spec
        classes = entity.get("class") or []
        when = ((self.part(entity, "release-date") or {}).get("properties") or {}).get("localizedDate") or ""
        spec["from"] = when[:16].replace("T", " ")
        attempts = self.part(entity, "attempt-restrictions") or {}
        spec["attempt"] = (attempts.get("properties") or {}).get("value") if "attempt-restrictions" in classes else None
        # The grade range is on when the attempt part says so in its class; its
        # numbers are there either way, as None while it is off (Quiz 12, 115).
        on = spec["attempt"] and "grade-restrictions" in (attempts.get("class") or [])
        grades = ((self.part(attempts, "grade-restrictions") or {}).get("properties") or {}) if on else {}
        spec["grades"] = ((round(grades.get("min-grade") or 0), round(grades.get("max-grade") or 0)) if on else None)
        spec["minutes"] = (((self.part(entity, "time-limit") or {}).get("properties") or {}).get("value")
                           if "time-limit" in classes else None)
        return spec

    def write(self, href, spec, additional):
        self.act(href, "update-show-attempt-score", {"showAttemptScore": spec["grade"]})
        hide = spec["questions"] == "none"
        self.act(href, "update-hide-show-questions", {"hideShowQuestions": "hide-questions" if hide else "show-questions"})
        if not hide:
            self.act(href, "update-show-questions", {"showQuestions": f"{spec['questions']}-questions"})
            self.act(href, "update-show-correct-answers", {"showCorrectAnswers": spec["answers"]})
            self.act(href, "update-show-learner-responses", {"showLearnerResponses": spec["responses"]})
            self.act(href, "update-show-question-score", {"showQuestionScore": spec["question_grades"]})
        self.act(href, "update-show-standards", {"showStandards": spec["standards"]})
        self.act(href, "update-show-stats-class-average", {"showStatsClassAverage": spec["class_average"]})
        self.act(href, "update-show-stats-score-distribution", {"showStatsScoreDistribution": spec["distribution"]})
        self.act(href, "update-message", {"message": spec["message_html"]})
        if not additional:
            return
        self.act(href, "update-release-date", {"releaseDate": local_to_utc(spec["from"])})
        self.act(href, "update-attempt-restrictions", {"restrictAttempts": bool(spec["attempt"])})
        if spec["attempt"]:
            self.act(href, "update-attempt-restriction-number", {"attemptRestrictionNumber": spec["attempt"]})
            self.act(href, "update-grade-restrictions", {"restrictGrades": bool(spec["grades"])})
            if spec["grades"]:
                self.act(href, "update-min-max-grade", {"minGrade": spec["grades"][0], "maxGrade": spec["grades"][1]})
        self.act(href, "update-time-limit", {"timeLimit": bool(spec["minutes"])})
        if spec["minutes"]:
            self.act(href, "update-time-limit-number", {"timeLimitNumber": spec["minutes"]})

    def add(self):
        before = {h for h, _, _ in self.views()}
        self.act(self.collection, "add")
        new = [h for h, extra, _ in self.views() if extra and h not in before]
        if len(new) != 1:
            raise Failed(f"an additional view was asked for, and {len(new)} arrived")
        return new[0]

    def read_all(self):
        """(the primary view, [the additional views]) as specs."""
        have = self.views()
        primary = next((self.read(e, False) for _, extra, e in have if not extra), None)
        return primary, [self.read(e, True) for _, extra, e in have if extra]


def write_views(s, ou, qid, primary=None, additional=None):
    """The primary view, and the additional views in place of those there,
    as the specs say; None leaves that part as it is. Read back."""
    qv = QuizViews(s, ou, qid)
    have = qv.views()
    if primary is not None:
        qv.write(next(h for h, extra, _ in have if not extra), primary, False)
    if additional is not None:
        for href, extra, _ in have:
            if extra:
                qv.act(href, "delete-submission-view")
        for spec in additional:
            qv.write(qv.add(), spec, True)
    got_primary, got_more = qv.read_all()
    checks = []
    if primary is not None:
        checks.append(("primary view", view_line(primary), view_line(got_primary)
                       if same_view(primary, got_primary) else view_line(got_primary) + " (differs)"))
    if additional is not None:
        checks.append(("additional views", len(additional), len(got_more)))
        for n, (want, got) in enumerate(zip(additional, got_more), 1):
            checks.append((f"additional view {n}", view_line(want, True),
                           view_line(got, True) if same_view(want, got) else view_line(got, True) + " (differs)"))
    check_landed(checks, "views written")


def views_yaml(primary, additional):
    """A quiz's views as a quiz file's brightspace: block would hold them: the
    fields that differ from VIEW_FIELDS, and a message as the HTML it is."""
    def fields(spec, indent):
        out = []
        for key, default in VIEW_FIELDS.items():
            if key == "message":
                continue
            v = spec[key]
            if key in ("answers", "responses", "question_grades") and spec["questions"] == "none":
                continue
            if v != default or key == "questions":
                out.append(f"{indent}{key}: {str(v).lower() if isinstance(v, bool) else v}")
        if spec.get("message_html"):
            out += [f"{indent}message: |", f"{indent}  ```{{=html}}", f"{indent}  {spec['message_html']}", f"{indent}  ```"]
        return out
    lines = ["  primary_view:"] + fields(primary, "    ")
    lines.append("  additional_views:" + ("" if additional else " []"))
    for spec in additional:
        lines.append(f"  - from: {spec['from']}")
        lines += [f"    {k}: {spec[k]}" for k in ("attempt", "minutes") if spec[k]]
        if spec["grades"]:
            lines.append(f"    grades: {spec['grades'][0]}-{spec['grades'][1]}")
        lines += fields(spec, "    ")
    return "\n".join(lines)


def cmd_views(args):
    """A quiz's submission views: printed as the YAML a quiz file holds, or
    written from one, primary_view: and additional_views: from its brightspace: block."""
    s, ou, _ = course(args)
    q = quiz_detail(s, ou, args.quiz)
    if not args.file:
        primary, more = QuizViews(s, ou, q["QuizId"]).read_all()
        print(f"# {q['Name']}: what a student sees once an attempt is published.")
        print("# Fields left out are " + ", ".join(f"{k}: {str(v).lower() if isinstance(v, bool) else v or chr(39) * 2}"
                               for k, v in VIEW_FIELDS.items()) + ".")
        print("brightspace:")
        print(f"  auto_publish: {str(bool(q.get('IsAutoSetGraded'))).lower()}")
        print(views_yaml(primary, more))
        return
    plan = quiz_plan(pathlib.Path(args.file))
    if not any(k in plan for k in VIEW_KEYS):
        raise Failed(f"{args.file} has neither primary_view: nor additional_views: in its brightspace: block")
    print(f"{q['Name']!r} (id {q['QuizId']}):")
    if "primary_view" in plan:
        print("  primary view: " + view_line(plan["primary_view"]))
    for n, spec in enumerate(plan.get("additional_views") or [], 1):
        print(f"  additional view {n}: " + view_line(spec, True))
    if "additional_views" in plan:
        print("  in place of the additional views it has" if plan["additional_views"] else
              "  and no additional views: those it has are deleted")
    if not args.go:
        print("  nothing sent; --go writes them")
        return
    write_views(s, ou, q["QuizId"], plan.get("primary_view"), plan.get("additional_views"))


def cmd_copy_quiz(args):
    """Copy the one quiz in a shell course into each of several sections.

    The API copies a whole tool, so asked for quizzes it copies every quiz in
    the course it copies from. That course is therefore a shell: no students,
    and exactly one quiz, the one to copy, made there with its questions and
    settings. Each section receives exactly that quiz, with its questions, its
    dates and whether it is shown -- the same in every section, the first one
    included.

    The copy drops the quiz's link to a grade item. So each section's copy is
    attached to that section's item of the quiz's own name, or, where there is
    none and --item-like names one, to a new item shaped like that one.

    Without --go it checks everything and prints the plan, and sends nothing.
    It stops before copying anything if the shell has students or holds any
    number of quizzes but one, or if a section already has a quiz of that name.
    Nothing is deleted anywhere, unless --clear asks for the shell's quiz to go
    once every section has its copy, which leaves the shell ready for the next.
    """
    shell = course_site(args.shell)
    s = open_session(args, shell.host)
    le, _ = s.versions()
    people = students_in(s, shell.ou)
    if people:
        raise Failed(f"{args.shell} has {len(people)} students, so it is a section and not a shell: "
                     "a copy is made from a course with no students")
    held = list(s.objects(f"/d2l/api/le/{le}/{shell.ou}/quizzes/"))
    if len(held) != 1:
        raise Failed(f"{args.shell} holds {len(held)} quizzes"
                     + (f" ({', '.join(repr(x['Name']) for x in held)})" if held else "")
                     + ". A copy takes every quiz in it, so it must hold exactly the one to copy.")
    q = held[0]
    name, questions = q["Name"], len(quiz_questions(s, shell.ou, q["QuizId"]))
    dates = (q.get("StartDate"), q.get("EndDate"), q.get("DueDate"))
    print(f"{name!r} in {args.shell}: {questions} questions, "
          f"{'shown' if q.get('IsActive') else 'hidden'}, "
          + ("start " + local(dates[0]) + ", " if dates[0] else "")
          + ("end " + local(dates[1]) if dates[1] else "no end date"))
    plan = []
    for label in args.to:
        dest = course_site(label)
        if dest.ou == shell.ou:
            raise Failed(f"{label} is the shell itself")
        have = list(s.objects(f"/d2l/api/le/{le}/{dest.ou}/quizzes/"))
        if any(same_name(x["Name"], name) for x in have):
            raise Failed(f"{label} already has a quiz named {name!r}, and a copy would sit beside it; "
                         "nothing has been copied anywhere")
        items = grade_items(s, dest.ou)
        item = next((g for g in items if same_name(g["Name"], name)), None)
        like = None
        if not item and args.item_like:
            # previous: the item of the quiz numbered before this one, 'Quiz 2' for 'Quiz 3'.
            want = previous_name(name) if args.item_like.strip().lower() == "previous" else args.item_like
            like = next((g for g in items if want and same_name(g["Name"], want)), None)
            if not like:
                raise Failed(f"{label} has no grade item {want!r} to shape {name!r}'s on" if want else
                             f"{name!r} ends in no number, so --item-like previous names no grade item")
        plan.append((label, dest, {x["QuizId"] for x in have}, item, like))
        print(f"  into {label}: " + (f"attached to its grade item {item['Name']!r}" if item else
                                     f"attached to a new grade item {name!r}, shaped like {like['Name']!r}" if like else
                                     f"no grade item, since it has none called {name!r}; --item-like makes one"))
    # LockDown Browser's settings are Respondus's, and a course copy does not
    # bring them (Artem, 2026-11-26). So they are read off the shell's quiz here,
    # before anything is copied or --clear deletes it, and written onto each copy.
    ldb, why = None, drive_unready(s)
    if why:
        if RESPONDUS_SUFFIX.search(name):
            print(f"  LockDown Browser: the shell's quiz requires it, and bs-drive cannot carry its settings "
                  f"here ({why}); each copy's are set by hand")
    else:
        ldb = drive_read_lockdown(s, shell.ou, str(q["QuizId"]))
        if ldb is None:
            raise Failed(f"bs-drive could not read {name!r}'s LockDown settings in {args.shell}; nothing has been copied")
        if ldb.get("required"):
            print(f"  LockDown Browser required: {lockdown_line(ldb)}; written onto each copy, and read back")
        else:
            print("  LockDown Browser not required in the shell, so there are no settings to carry")
            ldb = None
    if not args.go:
        print("\n  nothing sent; --go copies it")
        return
    failed = []
    for label, dest, before, item, like in plan:
        print(f"\n{label}:")
        try:
            status, token = copy_job(s, dest.ou, shell.ou, ["Quizzes"])
            if status != "COMPLETE":
                raise Failed(f"copy job {token} ended {status}, so nothing else was done here")
            new = [x for x in s.objects(f"/d2l/api/le/{le}/{dest.ou}/quizzes/") if x["QuizId"] not in before]
            if len(new) != 1 or not same_name(new[0]["Name"], name):
                raise Failed(f"copy job {token} finished, and what arrived is not one {name!r}: "
                             + (", ".join(f"{x['QuizId']} {x['Name']!r}" for x in new) or "nothing")
                             + ". It is left as it is.")
            qid = new[0]["QuizId"]
            print(f"  copied, quiz {qid} (job {token})")
            if like:
                full = s.api(f"/d2l/api/le/{le}/{dest.ou}/grades/{like['Id']}")
                made = s.send("POST", f"/d2l/api/le/{le}/{dest.ou}/grades/", clone_item_payload(full, name))
                item = s.api(f"/d2l/api/le/{le}/{dest.ou}/grades/{made['Id']}")
                print(f"  made grade item {item['Name']!r}, id {item['Id']}, shaped like {like['Name']!r}")
            if item:
                payload = quiz_update_payload(quiz_detail(s, dest.ou, str(qid)))
                payload |= {"GradeItemId": item["Id"], "AutoExportToGrades": True}
                s.send("PUT", f"/d2l/api/le/{le}/{dest.ou}/quizzes/{qid}", payload)
            lockdown = []
            if ldb:
                # Before the name is compared: Respondus renames a quiz it guards.
                written = drive(s, *lockdown_argv(dest.ou, qid, ldb))
                got = drive_read_lockdown(s, dest.ou, str(qid)) if written else None
                left = lockdown_differences(ldb, got) if got else ["(not read)"]
                lockdown = [("LockDown settings", "the shell's", "the shell's" if written and not left else
                             "not written" if not written else "different in " + "; ".join(left))]
            back = quiz_detail(s, dest.ou, str(qid))
            views = [QuizViews(s, o, i).read_all() for o, i in ((shell.ou, q["QuizId"]), (dest.ou, qid))]
            same = (same_view(views[0][0], views[1][0]) and len(views[0][1]) == len(views[1][1])
                    and all(same_view(a, b) for a, b in zip(views[0][1], views[1][1])))
            if not same:
                # Not yet seen with views worth carrying (2026-11-26): say how to mend it.
                print(f"  the shell's submission views did not come with the copy; brightspace.py views {label} "
                      f"{shell_quote(name)} QUIZ.yml --go writes them from the quiz's file")
            check_landed([("name", name, back.get("Name")),
                          ("questions", questions, len(quiz_questions(s, dest.ou, qid))),
                          ("shown", q.get("IsActive"), back.get("IsActive")),
                          ("dates", dates, (back.get("StartDate"), back.get("EndDate"), back.get("DueDate"))),
                          ("submission views", True, same)] + lockdown
                         + ([("grade item", item["Id"], back.get("GradeItemId"))] if item else []), "copied")
        except Failed as e:
            failed.append(label)
            print(f"  {e}")
    if failed:
        raise Failed(f"not done in {', '.join(failed)}"
                     + ("; the shell keeps its quiz" if args.clear else ""))
    if args.clear:
        if list(s.objects(f"/d2l/api/le/{le}/{shell.ou}/quizzes/{q['QuizId']}/attempts/")):
            raise Failed(f"{name!r} has attempts in the shell, so it stays there")
        s.send("DELETE", f"/d2l/api/le/{le}/{shell.ou}/quizzes/{q['QuizId']}", None)
        left = [x["QuizId"] for x in s.objects(f"/d2l/api/le/{le}/{shell.ou}/quizzes/")]
        check_landed([("gone from the shell", False, q["QuizId"] in left)], "cleared")
    print(f"\n{name!r} is in {', '.join(label for label, *_ in plan)}"
          + ("; the shell is empty again" if args.clear else "; the shell still holds it, and --clear empties it"))


def cmd_new_folder(args):
    """An assignment folder, shaped like one that already works in this course.

    The same trick as new-quiz: clone a folder that Brightspace already accepted
    and override the handful of fields that differ, rather than assembling a
    payload out of the documentation. What differs between two assignments in
    the same course is the name, the two dates and the link; everything else --
    submission type, what a resubmission does, how many points it is out of --
    is course policy and should carry over untouched.

    The instructions are the assignment's URL and nothing else, which is how
    A1 through A3 were made: the assignment text lives on the course site, and a
    second copy inside Brightspace is a copy that goes stale.
    """
    s, ou, _ = course(args)
    refuse_student_names(s, ou, f"the folder {args.name!r}", args.name)
    le, _ = s.versions()
    # setup calls this with arguments it builds itself, which carry neither.
    instructions = getattr(args, "instructions", None)
    submissions = getattr(args, "submissions", None)
    existing = folders(s, ou)
    if any(f.get("Name", "").strip().lower() == args.name.strip().lower() for f in existing):
        raise Failed(f"a folder called {args.name!r} already exists")

    template = find_folder(s, ou, args.like) if args.like else (existing[-1] if existing else None)
    if not template:
        raise Failed("no existing folder to copy the shape from; make the first one by hand")

    payload = dict(template)
    # Read-only: the server's own id for the folder and its activity, and the
    # submission counters, which are facts about the template rather than
    # settings to carry over; and its file types, which no write can carry.
    for key in FOLDER_FACTS + FOLDER_UNWRITABLE:
        payload.pop(key, None)

    # The link goes in the instructions, in the shape the API accepts. Sent in the
    # shape it reads back, it was dropped without an error -- A4's folder went out
    # empty that way on 2026-09-27. A link attachment, the UI's other way to carry
    # it, is read-only here: no field in the update data and no route. The
    # template's own link is never carried over.
    # --instructions puts a whole HTML file there instead -- an exam's problems,
    # written where only its folder shows them -- and --link follows it if both.
    content = open(instructions, encoding="utf-8").read() if instructions else ""
    if args.link:
        content += link_html(args.link)["Content"]
    payload["CustomInstructions"] = ({"Content": content, "Type": "Html"} if content
                                     else {"Content": "", "Type": "Text"})
    payload.pop("LinkAttachments", None)
    payload |= {
        "Name": args.name,
        "IsHidden": not args.active,
        "Attachments": [],
        "GradeItemId": None,
    }
    if submissions:
        payload["SubmissionsRule"] = SUBMISSIONS_RULE[submissions]
    if args.due:
        payload["DueDate"] = local_to_utc(args.due)
    if args.open or args.due:
        # Availability is a pair, so it is written as a pair: an end without a
        # start reads in the UI as "available from the beginning of time".
        payload["Availability"] = {
            "StartDate": local_to_utc(args.open) if args.open else None,
            "EndDate": local_to_utc(args.due) if args.due else None,
            "StartDateAvailabilityType": 0,
            "EndDateAvailabilityType": 0,
        }
    if args.points:
        payload["Assessment"] = dict(payload.get("Assessment") or {},
                                     ScoreDenominator=float(args.points))
    if args.grade_item:
        want = args.grade_item
        items = grade_items(s, ou)
        if want.isdigit():
            hits = [g for g in items if str(g.get("Id")) == want]
        else:
            hits = [g for g in items if g.get("Name", "").strip().lower() == want.strip().lower()]
        if len(hits) != 1:
            names = ", ".join(f"{g.get('Id')} {g.get('Name')!r}" for g in (hits or items))
            raise Failed(f"{'no' if not hits else 'several'} grade items match {want!r}: {names}")
        payload["GradeItemId"] = hits[0]["Id"]

    print(json.dumps(payload, indent=1))
    print(f"\n  modelled on {template.get('Name')!r}")
    print(f"  hidden from students: {yes_no(payload['IsHidden'])}")
    print(f"  a second submission: {rule_name(payload.get('SubmissionsRule'))}")
    if file_types(template):
        print(f"  file types: {template.get('Name')!r} takes {file_types(template)}, which the "
              "API cannot set, so this folder takes any file -- restrict it in the web page")
    if not payload["GradeItemId"]:
        print("  no grade item: --grade-item attaches one, or new-item --folder afterwards")
    if args.dry_run:
        print("  --dry-run: nothing sent")
        return
    made = s.send("POST", f"/d2l/api/le/{le}/{ou}/dropbox/folders/", payload)
    print(f"\ncreated folder {made.get('Name')!r}, id {made.get('Id')}, "
          f"hidden: {yes_no(made.get('IsHidden'))}")
    back = find_folder(s, ou, str(made.get("Id")))
    checks = [("name", args.name, back.get("Name")),
              ("hidden", payload["IsHidden"], back.get("IsHidden")),
              ("points", (payload.get("Assessment") or {}).get("ScoreDenominator"),
               (back.get("Assessment") or {}).get("ScoreDenominator"))]
    if args.due:
        checks.append(("due", payload["DueDate"], back.get("DueDate")))
    if args.open:
        checks.append(("opens", payload["Availability"]["StartDate"],
                       (back.get("Availability") or {}).get("StartDate")))
    if args.link and not instructions:
        checks.append(("link", args.link, text_of(back.get("CustomInstructions")).strip()))
    if instructions:
        checks.append(("instructions, letter for letter", True,
                       letters(content) == letters(back.get("CustomInstructions"))))
    if submissions:
        checks.append(("a second submission", submissions, rule_name(back.get("SubmissionsRule"))))
    if payload.get("GradeItemId"):
        checks.append(("grade item", payload["GradeItemId"], back.get("GradeItemId")))
    check_landed(checks)


def cmd_set_folder(args):
    """Show an assignment folder to students or hide it, or change what a second
    submission does, and change nothing else.

    D2L updates a folder by replacing the whole object, so the update carries
    back every setting it read, with the instructions reshaped into the form the
    API accepts -- sent the way they are read, they are dropped, which is how
    A4's went out empty. What it cannot carry back is a link or a file attached
    in the web page: both are read-only here. A folder holding either is refused
    rather than rewritten without it. Everything is read back and compared.
    """
    submissions = getattr(args, "submissions", None)
    if not (args.show or args.hide or submissions):
        raise Failed("say what to change: --show, --hide or --submissions")
    s, ou, _ = course(args)
    le, _ = s.versions()
    f = find_folder(s, ou, args.folder)
    if f.get("LinkAttachments") or f.get("Attachments"):
        raise Failed(f"{f['Name']!r} carries an attachment the API cannot send back, so it is not "
                     "rewritten here; change it in the web page")
    if file_types(f):
        raise Failed(f"{f['Name']!r} takes {file_types(f)}, which the API cannot send back, so it "
                     "is not rewritten here; change it in the web page")
    hidden = bool(args.hide) if args.show or args.hide else bool(f.get("IsHidden"))
    rule = SUBMISSIONS_RULE[submissions] if submissions else f.get("SubmissionsRule")
    changes = []
    if hidden != bool(f.get("IsHidden")):
        changes.append(f"hidden from students {yes_no(f.get('IsHidden'))} -> {yes_no(hidden)}")
    if rule != f.get("SubmissionsRule"):
        changes.append(f"a second submission {rule_name(f.get('SubmissionsRule'))} -> {rule_name(rule)}")
    if not changes:
        already = ([f"already {'hidden' if hidden else 'shown'}"] if args.show or args.hide else []) \
            + ([f"already {submissions}"] if submissions else [])
        raise Failed(f"{f['Name']!r} is {' and '.join(already)}: nothing to change")
    payload = {k: v for k, v in f.items()
               if k not in FOLDER_FACTS + FOLDER_UNWRITABLE + ("Attachments", "LinkAttachments")}
    payload["CustomInstructions"] = rich_input(f.get("CustomInstructions"))
    payload["IsHidden"] = hidden
    if submissions:
        payload["SubmissionsRule"] = rule
    print(json.dumps(payload, indent=1))
    print(f"\n  {f['Name']!r}: " + "; ".join(changes))
    if args.dry_run:
        print("  --dry-run: nothing sent")
        return
    s.send("PUT", f"/d2l/api/le/{le}/{ou}/dropbox/folders/{f['Id']}", payload)
    back = find_folder(s, ou, str(f["Id"]))
    kept = [("name", lambda x: x.get("Name")),
            ("due", lambda x: x.get("DueDate")),
            ("opens", lambda x: (x.get("Availability") or {}).get("StartDate")),
            ("closes", lambda x: (x.get("Availability") or {}).get("EndDate")),
            ("instructions", lambda x: text_of(x.get("CustomInstructions")).strip()),
            ("grade item", lambda x: x.get("GradeItemId")),
            ("points", lambda x: (x.get("Assessment") or {}).get("ScoreDenominator")),
            ("submission type", lambda x: x.get("SubmissionType"))]
    check_landed([("hidden", hidden, back.get("IsHidden"))]
                 + [(label, get(f), get(back)) for label, get in kept]
                 + [("resubmissions", rule, back.get("SubmissionsRule"))], verb="updated")


# --- an assignment, from the course site's own files ------------------------
#
# Everything an assignment's folder needs is already written down: its dates on
# its entry in assignments.yml, its page under the site URL in _quarto.yml, and
# the course's standing choices -- a folder or only a grade item, what they are
# called, when a folder opens, whether students see it -- in
# config/brightspace.yml. `setup` reads them there, so an assignment has nothing
# of its own to type. It replaced one aN-setup.sh per assignment on 2026-10-02,
# which typed the dates and the link a second time. Artem: "we also have a lot
# of duplication across the aX-setup.sh scripts".

def yaml_value(raw):
    """One scalar from the flat YAML these files use: quoted, bare or a boolean."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else raw[1:]
    raw = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
    return {"true": True, "false": False}.get(raw, raw)


def yaml_entries(text):
    """assignments.yml as a list of each entry's own scalar keys.

    A key holding a nested block has nothing after its colon and is skipped:
    `setup` reads only scalars, and this is not a YAML parser.
    """
    out = []
    for line in text.splitlines():
        m = re.match(r"^- ([\w-]+):\s*(.*)$", line)
        if m:
            out.append({m.group(1): yaml_value(m.group(2))})
            continue
        m = re.match(r"^  ([\w-]+):\s*(\S.*)$", line)
        if m and out:
            out[-1][m.group(1)] = yaml_value(m.group(2))
    return out


def front_matter(page):
    """A page's own flat front-matter keys."""
    m = re.match(r"^---\n(.*?)\n---\n", page.read_text(), re.S)
    keys = {}
    for line in (m.group(1).splitlines() if m else []):
        k = re.match(r"^([\w-]+):\s*(\S.*)$", line)
        if k:
            keys[k.group(1)] = yaml_value(k.group(2))
    return keys


def course_policy(site):
    """The `assignments:` block of the course's config/brightspace.yml."""
    path = site / "config" / "brightspace.yml"
    if not path.exists():
        raise Failed(f"no {path}: it says how this course's assignments look on Brightspace")
    block, policy = None, {}
    for line in path.read_text().splitlines():
        if re.match(r"^[^\s#]", line):
            block = line.split(":", 1)[0].strip()
            continue
        m = re.match(r"^\s+([\w-]+):\s*(\S.*)$", line)
        if block == "assignments" and m:
            policy[m.group(1)] = yaml_value(m.group(2))
    need = ["folder", "name"] + (["opens", "closes", "visible"] if policy.get("folder") is True else [])
    missing = [k for k in need if k not in policy]
    if missing:
        raise Failed(f"{path}: `assignments:` has no {', '.join(missing)}")
    if "{n}" not in str(policy["name"]):
        raise Failed(f"{path}: name {policy['name']!r} has no {{n}} for the assignment's number")
    return policy


def site_url(site):
    url = quarto_urls(site).get("site")
    if not url:
        raise Failed(f"{site / '_quarto.yml'}: no urls.site to link the assignment's page from")
    return url.rstrip("/")


def assignment_plan(site, aid, like=None, today=None):
    """What `setup` makes for one assignment, every value read and none typed."""
    m = re.fullmatch(r"a(\d+)", aid.strip().lower())
    if not m:
        raise Failed(f"{aid!r} is not an assignment id like a6")
    n = int(m.group(1))
    listing = site / "assignments.yml"
    if not listing.exists():
        raise Failed(f"no {listing}: {site} is not a course's website repo")
    hits = [e for e in yaml_entries(listing.read_text())
            if re.match(rf"a{n}(-|\.qmd$)", pathlib.PurePosixPath(str(e.get("path", ""))).name)
            or re.match(rf"Assignment {n}\b", str(e.get("title", "")))]
    if len(hits) != 1:
        raise Failed(f"{'no' if not hits else 'several'} entries for {aid} in {listing}")
    entry, path = dict(hits[0]), str(hits[0]["path"])
    if re.match(r"https?://", path):
        link = path
    else:
        link = f"{site_url(site)}/{re.sub(r'[.]qmd$', '.html', path)}"
        # The older layout keeps dates on the page; the entry wins, as on the site.
        for k, v in front_matter(site / path).items():
            entry.setdefault(k, v)
    policy = course_policy(site)
    if not like and n == 1:
        raise Failed(f"{aid} has no assignment before it to copy the settings from; pass --like")
    plan = {"id": aid, "name": policy["name"].format(n=n), "folder": policy["folder"] is True,
            "like": like or policy["name"].format(n=n - 1), "link": link}
    if plan["folder"]:
        for key in ("published", "due"):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(entry.get(key, ""))):
                raise Failed(f"{aid}: no quoted {key}: date on its entry in {listing} or on its page")
        today = today or dt.date.today().isoformat()
        plan |= {"published": entry["published"],
                 "opens": f"{entry['published']} {policy['opens']}",
                 "due": f"{entry['due']} {policy['closes']}",
                 # A folder made early is visible even so: its start date, the
                 # class the assignment goes out in, keeps students from handing
                 # in before then, and a hidden one waits for a step nobody runs
                 # (240's A9, 2026-11-05 to 11-12). Until 2026-11-17 this hid
                 # a folder whose published: day had not come.
                 "visible": policy["visible"] is True,
                 "shown_later": False}
    return plan


def setup_remainder(plan, course_label):
    """What is left once setup has run, printed last so it is in front of you."""
    if not plan["folder"]:
        return "Nothing is left to do by hand for the column."
    if plan["visible"]:
        return (f"VISIBLE: students see {plan['name']} in Assignments now, and can hand in from "
                f"{plan['opens']} until {plan['due']}.\n"
                "Nothing is left to do by hand for the folder and its column.")
    when = f"on {plan['published']}, " if plan["shown_later"] else ""
    return (f"HIDDEN: students cannot see {plan['name']} or hand in to it, and nothing will remind you.\n"
            f"Then, {when}when {plan['name']} goes out:\n"
            f"    brightspace.py set-folder {course_label} '{plan['name']}' --show\n"
            "and check it landed with --check, which says HIDDEN in capitals while it is.\n"
            "240's A9 went out this way on 2026-11-05 and was never shown: 0 of 8 handed in by its\n"
            "deadline. config/brightspace.yml's visible: true makes the folder visible from the start.\n"
            "The column shows in Grades from the start, empty, since new-item cannot hide one.")


def check_setup(s, ou, plan, course_label="COURSE"):
    """Brightspace beside the course site's files. True when nothing differs."""
    rows = []
    def row(what, want, got, same=None):
        rows.append([what, want, got, "" if (want == got if same is None else same) else "DIFFERS"])
    items = [g for g in grade_items(s, ou) if g["Name"].strip().lower() == plan["name"].lower()]
    item = items[0] if len(items) == 1 else None
    row("grade item", plan["name"], item["Name"] if item else "(none)")
    if plan["folder"]:
        hits = [f for f in folders(s, ou) if f["Name"].strip().lower() == plan["name"].lower()]
        f = hits[0] if len(hits) == 1 else None
        row("folder", plan["name"], f["Name"] if f else "(none)")
        if f:
            start = (f.get("Availability") or {}).get("StartDate")
            row("opens", plan["opens"][:16], local(start), local_to_utc(plan["opens"]) == start)
            row("due", plan["due"][:16], local(f.get("DueDate")), local_to_utc(plan["due"]) == f.get("DueDate"))
            hrefs = [a.get("Href") for a in f.get("LinkAttachments") or []]
            got = text_of(f.get("CustomInstructions")).strip()
            row("link", plan["link"], got or ", ".join(h for h in hrefs if h) or "(none)",
                plan["link"] == got or plan["link"] in hrefs)
            row("its grade item", item["Id"] if item else "-", f.get("GradeItemId") or "(none)")
            row("visible to students", "yes" if plan["visible"] else "no", yes_no(not f.get("IsHidden")))
    table(["", "the site says", "Brightspace has", ""], rows)
    if plan["folder"] and f and f.get("IsHidden"):
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        opened = local_to_utc(plan["opens"]) <= now
        print(f"\nHIDDEN: students cannot see {plan['name']} or hand in to it"
              + (", and it opened " + plan["opens"][:16] if opened else "")
              + f".\n    brightspace.py set-folder {course_label} '{plan['name']}' --show")
    return not any(r[3] for r in rows)


def cmd_setup(args):
    """An assignment's Brightspace objects, read off the course site's own files.

    The folder students hand in to and its grade item, or only the grade item
    in a course that hands in elsewhere, as config/brightspace.yml says. The
    dates are the entry's in assignments.yml, the link is its page, and the
    settings are copied from the assignment before it unless --like names
    another. Without --go it prints what it would send; --check compares what
    is on Brightspace with the files, so a deadline moved in assignments.yml
    shows up rather than going unnoticed. It never rewrites a folder: an update
    replaces the whole object, link attachments added in the UI included.
    """
    if args.go and args.check:
        raise Failed("--go or --check, not both")
    site = pathlib.Path(args.site).expanduser() if args.site else course_site(args.course).site
    if not site:
        raise Failed(f"{args.course} has no site in {COURSES_FILE} to read; pass --site")
    plan = assignment_plan(site, args.assignment, args.like)
    print(f"{plan['name']}: {plan['link']}")
    if plan["folder"]:
        print(f"  opens {plan['opens']}, due {plan['due']}, "
              f"{'shown' if plan['visible'] else 'hidden'}; settings from {plan['like']!r}\n")
    else:
        print(f"  a grade item only; settings from {plan['like']!r}\n")
    if args.check:
        s, ou, _ = course(args)
        if not check_setup(s, ou, plan, args.course):
            sys.exit(1)
        return
    common = dict(vars(args), name=plan["name"], like=plan["like"], dry_run=not args.go)
    if plan["folder"]:
        cmd_new_folder(argparse.Namespace(**common, open=plan["opens"], due=plan["due"],
                                          link=plan["link"], active=plan["visible"],
                                          points=None, grade_item=None))
        print()
    # Attached from the item's side so the folder is never rewritten. A plan
    # cannot name the folder, which does not exist until --go.
    s, ou, _ = course(args)
    had = [g for g in grade_items(s, ou) if g["Name"].strip().lower() == plan["name"].lower()]
    if had and plan["folder"]:
        # The column is already in the gradebook: a folder made again after
        # delete-folder. The new folder is attached to it, from the item's side.
        print(f"grade item {plan['name']!r} exists already, id {had[0]['Id']}; "
              + ("attaching the folder to it" if args.go else "--go attaches the new folder to it"))
        if args.go:
            cmd_set_item(argparse.Namespace(**{**common, "item": str(had[0]["Id"]), "name": None,
                                                "category": None, "weight": None, "points": None,
                                                "folder": plan["name"]}))
    else:
        cmd_new_item(argparse.Namespace(**common, points=None, category=None, weight=None,
                                        folder=plan["name"] if plan["folder"] and args.go else None))
    print("\n" + setup_remainder(plan, args.course))


# --- a quiz, from its own YAML file, its flags and its course's defaults -------
#
# A quiz written for extras/bs-yaml-quiz can say where it goes: a `brightspace:`
# block beside its questions, with the course, the quiz's name, the quiz to copy
# the settings from, the dates, the timer, the attempts and the description.
# `setup-quiz` makes the quiz and its grade item from there, so each of those is
# written once, in the file that holds the questions, and the web page takes the
# same file. Artem, 2026-10-04: "it's good to have all in one place". It replaces
# a setup.sh per quiz, which typed them all as flags.
#
# Each key is a flag too, so a quiz whose questions are written in Brightspace
# needs no file at all, and whatever a quiz leaves out comes from its course's
# defaults: the quiz defaults file, a block per course in the same keys, with
# start and end as times of day. Artem, 2026-10-04: "a way to store defaults
# for the quiz settings that we know how to set through API ... one default per
# course". They cover what the API sets; the rest stays a step by hand, and
# setup-quiz ends by naming it.
#
# A course taught as several Brightspace sections has a quiz made once, in an
# empty shell, and copied into each: its defaults name its sections, and
# setup-quiz --shell is the first step, copy-quiz the second. In between, the
# questions go into the shell by hand.
#
# The file is bs-yaml-quiz's, and bs-yaml-quiz reads it here too, so the two can
# never read one file two ways: its questions are made into the CSV before
# anything is sent, and its Markdown is the description's. That makes PyYAML a
# need of this command and of quiz-defaults; everything else here is the
# standard library.

# A quiz's keys: its brightspace: block, and setup-quiz's flags of the same names.
QUIZ_KEYS = ("course", "name", "date", "like", "start", "end", "minutes", "attempts", "ip", "password",
             "points", "grade_item", "description", "header", "footer", "shell", "calendar", "special_access_only",
             "lockdown", "lockdown_domains", "lockdown_password", "lockdown_lock_in", "lockdown_for_results",
             "lockdown_calculator", "monitor", "second_camera", "proctored_lab",
             "monitor_steps", "face_detection", "live_proctoring", "live_proctoring_code",
             "shuffle_questions", "paging", "auto_publish", "primary_view", "additional_views", "visible")
# The quiz's paging, as the YAML names it and as PagingTypeId holds it: read off
# the editor's menu for each code in the 120 Devshell, 2026-11-25; 5 and up are
# refused, "PagingTypeId must be between 0 and 4".
PAGING = {"all": 0, "1": 1, "sections": 2, "5": 3, "10": 4}
PAGING_LABELS = {0: "all questions together", 1: "1 question per page", 2: "a page break after each section",
                 3: "5 questions per page", 4: "10 questions per page"}
# The three texts a quiz carries in Markdown, each kept as written, indentation
# and all, since a fenced program inside one keeps its spaces.
TEXT_KEYS = ("description", "header", "footer")
# Respondus Monitor's startup steps that can be left out, face detection's two
# options, and live proctoring's video systems: bs-drive's names for them.
MONITOR_STEPS = ("instructions", "guidelines", "photo", "id", "environment")
FACE_OPTIONS = ("block", "notify")
LIVE_SYSTEMS = ("zoom", "teams", "google meet", "collaborate", "webex", "other")
# Respondus appends this to the name of a quiz it guards, so a quiz set up with
# lockdown: true is found by either name afterwards.
LOCKDOWN_SUFFIX = "- Requires Respondus LockDown Browser"
HOST = re.compile(r"(?i)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")
# A course's defaults: a quiz's keys but the first three, start and end as times
# of day, and two for a course taught as several sections: which they are, and
# the grade item each section's copy is shaped like where it has none of the
# quiz's name.
DEFAULT_KEYS = QUIZ_KEYS[3:] + ("sections", "item_like")
# grade_item written at all says there is none, in whichever word for nothing.
NO_ITEM = ("none", "false", "no", "null", "~", "")
DEFAULTS_HEADER = """\
# Quiz defaults, a block per course: what a quiz leaves out of its brightspace:
# block, of setup-quiz's flags or of the web page's form, its course's block
# here gives. The keys are in brightspace-cli's README, under "Quiz defaults".
# `brightspace.py quiz-defaults` shows them, and with --set and --unset changes
# one course's, touching no line but those of the keys it is given.
"""


@functools.cache
def quiz_format():
    """extras/bs-yaml-quiz, as a module: the quiz file's reader, CSV and Markdown."""
    path = pathlib.Path(__file__).resolve().parent / "extras" / "bs-yaml-quiz" / "bs-yaml-quiz.py"
    spec = importlib.util.spec_from_file_location("bs_yaml_quiz", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except FileNotFoundError:
        raise Failed(f"no {path}: a quiz file is read by the bs-yaml-quiz beside this tool")
    except ImportError:
        raise Failed("a quiz file and the quiz defaults are read with PyYAML, which this Python does not "
                     "have: `pip install pyyaml`, or on Nix python3.withPackages (ps: [ ps.pyyaml ])")
    return mod


def time_of_day(text):
    """'14:00' as written, or None if it is not a time of day."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m and int(m.group(1)) < 24 and int(m.group(2)) < 60 else None


TRUTH, FALSITY = {"true", "yes", "on"}, {"false", "no", "off"}


def as_flag(value):
    """A block's true or false, or a flag's, as a bool; None when not given."""
    if value is None or isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUTH


def check_value(key, value, at):
    """Refuse a value a quiz's key, or a default's, cannot take."""
    if key == "minutes" and no_limit(value):
        return
    if key == "attempts" and value.lower() == "unlimited":
        return
    if key in ("minutes", "attempts") and not re.fullmatch(r"[1-9]\d*", value):
        raise Failed(f"{at} {key}: is a whole number, not {value!r}"
                     + (" -- or none, for a quiz with no time limit" if key == "minutes" else
                        " -- or unlimited"))
    if key == "paging":
        paging_code(value)
    if key in ("calendar", "special_access_only", "lockdown", "monitor", "second_camera", "proctored_lab",
               "shuffle_questions", "auto_publish", "lockdown_lock_in", "lockdown_for_results", "visible") \
            and value.strip().lower() not in TRUTH | FALSITY:
        raise Failed(f"{at} {key}: is true or false, not {value!r}")
    for name, known in (("monitor_steps", MONITOR_STEPS), ("face_detection", FACE_OPTIONS)):
        if key == name:
            names = [x.strip().lower() for x in value.split(",") if x.strip()]
            bad = [x for x in names if x not in known] if names != ["none"] else []
            if not names or bad:
                raise Failed(f"{at} {name}: are some of {', '.join(known)}, as a list or one line with commas, "
                             f"or none, not {', '.join(map(repr, bad)) if bad else repr(value)}")
    if key == "live_proctoring" and value.strip().lower() not in LIVE_SYSTEMS:
        raise Failed(f"{at} live_proctoring: is the video system an instructor proctors over, one of "
                     f"{', '.join(LIVE_SYSTEMS)}, not {value!r}; monitor: false turns proctoring off")
    if key == "live_proctoring_code" and not value.strip():
        raise Failed(f"{at} live_proctoring_code: is the start code students are given, and is empty")
    if key == "lockdown_calculator" and value.strip().lower() not in ("standard", "scientific", "none"):
        raise Failed(f"{at} lockdown_calculator: is standard, scientific or none, the calculator on LockDown "
                     f"Browser's toolbar, not {value!r}")
    if key == "lockdown_password" and not value.strip():
        raise Failed(f"{at} lockdown_password: is LockDown Browser's password to access the exam, and is empty")
    if key == "lockdown_domains":
        hosts = domain_list(value)
        bad = [h for h in hosts if not HOST.fullmatch(h)]
        if not hosts or bad:
            raise Failed(f"{at} lockdown_domains: are host names like cu-cs-courses.site, as a list or one line "
                         f"with commas, not {', '.join(map(repr, bad)) if bad else repr(value)}")
    if key == "points" and not re.fullmatch(r"\d+(\.\d+)?", value):
        raise Failed(f"{at} points: is a number, not {value!r}")
    if key == "ip" and not re.fullmatch(r"[\d.]+\s*-\s*[\d.]+", value):
        raise Failed(f"{at} ip: is a range like 148.137.150.0-148.137.150.255, not {value!r}")
    if key == "grade_item" and value.strip().lower() not in NO_ITEM:
        raise Failed(f"{at} grade_item: is none, or left out for an item of the quiz's own name")
    if key == "date":
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            raise Failed(f"{at} date: is a day like 2026-10-12, not {value!r}")


def domain_list(value):
    """The hosts in a lockdown_domains value: a line with commas, or what a list
    became when it was joined into one."""
    return [x for x in re.split(r"[,\s]+", value or "") if x]


# Respondus's suffix in either of its forms: "- Requires Respondus LockDown Browser",
# and with Monitor " - Requires Respondus LockDown Browser + Webcam" (2026-11-24).
RESPONDUS_SUFFIX = re.compile(r"\s*-\s*Requires Respondus LockDown Browser(\s*\+\s*Webcam)?\s*$")


def quiz_named(have, want):
    """A quiz's name is the one asked for, or that with Respondus's suffix in either form."""
    return same_name(have, want) or same_name(RESPONDUS_SUFFIX.sub("", have or ""), RESPONDUS_SUFFIX.sub("", want or ""))


def section_labels(value):
    return [x for x in re.split(r"[\s,]+", value or "") if x]


def quiz_defaults(path=None):
    """course -> {key: value}, from the quiz defaults file; empty when there is none."""
    path = path or QUIZ_DEFAULTS_FILE
    if not path.exists():
        return {}
    fmt = quiz_format()
    try:
        data = fmt.load(path.read_text(encoding="utf-8"))
    except fmt.yaml.YAMLError as e:
        raise Failed(f"{path}: not YAML: {e}")
    if data in (None, ""):
        return {}
    if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
        raise Failed(f"{path}: each course is its label and a block of `key: value` lines under it")
    out = {}
    for label, keys in data.items():
        at = f"{path}, {label}:"
        out[label] = {}
        for key, value in keys.items():
            if key not in DEFAULT_KEYS:
                raise Failed(f"{at} {key} is not a default; a course's are {', '.join(DEFAULT_KEYS)}")
            if key in VIEW_KEYS:
                check_default(key, value, at)
                out[label][key] = value
                continue
            if not isinstance(value, str):
                raise Failed(f"{at} {key}: is text, not {value!r}")
            if key != "grade_item" and value.strip() in fmt.NONE:
                continue
            check_default(key, value, at)
            out[label][key] = value if key in TEXT_KEYS else value.strip()
    return out


def check_default(key, value, at):
    if key == "primary_view":
        view_spec(value, f"{at} primary_view:", False, "2000-01-01")
    elif key == "additional_views":
        if not isinstance(value, list):
            raise Failed(f"{at} additional_views: is a list of views, each starting with - from:")
        for n, v in enumerate(value, 1):
            view_spec(v, f"{at} additional_views: view {n}:", True, "2000-01-01")
    elif key in ("start", "end"):
        if not time_of_day(value):
            raise Failed(f"{at} {key}: is a time of day like 14:00, not {value!r}")
    elif key in TEXT_KEYS:
        quiz_format().md_html(value, f"{at} {key}")
    else:
        check_value(key, value.strip(), at)


def previous_name(name):
    """'Quiz 2' for 'Quiz 3': the name with its number one less, or None."""
    m = re.fullmatch(r"(.*?)(\d+)", name.strip())
    return f"{m.group(1)}{int(m.group(2)) - 1}" if m and int(m.group(2)) > 1 else None


def quiz_plan(path=None, flags=None, defaults_path=None):
    """What setup-quiz makes: the quiz file's brightspace: block with the flags
    over it and its course's defaults under it, checked, with the questions
    counted and the CSV beside the file compared with them when there is one."""
    flags = {k: v for k, v in (flags or {}).items() if v is not None}
    item_flag = flags.pop("grade_item", None)
    fmt = quiz_format()
    data, csv_text, block, views_given = None, None, {}, {}
    if path:
        try:
            data = fmt.load(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise Failed(f"{path}: {e.strerror or e}")
        except fmt.yaml.YAMLError as e:
            raise Failed(f"{path}: not YAML: {e}")
        try:
            # An MSA question, several blanks, goes into the CSV as a short answer
            # with its first blank, and bs-yaml-quiz.py writes it so only when told
            # --msa-as-sa. Here the CSV is only compared and counted, never written,
            # so the question is taken that way, and the blanks after the first are
            # among the steps by hand that quiz_remainder prints.
            csv_text = fmt.to_csv(data, str(path), msa_as_sa=True)
            msa = fmt.msa_by_hand(data, str(path))
            shuffle = fmt.shuffled(data, str(path))
        except fmt.Failed as e:
            raise Failed(str(e))
        block = data.get("brightspace") or {}
        # a YAML list is the natural way to write several hosts, or steps; the rest of
        # the plan carries values as text, so each is one line with commas from here
        for key in ("lockdown_domains", "monitor_steps", "face_detection"):
            if isinstance(block, dict) and isinstance(block.get(key), list):
                block = dict(block, **{key: ", ".join(str(x).strip() for x in block[key])})
        if not block and "course" not in flags:
            raise Failed(f"{path} has no brightspace: block to say which course the quiz goes in and when "
                         "it runs; extras/bs-yaml-quiz/README.md has its keys")
        if isinstance(block, dict):
            views_given = {k: block[k] for k in VIEW_KEYS if k in block}
            block = {k: v for k, v in block.items() if k not in VIEW_KEYS}
        unknown = [k for k in block if k not in QUIZ_KEYS]
        if unknown:
            raise Failed(f"{path}: brightspace: has {', '.join(unknown)}, which setup-quiz does not know; "
                         f"it reads {', '.join(QUIZ_KEYS)}")
    at = f"{path}: brightspace:" if path else "setup-quiz"
    given = {k: v.strip() for k, v in block.items() if k != "grade_item" and v.strip() not in fmt.NONE}
    for key in TEXT_KEYS:
        if key in given:
            given[key] = block[key]
    given |= {k: v.strip() if k not in TEXT_KEYS else v for k, v in flags.items() if str(v).strip()}
    for key in ("course", "name"):
        if key not in given:
            raise Failed(f"{at} has no {key}")
    course_label = given["course"]
    defaults = quiz_defaults(defaults_path).get(course_label, {})
    plan, used = dict(given), []
    for key in DEFAULT_KEYS[:-2]:
        if key not in plan and key != "grade_item" and key in defaults and key not in VIEW_KEYS:
            plan[key], used = defaults[key], used + [key]
    for key in VIEW_KEYS:
        if key not in views_given and key in defaults:
            views_given[key], used = defaults[key], used + [key]
    if item_flag is not None:
        plan["item"] = item_flag
    elif "grade_item" in block:
        check_value("grade_item", block["grade_item"], at)
        plan["item"] = False
    elif "grade_item" in defaults:
        plan["item"], used = False, used + ["grade_item"]
    else:
        plan["item"] = True
    plan["used"] = used
    for key in ("date", "minutes", "attempts", "points", "ip", "calendar", "special_access_only",
                "lockdown", "lockdown_domains", "lockdown_password", "lockdown_lock_in", "lockdown_for_results",
                "lockdown_calculator", "monitor", "second_camera", "proctored_lab",
                "monitor_steps", "face_detection", "live_proctoring", "live_proctoring_code",
                "shuffle_questions", "paging", "auto_publish", "visible"):
        if key in plan:
            check_value(key, plan[key], at)
    if "lockdown_domains" in plan:
        if "lockdown" in plan and not as_flag(plan["lockdown"]):
            raise Failed(f"{at} lockdown_domains: are the sites LockDown Browser lets a student open, "
                         "and lockdown: false turns LockDown Browser off")
        plan["lockdown_domains"] = ", ".join(domain_list(plan["lockdown_domains"]))
        plan.setdefault("lockdown", "true")
    # Respondus Monitor, the webcam, runs inside LockDown Browser, and a second camera
    # is Monitor's: each asks for the one before it, and refuses its being turned off.
    for key in ("second_camera", "proctored_lab"):
        if key in plan and as_flag(plan[key]):
            if "monitor" in plan and not as_flag(plan["monitor"]):
                raise Failed(f"{at} {key}: is Respondus Monitor's, and monitor: false turns Monitor off")
            plan.setdefault("monitor", "true")
    for key in ("monitor_steps", "face_detection"):
        if key in plan:
            if "monitor" in plan and not as_flag(plan["monitor"]):
                raise Failed(f"{at} {key}: are Respondus Monitor's, and monitor: false turns Monitor off")
            plan[key] = ", ".join(x.strip().lower() for x in plan[key].split(",") if x.strip())
            plan.setdefault("monitor", "true")
    # Live proctoring, an instructor watching over Zoom or the like, is instead of
    # Monitor, inside LockDown Browser as Monitor is.
    if "live_proctoring" in plan:
        if "monitor" in plan and as_flag(plan["monitor"]):
            raise Failed(f"{at} live_proctoring: is instead of Respondus Monitor, and the plan has Monitor "
                         "(monitor:, second_camera:, proctored_lab:, monitor_steps: or face_detection:)")
        plan["live_proctoring"] = plan["live_proctoring"].strip().lower()
        if "lockdown" in plan and not as_flag(plan["lockdown"]):
            raise Failed(f"{at} live_proctoring: runs inside LockDown Browser, and lockdown: false turns it off")
        plan.setdefault("lockdown", "true")
    # The proctored lab, a sitting with a person in the room instead of the webcam,
    # needs LockDown Browser's own exam password, which the proctor types in:
    # Respondus will not save the box without one. The password is LockDown's.
    if as_flag(plan.get("proctored_lab")) and "lockdown_password" not in plan:
        raise Failed(f"{at} proctored_lab: needs lockdown_password:, the password the lab's proctor types in; "
                     "Respondus will not save a proctored lab without one")
    for key in ("lockdown_password", "lockdown_lock_in", "lockdown_for_results", "lockdown_calculator"):
        if key in plan:
            if "lockdown" in plan and not as_flag(plan["lockdown"]):
                raise Failed(f"{at} {key}: is LockDown Browser's, and lockdown: false turns it off")
            plan.setdefault("lockdown", "true")
    if "lockdown_calculator" in plan:
        plan["lockdown_calculator"] = plan["lockdown_calculator"].strip().lower()
    if "live_proctoring_code" in plan and "live_proctoring" not in plan:
        raise Failed(f"{at} live_proctoring_code: is live proctoring's start code, and the plan has no live_proctoring:")
    if "monitor" in plan and as_flag(plan["monitor"]):
        if "lockdown" in plan and not as_flag(plan["lockdown"]):
            raise Failed(f"{at} monitor: Respondus Monitor runs inside LockDown Browser, "
                         "and lockdown: false turns LockDown Browser off")
        plan.setdefault("lockdown", "true")
    day = plan.get("date")
    for key in ("start", "end"):
        if key not in plan:
            continue
        clock = time_of_day(plan[key])
        if clock:
            if not day:
                raise Failed(f"{at} {key}: {plan[key]} is a time of day, and there is no date to put it on")
            plan[key] = f"{day} {clock}"
        try:
            local_to_utc(plan[key])
        except Failed as e:
            raise Failed(f"{at} {key}: {e}")
    if day and "start" not in plan and "end" not in plan:
        raise Failed(f"{at} date: {day} needs a time to open and one to close: start and end, "
                     f"given or in {course_label}'s defaults")
    # What a student sees once an attempt is published: written over the quizzes
    # API after the quiz is made. A view for one attempt needs a quiz with more.
    if "primary_view" in views_given:
        plan["primary_view"] = view_spec(views_given["primary_view"], f"{at} primary_view:", False, day)
    if "additional_views" in views_given:
        raw = views_given["additional_views"]
        if not isinstance(raw, list):
            raise Failed(f"{at} additional_views: is a list of views, each starting with - from:")
        plan["additional_views"] = [view_spec(v, f"{at} additional_views: view {n}:", True, day)
                                    for n, v in enumerate(raw, 1)]
        attempts = str(plan.get("attempts", "1")).lower()
        for n, v in enumerate(plan["additional_views"], 1):
            if v["attempt"] and attempts != "unlimited" and v["attempt"] > int(attempts):
                raise Failed(f"{at} additional_views: view {n}: attempt: {v['attempt']}, and the quiz allows "
                             f"{attempts} attempt{'s' if attempts != '1' else ''}")
    if "points" in given and not plan["item"]:
        raise Failed(f"{at} points: is what its grade item is out of, and grade_item: none makes none")
    if "points" in given and "shell" in plan:
        raise Failed(f"{at} points: is what its grade item is out of, and a shell's quiz has none: each "
                     "section's copy goes to an item shaped like item_like's, in the second step")
    sections = section_labels(defaults.get("sections"))
    plan["sections"] = sections or [course_label]
    plan["item_like"] = defaults.get("item_like", "previous")
    if sections and "shell" not in plan:
        raise Failed(f"{course_label} is taught as sections ({', '.join(sections)}), so its quiz is made in a "
                     f"shell and copied into each: give a shell, or name one in {course_label}'s defaults")
    # The quiz to copy the settings from is looked for where the quiz is going:
    # the course, or, for a quiz made in a shell, the first of the sections.
    plan["template_course"] = plan["sections"][0] if "shell" in plan else course_label
    if "shell" in plan:
        plan["item"] = False      # a shell's has nowhere to send scores; each copy gets its section's
    for key in TEXT_KEYS:
        if key in plan:
            try:
                plan[f"{key}_html"] = fmt.md_html(plan[key], f"{at} {key}")
            except fmt.Failed as e:
                raise Failed(str(e))
    plan |= {"path": path, "questions": None, "csv_state": None, "msa": [], "shuffle": [], "csv_text": csv_text}
    if path:
        csv_path = path.with_suffix(".csv")
        have = csv_path.read_bytes().decode("utf-8") if csv_path.exists() else None
        plan |= {"csv": csv_path, "questions": len(data["questions"]), "msa": msa, "shuffle": shuffle,
                 "worth": sum(float(q.get("points", fmt.POINTS)) for q in data["questions"]),
                 "csv_state": None if have is None else "current" if have == csv_text else "stale"}
    return plan


def quiz_summary(plan, template=None):
    """What a plan makes, in a few lines a person reads before the payloads."""
    def nums(key, word):
        n = plan.get(key)
        return f"{n} {word}{'' if n == '1' else 's'}" if n else None
    shell = plan.get("shell")
    where = (f"made in {shell}, then copied into {', '.join(plan['sections'])}" if shell
             else f"in {plan['course']}")
    has = (f"{plan['questions']} questions from {plan['path'].name}, worth {plan['worth']:g}"
           if plan["path"] else "its questions written in Brightspace")
    when = (f"opens {plan.get('start', 'with no start date')}, closes {plan.get('end', 'with no end date')}; "
            + ", ".join(x for x in (("no time limit" if no_limit(plan.get("minutes")) else nums("minutes", "minute"))
                                    or "the time limit of the quiz it copies",
                                    "unlimited attempts" if str(plan.get("attempts")).lower() == "unlimited"
                                    else nums("attempts", "attempt") or "1 attempt") if x))
    copied = f"settings copied from {template!r}" if template else (
        f"settings copied from {plan['like']!r}" if plan.get("like") not in (None, "previous")
        else "settings copied from the quiz numbered before it")
    if shell:
        copied += f" in {plan['template_course']}"
        item = "no grade item in the shell: each copy goes to its section's"
    elif plan["item"]:
        item = (f"scores to a grade item {plan['name']!r}"
                + (f", out of {plan['points']}" if "points" in plan else ""))
    else:
        item = "no grade item"
    lines = [f"{plan['name']} {where}: {has}", f"  {when}",
             f"  {copied}; {item}" + "".join(f"; a {k}" for k in TEXT_KEYS if k in plan)]
    extras = []
    if "lockdown" in plan:
        n = len(domain_list(plan.get("lockdown_domains")))
        extras.append(("LockDown Browser required, " + (f"{n} site{'s' if n != 1 else ''} allowed" if n
                                                        else "no other site allowed")
                       + (f", exam password {plan['lockdown_password']}" if "lockdown_password" in plan else "")
                       + ((", students locked in until done" if as_flag(plan["lockdown_lock_in"]) else
                           ", not locked in") if "lockdown_lock_in" in plan else "")
                       + ((", results seen only in it" if as_flag(plan["lockdown_for_results"]) else
                           ", results seen in any browser") if "lockdown_for_results" in plan else "")
                       + ((f", a {plan['lockdown_calculator']} calculator" if plan["lockdown_calculator"] != "none"
                           else ", no calculator") if "lockdown_calculator" in plan else ""))
                      if as_flag(plan["lockdown"]) else "LockDown Browser not required")
    if "live_proctoring" in plan:
        extras.append(f"live proctoring over {plan['live_proctoring'].title()}"
                      + (f", start code {plan['live_proctoring_code']}" if "live_proctoring_code" in plan else ""))
    elif "monitor" in plan:
        extras.append(("Respondus Monitor required" + (", with a second camera" if as_flag(plan.get("second_camera"))
                                                       else "")
                       + (", or in a proctored lab" if as_flag(plan.get("proctored_lab")) else "")
                       + (f", startup steps: {plan['monitor_steps']}" if "monitor_steps" in plan else "")
                       + (f", face detection: {plan['face_detection']}" if "face_detection" in plan else ""))
                      if as_flag(plan["monitor"]) else "Respondus Monitor not required")
    if "auto_publish" in plan:
        extras.append("results published on submission" if as_flag(plan["auto_publish"]) else
                      "results published by hand")
    if "shuffle_questions" in plan:
        extras.append("questions shuffled" if as_flag(plan["shuffle_questions"]) else "questions in order")
    if "paging" in plan:
        extras.append(PAGING_LABELS[paging_code(plan["paging"])])
    if "visible" in plan:
        extras.append("made visible once its questions are in" if as_flag(plan["visible"]) else "left hidden")
    if extras:
        lines.append("  " + "; ".join(extras))
    if "primary_view" in plan:
        lines.append("  primary view: " + view_line(plan["primary_view"]))
    for n, spec in enumerate(plan.get("additional_views") or [], 1):
        lines.append(f"  additional view {n}: " + view_line(spec, True))
    if plan["used"]:
        lines.append(f"  from {plan['course']}'s defaults: {', '.join(plan['used'])}")
    return "\n".join(lines) + "\n"


def quiz_remainder(plan, template, shown, done=()):
    """What is left once setup-quiz has run, printed last so it is in front of you.
    done names the steps bs-drive has already taken: questions, lockdown, visible."""
    shell = plan.get("shell")
    if "questions" in done:
        steps = []
    elif plan["path"]:
        yml, csv_name = shown + (" --msa-as-sa" if plan["msa"] else ""), plan["csv"].name
        made = {"current": "", None: f", which bs-yaml-quiz.py {yml} makes",
                "stale": f" once bs-yaml-quiz.py {yml} has made it again: it is not what the file makes now"}
        steps = [f"Question Library -> Import -> Upload a File -> {csv_name}{made[plan['csv_state']]}."]
        if plan["msa"]:
            # The CSV carries one blank of each MSA question; the rest, before the
            # questions go into the quiz, so it gets them whole.
            steps.append("Question Library -> each MSA question -> the blanks after its first, which the CSV "
                         "cannot carry:\n" + "\n".join("    " + x for x in plan["msa"]))
        on = [t or f"question {n}" for n, t, s in plan["shuffle"] if s]
        if on:
            # Before Add Existing too, as the blanks are: see drive_steps.
            steps.append("Question Library -> each of these -> Randomize answer order, which the CSV cannot "
                         "carry:\n" + "\n".join("    " + t for t in on))
        steps.append(f"{plan['name']} -> Add Existing -> the {plan['questions']} questions it imported.")
    else:
        steps = [f"Its questions: {plan['name']} -> Add/Edit Questions, written there or imported\n"
                 "    into the Question Library and added with Add Existing."]
    for key in ("header", "footer"):
        if key in plan and "header" not in done:
            # The editor's source view takes the HTML as it is; the API would turn
            # {OrgDefinedId} into your own id for every student.
            steps.append(f"Timing & Display -> Manage Header and Footer -> {key.title()} -> the source code view, "
                         f"this HTML, made from {key}: in the file:\n    {plan[key + '_html']}")
    if "lockdown" in plan and "lockdown" not in done:
        monitor = ""
        if "live_proctoring" in plan:
            monitor = (f"; Allow instructor live proctoring -> {plan['live_proctoring'].title()}"
                       + (f", start code {plan['live_proctoring_code']}" if "live_proctoring_code" in plan else ""))
        elif "monitor" in plan:
            monitor = ("; Require Respondus Monitor" + ("; Monitor's Second Camera -> Require students use a second "
                                                       "camera" if as_flag(plan.get("second_camera")) else
                                                       "; Monitor's Second Camera -> not required"
                                                       if "second_camera" in plan else "")
                       if as_flag(plan["monitor"]) else "; Don't require Respondus Monitor")
            if as_flag(plan["monitor"]) and "proctored_lab" in plan:
                monitor += ("; Monitor's Advanced Settings -> " + ("" if as_flag(plan["proctored_lab"]) else "don't ")
                            + "allow this exam to additionally be delivered in a proctored lab")
            if as_flag(plan["monitor"]) and "monitor_steps" in plan:
                monitor += f"; Startup Sequence: on, {plan['monitor_steps']}, the rest off"
            if as_flag(plan["monitor"]) and "face_detection" in plan:
                monitor += f"; Face Detection Options: on, {plan['face_detection']}, the rest off"
        password = (f"; Password to access exam: {plan['lockdown_password']}" if "lockdown_password" in plan else "")
        if "lockdown_lock_in" in plan:
            password += ("; Advanced Settings -> " + ("" if as_flag(plan["lockdown_lock_in"]) else "don't ")
                         + "lock students into the browser until exam is completed")
        if "lockdown_for_results" in plan:
            password += ("; Advanced Settings -> " + ("" if as_flag(plan["lockdown_for_results"]) else "don't ")
                         + "require LockDown Browser to view post-exam feedback and results")
        if "lockdown_calculator" in plan:
            password += ("; Advanced Settings -> " + (f"Enable Calculator on the toolbar, {plan['lockdown_calculator']}"
                                                      if plan["lockdown_calculator"] != "none" else
                                                      "no calculator on the toolbar"))
        steps.append(("LockDown Browser tab -> the quiz's row -> Settings -> Require LockDown Browser" + password + monitor
                      + (f"; Advanced Settings -> allow access to external domains, one line with commas:\n"
                         f"    {plan['lockdown_domains']}" if plan.get("lockdown_domains") else ".")
                      ) if as_flag(plan["lockdown"]) else
                     "LockDown Browser tab -> the quiz's row -> Settings -> Don't require LockDown Browser.")
    if not any(k in plan for k in VIEW_KEYS):
        steps.append("Submission views, if students are to see more after an attempt than the default:\n"
                     f"    none came from {template!r}; primary_view: and additional_views: in the file set them,\n"
                     "    or brightspace.py views on the quiz once it is made.")
    if "visible" not in done and as_flag(plan.get("visible", "true")):
        steps.append("Make it visible to students, if the copies are to be: each keeps the shell's." if shell
                     else "Make it visible to students, once it has its questions.")
    lines = [f"Then by hand, in {shell}, since no API route does these:" if shell
             else "Then by hand, since no API route does these:"]
    lines += [f" {n}. {step}" for n, step in enumerate(steps, 1)]
    if shell:
        lines += [f"Then copy it into {', '.join(plan['sections'])}, step 2, which empties the shell again:",
                  f"    brightspace.py copy-quiz {shell} --to {' '.join(plan['sections'])}"
                  f" --item-like {shell_quote(plan['item_like'])} --go --clear"]
    else:
        lines.append(f"Then setup-quiz {shown} --check reads it all back, the questions counted." if shown else
                     "Then the same setup-quiz with --check reads it all back.")
    return "\n".join(lines)


def shell_quote(text):
    return text if re.fullmatch(r"[\w.,:/=+-]+", text) else "'" + text.replace("'", "'\\''") + "'"


def check_quiz(s, ou, plan, by_hand=False):
    """Brightspace beside the quiz's file, flags and defaults. True when nothing differs."""
    rows = []
    def row(what, want, got, same=None):
        rows.append([what, want, got, "" if (want == got if same is None else same) else "DIFFERS"])
    le, _ = s.versions()
    hits = [q for q in s.objects(f"/d2l/api/le/{le}/{ou}/quizzes/") if quiz_named(q["Name"], plan["name"])]
    q = hits[0] if len(hits) == 1 else None
    row("quiz", plan["name"], q["Name"] if q else f"{len(hits)} of that name" if hits else "(none)",
        bool(q))    # under Respondus's suffix too, once LockDown is required
    if q:
        for what, key, field in (("opens", "start", "StartDate"), ("closes", "end", "EndDate")):
            want = plan.get(key)
            row(what, want or "(none)", local(q.get(field)) or "(none)",
                (local_to_utc(want) if want else None) == q.get(field))
        for key, field in (("calendar", "DisplayInCalendar"), ("special_access_only", "AllowOnlyUsersWithSpecialAccess")):
            if key in plan:
                row(key.replace("_", " "), str(as_flag(plan[key])).lower(), str(bool(q.get(field))).lower(),
                    as_flag(plan[key]) == bool(q.get(field)))
        on, minutes = limit_of(q.get("SubmissionTimeLimit"))
        have = limit_text(q.get("SubmissionTimeLimit")).replace("none", "(none)")
        if no_limit(plan.get("minutes")):
            row("time limit", "(none)", have, not minutes)
        else:
            row("time limit", f"{plan['minutes']} minutes" if "minutes" in plan else "-", have,
                "minutes" not in plan or (on and str(minutes) == plan["minutes"]))
        row("attempts", str(plan.get("attempts", "1")).lower(), attempts_of(q))
        if "shuffle_questions" in plan:
            row("questions shuffled", yes_no(as_flag(plan["shuffle_questions"])), yes_no(q.get("Shuffle")))
        if "paging" in plan:
            row("paging", PAGING_LABELS[paging_code(plan["paging"])],
                PAGING_LABELS.get(q.get("PagingTypeId"), str(q.get("PagingTypeId"))))
        ips = [f"{r.get('IPRangeStart')}-{r.get('IPRangeEnd')}" for r in q.get("RestrictIPAddressRange") or []]
        row("IP range", re.sub(r"\s+", "", plan.get("ip", "")) or "(none)", ", ".join(ips) or "(none)")
        row("password", plan.get("password", "(none)"), q.get("Password") or "(none)")
        item = next((g for g in grade_items(s, ou) if g["Id"] == q.get("GradeItemId")), None)
        row("grade item", plan["name"] if plan["item"] else "(none)", item["Name"] if item else "(none)",
            (bool(item) and same_name(item["Name"], plan["name"])) if plan["item"] else not item)
        if item:
            row("out of", plan.get("points", "-"), f"{item.get('MaxPoints'):g}",
                "points" not in plan or float(plan["points"]) == item.get("MaxPoints"))
        held = quiz_questions(s, ou, q["QuizId"])
        got = str(len(held))
        row("questions", str(plan["questions"]) if plan["questions"] is not None else "-", got,
            plan["questions"] is None or got == str(plan["questions"]))
        if plan["shuffle"]:
            # QuestionInfo's Randomize, on a multiple choice (type 1) and a multi-select (4).
            # A question not in the quiz is the questions row's difference, not this one's.
            by_name = {x.get("Name"): bool((x.get("QuestionInfo") or {}).get("Randomize")) for x in held
                       if x.get("QuestionTypeId") in (1, 4)}
            mine = [(t, on) for _, t, on in plan["shuffle"] if t in by_name]
            off = [f"{t} {'shuffled' if by_name[t] else 'in order'}" for t, on in mine if by_name[t] != on]
            row("options shuffled", f"{sum(on for _, _, on in plan['shuffle'])} of {len(plan['shuffle'])}",
                (f"{sum(by_name[t] for t, _ in mine)} of {len(mine)}" if mine else "(none in the quiz yet)")
                + (": " + "; ".join(off) if off else ""), not off)
        if "description" in plan:
            want, got = text_pair(plan["description_html"], text_of((q.get("Description") or {}).get("Text")))
            cut = lambda t: t if len(t) <= 32 else t[:31] + "…"
            row("description", cut(want), cut(got) or "(none)", want == got)
        if "header" in plan or "footer" in plan:
            why = drive_unready(s, by_hand)
            got = None if why else drive_read_header(s, ou, q["Name"])
            for key in ("header", "footer"):
                if key not in plan:
                    continue
                cut = lambda t: t if len(t) <= 32 else t[:31] + "…"
                want = re.sub(r"<[^>]+>", "", plan[f"{key}_html"]).strip()
                if got is None:
                    row(key, cut(want), f"(not read: {why or 'bs-drive failed'})", True)
                else:
                    row(key, cut(want), cut(re.sub(r"<[^>]+>", "", got.get(key) or "").strip()) or "(none)",
                        same_html(plan[f"{key}_html"], got.get(key)))
        if "auto_publish" in plan:
            row("auto-publish", yes_no(as_flag(plan["auto_publish"])), yes_no(q.get("IsAutoSetGraded")))
        if any(k in plan for k in VIEW_KEYS):
            primary, more = QuizViews(s, ou, q["QuizId"]).read_all()
            if "primary_view" in plan:
                row("primary view", view_line(plan["primary_view"]), view_line(primary),
                    same_view(plan["primary_view"], primary))
            if "additional_views" in plan:
                want = plan["additional_views"]
                for n in range(max(len(want), len(more))):
                    row(f"additional view {n + 1}", view_line(want[n], True) if n < len(want) else "(none)",
                        view_line(more[n], True) if n < len(more) else "(none)",
                        n < len(want) and n < len(more) and same_view(want[n], more[n]))
        if "visible" in plan:
            row("visible", yes_no(as_flag(plan["visible"])), yes_no(q.get("IsActive")))
        else:
            rows.append(["visible", "", yes_no(q.get("IsActive")), ""])
        if "lockdown" in plan:
            why = drive_unready(s, by_hand)
            got = None if why else drive_read_lockdown(s, ou, q["Name"])
            want = "required" if as_flag(plan["lockdown"]) else "not required"
            if got is None:
                row("LockDown Browser", want, f"(not read: {why or 'bs-drive failed'})", True)
            else:
                row("LockDown Browser", want, "required" if got.get("required") else "not required")
                if as_flag(plan["lockdown"]):
                    have = (got.get("domains") or "") if got.get("allow") else ""
                    row("LockDown sites", plan.get("lockdown_domains") or "(none)", have or "(none)")
                    if "lockdown_password" in plan:
                        row("exam password", plan["lockdown_password"], got.get("password") or "(none)")
                    if "lockdown_lock_in" in plan:
                        row("locked in until done", yes_no(as_flag(plan["lockdown_lock_in"])), yes_no(got.get("lock_in")))
                    if "lockdown_for_results" in plan:
                        row("results only in LockDown", yes_no(as_flag(plan["lockdown_for_results"])),
                            yes_no(got.get("for_results")))
                    if "lockdown_calculator" in plan:
                        row("calculator", plan["lockdown_calculator"], got.get("calculator") or "(not read)")
                if "live_proctoring" in plan or "monitor" in plan:
                    row("proctoring", "live proctoring" if "live_proctoring" in plan else
                        "Respondus Monitor" if as_flag(plan["monitor"]) else "none",
                        {"required": "Respondus Monitor", "not required": "none"}.get(got.get("monitor"),
                                                                                      got.get("monitor") or "(not read)"))
                if "live_proctoring" in plan:
                    row("video system", plan["live_proctoring"], got.get("system") or "(none)")
                if "live_proctoring_code" in plan:
                    row("start code", plan["live_proctoring_code"], got.get("code") or "(none)")
                for key, what in (("monitor_steps", "startup steps"), ("face_detection", "face detection")):
                    if key in plan:
                        want = sorted(x for x in plan[key].split(", ") if x != "none")
                        have = sorted(got.get({"monitor_steps": "steps", "face_detection": "face"}[key]) or [])
                        row(what, ", ".join(want) or "none", ", ".join(have) or "none", want == have)
                if "second_camera" in plan:
                    row("second camera", yes_no(as_flag(plan["second_camera"])), yes_no(got.get("second_camera")))
                if "proctored_lab" in plan:
                    row("proctored lab", yes_no(as_flag(plan["proctored_lab"])), yes_no(got.get("lab")))
    if plan["csv_state"]:
        row(plan["csv"].name, "current", plan["csv_state"])
    table(["", "the plan says", "Brightspace has", ""], rows)
    return not any(r[3] for r in rows)


DRIVE = pathlib.Path(__file__).resolve().parent / "extras" / "bs-drive" / "bs-drive.py"


def drive_unready(s, by_hand=False):
    """Why bs-drive cannot take the steps the API has no route for, here and
    now; None when it can. It drives the real web page in a headless Chromium,
    so a test double, a machine without a browser, or --by-hand leaves them
    printed for a person instead."""
    if by_hand or os.environ.get("BRIGHTSPACE_BY_HAND"):
        return "--by-hand" if by_hand else "BRIGHTSPACE_BY_HAND is set"
    if urllib.parse.urlsplit(s.base).scheme != "https":
        return "the host is not https, so not Brightspace's web page"
    if not DRIVE.exists():
        return f"no {DRIVE}"
    if not shutil.which("chromium"):
        return "no chromium on PATH (nix-shell -p chromium python3Packages.websocket-client)"
    if importlib.util.find_spec("websocket") is None:
        return "no websocket-client in this Python"
    return None


def shown_argv(argv):
    """bs-drive's argv as a line to read: a header's HTML cut to its start."""
    return " ".join(shell_quote(a if len(a) <= 72 else a[:60] + f"… ({len(a)} chars)") for a in argv)


def drive(s, *argv):
    """bs-drive with argv, as this session acts, its output shown as it comes.
    True when it says it did what it was asked, read back."""
    env = dict(os.environ, BS_HOST=urllib.parse.urlsplit(s.base).hostname)
    print(f"\n$ bs-drive.py {shown_argv(argv)}", flush=True)
    return subprocess.run([sys.executable, str(DRIVE), *argv], env=env).returncode == 0


def drive_read_header(s, ou, name):
    """The quiz's {header, footer} HTML as the editor holds it, which is what
    students get: the API's read puts the caller's own id for {OrgDefinedId}.
    None when bs-drive could not read it."""
    env = dict(os.environ, BS_HOST=urllib.parse.urlsplit(s.base).hostname)
    r = subprocess.run([sys.executable, str(DRIVE), "header", str(ou), name, "--json"],
                       env=env, capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else None
    except (ValueError, IndexError):
        return None


def same_html(a, b):
    """Two pieces of HTML the same, as bs-drive's header readback compares them:
    the quiz editor writes a void tag without its slash, & as &amp;, a space
    after a style's colon, and rel="noopener" on every link, and keeps
    newlines between blocks; a view's message comes back with ' as &#39;."""
    def norm(t):
        t = re.sub(r"\s*/>", ">", re.sub(r">\s+<", "><", html.unescape((t or "").replace("\n", " "))))
        t = re.sub(r'\s+rel="noopener(?: noreferrer)?"', "", t)
        return re.sub(r'style="([^"]*)"', lambda m: 'style="' + re.sub(r"\s*([:;])\s*", r"\1", m[1]).strip() + '"',
                      t).strip()
    return norm(a) == norm(b)


def lockdown_argv(ou, quiz, got):
    """bs-drive's lockdown argv that gives a quiz the LockDown settings got, as
    drive_read_lockdown reads them off another: copy-quiz's, for each copy."""
    argv = ["lockdown", str(ou), str(quiz), "--go"]
    if got.get("allow") and (got.get("domains") or "").strip():
        argv += ["--domains", ", ".join(d.strip() for d in re.split(r"[,\n]", got["domains"]) if d.strip())]
    argv += ["--lock-in" if got.get("lock_in") else "--no-lock-in",
             "--for-results" if got.get("for_results") else "--no-for-results",
             "--calculator", got.get("calculator") or "none"]
    if got.get("password"):
        argv += ["--lockdown-password", got["password"]]
    if got.get("monitor") == "required":
        argv += ["--monitor", "--second-camera" if got.get("second_camera") else "--no-second-camera",
                 "--proctored-lab" if got.get("lab") else "--no-proctored-lab",
                 "--monitor-steps", ", ".join(got.get("steps") or []) or "none",
                 "--face-detection", ", ".join(got.get("face") or []) or "none"]
    elif got.get("monitor") == "live proctoring":
        argv += ["--live-proctoring", got.get("system") or "other"] + (["--live-code", got["code"]] if got.get("code") else [])
    else:
        argv.append("--no-monitor")
    return argv


def lockdown_line(got):
    """The LockDown settings drive_read_lockdown read, in a line."""
    n = len([d for d in re.split(r"[,\n]", got.get("domains") or "") if d.strip()]) if got.get("allow") else 0
    parts = [f"{n} site{'s' if n != 1 else ''} allowed" if n else "no other site allowed"]
    parts += (["students locked in until done"] if got.get("lock_in") else [])
    parts += (["results seen only in it"] if got.get("for_results") else [])
    parts += ([f"a {got['calculator']} calculator"] if got.get("calculator") not in (None, "none") else [])
    parts += (["an exam password"] if got.get("password") else [])
    if got.get("monitor") == "required":
        parts.append("Respondus Monitor" + (", with a second camera" if got.get("second_camera") else "")
                     + (", or in a proctored lab" if got.get("lab") else ""))
    elif got.get("monitor") == "live proctoring":
        parts.append(f"live proctoring over {(got.get('system') or 'other').title()}")
    return ", ".join(parts)


# Respondus Monitor's own controls, which do nothing unless Monitor is required:
# its startup steps, face detection, the second camera, the proctored lab.
MONITOR_ONLY = {"FACE_PIC_CHECK_ENABLED", "ID_CHECK_ENABLED", "ENV_CHECK_ENABLED", "ADDITIONAL_INSTRUCTIONS_ENABLED",
                "VIDEO_GUIDELINES_ENABLED", "forceFacialDetection", "notifyFacialDetectionLoss",
                "secondCameraEnabled", "monitorOptional"}


def lockdown_differences(a, b):
    """The labels of the Respondus settings in effect on which two forms differ:
    live proctoring's only when a uses it, and Monitor's only when a requires it,
    since a page keeps what was typed there before another choice was made."""
    mode = (a or {}).get("monitor")
    fa, fb = (a or {}).get("form") or {}, (b or {}).get("form") or {}
    def counts(k):
        if k.startswith("liveProctoring"):
            return mode == "live proctoring"
        if k in MONITOR_ONLY or k.startswith("d_"):
            return mode == "required"
        return True
    return sorted({(fa.get(k) or fb.get(k))[1] or k for k in set(fa) | set(fb)
                   if counts(k) and (fa.get(k) or [None])[0] != (fb.get(k) or [None])[0]})


def drive_read_lockdown(s, ou, name):
    """The quiz's LockDown setting, as bs-drive reads it off the Respondus page:
    {required, allow, domains}, or None when it could not."""
    env = dict(os.environ, BS_HOST=urllib.parse.urlsplit(s.base).hostname)
    r = subprocess.run([sys.executable, str(DRIVE), "lockdown", str(ou), name, "--json"],
                       env=env, capture_output=True, text=True)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else None
    except (ValueError, IndexError):
        return None


def drive_steps(plan, ou):
    """[(the step's key, bs-drive's argv)] for what setup-quiz cannot do through
    the API: the questions, in up to four verbs, then LockDown Browser."""
    steps = []
    if plan["path"]:
        section = plan["name"]
        steps.append(("questions", ["import", str(ou), str(plan["csv"]), "--section", section, "--go"]))
        if plan["msa"]:
            steps.append(("questions", ["blanks", str(ou), str(plan["path"]), "--section", section, "--go"]))
        # The import leaves every option in order; a question added to the quiz
        # after this has its options shuffled from the start.
        if any(on for _, _, on in plan["shuffle"]):
            steps.append(("questions", ["shuffle", str(ou), str(plan["path"]), "--section", section, "--go"]))
        steps.append(("questions", ["add", str(ou), plan["name"], str(plan["csv"]), "--section", section, "--go"]))
    if "header" in plan or "footer" in plan:
        steps.append(("header", ["header", str(ou), plan["name"], "--go"]
                      + [x for k in ("header", "footer") if k in plan for x in (f"--{k}-html", plan[f"{k}_html"])]))
    if "lockdown" in plan:
        if as_flag(plan["lockdown"]):
            steps.append(("lockdown", ["lockdown", str(ou), plan["name"], "--go"]
                          + (["--domains", plan["lockdown_domains"]] if plan.get("lockdown_domains") else [])
                          + (["--lockdown-password", plan["lockdown_password"]] if "lockdown_password" in plan else [])
                          + ([("--" if as_flag(plan["lockdown_lock_in"]) else "--no-") + "lock-in"]
                             if "lockdown_lock_in" in plan else [])
                          + ([("--" if as_flag(plan["lockdown_for_results"]) else "--no-") + "for-results"]
                             if "lockdown_for_results" in plan else [])
                          + (["--calculator", plan["lockdown_calculator"]] if "lockdown_calculator" in plan else [])
                          + ([("--" if as_flag(plan["monitor"]) else "--no-") + "monitor"] if "monitor" in plan else [])
                          + ([("--" if as_flag(plan["second_camera"]) else "--no-") + "second-camera"]
                             if "second_camera" in plan else [])
                          + ([("--" if as_flag(plan["proctored_lab"]) else "--no-") + "proctored-lab"]
                             if "proctored_lab" in plan else [])
                          + (["--monitor-steps", plan["monitor_steps"]] if "monitor_steps" in plan else [])
                          + (["--face-detection", plan["face_detection"]] if "face_detection" in plan else [])
                          + (["--live-proctoring", plan["live_proctoring"]] if "live_proctoring" in plan else [])
                          + (["--live-code", plan["live_proctoring_code"]] if "live_proctoring_code" in plan else [])))
        else:
            steps.append(("lockdown", ["lockdown", str(ou), plan["name"], "--off", "--go"]))
    return steps


def show_quiz(s, ou, quiz_id):
    """Make a quiz visible to students, through the API, and read it back."""
    le, _ = s.versions()
    before = quiz_detail(s, ou, str(quiz_id))
    if before.get("IsActive"):
        return True
    s.send("PUT", f"/d2l/api/le/{le}/{ou}/quizzes/{quiz_id}", quiz_update_payload(before) | {"IsActive": True})
    after = quiz_detail(s, ou, str(quiz_id))
    kept = [k for k in ("StartDate", "EndDate", "DueDate", "GradeItemId", "SubmissionTimeLimit", "AttemptsAllowed")
            if before.get(k) != after.get(k)]
    print(f"\nvisible to students: {yes_no(after.get('IsActive'))}"
          + (f"; CHANGED by the update: {', '.join(kept)}" if kept else ""))
    return bool(after.get("IsActive")) and not kept


def pick_template(s, ou, quizzes, plan, label):
    """The quiz a new one copies its settings from: like, if the plan names one;
    otherwise the one numbered before it, or failing that the last."""
    like = plan.get("like")
    if like and like != "previous":
        return quiz_detail(s, ou, like)
    before = previous_name(plan["name"])
    hit = next((q for q in quizzes if before and same_name(q["Name"], before)), None)
    if hit:
        return hit
    if like == "previous":
        raise Failed(f"{label} has no quiz {before!r} to copy the settings from" if before else
                     f"{plan['name']!r} ends in no number, so no quiz comes before it; name one with like")
    if not quizzes:
        raise Failed(f"{label} has no quiz to copy the settings from; make the first one by hand")
    return quizzes[-1]


def cmd_setup_quiz(args):
    """A quiz and its grade item, made from its brightspace: block, its flags and
    its course's defaults.

    The block is in a quiz written for extras/bs-yaml-quiz, whose README lists
    the keys; each is a flag of the same name too, which wins over the block,
    so a quiz whose questions are written in Brightspace needs no file. Whatever
    both leave out comes from the course's block in the quiz defaults file,
    where start and end are times of day and --date says which day.

    The quiz goes to a grade item of its own name, made like the one the quiz
    it copies sends its scores to, unless grade_item says none; an item of that
    name already there is the one it goes to. It copies its settings from the
    quiz like names, or else the quiz numbered before it, or else the last.

    --shell makes it in an empty shell instead, the first of two steps for a
    course taught as several sections, whose defaults name them: its settings
    come from the first section, the questions go into the shell by hand, and
    copy-quiz is the second step. A course that has sections is never made any
    other way.

    Without --go it prints what it would send. --check sets Brightspace beside
    the plan, the questions counted, and exits 1 on any difference. With a
    file, every question is made into the CSV first, so a quiz whose questions
    would not import is never made.
    """
    if args.go and args.check:
        raise Failed("--go or --check, not both")
    flags = {k: getattr(args, k) for k in QUIZ_KEYS if getattr(args, k, None) is not None}
    flags = {k: (str(v).lower() if isinstance(v, bool) and k != "grade_item" else v) for k, v in flags.items()}
    plan = quiz_plan(pathlib.Path(args.file).expanduser() if args.file else None, flags)
    shell = plan.get("shell")
    target = shell or plan["course"]
    args.course = target
    s, ou, _ = course(args)
    if args.check:
        print(quiz_summary(plan))
        if not check_quiz(s, ou, plan, args.by_hand):
            sys.exit(1)
        return
    le, _ = s.versions()
    quizzes = list(s.objects(f"/d2l/api/le/{le}/{ou}/quizzes/"))
    if shell:
        people = students_in(s, ou)
        if people:
            raise Failed(f"{shell} has {len(people)} students, so it is a section and not a shell")
        if quizzes:
            raise Failed(f"{shell} holds {len(quizzes)} quizzes ({', '.join(repr(x['Name']) for x in quizzes)}): "
                         "a shell starts empty, since copying it takes every quiz in it")
    elif any(quiz_named(q["Name"], plan["name"]) for q in quizzes):
        raise Failed(f"{target} already has a quiz called {plan['name']!r}; --check reads it back")
    from_label = plan["template_course"]
    from_ou = course_site(from_label).ou if from_label != target else ou
    their = quizzes if from_ou == ou else list(s.objects(f"/d2l/api/le/{le}/{from_ou}/quizzes/"))
    template = pick_template(s, from_ou, their, plan, from_label)
    print(quiz_summary(plan, template["Name"]))
    common = {"course": target, "base_url": args.base_url, "json": args.json, "name": plan["name"],
              "dry_run": not args.go}
    attach = None
    if plan["item"]:
        items = grade_items(s, ou)
        have = [g for g in items if same_name(g["Name"], plan["name"])]
        shape = next((g for g in items if g["Id"] == template.get("GradeItemId")), None)
        if have:
            # A run that stopped between the two leaves the item; this picks it up.
            print(f"The grade item {have[0]['Name']!r} is already there, out of {have[0].get('MaxPoints'):g}; "
                  "the quiz goes to it.")
            if "points" in plan and float(plan["points"]) != have[0].get("MaxPoints"):
                print(f"  It is not out of {plan['points']}, as the plan says: set-item does not change "
                      "points, so change them in the web page.")
            print()
            attach = plan["name"]
        elif not shape:
            raise Failed(f"{template['Name']!r} sends its scores to no grade item, so there is none to shape "
                         f"{plan['name']!r}'s like; make one with new-item first, or say grade_item: none")
        else:
            cmd_new_item(argparse.Namespace(**common, like=str(shape["Id"]), points=plan.get("points"),
                                            category=None, weight=None, folder=None))
            print()
            attach = plan["name"] if args.go else None
    made = make_quiz(argparse.Namespace(**common, like=None, start=plan.get("start"), end=plan.get("end"),
                                 minutes=plan.get("minutes"), attempts=plan.get("attempts", "1"),
                                 shuffle_questions=plan.get("shuffle_questions"), paging=plan.get("paging"),
                                 auto_publish=plan.get("auto_publish"),
                                 ip=plan.get("ip"), password=plan.get("password"), grade_item=attach,
                                 calendar=plan.get("calendar"), special_access_only=plan.get("special_access_only"),
                                 active=False),
              description=plan.get("description_html"), template=template,
              template_from=from_label if from_ou != ou else None,
              item_hint=("a shell's has nowhere to send scores; each copy goes to its section's" if shell else
                         "the plan says grade_item: none" if not plan["item"] else
                         "--go attaches the one made above, which a plan cannot name"))
    names_in = ("header", "footer", "lockdown_password", "live_proctoring_code")
    if args.go and any(k in plan for k in names_in):
        refuse_student_names(s, ou, f"the quiz {plan['name']!r}'s header, footer, exam password or start code",
                             *[plan.get(k) for k in names_in])
    views_failed = False
    if args.go and made and any(k in plan for k in VIEW_KEYS):
        # A view that will not land stops nothing else: the questions still go in,
        # and `views` writes the views again from the same file.
        print("\nsubmission views, through the quizzes API:")
        try:
            write_views(s, ou, made["QuizId"], plan.get("primary_view"), plan.get("additional_views"))
        except Failed as e:
            views_failed = True
            print(f"  {e}\n  the rest goes on; brightspace.py views {plan['course']} {shell_quote(plan['name'])} "
                  f"{shell_quote(args.file)} --go writes them again")
    why = drive_unready(s, args.by_hand)
    steps = [] if why else drive_steps(plan, ou)
    done = set()
    if steps and not args.go:
        print("\nThen, with --go, bs-drive takes the steps the API has no route for:")
        for _, argv in steps:
            print(f"    bs-drive.py {shown_argv(argv)}")
        if as_flag(plan.get("visible")) and plan["path"]:
            print("    and the quiz is made visible, through the API, once its questions are in")
        done = {key for key, _ in steps} | ({"visible"} if as_flag(plan.get("visible")) and plan["path"] else set())
    elif steps:
        if plan["path"] and plan["csv_state"] != "current":
            plan["csv"].write_text(plan["csv_text"], encoding="utf-8", newline="")
            print(f"\nwrote {plan['csv'].name}, what bs-yaml-quiz.py makes of {plan['path'].name}"
                  + (" with --msa-as-sa" if plan["msa"] else ""))
        failed = None
        for key, argv in steps:
            if not drive(s, *argv):
                failed = argv[0]
                break
            done.add(key)
        if failed:
            done.discard("questions" if failed in ("import", "blanks", "shuffle", "add") else failed)
            print(f"\nbs-drive stopped at {failed}; what it did before that is in place and read back.")
        elif as_flag(plan.get("visible")) and plan["path"] and made:
            if show_quiz(s, ou, made["QuizId"]):
                done.add("visible")
    elif why and (plan["path"] or any(k in plan for k in ("lockdown", "header", "footer"))):
        print(f"\nbs-drive does not take the steps by hand here: {why}.")
    print("\n" + quiz_remainder(plan, template["Name"], args.file, done))
    wanted = {k for k, _ in steps} | ({"visible"} if steps and as_flag(plan.get("visible")) and plan["path"] else set())
    if args.go and (not done >= wanted or views_failed):
        sys.exit(1)


def yaml_scalar(text):
    """One line's value as YAML writes it, read back by the BaseLoader as the
    same text: bare where it can be, quoted where it cannot."""
    bare = re.fullmatch(r"[A-Za-z0-9_][\w .,/()+-]*", text) and not text.endswith(" ")
    return text if bare and text not in NO_ITEM else json.dumps(text, ensure_ascii=False)


def default_lines(key, value, indent):
    """A default's lines in the file: on its key's line, or a block under it."""
    if "\n" not in value.strip("\n"):
        return [f"{indent}{key}: {yaml_scalar(value.strip())}"]
    body = value.strip("\n").split("\n")
    return ([f"{indent}{key}: |" + ("2" if body[0].startswith(" ") else "")]
            + [f"{indent}  {line}".rstrip() if line.strip() else "" for line in body])


def edit_defaults(path, label, changes):
    """The defaults file's text with one course's keys set (to a value) or
    removed (None), every other line as it was, so comments elsewhere stay."""
    lines = path.read_text(encoding="utf-8").split("\n") if path.exists() else DEFAULTS_HEADER.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    head = next((i for i, x in enumerate(lines)
                 if re.fullmatch(r"""(['"]?)""" + re.escape(label) + r"""\1\s*:\s*(#.*)?""", x)), None)
    if head is None:
        lines += ["", f"{yaml_scalar(label)}:"]
        head = len(lines) - 1
    end = head + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
        end += 1
    while end > head + 1 and not lines[end - 1].strip():
        end -= 1
    block = lines[head + 1:end]
    keyed = [x for x in block if x.strip() and not x.lstrip().startswith("#")]
    indent = keyed[0][:len(keyed[0]) - len(keyed[0].lstrip())] if keyed else "  "
    for key, value in changes.items():
        at = next((i for i, x in enumerate(block) if re.match(re.escape(indent) + re.escape(key) + r"\s*:", x)
                   and x[len(indent)] not in " \t"), None)
        new = [] if value is None else default_lines(key, value, indent)
        if at is None:
            block += new
            continue
        stop = at + 1
        while stop < len(block) and (not block[stop].strip()
                                     or len(block[stop]) - len(block[stop].lstrip()) > len(indent)):
            stop += 1
        while stop > at + 1 and not block[stop - 1].strip():
            stop -= 1
        block[at:stop] = new
    if any(x.strip() and not x.lstrip().startswith("#") for x in block):
        lines[head + 1:end] = block
    else:
        lines[head:end] = []            # nothing left: the course goes, its comments with it
        while head > 0 and head <= len(lines) and not lines[head - 1].strip():
            del lines[head - 1]
            head -= 1
    return "\n".join(lines).rstrip("\n") + "\n"


def cmd_quiz_defaults(args):
    """Each course's quiz defaults, or one course's; --set and --unset change it.

    The defaults are what setup-quiz, and the web page, give a quiz that leaves
    a key out: a block per course in the quiz defaults file, in the keys of a
    quiz's brightspace: block, with start and end as times of day. A course
    taught as several sections names them, and the shell its quizzes are made
    in, so that `setup-quiz --course 115` makes it there and the page knows
    where to copy it.

    The file is YAML and yours to edit; --set changes the lines of the keys it
    is given and no others, so comments elsewhere in it stay. Each value is
    checked first, and the file is read back before it replaces the old one.
    """
    path = QUIZ_DEFAULTS_FILE
    if not args.set and not args.unset:
        everything = quiz_defaults()
        shown = {args.course: everything.get(args.course, {})} if args.course else everything
        if not any(shown.values()):
            print(f"no defaults for {args.course} in {path}" if args.course else f"no defaults in {path}")
            return
        rows = [[label, key, (value.strip().split("\n")[0][:48] + (" …" if "\n" in value.strip() or
                              len(value.strip().split("\n")[0]) > 48 else "")) if key == "description" else value]
                for label, keys in shown.items() for key, value in keys.items()]
        table(["course", "key", "default"], rows)
        print(f"\n  from {path}")
        return
    if not args.course:
        raise Failed("--set and --unset change one course's defaults: name it")
    changes = {}
    for item in args.set:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep:
            raise Failed(f"--set {item!r}: is KEY=VALUE")
        if key not in DEFAULT_KEYS:
            raise Failed(f"--set {key}: is not a default; a course's are {', '.join(DEFAULT_KEYS)}")
        at = f"--set {args.course}:"
        if value.strip() in quiz_format().NONE and key != "grade_item":
            raise Failed(f"--set {key}= sets nothing; --unset {key} removes it")
        check_default(key, value, at)
        for label in (section_labels(value) if key == "sections" else [value.strip()] if key == "shell" else []):
            course_site(label)           # a section or a shell is a course of yours
        changes[key] = value if key == "description" else value.strip()
    for key in args.unset:
        if key not in DEFAULT_KEYS:
            raise Failed(f"--unset {key}: is not a default; a course's are {', '.join(DEFAULT_KEYS)}")
        changes[key] = None
    before = quiz_defaults()
    text = edit_defaults(path, args.course, changes)
    # Read back before it replaces anything, by the reader setup-quiz uses:
    # this course exactly as asked, and every other as it was.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o600)
    try:
        after = quiz_defaults(tmp)
    except Failed as e:
        tmp.unlink()
        raise Failed(f"the edited file would not read back, so {path} is as it was: {e}")
    want = {k: v for k, v in (before.get(args.course, {}) | changes).items() if v is not None}
    same = lambda d: {k: v.strip("\n") if k == "description" else v for k, v in d.items()}
    if (same(after.get(args.course, {})) != same(want)
            or {k: v for k, v in after.items() if k != args.course}
            != {k: v for k, v in before.items() if k != args.course}):
        tmp.unlink()
        raise Failed(f"the edited file would not read back as asked, so {path} is as it was")
    os.replace(tmp, path)
    print(f"wrote {path}: {args.course}'s "
          + ", ".join(f"{k} {'set' if v is not None else 'removed'}" for k, v in changes.items()))
    args.set, args.unset = [], []
    cmd_quiz_defaults(args)


def cmd_classlist(args):
    s, ou, _ = course(args)
    le, _ = s.versions()
    users = s.api(f"/d2l/api/le/{le}/{ou}/classlist/")
    users.sort(key=lambda u: (u.get("ClasslistRoleDisplayName", ""), u.get("LastName", ""), u.get("FirstName", "")))
    rows = [[u.get("DisplayName", ""), u.get("ClasslistRoleDisplayName", ""), u.get("Username", "")]
            + ([u.get("Email", "")] if args.emails else []) for u in users]
    emit(args, rows, ["name", "role", "username"] + (["email"] if args.emails else []), users)


# --- checklists -----------------------------------------------------------
#
# A checklist is a name, a description and items, and every item sits in a
# category: the API refuses an item with no category, as it refuses a
# checklist with no description. So a checklist made here has one category, of
# its own name, holding the items in the order given. A name is plain text of
# at most 512 characters, a checklist's and an item's alike, and a description
# is rich text, which Brightspace rewrites on the way in -- a link gains
# rel="noopener", &mdash; becomes the dash -- so it is read back by its words.
# Measured on a sandbox, 2026-10-07. Nothing in the API reads which items a
# student has ticked.

NAME_MAX = 512
NO_TEXT = {"Content": "", "Type": "Text"}


def checklists_of(s, ou):
    return list(s.objects(f"/d2l/api/le/{s.versions()[0]}/{ou}/checklists/"))


def checklist_items(s, ou, cid):
    """A checklist's items in the order students see them: by category, then
    each in its own place."""
    base = f"/d2l/api/le/{s.versions()[0]}/{ou}/checklists/{cid}/"
    rank = {c["CategoryId"]: c.get("SortOrder") or 0 for c in s.objects(base + "categories/")}
    return sorted(s.objects(base + "items/"),
                  key=lambda i: (rank.get(i.get("CategoryId"), 0), i.get("SortOrder") or 0, i["ChecklistItemId"]))


def find_checklist(s, ou, want):
    have = checklists_of(s, ou)
    if want.isdigit():
        hits = [c for c in have if c["ChecklistId"] == int(want)]
    else:
        hits = [c for c in have if same_name(c["Name"], want)] or [
            c for c in have if want.strip().lower() in c["Name"].lower()]
    if len(hits) == 1:
        return hits[0]
    names = ", ".join(f"{c['ChecklistId']} {c['Name']!r}" for c in (hits or have)) or "it has none"
    raise Failed(f"{'no' if not hits else 'several'} checklists match {want!r}: {names}")


def md_plain(line):
    """What one line of Markdown says, as plain text: what a name can hold."""
    fmt = quiz_format()
    return " ".join(html.unescape(fmt.TAG.sub("", fmt.inline(line))).split())


def md_text(text):
    """Text as Markdown that says it back: a backslash before what would be
    taken for code, emphasis or a link, and before a < only where a tag could
    open, so that 'A < B' stays as it is."""
    return re.sub(r"[\\`*\[\]]|<(?=[A-Za-z/!])", lambda m: "\\" + m.group(), text)


def item_parts(text, where):
    """(name, description HTML) for an item written in Markdown.

    Its first line is the name, which Brightspace keeps as plain text, and the
    lines after it are the description. A first line with a link or emphasis
    in it, which a name cannot carry, opens the description as well, so that
    nothing written is lost.
    """
    fmt = quiz_format()
    first, _, rest = text.replace("\r\n", "\n").strip("\n").partition("\n")
    name = md_plain(first)
    if not name:
        raise Failed(f"{where} has no text")
    if fmt.FENCE.fullmatch(first.strip()):
        raise Failed(f"{where}: its first line is its name, and cannot open a block of code")
    if len(name) > NAME_MAX:
        raise Failed(f"{where}: its first line is its name, {len(name)} characters long, and "
                     f"Brightspace takes {NAME_MAX} at most")
    if fmt.TAG.search(fmt.inline(first)):
        rest = first + "\n\n" + rest
    try:
        return name, fmt.md_html(rest, where) if rest.strip() else ""
    except fmt.Failed as e:
        raise Failed(str(e))


class Markdown(html.parser.HTMLParser):
    """Rich text as Markdown again: what md_html makes, as Brightspace keeps it,
    and anything else as near as that Markdown gets. A paragraph is a paragraph,
    a line break a new line, a list item a line of its own and a program a
    fenced block; a link, emphasis and code stay; any other tag keeps its text
    and loses itself."""

    BLOCKS = {"p", "div", "li", "ul", "ol", "pre", "blockquote", "table", "tr", "section",
              "h1", "h2", "h3", "h4", "h5", "h6"}
    MARKS = {"strong": "**", "b": "**", "em": "*", "i": "*"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.open = [["", [], None]]       # [tag, pieces, href], innermost last
        self.program = None                # a <pre>'s text, while inside one

    def flush(self):
        while len(self.open) > 1:          # a block ends whatever opened inside it
            self.close_inline()
        text = re.sub(r" *\n *", "\n", "".join(self.open[0][1])).strip()
        if text:
            self.blocks.append(text)
        self.open = [["", [], None]]

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.program is not None:       # a program: only its lines count
            if tag == "br":
                self.program.append("\n")
        elif tag in self.BLOCKS:
            # A list item's own paragraph stays on the item's line.
            if not (len(self.open) == 1 and "".join(self.open[0][1]) == "- " and tag not in ("li", "pre")):
                self.flush()
            if tag == "pre":
                self.program = []
            elif tag == "li":
                self.open[0][1].append("- ")
        elif tag == "br":
            self.open[-1][1].append("\n")
        elif tag in ("td", "th"):
            self.open[-1][1].append(" ")
        elif tag in self.MARKS or tag in ("code", "a"):
            self.open.append([tag, [], attrs.get("href")])
        elif tag == "img" and attrs.get("alt"):
            self.open[-1][1].append(md_text(attrs["alt"]))

    def handle_endtag(self, tag):
        if self.program is not None:
            if tag == "pre":
                code = "".join(self.program).replace("\xa0", " ").strip("\n")
                self.program = None
                if code.strip():
                    run = "`" * max(3, max((len(r) for r in re.findall(r"`+", code)), default=0) + 1)
                    self.blocks.append(f"{run}\n{code}\n{run}")
        elif tag in self.BLOCKS:
            self.flush()
        elif any(t == tag for t, *_ in self.open[1:]):
            while self.open[-1][0] != tag:    # misnested: what opened inside it closes first
                self.close_inline()
            self.close_inline()

    def close_inline(self):
        tag, pieces, href = self.open.pop()
        inner = "".join(pieces)
        core = inner.strip()
        lead, trail = inner[:len(inner) - len(inner.lstrip())], inner[len(inner.rstrip()):]
        if not core:
            made = inner
        elif tag == "code":
            run = "`" * (max((len(r) for r in re.findall(r"`+", core)), default=0) + 1)
            made = run + core + run
        elif tag == "a":
            url = re.sub(r"[\s()]", lambda m: f"%{ord(m.group()):02X}", href or "")
            made = f"[{core}]({url})" if url else core
        else:
            made = self.MARKS[tag] + core + self.MARKS[tag]
        self.open[-1][1].append(lead + made + trail)

    def handle_data(self, data):
        if self.program is not None:
            self.program.append(data)
        elif self.open[-1][0] == "code":
            self.open[-1][1].append(data.replace("\n", " "))
        else:
            self.open[-1][1].append(md_text(re.sub(r"\s+", " ", data)))

    def close(self):
        super().close()
        if self.program is not None:
            self.handle_endtag("pre")
        self.flush()


def html_markdown(h):
    """Markdown for rich text, a paragraph to a block: see Markdown."""
    md = Markdown()
    md.feed(h or "")
    md.close()
    return "\n\n".join(md.blocks)


def item_text(item):
    """An item as new-checklist's --item takes it: its name, and its description
    under it -- unless the description opens with the name, as it does for a
    first line that had a link in it."""
    name = " ".join((item.get("Name") or "").split())
    rich = item.get("Description") or {}
    desc = html_markdown(rich.get("Html")) or md_text(rich.get("Text") or "").strip()
    if desc and md_plain(desc.partition("\n")[0]) == name:
        return desc
    # A name Markdown would read as something else is escaped; any other stays as typed.
    return (name if md_plain(name) == name else md_text(name)) + ("\n" + desc if desc else "")


def due_utc(text, where):
    """A due date in this machine's time, '2026-10-13 23:59', as D2L wants it;
    None for none."""
    text = (text or "").strip()
    if not text:
        return None
    if not re.fullmatch(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d", text):
        raise Failed(f"{where}: {text!r} is not a date and a time, as '2026-10-13 23:59'")
    return local_to_utc(text.replace("T", " "))


def cmd_checklists(args):
    """A course's checklists; or, named, one checklist's items, each written the
    way new-checklist takes it, so that what one prints the other can make."""
    s, ou, _ = course(args)
    s.keepalive = True
    if not args.checklist:
        have = checklists_of(s, ou)
        rows = []
        for c in have:
            items = checklist_items(s, ou, c["ChecklistId"])
            dues = sorted(local(i["DueDate"]) for i in items if i.get("DueDate"))
            rows.append([c["ChecklistId"], c["Name"], len(items),
                         f"{dues[0]} to {dues[-1]}" if len(dues) > 1 else "".join(dues)])
        return emit(args, rows, ["id", "name", "items", "due"], have)
    c = find_checklist(s, ou, args.checklist)
    items = checklist_items(s, ou, c["ChecklistId"])
    if args.json:
        print(json.dumps({"checklist": c, "items": items}, indent=1))
        return
    print(f"{c['Name']!r}, checklist {c['ChecklistId']}: {len(items)} item{'' if len(items) == 1 else 's'}")
    for n, item in enumerate(items, 1):
        print(f"\n{n}. " + (f"due {local(item['DueDate'])}" if item.get("DueDate") else "no due date"))
        print("\n".join("   " + line if line else "" for line in item_text(item).split("\n")))


class ItemFlag(argparse.Action):
    """--item and --due into one list, each --due the due date of the --item
    just before it."""

    def __call__(self, parser, ns, value, option_string=None):
        items = getattr(ns, self.dest) or []
        if option_string == "--item":
            items.append([value, None])
        elif not items or items[-1][1] is not None:
            parser.error(f"--due {value}: each --due follows the --item it is the due date of")
        else:
            items[-1][1] = value
        setattr(ns, self.dest, items)


def post_checklist(s, ou, name, items):
    """Make one checklist, its category and its items, and read them back."""
    le, _ = s.versions()
    base = f"/d2l/api/le/{le}/{ou}/checklists/"
    cid = s.send("POST", base, {"Name": name, "Description": NO_TEXT})["ChecklistId"]
    try:
        cat = s.send("POST", f"{base}{cid}/categories/",
                     {"Name": name, "Description": NO_TEXT, "SortOrder": 1})["CategoryId"]
        for item in items:
            s.send("POST", f"{base}{cid}/items/", {"CategoryId": cat, **item})
    except Failed as e:
        raise Failed(f"{e}\n  Checklist {cid} is there, made in part: delete it in Brightspace "
                     "before posting it here again.")
    print(f"  made checklist {cid}, with {len(items)} item{'' if len(items) == 1 else 's'}")
    kept = checklist_items(s, ou, cid)
    said = [text_pair(i["Description"]["Content"], (k.get("Description") or {}).get("Html"))
            for i, k in zip(items, kept)]
    check_landed([("name", name, s.api(f"{base}{cid}").get("Name")),
                  ("items", [i["Name"] for i in items], [k.get("Name") for k in kept]),
                  ("descriptions", [want for want, _ in said], [got for _, got in said]),
                  ("due dates", [i["DueDate"] for i in items], [k.get("DueDate") for k in kept])],
                 f"made checklist {cid}")


def content_root(s, ou):
    """The course's top-level modules in Content."""
    le, _ = s.versions()
    return s.api(f"/d2l/api/le/{le}/{ou}/content/root/")


def cmd_new_links(args):
    """A module in Content, hidden from students, holding one link topic per
    --link: a place on Brightspace where the instructor keeps addresses at hand,
    such as an exam desk's own pages. Each link opens in a new window.

    Without --go it prints what it would send and sends nothing. It refuses a
    name a top-level module already has, unless --replace, which deletes that
    module first and only if all it holds is link topics; so a module whose
    links carry a token can be made again when the token changes. It reads the
    module and every topic back once they are made. The module is hidden and so
    is every topic in it, so that a module shown by mistake still shows a
    student nothing.
    """
    name = " ".join((args.module or "").split())
    if not name:
        raise Failed("the module needs a name: --module")
    if not args.links:
        raise Failed("a module needs a link at least: --link TITLE URL")
    for title, url in args.links:
        if urllib.parse.urlparse(url).scheme not in ("https", "http"):
            raise Failed(f"{url!r}, for {title!r}, is not a web address")
    s, ou, _ = course(args)
    refuse_student_names(s, ou, f"the module {name!r}", name, *[title for title, _ in args.links])
    le, _ = s.versions()
    module = {"Title": name, "ShortTitle": "", "Type": 0, "ModuleStartDate": None, "ModuleEndDate": None,
              "ModuleDueDate": None, "IsHidden": True, "IsLocked": False, "Description": NO_TEXT}
    topics = [{"Title": " ".join(title.split()), "ShortTitle": "", "Type": 1, "TopicType": 3, "Url": url,
               "StartDate": None, "EndDate": None, "DueDate": None, "IsHidden": True, "IsLocked": False,
               "OpenAsExternalResource": True, "Description": NO_TEXT, "MajorUpdate": None,
               "MajorUpdateText": "", "ResetCompletionTracking": None}
              for title, url in args.links]
    print(f"{name!r}, hidden, in Content, with {len(topics)} link{'' if len(topics) == 1 else 's'}:")
    for t in topics:
        print(f"  {t['Title']}: {t['Url']}")
    print(f"\n  POST /d2l/api/le/{le}/{ou}/content/root/ " + json.dumps(module))
    print(f"  POST .../content/modules/<its id>/structure/, once for each link")
    old = [m for m in content_root(s, ou) if same_name(m.get("Title", ""), name)]
    if len(old) > 1:
        raise Failed(f"Content has {len(old)} modules named {name!r}; nothing has been sent")
    if old and not args.replace:
        raise Failed(f"Content already has a module named {name!r}; --replace makes it anew. Nothing has been sent")
    if old:
        oid = old[0]["Id"]
        held = s.api(f"/d2l/api/le/{le}/{ou}/content/modules/{oid}/structure/")
        others = [h.get("Title") for h in held if h.get("Type") != 1 or h.get("TopicType") != 3]
        if others:
            raise Failed(f"module {oid}, {name!r}, holds more than links ({', '.join(map(repr, others))}); "
                         "nothing has been sent")
        print(f"  replaces module {oid}, which holds {len(held)} link{'' if len(held) == 1 else 's'}: "
              + ", ".join(repr(h.get("Title")) for h in held))
    if not args.go:
        print("\n  nothing sent; --go makes it")
        return
    if old:
        s.send("DELETE", f"/d2l/api/le/{le}/{ou}/content/modules/{oid}", None)
        if any(m.get("Id") == oid for m in content_root(s, ou)):
            raise Failed(f"module {oid} is still there after its DELETE; nothing else has been sent")
        print(f"  deleted module {oid}")
    mid = s.send("POST", f"/d2l/api/le/{le}/{ou}/content/root/", module)["Id"]
    try:
        for t in topics:
            s.send("POST", f"/d2l/api/le/{le}/{ou}/content/modules/{mid}/structure/", t)
    except Failed as e:
        raise Failed(f"{e}\n  Module {mid} is there, made in part: delete it in Brightspace before making it again.")
    print(f"\n  made module {mid}, with {len(topics)} link{'' if len(topics) == 1 else 's'}")
    got = s.api(f"/d2l/api/le/{le}/{ou}/content/modules/{mid}")
    kept = s.api(f"/d2l/api/le/{le}/{ou}/content/modules/{mid}/structure/")
    check_landed([("name", name, got.get("Title")),
                  ("hidden", True, got.get("IsHidden")),
                  ("titles", [t["Title"] for t in topics], [k.get("Title") for k in kept]),
                  ("addresses", [t["Url"] for t in topics], [k.get("Url") for k in kept]),
                  ("topics hidden", [True] * len(topics), [k.get("IsHidden") for k in kept])],
                 f"made module {mid}")


def cmd_new_checklist(args):
    """A checklist, the same in each of several courses: a name, and items
    written in Markdown, each due at a time of its own or not at all.

    Each --item is one item, and a --due after it is its due date, in this
    machine's time. The item's first line is its name, which Brightspace keeps
    as plain text; the lines after it are its description. A first line with a
    link or emphasis in it opens the description too, so that nothing written
    is lost.

    Brightspace puts every item in a category, so the checklist gets one, of
    its own name. Without --go it checks everything and prints what it would
    send, and sends nothing. It refuses a course that already has a checklist
    of the name before sending anything anywhere, and reads each one back once
    it is made. The Markdown is bs-yaml-quiz's, so this needs PyYAML, as
    setup-quiz does.
    """
    name = " ".join((args.name or "").split())
    if not name:
        raise Failed("a checklist needs a name: --name")
    if len(name) > NAME_MAX:
        raise Failed(f"the name is {len(name)} characters long, and Brightspace takes {NAME_MAX} at most")
    if not args.items:
        raise Failed("a checklist needs an item at least: --item, and --due after it for a due date")
    items = []
    for n, (text, due) in enumerate(args.items, 1):
        part, desc = item_parts(text, f"item {n}")
        items.append({"Name": part, "Description": {"Content": desc, "Type": "Html"} if desc else NO_TEXT,
                      "SortOrder": n, "DueDate": due_utc(due, f"item {n}")})
    labels = list(dict.fromkeys(args.courses))
    places = [(label, course_site(label)) for label in labels]
    s = open_session(args, places[0][1].host)
    le, _ = s.versions()

    print(f"{name!r}, {len(items)} item{'' if len(items) == 1 else 's'}:")
    for item in items:
        print(f"  {item['SortOrder']}. {item['Name']}  ({'due ' + local(item['DueDate']) if item['DueDate'] else 'no due date'})")
        if item["Description"]["Content"]:
            print(f"     {words(item['Description']['Content'])}")
    print("\nwhat goes to each course, in this order:")
    dump = lambda body: json.dumps(body, ensure_ascii=False)
    print(f"  POST /d2l/api/le/{le}/<course>/checklists/ " + dump({"Name": name, "Description": NO_TEXT}))
    print("  POST .../checklists/<its id>/categories/ " + dump({"Name": name, "Description": NO_TEXT, "SortOrder": 1}))
    for item in items:
        print("  POST .../checklists/<its id>/items/ " + dump({"CategoryId": "<the category's id>", **item}))
    print()
    clash = []
    for label, place in places:
        have = checklists_of(s, place.ou)
        if any(same_name(c["Name"], name) for c in have):
            clash.append(label)
        print(f"  into {label}: " + ("it already has a checklist of this name" if label in clash else
                                     f"{len(have)} checklist{'' if len(have) == 1 else 's'} there now, none of this name"))
    if clash:
        raise Failed(f"{', '.join(clash)} already {'has' if len(clash) == 1 else 'have'} a checklist named "
                     f"{name!r}; nothing has been sent anywhere")
    if not args.go:
        print("\n  nothing sent; --go posts it")
        return
    failed = []
    for label, place in places:
        print(f"\n{label}:")
        try:
            post_checklist(s, place.ou, name, items)
        except Failed as e:
            failed.append(label)
            print(f"  {e}")
    if failed:
        raise Failed(f"not done in {', '.join(failed)}")
    print(f"\n{name!r} is in {', '.join(labels)}")


# --- main -----------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", help="the Brightspace host; default: the course's urls.brightspace, or $BRIGHTSPACE_URL")
    p.add_argument("--json", action="store_true", help="print what the API returned instead of a table")
    sub = p.add_subparsers(dest="cmd", required=True)

    x = sub.add_parser("session", help="hand it the session your logged-in browser holds (the way in at CU)",
                       description=cmd_session.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    x.add_argument("--paste", action="store_true",
                   help="type or paste the cookies instead of reading Firefox")
    x.add_argument("--from-firefox", action="store_true", dest="from_firefox",
                   help="read Firefox and fail if it has no session, rather than falling back to the prompts")
    x.add_argument("--profile", help="which Firefox profile: a name, looked for in ~/.mozilla/firefox "
                                     "and the snap's and Flatpak's copies of it, or a path")
    x.add_argument("--token", action="store_true", help="paste a bearer token instead of the two cookies")
    x.add_argument("--xsrf", action="store_true",
                   help="paste XSRF.Token by hand; normally it is read off /d2l/home by itself")
    x.add_argument("--how", action="store_true", help="print where to find them and exit")
    x.add_argument("--export", action="store_true",
                   help="also print the session as an entry for someone else's keepalive file")
    x.set_defaults(fn=cmd_session)
    x = sub.add_parser("init", help="write the courses file, and the keepalive file if you keep your own session alive",
                       description=cmd_init.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    x.add_argument("--sites", nargs="+", metavar="DIR",
                   help="directories to look in for each course's website repo")
    x.add_argument("--keepalive", action=argparse.BooleanOptionalAction,
                   help="whether this machine keeps your session alive and so needs a keepalive file; "
                        "asked when not given")
    x.add_argument("--dry-run", action="store_true", dest="dry_run", help="print both files and write nothing")
    x.set_defaults(fn=cmd_init)

    x = sub.add_parser("login", help="a local D2L account; a CU account goes through single sign-on, so use `session`")
    x.add_argument("--user", help="the Brightspace username; asked for otherwise")
    x.add_argument("--force", action="store_true", help="post the form even where single sign-on is offered")
    x.set_defaults(fn=cmd_login)
    sub.add_parser("logout", help="forget the kept session or token").set_defaults(fn=cmd_logout)
    sub.add_parser("whoami", help="who the session belongs to").set_defaults(fn=cmd_whoami)
    x = sub.add_parser("ping", help="one cheap call, to keep the session from idling out; for a timer")
    x.add_argument("--quiet", action="store_true", help="say nothing when it works")
    x.set_defaults(fn=cmd_ping)
    x = sub.add_parser("keepalive", help="ping every session in the keepalive file; for a timer",
                       description=cmd_keepalive.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    x.add_argument("--file", help=f"the keepalive file (default: {KEEPALIVE_FILE})")
    x.add_argument("--quiet", action="store_true", help="print only the sessions that failed")
    x.set_defaults(fn=cmd_keepalive)
    sub.add_parser("courses", help="your course enrollments and their org unit ids").set_defaults(fn=cmd_courses)

    def with_course(name, fn, **kw):
        c = sub.add_parser(name, **kw)
        c.add_argument("course", help=f"a label from {COURSES_FILE}, or an org unit id")
        c.set_defaults(fn=fn)
        return c

    with_course("folders", cmd_folders, help="the assignment entries and who has handed in")
    x = with_course("submissions", cmd_submissions, help="what one entry holds, per student; --download fetches the files")
    x.add_argument("folder", help="the entry's name (or enough of it), or its id")
    x.add_argument("--download", action="store_true", help="save each student's latest files under the course's dropbox, in submissions/")
    x.add_argument("--every", action="store_true", help="with --download: every submission, not only the latest")
    x.add_argument("--out", help="save under this directory instead")
    x = with_course("journal", cmd_journal, help="the Journal's entries for a day, printed and saved")
    x.add_argument("--folder", default="journal", help="the entry holding the Journal (default: the one named journal)")
    x.add_argument("--since", metavar="YYYY-MM-DD", help="entries from this day on (default: today only)")
    x.add_argument("--on", metavar="YYYY-MM-DD", help="entries from that one day")
    x.add_argument("--all", action="store_true", help="every entry ever")
    x.add_argument("--out", help="save under this directory instead of the course's dropbox, in journals/")
    x.add_argument("--no-write", action="store_true", help="only print")
    x = with_course("index", cmd_index, help="an index.html, as the Download button writes, for Journal downloads made without one")
    x.add_argument("dirs", nargs="+", metavar="DIR", help="a download: the directory holding the students' folders")
    with_course("quizzes", cmd_quizzes, help="each quiz with its attempt counts")
    with_course("quiz", cmd_quiz, help="one quiz's settings: IP restriction, password, dates, attempts").add_argument(
        "quiz", help="the quiz's name (or enough of it), or its id")
    x = with_course("views", cmd_views, description=cmd_views.__doc__,
                    help="a quiz's submission views, as YAML; with a quiz file and --go, written from it")
    x.add_argument("quiz", help="the quiz's name (or enough of it), or its id")
    x.add_argument("file", nargs="?", help="a quiz file whose brightspace: block has primary_view: or additional_views:")
    x.add_argument("--go", action="store_true", help="write them; without it, the plan is printed")
    with_course("checklists", cmd_checklists, description=cmd_checklists.__doc__,
                help="the checklists; named, one checklist's items, as new-checklist takes them").add_argument(
        "checklist", nargs="?", help="the checklist's name (or enough of it), or its id")
    x = with_course("new-category", cmd_new_category, help="create a grade category")
    x.add_argument("name")
    x.add_argument("--weight", required=True, help="its share of the final grade")
    x.add_argument("--max-points", default=10, dest="max_points")
    x.add_argument("--dry-run", action="store_true", dest="dry_run", help="print the payload and stop")
    x = with_course("set-item", cmd_set_item,
                    help="rename a grade item, move it, reweight it, or change what it is out of")
    x.add_argument("item", help="the item's name or id")
    x.add_argument("--name", help="rename it")
    x.add_argument("--category", help="move it into this category, by name or id")
    x.add_argument("--weight")
    x.add_argument("--points", help="what it is out of: a quiz that grows a question grows its column")
    x.add_argument("--folder", help="attach it to this assignment folder, by name or id, from the item's side")
    x.add_argument("--clip", action="store_true",
                   help="with a lower --points: go ahead though marks are above it; Brightspace caps them, not rescales")
    x.add_argument("--dry-run", action="store_true", dest="dry_run", help="print the payload and stop")
    x = with_course("new-quiz", cmd_new_quiz, help="create a quiz; its question is added by hand after")
    x.add_argument("name")
    x.add_argument("--start", help="local time, '2026-09-29 12:30'")
    x.add_argument("--end", help="local time, '2026-09-29 13:25'")
    x.add_argument("--minutes", help="enforced time limit, or none for no timer; without it, --like's limit is kept as it is")
    x.add_argument("--attempts", default=1, help="a whole number, or unlimited")
    x.add_argument("--auto-publish", dest="auto_publish", action=argparse.BooleanOptionalAction,
                   help="publish attempt results on submission; without either, --like's")
    x.add_argument("--shuffle-questions", dest="shuffle_questions", action=argparse.BooleanOptionalAction,
                   help="the questions in a new order for each student; without either, --like's")
    x.add_argument("--paging", help="questions to a page: all, 1, 5, 10, or sections; without it, --like's")
    x.add_argument("--ip", help="allowed range, '148.137.150.0-148.137.150.255'")
    x.add_argument("--password")
    x.add_argument("--like", help="an existing quiz to copy the settings from")
    x.add_argument("--like-from", dest="like_from", metavar="COURSE",
                   help="read --like's quiz in this course instead: 230, for a sandbox with no quiz of its own")
    x.add_argument("--grade-item", dest="grade_item", help="attach this grade item, by name or id")
    x.add_argument("--active", action="store_true", help="visible to students immediately")
    x.add_argument("--calendar", action=argparse.BooleanOptionalAction,
                   help="show its dates in the course calendar; without it, --like's setting")
    x.add_argument("--special-access-only", dest="special_access_only", action=argparse.BooleanOptionalAction,
                   help="only users given special access in the UI see it; without it, --like's setting")
    x.add_argument("--dry-run", action="store_true", dest="dry_run", help="print the payload and stop")

    x = with_course("new-folder", cmd_new_folder, help="create an assignment folder students hand in to")
    x.add_argument("name")
    x.add_argument("--due", help="local time, '2026-09-29 23:59'")
    x.add_argument("--open", help="local time it becomes available, '2026-09-23 00:01'")
    x.add_argument("--link", help="the assignment page's URL, which is all the instructions carry")
    x.add_argument("--instructions", help="an HTML file for the instructions instead; --link follows it if both")
    x.add_argument("--submissions", choices=list(SUBMISSIONS_RULE),
                   help="what a second submission does: keep-all, overwrite (the latest only) or one; "
                        "the template's otherwise")
    x.add_argument("--points", help="what it is out of, if not the template's")
    x.add_argument("--grade-item", dest="grade_item", help="attach this grade item, by name or id")
    x.add_argument("--like", help="an existing folder to copy the settings from")
    x.add_argument("--active", action="store_true", help="visible to students immediately")
    x.add_argument("--dry-run", action="store_true", dest="dry_run", help="print the payload and stop")

    x = with_course("set-quiz", cmd_set_quiz, help="change a quiz's name, shuffle or auto-publish")
    x.add_argument("quiz", help="the quiz's name (or enough of it), or its id")
    x.add_argument("--name", help="rename it")
    x.add_argument("--shuffle", action=argparse.BooleanOptionalAction, help="shuffle questions and options")
    x.add_argument("--auto-publish", dest="auto_publish", action=argparse.BooleanOptionalAction,
                   help="publish attempt results immediately upon completion (IsAutoSetGraded)")
    x.add_argument("--header", metavar="FILE", help="an HTML file to show above every page of the quiz")
    x.add_argument("--password", help="the password a student types to start it; it goes on the whiteboard")
    x.add_argument("--start", help="when it opens, local time, '2026-10-19 14:00'")
    x.add_argument("--end", help="when it closes, and is due, local time, '2026-10-19 15:50'")
    x.add_argument("--calendar", action=argparse.BooleanOptionalAction,
                   help="show the quiz's dates in the course calendar (DisplayInCalendar)")
    x.add_argument("--special-access-only", dest="special_access_only", action=argparse.BooleanOptionalAction,
                   help="only users given special access in the UI see the quiz (AllowOnlyUsersWithSpecialAccess)")
    x.add_argument("--minutes", help="the time limit, enforced, with its clock shown; 'none' takes it off")
    x.add_argument("--description", help="what a student reads before starting, in markdown, as a quiz's YAML holds it")
    x.add_argument("--dry-run", action="store_true", dest="dry_run", help="print the change and stop")
    x = with_course("new-item", cmd_new_item, help="create a grade item, shaped like a named one")
    x.add_argument("name")
    x.add_argument("--like", required=True, help="the grade item to copy: category, points, scale")
    x.add_argument("--points", help="what it is out of, if not the template's")
    x.add_argument("--category", help="a different category, by name or id")
    x.add_argument("--weight", help="its weight; only for an item outside a category, or a category weighted by hand")
    x.add_argument("--folder", help="attach it to this assignment folder, by name or id")
    x.add_argument("--dry-run", action="store_true", dest="dry_run", help="print the payload and stop")
    x = with_course("grade", cmd_grade, help="give named students a mark in one grade item, such as its full marks",
                    description=cmd_grade.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    x.add_argument("item", help="the grade item's name or id")
    x.add_argument("students", nargs="+", metavar="STUDENT", help="First Last, as on the classlist, or a user id")
    x.add_argument("--max", action="store_true", help="the item's full marks")
    x.add_argument("--points", help="this many points instead")
    x.add_argument("--go", action="store_true", help="set them; without it, print the plan")
    x = with_course("set-folder", cmd_set_folder,
                    help="show an assignment folder to students or hide it, or change what a second submission does",
                    description=cmd_set_folder.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    x.add_argument("folder", help="the folder's name (or enough of it), or its id")
    way = x.add_mutually_exclusive_group()
    way.add_argument("--show", action="store_true", help="visible to students")
    way.add_argument("--hide", action="store_true", help="hidden from students")
    x.add_argument("--submissions", choices=list(SUBMISSIONS_RULE),
                   help="what a second submission does: keep-all, overwrite (the latest only) or one")
    x.add_argument("--dry-run", action="store_true", dest="dry_run", help="print the payload and stop")
    x = with_course("setup", cmd_setup, help="an assignment's folder and grade item, read off the course site's files",
                    description=cmd_setup.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    x.add_argument("assignment", help="its id, the way its page is named: a6")
    x.add_argument("--go", action="store_true", help="create them; without it, print what would be sent")
    x.add_argument("--check", action="store_true", help="read them back and compare them with the site's files")
    x.add_argument("--like", help="the assignment to copy the settings from (default: the one before)")
    x.add_argument("--site", help="the course website repo to read (default: its site in the courses file)")
    x = with_course("announcements", cmd_announcements, help="the latest announcements, when each appears")
    x.add_argument("--last", type=int, default=10, help="how many (default 10)")

    x = sub.add_parser("setup-quiz", help="a quiz and its grade item, from its YAML, its flags and its course's defaults",
                       description=cmd_setup_quiz.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    x.add_argument("file", nargs="?", help="the quiz's .yml, as extras/bs-yaml-quiz reads it; the flags do without")
    x.add_argument("--course", help=f"a label from {COURSES_FILE}, or an org unit id")
    x.add_argument("--name", help="the quiz's name, and its grade item's")
    x.add_argument("--date", metavar="YYYY-MM-DD", help="the day it runs, for a start and end given as times of day")
    x.add_argument("--start", help="'14:00' on --date, or '2026-10-12 14:00'")
    x.add_argument("--end", help="'15:29' on --date, or '2026-10-12 15:29'")
    x.add_argument("--minutes", help="enforced time limit, or none for no timer; without it, the copied quiz's, "
                                     "unless the course's defaults say otherwise")
    x.add_argument("--attempts", help="a whole number, or unlimited")
    x.add_argument("--auto-publish", dest="auto_publish", action=argparse.BooleanOptionalAction,
                   help="publish attempt results on submission; without either, the copied quiz's")
    x.add_argument("--shuffle-questions", dest="shuffle_questions", action=argparse.BooleanOptionalAction,
                   help="the questions in a new order for each student; without either, the copied quiz's")
    x.add_argument("--paging", help="questions to a page: all, 1, 5, 10, or sections; without it, the copied quiz's")
    x.add_argument("--points", help="what its grade item is out of")
    x.add_argument("--like", help="the quiz to copy the settings from (default: the one numbered before it)")
    x.add_argument("--ip", help="allowed range, '148.137.150.0-148.137.150.255'")
    x.add_argument("--password")
    x.add_argument("--grade-item", dest="grade_item", action=argparse.BooleanOptionalAction,
                   help="a grade item of the quiz's name, or with --no-grade-item none")
    x.add_argument("--description", help="what students read before they start, in Markdown")
    x.add_argument("--shell", help="make it in this empty shell, to copy into the course's sections after")
    x.add_argument("--lockdown", action=argparse.BooleanOptionalAction,
                   help="require LockDown Browser, or with --no-lockdown not")
    x.add_argument("--lockdown-domains", dest="lockdown_domains",
                   help="the sites LockDown Browser lets a student open, one line with commas")
    x.add_argument("--monitor", action=argparse.BooleanOptionalAction,
                   help="require Respondus Monitor, the webcam, or with --no-monitor not; it takes LockDown")
    x.add_argument("--second-camera", dest="second_camera", action=argparse.BooleanOptionalAction,
                   help="require a second camera, a phone, which is Monitor's")
    x.add_argument("--proctored-lab", dest="proctored_lab", action=argparse.BooleanOptionalAction,
                   help="also allow a sitting in a proctored lab, no webcam recording, which is Monitor's "
                        "and needs --lockdown-password")
    x.add_argument("--lockdown-password", dest="lockdown_password", metavar="PASSWORD",
                   help="LockDown Browser's own password to access the exam, which a lab's proctor types in")
    x.add_argument("--lockdown-lock-in", dest="lockdown_lock_in", action=argparse.BooleanOptionalAction,
                   help="lock students into LockDown Browser until the exam is completed")
    x.add_argument("--lockdown-for-results", dest="lockdown_for_results", action=argparse.BooleanOptionalAction,
                   help="require LockDown Browser to view post-exam feedback and results")
    x.add_argument("--lockdown-calculator", dest="lockdown_calculator", metavar="standard|scientific|none",
                   help="the calculator on LockDown Browser's toolbar")
    x.add_argument("--monitor-steps", dest="monitor_steps", metavar="'instructions, environment'",
                   help="Monitor's startup steps on, the rest off: " + ", ".join(MONITOR_STEPS) + "; or none")
    x.add_argument("--face-detection", dest="face_detection", metavar="'block, notify'",
                   help="Monitor's face detection options on: block (no start), notify (during); or none")
    x.add_argument("--live-proctoring", dest="live_proctoring", metavar="SYSTEM",
                   help="an instructor proctoring over " + ", ".join(LIVE_SYSTEMS) + ", instead of Monitor")
    x.add_argument("--live-proctoring-code", dest="live_proctoring_code", metavar="CODE",
                   help="live proctoring's start code")
    x.add_argument("--visible", action=argparse.BooleanOptionalAction,
                   help="show it to students once its questions are in, or with --no-visible leave it hidden")
    x.add_argument("--by-hand", dest="by_hand", action="store_true",
                   help="print the steps the API cannot take, instead of having bs-drive take them")
    x.add_argument("--go", action="store_true", help="make them; without it, print what would be sent")
    x.add_argument("--check", action="store_true", help="read them back and compare them with the plan")
    x.set_defaults(fn=cmd_setup_quiz)
    x = sub.add_parser("quiz-defaults", help="each course's quiz defaults; --set and --unset change one's",
                       description=cmd_quiz_defaults.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    x.add_argument("course", nargs="?", help="a course's label, or a name for several sections")
    x.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="give it a default")
    x.add_argument("--unset", action="append", default=[], metavar="KEY", help="take one away")
    x.set_defaults(fn=cmd_quiz_defaults)

    x = sub.add_parser("copy-quiz", help="copy the one quiz in a shell course into each of several sections",
                       description=cmd_copy_quiz.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    x.add_argument("shell", help="the course the quiz was made in, with no students and no other quiz")
    x.add_argument("--to", nargs="+", required=True, metavar="COURSE", help="the sections to copy it into")
    x.add_argument("--item-like", dest="item_like", metavar="ITEM",
                   help="where a section has no grade item named like the quiz, make one shaped like this one")
    x.add_argument("--go", action="store_true", help="copy; without it, check and print the plan")
    x.add_argument("--clear", action="store_true",
                   help="once every section has its copy, delete the quiz from the shell")
    x.set_defaults(fn=cmd_copy_quiz)

    x = sub.add_parser("new-checklist", help="a checklist, the same in each of several courses: a name, and items "
                                             "in Markdown with their due dates",
                       description=cmd_new_checklist.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    x.add_argument("courses", nargs="+", metavar="COURSE", help=f"labels from {COURSES_FILE}, or org unit ids")
    x.add_argument("--name", required=True, help="the checklist's name")
    x.add_argument("--item", dest="items", action=ItemFlag, metavar="TEXT",
                   help="an item, in Markdown: its first line is its name, the lines after it its description")
    x.add_argument("--due", dest="items", action=ItemFlag, metavar="WHEN",
                   help="the due date of the --item before it, in this machine's time: '2026-10-13 23:59'")
    x.add_argument("--go", action="store_true", help="post it; without it, check and print what would be sent")
    x.set_defaults(fn=cmd_new_checklist)

    x = with_course("new-links", cmd_new_links, help="a module in Content, hidden from students, of link topics",
                    description=cmd_new_links.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    x.add_argument("--module", required=True, help="the module's name")
    x.add_argument("--link", dest="links", nargs=2, action="append", metavar=("TITLE", "URL"),
                   help="a link topic: its title, then its address; once for each")
    x.add_argument("--replace", action="store_true",
                   help="delete a module of the same name first, if all it holds is link topics")
    x.add_argument("--go", action="store_true", help="make it; without it, print what would be sent")

    x = with_course("delete-quiz", cmd_delete_quiz,
                    help="delete a quiz with no attempts, no questions and no grade item")
    x.add_argument("quiz", help="the quiz's name (or enough of it), or its id")
    x.add_argument("--go", action="store_true", help="delete it; without this it only checks")
    x = with_course("delete-folder", cmd_delete_folder,
                    help="delete an assignment folder nobody has handed in to, so setup can make it again")
    x.add_argument("folder", help="the folder's name (or enough of it), or its id")
    x.add_argument("--go", action="store_true", help="delete it; without this it only checks")

    with_course("classlist", cmd_classlist, help="who is enrolled, with roles").add_argument(
        "--emails", action="store_true", help="add the email column")

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except NotLoggedIn as e:
        sys.exit(f"brightspace.py: {e}")
    except Failed as e:
        sys.exit(f"brightspace.py: {e}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
