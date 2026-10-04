#!/usr/bin/env python3
"""A web page for brightspace.py, for whoever would rather not type its commands:
making a quiz, in its course or in a shell to copy into several sections,
copying it, and each course's quiz defaults.

    ./brightspace-web.py                          on your own machine, as you
    ./brightspace-web.py --access web.ini         behind Cloudflare Access, as whoever signed in

Each button runs the command the page shows, and prints what it printed. The
page is a way to fill in a command and not a second copy of one, so it checks
and refuses exactly what the command does.

**On your own machine** it acts as you: the session `brightspace.py session`
took, your courses file and your quiz defaults. It opens itself in your
browser.

**Behind Cloudflare Access** it acts as one of the sessions in the keepalive
file, each with its own courses file and quiz defaults, and each person Access
lets in picks among those web.ini gives them:

    [access]
    team = yourteam.cloudflareaccess.com   # the team domain, from Zero Trust
    aud = 4714c1a2...                      # the application's Audience (AUD) tag
    host = brightspace.example.edu         # the hostname Access protects

    [session ada]                          # a [name] in the keepalive file
    courses = ~/.config/brightspace/courses.ini
    defaults = ~/.config/brightspace/quiz-defaults.yml   # quiz-defaults-ada.yml beside this file if left out

    [user ada@example.edu]
    sessions = ada, bob                    # the ones they may act as, the first where they start

Access puts a signed token on every request it lets through, and this checks
that token itself -- signature, audience, issuer, expiry -- before it acts as
anyone. A header alone proves nothing, since anything on the same machine can
send one. Until web.ini says which Access application to trust, it refuses
everything.

Either way it listens on 127.0.0.1 only, answers only a request addressed to
the name it serves, and every form carries a token made when it starts, so a
page you visit elsewhere cannot press its buttons for you. Ctrl-C stops it.
"""

import argparse
import base64
import hashlib
import hmac
import html
import http.server
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import brightspace as B  # noqa: E402

TOKEN = secrets.token_urlsafe(24)
# SHA-256's DigestInfo prefix, which an RS256 signature carries before the digest.
SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class Refused(Exception):
    pass


# --- who is asking ---------------------------------------------------------------

def b64url(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verified_claims(token, keys, aud, issuer, now=None):
    """A Cloudflare Access token's claims, once its RS256 signature checks out
    against keys {kid: (n, e)}, it is for aud, from issuer, and current."""
    try:
        head64, body64, sig64 = token.split(".")
        head, claims = json.loads(b64url(head64)), json.loads(b64url(body64))
        sig = b64url(sig64)
    except (ValueError, TypeError):
        raise Refused("not a token")
    if head.get("alg") != "RS256" or head.get("kid") not in keys:
        raise Refused("not signed by a key Access publishes")
    n, e = keys[head["kid"]]
    size = (n.bit_length() + 7) // 8
    if len(sig) != size:
        raise Refused("not signed by a key Access publishes")
    digest = hashlib.sha256(f"{head64}.{body64}".encode()).digest()
    want = b"\x00\x01" + b"\xff" * (size - 3 - len(SHA256_INFO) - len(digest)) + b"\x00" + SHA256_INFO + digest
    if not hmac.compare_digest(pow(int.from_bytes(sig, "big"), e, n).to_bytes(size, "big"), want):
        raise Refused("its signature does not check out")
    now = time.time() if now is None else now
    if not (isinstance(claims.get("exp"), (int, float)) and claims["exp"] > now):
        raise Refused("it has expired; reload the page")
    if claims.get("nbf", 0) > now + 60:
        raise Refused("it is not valid yet")
    auds = claims.get("aud")
    if aud not in ([auds] if isinstance(auds, str) else auds or []):
        raise Refused("it is for another application")
    if claims.get("iss") != issuer:
        raise Refused("it is from another issuer")
    return claims


class Access:
    """web.ini: which Access application to trust, the sessions the page can act
    as, and who may use the page as which of them."""

    def __init__(self, path):
        self.path = pathlib.Path(path).expanduser()
        self.keys, self.fetched, self.lock = {}, 0.0, threading.Lock()
        self.users, self.sessions, self.problem = {}, {}, None
        conf = dict(B.read_ini(self.path))
        top = conf.get("access", {})
        self.team, self.aud, self.host = top.get("team", ""), top.get("aud", ""), top.get("host", "")
        self.certs = top.get("certs") or f"https://{self.team}/cdn-cgi/access/certs"
        for section, keys in conf.items():
            if section.startswith("session "):
                name = section[8:].strip()
                if not keys.get("courses"):
                    raise B.Failed(f"{self.path}, [{section}]: a session needs courses, the file its labels are in")
                self.sessions[name] = self.profile(name, keys["courses"], keys.get("defaults"))
        for section, keys in conf.items():
            if not section.startswith("user "):
                continue
            email = section[5:].strip().lower()
            if keys.get("sessions"):
                names = [x for x in re.split(r"[\s,]+", keys["sessions"]) if x]
            elif keys.get("session") and keys.get("courses"):
                # web.ini's first form: one session each, its courses beside it.
                names = [keys["session"]]
                self.sessions.setdefault(keys["session"], self.profile(keys["session"], keys["courses"], None))
            else:
                raise B.Failed(f"{self.path}, [{section}]: a user needs sessions, the [session NAME]s they may "
                               "act as, theirs first")
            unknown = [n for n in names if n not in self.sessions]
            if unknown:
                raise B.Failed(f"{self.path}, [{section}]: there is no [session {unknown[0]}] for it to act as")
            self.users[email] = names
        if not (self.team and self.aud and self.host):
            self.problem = (f"{self.path} does not yet say which Access application to trust: "
                            "its [access] needs team, aud and host")

    def profile(self, name, courses, defaults):
        """A session the page can act as: its name in the keepalive file, its
        courses file, and its quiz defaults, by default a file of its own."""
        return {"name": name, "session": name, "courses": B.file_path(self.path, courses),
                "defaults": (B.file_path(self.path, defaults) if defaults
                             else self.path.resolve().parent / f"quiz-defaults-{name}.yml")}

    def key_set(self, refresh=False):
        """{kid: (n, e)} from Access's published keys, fetched again at most every
        ten minutes, or sooner once, when a token names a key not yet seen."""
        with self.lock:
            if refresh or not self.keys or time.time() - self.fetched > 3600:
                if not self.keys or time.time() - self.fetched > 600:
                    with urllib.request.urlopen(self.certs, timeout=10) as r:
                        published = json.load(r)
                    self.keys = {k["kid"]: (int.from_bytes(b64url(k["n"]), "big"), int.from_bytes(b64url(k["e"]), "big"))
                                 for k in published.get("keys", []) if k.get("kty") == "RSA"}
                    self.fetched = time.time()
            return self.keys

    def who(self, headers):
        """(email, the sessions they may act as) for a request, or Refused."""
        if self.problem:
            raise Refused(self.problem)
        token = headers.get("Cf-Access-Jwt-Assertion")
        if not token:
            raise Refused("this page is only reached through Cloudflare Access")
        issuer = f"https://{self.team}"
        try:
            claims = verified_claims(token, self.key_set(), self.aud, issuer)
        except Refused as e:
            if "key" not in str(e):
                raise
            claims = verified_claims(token, self.key_set(refresh=True), self.aud, issuer)
        email = str(claims.get("email", "")).lower()
        if email not in self.users:
            raise Refused(f"you signed in as {email or 'nobody'}, who is not listed in {self.path}")
        return email, [self.sessions[n] for n in self.users[email]]


# On one's own machine: the session `brightspace.py session` took, and the
# courses file and quiz defaults the command finds by itself.
YOU = {"name": "you", "session": None, "courses": None, "defaults": None}


# --- what the page shows ----------------------------------------------------------

STYLE = """<style>
:root { --bg: #fbfbf9; --fg: #1f1f1f; --muted: #666; --line: #d8d8d2; --box: #fff;
        --ok: #1d6b36; --bad: #a12a1f; --accent: #2d4f8a; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #17181a; --fg: #e8e8e6; --muted: #a2a2a0; --line: #36383b; --box: #1f2023;
          --ok: #6fcf8b; --bad: #f08a7e; --accent: #8fb0ea; } }
body { background: var(--bg); color: var(--fg); margin: 0;
       font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 48rem; margin: 0 auto; padding: 1.5rem 1rem 3rem; }
h1 { font-size: 1.4rem; margin: 0 0 .5rem; }
.as { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; margin: 0 0 .25rem; }
.as select { width: auto; }
.who { color: var(--muted); margin: 0 0 1.5rem; }
fieldset { border: 1px solid var(--line); border-radius: 8px; background: var(--box);
           margin: 0 0 1rem; padding: .75rem 1rem 1rem; min-width: 0; }
legend { font-weight: 600; padding: 0 .3rem; }
.hint { color: var(--muted); font-size: .9rem; margin: .1rem 0 .6rem; }
label { display: block; margin: .3rem 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(10.5rem, 1fr)); gap: .25rem 1rem; }
.grid label { font-size: .9rem; color: var(--muted); }
.grid input, .grid select { margin-top: .15rem; color: var(--fg); font-size: 1rem; }
.name { color: var(--muted); font-size: .9rem; }
select, input[type=text], input[type=date], textarea { font: inherit; padding: .35rem .5rem;
  border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--fg); width: 100%;
  box-sizing: border-box; }
textarea { font: .85rem/1.4 ui-monospace, "SF Mono", Menlo, Consolas, monospace; margin-top: .3rem; resize: vertical; }
::placeholder { color: var(--muted); opacity: .75; }
input[type=radio], input[type=checkbox] { margin-right: .35rem; }
h2.part { font-size: 1.15rem; margin: 2.5rem 0 .5rem; padding-top: 1.25rem; border-top: 1px solid var(--line); }
details { margin: 1rem 0 0; }
summary { cursor: pointer; color: var(--accent); }
.buttons { display: flex; gap: .75rem; flex-wrap: wrap; margin-top: 1rem; }
button { font: inherit; padding: .5rem 1.1rem; border-radius: 6px; border: 1px solid var(--accent);
         cursor: pointer; background: var(--box); color: var(--accent); }
button.go { background: var(--accent); color: var(--bg); }
.result { margin-top: 1.5rem; }
.result h2 { font-size: 1.1rem; margin: 0 0 .5rem; }
.ok { color: var(--ok); } .bad { color: var(--bad); }
pre { background: var(--box); border: 1px solid var(--line); border-radius: 8px; padding: .75rem 1rem;
      overflow-x: auto; font-size: .85rem; line-height: 1.4; white-space: pre-wrap; }
code { font-size: .9em; }
table { border-collapse: collapse; width: 100%; font-size: .9rem; margin: .5rem 0 1rem; }
td { border-top: 1px solid var(--line); padding: .4rem .5rem .4rem 0; vertical-align: top; }
td:first-child { font-weight: 600; white-space: nowrap; }
td:last-child { text-align: right; white-space: nowrap; }
a { color: var(--accent); }
</style>"""

# Fills each field's greyed default from the chosen course's, and keeps a
# course taught as sections to the shell; and reads a chosen file into the box.
SCRIPT = """<script>
var DEFAULTS = JSON.parse(document.getElementById("defaults").textContent);
function course_changed() {
  var form = document.getElementById("make"), d = DEFAULTS[form.course.value] || {};
  form.querySelectorAll("[data-default]").forEach(function (el) {
    var key = el.dataset.default, said = d[key];
    el.placeholder = said !== undefined ? said : (el.dataset.none || "");
  });
  var item = form.querySelector("option[value='']");
  if (item) item.textContent = "as the course's defaults say: " + ("grade_item" in d ? "none" : "one of the quiz's name");
  var sections = !!d.sections, here = form.querySelector("input[name=route][value=course]");
  here.disabled = sections;
  if (sections) form.querySelector("input[name=route][value=shell]").checked = true;
  if (d.shell && !form.dataset.touched) form.shell.value = d.shell;
}
document.getElementById("make").course.addEventListener("change", course_changed);
document.getElementById("make").shell.addEventListener("change", function () { this.form.dataset.touched = "1"; });
course_changed();
document.getElementById("pick").addEventListener("change", function () {
  var file = this.files[0], form = this.form;
  if (file) file.text().then(function (text) { form.yaml.value = text; form.filename.value = file.name; });
});
</script>"""

REFUSED = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Brightspace: not here</title></head>
<body style="font: 16px/1.5 system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem">
<h1 style="font-size: 1.3rem">Not here</h1><p>{why}</p></body></html>
"""

# The make form's fields past the course and the route, each a flag of
# setup-quiz's and, but for the first two, a default: (key, label, what an
# empty one means when the course's defaults say nothing).
MAKE = [("name", "Name", ""), ("date", "Date", ""), ("start", "Opens", "a time, as 14:00"),
        ("end", "Closes", "a time, as 15:29"),
        ("minutes", "Minutes", "the copied quiz's"), ("attempts", "Attempts", "1"),
        ("points", "Grade item out of", "the copied item's"), ("like", "Settings copied from", "previous"),
        ("ip", "IP range", "anywhere"), ("password", "Password", "none")]
# The defaults form's: a quiz's, and the three that only a course has.
DEFAULT_FIELDS = MAKE[2:] + [("item_like", "Grade item each copy is shaped like", "previous")]


def known(profile):
    """(a line saying who the page acts as, [(label, org unit id, its name)])."""
    names, who = {}, ""
    try:
        s = B.open_session(argparse.Namespace(base_url=None, json=False), session=profile["session"])
        me = B.whoami(s)
        who = f"Acting as {me.get('FirstName')} {me.get('LastName')} ({me.get('UniqueName')}) in Brightspace."
        _, lp = s.versions()
        for item in s.paged(f"/d2l/api/lp/{lp}/enrollments/myenrollments/", {"orgUnitTypeId": "3"}):
            ou = item.get("OrgUnit") or {}
            names[ou.get("Id")] = " ".join((ou.get("Name") or "").split())
    except (B.Failed, B.NotLoggedIn) as e:
        who = f"Not logged in to Brightspace: {e}"
    try:
        labels = list(B.course_entries(profile["courses"]))
    except B.Failed as e:
        return f"{who} The courses file is broken: {e}", []
    rows = []
    for label in labels:
        try:
            ou = B.course_site(label, profile["courses"]).ou
        except (OSError, B.Failed):
            ou = None
        rows.append((label, ou, names.get(ou, "")))
    if not rows:
        who += f" No courses are listed in {profile['courses'] or B.COURSES_FILE}."
    return who, rows


def defaults_of(profile):
    """({course: its defaults}, a problem reading them or None)."""
    try:
        return B.quiz_defaults(profile["defaults"]), None
    except B.Failed as e:
        return {}, str(e)


def labels_of(profile):
    try:
        return list(B.course_entries(profile["courses"]))
    except B.Failed:
        return []


def options(pairs, chosen):
    esc = html.escape
    return "".join(f'<option value="{esc(v)}"{" selected" if v == chosen else ""}>{esc(text)}</option>'
                   for v, text in pairs)


def gist(keys):
    """A course's defaults in one line, for the table of them."""
    bits = []
    if keys.get("start") or keys.get("end"):
        bits.append(f"opens {keys.get('start', '—')}, closes {keys.get('end', '—')}")
    for key, word in (("minutes", "minutes"), ("attempts", "attempts")):
        if keys.get(key):
            bits.append(f"{keys[key]} {word}")
    if "grade_item" in keys:
        bits.append("no grade item")
    elif keys.get("points"):
        bits.append(f"out of {keys['points']}")
    for key, word in (("like", "settings from"), ("ip", "IP"), ("sections", "sections"), ("shell", "shell"),
                      ("item_like", "copies' items like")):
        if keys.get(key):
            bits.append(f"{word} {keys[key]}")
    if keys.get("password"):
        bits.append("a password")
    if keys.get("description"):
        bits.append("a description")
    return "; ".join(bits) or "nothing yet"


def page(profiles, profile, form=None, slots=None, edit=None, copy=None):
    form, slots, copy = form or {}, slots or {}, copy or {}
    esc = html.escape
    who, rows = known(profile)
    defaults, problem = defaults_of(profile)
    labels = [label for label, _, _ in rows]
    named = {label: f"{label} — {name}" if name else label for label, _, name in rows}
    groups = [c for c, keys in defaults.items() if keys.get("sections") and c not in named]
    courses = [(label, named[label]) for label in labels] + [
        (g, f"{g} — sections {', '.join(B.section_labels(defaults[g]['sections']))}") for g in groups]
    course = form.get("course") if form.get("course") in dict(courses) else (courses[0][0] if courses else "")
    mine = defaults.get(course, {})
    route = form.get("route") or ("shell" if mine.get("sections") or mine.get("shell") else "course")
    shell = form.get("shell") or mine.get("shell", "")
    hidden = (f'<input type="hidden" name="token" value="{TOKEN}">'
              f'<input type="hidden" name="as" value="{esc(profile["name"])}">')

    def field(key, label, none, values, placeholders, kind="text"):
        said = placeholders.get(key)
        hold = said if said is not None else none
        return (f'<label>{esc(label)}<input type="{kind}" name="{key}" value="{esc(values.get(key, ""))}"'
                f' placeholder="{esc(hold)}" data-default="{key}" data-none="{esc(none)}"'
                + (" required" if key == "name" else "") + "></label>")

    out = ["<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">",
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           "<title>Brightspace quizzes</title>", STYLE, "</head><body><main>",
           "<h1>Quizzes on Brightspace</h1>"]
    # Whose session: each person picks among those web.ini gives them.
    if len(profiles) > 1:
        out.append('<form method="get" action="/" class="as"><label for="as">Acting as</label>'
                   f'<select id="as" name="as" onchange="this.form.submit()">'
                   + options([(p["name"], p["name"]) for p in profiles], profile["name"])
                   + "</select><noscript><button>Switch</button></noscript></form>")
    out.append(f'<p class="who">{esc(who)}</p>')

    # 1. Make a quiz: in the course, or in a shell to copy from.
    out.append('<h2 class="part" id="make-it">1. Make a quiz</h2>')
    out.append(f'<form method="post" action="/make#make-it" id="make" data-touched="{"1" if form.get("shell") else ""}">'
               + hidden + '<fieldset><legend>Where</legend>'
               f'<label>Course<select name="course">{options(courses, course)}</select></label>'
               f'<label><input type="radio" name="route" value="course"{" checked" if route == "course" else ""}>'
               "In the course itself</label>"
               f'<label><input type="radio" name="route" value="shell"{" checked" if route == "shell" else ""}>'
               "In an empty shell, to copy into the course's sections afterwards, in step 2:"
               f'<select name="shell">{options([(x, named[x]) for x in labels], shell)}</select></label>'
               '<p class="hint">A course taught as several sections is always made in a shell, so that each '
               "section gets the same quiz. Its questions go into the shell by hand, between the two steps.</p>"
               "</fieldset><fieldset><legend>The quiz</legend>"
               "<p class=\"hint\">What is left empty comes from the course's defaults, shown greyed; "
               'step 3 below changes them. Start and end are times on the date.</p><div class="grid">')
    for key, label, none in MAKE:
        out.append(field(key, label, none, form, mine, "date" if key == "date" else "text"))
    item_default = "none" if "grade_item" in mine else "one of the quiz's name"
    out.append('<label>Grade item<select name="grade_item">'
               + options([("", f"as the course's defaults say: {item_default}"), ("yes", "one of the quiz's name"),
                          ("no", "none")], form.get("grade_item", "")) + "</select></label></div>")
    out.append('<label>Description, in Markdown<textarea name="description" rows="5" data-default="description"'
               f' data-none="none" placeholder="{esc(mine.get("description", "none"))}">'
               f'{esc(form.get("description", ""))}</textarea></label></fieldset>'
               '<div class="buttons"><button name="action" value="plan">Check, and send nothing</button>'
               '<button name="action" value="go" class="go">Make it</button>'
               '<button name="action" value="check">Compare with Brightspace</button></div></form>')
    out.append(slots.get("made", ""))
    out.append(f'<details{" open" if form.get("yaml") or slots.get("made_file") else ""}>'
               "<summary>Or make it from a quiz file</summary>"
               '<form method="post" action="/setup-quiz#make-it">' + hidden
               + f'<input type="hidden" name="filename" value="{esc(form.get("filename", ""))}">'
               "<fieldset><legend>The quiz file</legend>"
               "<p class=\"hint\">A quiz written for bs-yaml-quiz, whose <code>brightspace:</code> block says "
               "which course it goes in and when it runs, in the keys above. Choose its file, or paste it. "
               "Its questions go in afterwards, from the CSV this hands back.</p>"
               '<input type="file" accept=".yml,.yaml" id="pick">'
               f'<textarea name="yaml" rows="12" spellcheck="false">{esc(form.get("yaml", ""))}</textarea>'
               "</fieldset>"
               '<div class="buttons"><button name="action" value="plan">Check, and send nothing</button>'
               '<button name="action" value="go" class="go">Make it</button>'
               '<button name="action" value="check">Compare with Brightspace</button></div></form>'
               + slots.get("made_file", "") + "</details>")

    # 2. Copy it into the sections: copy-quiz.
    ticked = set(copy.get("to") or form.get("to") or [])
    out.append('<h2 class="part" id="copy-it">2. Copy it into the sections</h2>'
               '<p class="hint">For a quiz made in a shell, once its questions are in there.</p>'
               '<form method="post" action="/copy-quiz#copy-it">' + hidden
               + '<fieldset><legend>From</legend><p class="hint">The shell: a course of yours with no students, '
               "holding exactly the quiz to copy.</p>"
               f'<select name="shell">{options([(x, named[x]) for x in labels], copy.get("shell") or form.get("from", ""))}'
               "</select></fieldset>"
               "<fieldset><legend>Into</legend><p class=\"hint\">Every section that should receive it.</p>"
               + "".join(f'<label><input type="checkbox" name="to" value="{esc(x)}"{" checked" if x in ticked else ""}>'
                         f'{esc(named[x])}</label>' for x in labels)
               + "</fieldset><fieldset><legend>Grade item</legend>"
               "<p class=\"hint\">A copy loses its grade item, so each is attached to its section's item of the "
               "quiz's own name. Where a section has none, a new one is made shaped like this one; "
               "<code>previous</code> is the item of the quiz numbered before it. Empty makes none.</p>"
               f'<input type="text" name="item_like" value="{esc(copy.get("item_like", form.get("item_like", "")))}"'
               ' placeholder="previous">'
               f'<label><input type="checkbox" name="clear"{" checked" if copy.get("clear", form.get("clear")) else ""}>'
               "Afterwards, delete the quiz from the shell, ready for the next one</label></fieldset>"
               '<div class="buttons"><button name="action" value="check">Check, and send nothing</button>'
               '<button name="action" value="copy" class="go">Copy</button></div></form>')
    out.append(slots.get("copied", ""))

    # 3. Course defaults: quiz-defaults.
    out.append('<h2 class="part" id="defaults-of">3. Course defaults</h2>'
               "<p class=\"hint\">One block per course: what a quiz leaves empty. They cover what the API can set; "
               "submission views and the questions stay a step by hand, and making a quiz says so.</p>")
    if problem:
        out.append(f'<p class="bad">{esc(problem)}</p>')
    if defaults:
        out.append("<table>" + "".join(
            f'<tr><td>{esc(c)}</td><td>{esc(gist(keys))}</td><td><a href="/?as={urllib.parse.quote(profile["name"])}'
            f'&amp;edit={urllib.parse.quote(c)}#defaults-of">edit</a></td></tr>' for c, keys in defaults.items())
            + "</table>")
    edit = edit if edit is not None else (form.get("dcourse") or "")
    now = dict(defaults.get(edit, {}))
    values = {k: form[f"d_{k}"] for k in (k for k, _, _ in DEFAULT_FIELDS) if f"d_{k}" in form} or now
    out.append('<form method="post" action="/defaults#defaults-of">' + hidden
               + "<fieldset><legend>A course's defaults</legend>"
               '<p class="hint">A course of yours, or a new name such as <code>115</code> for a course taught '
               "as several sections, which then names them below.</p>"
               f'<label>Course<input type="text" name="dcourse" list="courses" value="{esc(edit)}" required></label>'
               '<datalist id="courses">' + "".join(f'<option value="{esc(c)}">' for c in dict.fromkeys(
                   labels + list(defaults))) + '</datalist><div class="grid">')
    for key, label, none in DEFAULT_FIELDS:
        out.append(f'<label>{esc(label)}<input type="text" name="d_{key}" value="{esc(values.get(key, ""))}"'
                   f' placeholder="{esc(none)}"></label>')
    out.append('<label>Grade item<select name="d_grade_item">'
               + options([("", "one of the quiz's name"), ("none", "none")],
                         "none" if "grade_item" in values else "") + "</select></label>"
               '<label>Shell<select name="d_shell">'
               + options([("", "none")] + [(x, named[x]) for x in labels], values.get("shell", ""))
               + "</select></label></div>"
               '<label>Description, in Markdown<textarea name="d_description" rows="4">'
               f'{esc(values.get("description", ""))}</textarea></label>'
               "<p class=\"hint\">Sections, for a course taught as several: each gets a copy of the quiz made in "
               "the shell.</p>"
               + "".join(f'<label><input type="checkbox" name="d_sections" value="{esc(x)}"'
                         f'{" checked" if x in B.section_labels(values.get("sections", "")) else ""}>{esc(named[x])}</label>'
                         for x in labels)
               + '</fieldset><div class="buttons"><button class="go">Save</button></div></form>')
    out.append(slots.get("saved", ""))
    out.append('</main><script type="application/json" id="defaults">'
               + json.dumps(defaults).replace("</", "<\\/") + "</script>" + SCRIPT + "</body></html>")
    return "\n".join(out)


def command(argv, profile, cwd=None):
    """brightspace.py with these arguments, as the session: (the command line, exit status, output)."""
    env = dict(os.environ)
    for var, key in (("BRIGHTSPACE_SESSION", "session"), ("BRIGHTSPACE_COURSES", "courses"),
                     ("BRIGHTSPACE_QUIZ_DEFAULTS", "defaults")):
        if profile[key]:
            env[var] = str(profile[key])
    r = subprocess.run([sys.executable, str(HERE / "brightspace.py"), *argv],
                       capture_output=True, text=True, timeout=1800, env=env, cwd=cwd)
    return "brightspace.py " + " ".join(map(shell_word, argv)), r.returncode, (r.stdout + r.stderr).strip()


def run_make(form, profile):
    """setup-quiz for the make form: the course and the route, and every field
    filled in as a flag; an empty one is the course's default."""
    labels = labels_of(profile)
    groups = [c for c, keys in defaults_of(profile)[0].items() if keys.get("sections")]
    course = form.get("course", "")
    if course not in labels + groups:
        raise ValueError(f"{course!r} is not one of your courses")
    argv = ["setup-quiz", "--course", course]
    if form.get("route") == "shell":
        if form.get("shell") not in labels:
            raise ValueError(f"{form.get('shell')!r} is not one of your courses")
        argv += ["--shell", form["shell"]]
    if not form.get("name", "").strip():
        raise ValueError("the quiz needs a name")
    for key, _, _ in MAKE + [("description", "", "")]:
        value = form.get(key, "").replace("\r\n", "\n")
        if value.strip():
            argv.append(f"--{key}={value if key == 'description' else value.strip()}")
    argv += {"yes": ["--grade-item"], "no": ["--no-grade-item"]}.get(form.get("grade_item"), [])
    argv += {"go": ["--go"], "check": ["--check"]}.get(form.get("action"), [])
    return command(argv, profile)


def run_copy(form, profile):
    """copy-quiz for the copy form."""
    labels = labels_of(profile)
    def course(value):
        if value in labels or re.fullmatch(r"\d+", value or ""):
            return value
        raise ValueError(f"{value!r} is not one of your courses")
    shell = course(form.get("shell", ""))
    to = [course(v) for v in form.get("to", [])]
    if not to:
        raise ValueError("tick at least one section to copy into")
    argv = ["copy-quiz", shell, "--to", *to]
    if form.get("item_like", "").strip():
        argv.append("--item-like=" + form["item_like"].strip())
    if form.get("action") == "copy":
        argv.append("--go")
        if form.get("clear"):
            argv.append("--clear")
    return command(argv, profile)


def run_defaults(form, profile):
    """quiz-defaults for the defaults form: a --set for each field changed, an
    --unset for each emptied."""
    course = form.get("dcourse", "").strip()
    if not re.fullmatch(r"[\w.-]+", course):
        raise ValueError("a course is one of your labels, or a name like 115 for one taught as several sections")
    now = defaults_of(profile)[0].get(course, {})
    asked = {k: form.get(f"d_{k}", "").replace("\r\n", "\n") for k, _, _ in DEFAULT_FIELDS}
    asked |= {"description": form.get("d_description", "").replace("\r\n", "\n"),
              "shell": form.get("d_shell", ""), "sections": " ".join(form.get("d_sections", [])),
              "grade_item": "none" if form.get("d_grade_item") == "none" else ""}
    argv = ["quiz-defaults", course]
    for key, value in asked.items():
        value = value.strip("\n") if key == "description" else value.strip()
        if value and value != (now.get(key) or "").strip("\n").strip():
            argv.append(f"--set={key}={value}")
        elif not value and key in now:
            argv.append(f"--unset={key}")
    if len(argv) == 2:
        raise ValueError(f"nothing to change in {course}'s defaults")
    return command(argv, profile)


def run_setup(form, profile):
    """setup-quiz for a quiz file sent from the page, run as the session, in a
    directory of its own that holds the file and the CSV made from it, as
    bs-yaml-quiz.py would leave them: (the command line, exit status, output,
    (the CSV's name, its text) or None)."""
    text = form.get("yaml", "").replace("\r\n", "\n")
    if not text.strip():
        raise ValueError("choose the quiz's file, or paste it into the box")
    name = pathlib.PurePath(form.get("filename") or "").name
    if not re.fullmatch(r"\w[\w.-]*\.ya?ml", name):     # never an option, never a path
        name = "quiz.yml"
    argv = ["setup-quiz", name, *{"go": ["--go"], "check": ["--check"]}.get(form.get("action"), [])]
    csv = None
    with tempfile.TemporaryDirectory(prefix="setup-quiz-") as tmp:
        src = pathlib.Path(tmp) / name
        src.write_text(text, encoding="utf-8")
        try:
            fmt = B.quiz_format()
            csv = (src.with_suffix(".csv").name, fmt.to_csv(fmt.load(text), name))
            src.with_suffix(".csv").write_bytes(csv[1].encode("utf-8"))
        except Exception:
            csv = None      # whatever stops the CSV, setup-quiz reports it in its own words
        line, code, output = command(argv, profile, cwd=tmp)
    return line, code, output, csv


def shell_word(arg):
    """An argument as a shell would need it typed, a --key=value's quotes on the value."""
    if re.fullmatch(r"[\w.=:/-]+", arg):
        return arg
    quote = lambda s: "'" + s.replace("'", "'\\''") + "'"
    m = re.fullmatch(r"(--[\w-]+=)(.*)", arg, re.S)
    return m.group(1) + quote(m.group(2)) if m else quote(arg)


def result(verdict, good, line, output, extra=""):
    esc = html.escape
    return (f'<section class="result"><h2 class="{"ok" if good else "bad"}">{esc(verdict)}</h2>'
            f"<p><code>{esc(line)}</code></p>{extra}<pre>{esc(output)}</pre></section>")


class Handler(http.server.BaseHTTPRequestHandler):
    access = None      # an Access, behind Cloudflare Access; None on one's own machine

    def log_message(self, *a):
        pass

    def reply(self, code, body):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def people(self):
        """The sessions the request may act as, or Refused: addressed to the name
        served, from a form of this page if a browser says where it came from,
        and, behind Access, carrying a token Access signed for someone web.ini
        lists."""
        port = self.server.server_address[1]
        if self.access and self.access.problem:
            raise Refused(self.access.problem)
        if self.access:
            hosts, origins = {self.access.host}, {f"https://{self.access.host}"}
        else:
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origins = {f"http://{h}" for h in hosts}
        if self.headers.get("Host") not in hosts:
            raise Refused("this page is not served under that name")
        if self.headers.get("Origin") not in (None, *origins):
            raise Refused("that form came from another site")
        if not self.access:
            return [YOU]
        return self.access.who(self.headers)[1]

    def do_GET(self):
        try:
            profiles = self.people()
        except (Refused, OSError, ValueError, B.Failed) as e:
            return self.reply(403, REFUSED.format(why=html.escape(str(e))))
        u = urllib.parse.urlsplit(self.path)
        if u.path != "/":
            return self.reply(404, REFUSED.format(why="Nothing is at that address."))
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        profile = next((p for p in profiles if p["name"] == q.get("as")), profiles[0])
        self.reply(200, page(profiles, profile, {"course": q.get("course", "")}, edit=q.get("edit")))

    def do_POST(self):
        try:
            profiles = self.people()
        except (Refused, OSError, ValueError, B.Failed) as e:
            return self.reply(403, REFUSED.format(why=html.escape(str(e))))
        where = urllib.parse.urlsplit(self.path).path
        if where not in ("/make", "/setup-quiz", "/copy-quiz", "/defaults"):
            return self.reply(404, REFUSED.format(why="Nothing is at that address."))
        size = int(self.headers.get("Content-Length") or 0)
        if size > 2_000_000:
            return self.reply(413, REFUSED.format(why="That is more than a quiz file; nothing was run."))
        fields = urllib.parse.parse_qs(self.rfile.read(size).decode("utf-8"), keep_blank_values=True)
        form = {k: (v if k in ("to", "d_sections") else v[0]) for k, v in fields.items()}
        if not secrets.compare_digest(form.get("token", ""), TOKEN):
            return self.reply(403, REFUSED.format(why="That form is from an earlier run of this page, or from "
                                                      "somewhere else. Reload the page and try again."))
        profile = next((p for p in profiles if p["name"] == form.get("as", profiles[0]["name"])), None)
        if profile is None:
            return self.reply(403, REFUSED.format(why=f"You may not act as {html.escape(form.get('as', ''))}."))
        slot = {"/make": "made", "/setup-quiz": "made_file", "/copy-quiz": "copied", "/defaults": "saved"}[where]
        action, csv, copy, edit = form.get("action"), None, None, None
        try:
            if where == "/make":
                line, code, output = run_make(form, profile)
            elif where == "/setup-quiz":
                line, code, output, csv = run_setup(form, profile)
            elif where == "/copy-quiz":
                line, code, output = run_copy(form, profile)
                form["from"] = form.get("shell", "")
            else:
                line, code, output = run_defaults(form, profile)
                edit = form.get("dcourse", "").strip()
        except ValueError as e:
            note = f'<section class="result"><h2 class="bad">Not run</h2><p>{html.escape(str(e))}</p></section>'
            return self.reply(200, page(profiles, profile, form, {slot: note},
                                        edit=form.get("dcourse") if where == "/defaults" else None))
        except subprocess.TimeoutExpired:
            note = ('<section class="result"><h2 class="bad">Still running after half an hour</h2>'
                    "<p>Look in Brightspace before trying again.</p></section>")
            return self.reply(200, page(profiles, profile, form, {slot: note}))
        if where == "/copy-quiz":
            verdict = "Stopped" if code else "Copied" if action == "copy" else "Checked: nothing sent"
        elif where == "/defaults":
            verdict = "Not saved" if code else "Saved"
        elif code:
            verdict = "Not what the plan says, or not compared: below is why" if action == "check" else "Stopped"
        else:
            verdict = {"go": "Made", "check": "Brightspace has what the plan says"}.get(action, "Checked: nothing sent")
        extra = ""
        if csv and not code:
            data = base64.b64encode(csv[1].encode("utf-8")).decode()
            extra = (f'<p><a download="{html.escape(csv[0])}" href="data:text/csv;charset=utf-8;base64,{data}">'
                     f"Download {html.escape(csv[0])}</a>, the questions for the Question Library's "
                     "Import, made from the file.</p>")
        if where == "/make" and form.get("route") == "shell" and action == "go" and not code:
            # Step 1 done: step 2 is ready, once the questions are in the shell.
            keys = defaults_of(profile)[0].get(form.get("course", ""), {})
            copy = {"shell": form.get("shell"), "to": B.section_labels(keys.get("sections")) or [form.get("course")],
                    "item_like": keys.get("item_like", "previous"), "clear": True}
            extra += ("<p>Next, in the shell, its questions; then step 2 below copies it into the sections, "
                      "ready as it is.</p>")
        self.reply(200, page(profiles, profile, form, {slot: result(verdict, code == 0, line, output, extra)},
                             edit=edit, copy=copy))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--access", metavar="WEB_INI", help="serve behind Cloudflare Access, as web.ini says")
    p.add_argument("--port", type=int, default=8140, help="default 8140")
    p.add_argument("--no-browser", action="store_true", help="do not open the page in a browser")
    args = p.parse_args(argv)
    try:
        Handler.access = Access(args.access) if args.access else None
    except (OSError, B.Failed) as e:
        sys.exit(f"brightspace-web.py: {e}")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    if Handler.access:
        print(f"serving {url} for https://{Handler.access.host or '(no host yet)'}/ behind Cloudflare Access"
              + (f"; {Handler.access.problem}, so everything is refused" if Handler.access.problem else ""),
              flush=True)
    else:
        print(f"serving {url} as you; Ctrl-C stops it", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
