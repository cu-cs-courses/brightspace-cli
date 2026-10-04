#!/usr/bin/env python3
"""A web page for brightspace.py, for whoever would rather not type its commands.
Making a quiz from its YAML file, and copying a quiz into several sections, so far.

    ./brightspace-web.py                          on your own machine, as you
    ./brightspace-web.py --access web.ini         behind Cloudflare Access, as whoever signed in

Each button runs the command the page shows, and prints what it printed. The
page is a way to fill in a command and not a second copy of one, so it checks
and refuses exactly what the command does.

**On your own machine** it acts as you: the session `brightspace.py session`
took and the courses in your courses file. It opens itself in your browser.

**Behind Cloudflare Access** it acts as whoever Access says signed in, each
with their own session and courses, as web.ini lists them:

    [access]
    team = yourteam.cloudflareaccess.com   # the team domain, from Zero Trust
    aud = 4714c1a2...                      # the application's Audience (AUD) tag
    host = brightspace.example.edu         # the hostname Access protects

    [user ada@example.edu]
    session = ada                          # a [name] in the keepalive file
    courses = ~/.config/brightspace/courses.ini

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
    """web.ini: which Access application to trust, and who may use the page."""

    def __init__(self, path):
        self.path = pathlib.Path(path).expanduser()
        self.keys, self.fetched, self.lock = {}, 0.0, threading.Lock()
        self.users, self.problem = {}, None
        conf = dict(B.read_ini(self.path))
        top = conf.get("access", {})
        self.team, self.aud, self.host = top.get("team", ""), top.get("aud", ""), top.get("host", "")
        self.certs = top.get("certs") or f"https://{self.team}/cdn-cgi/access/certs"
        for section, keys in conf.items():
            if section.startswith("user "):
                email = section[5:].strip().lower()
                if not keys.get("session") or not keys.get("courses"):
                    raise B.Failed(f"{self.path}, [{section}]: a user needs session and courses")
                self.users[email] = {"session": keys["session"],
                                     "courses": B.file_path(self.path, keys["courses"])}
        if not (self.team and self.aud and self.host):
            self.problem = (f"{self.path} does not yet say which Access application to trust: "
                            "its [access] needs team, aud and host")

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
        """(email, that user's settings) for a request, or Refused."""
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
        return email, self.users[email]


# --- the page ----------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Brightspace quizzes</title>
<style>
:root {{ --bg: #fbfbf9; --fg: #1f1f1f; --muted: #666; --line: #d8d8d2; --box: #fff;
         --ok: #1d6b36; --bad: #a12a1f; --accent: #2d4f8a; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #17181a; --fg: #e8e8e6; --muted: #a2a2a0; --line: #36383b; --box: #1f2023;
           --ok: #6fcf8b; --bad: #f08a7e; --accent: #8fb0ea; }} }}
body {{ background: var(--bg); color: var(--fg); margin: 0;
        font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 46rem; margin: 0 auto; padding: 1.5rem 1rem 3rem; }}
h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
.who {{ color: var(--muted); margin: 0 0 1.5rem; }}
fieldset {{ border: 1px solid var(--line); border-radius: 8px; background: var(--box);
            margin: 0 0 1rem; padding: .75rem 1rem 1rem; }}
legend {{ font-weight: 600; padding: 0 .3rem; }}
.hint {{ color: var(--muted); font-size: .9rem; margin: .1rem 0 .6rem; }}
label {{ display: block; margin: .3rem 0; }}
.name {{ color: var(--muted); font-size: .9rem; }}
select, input[type=text], textarea {{ font: inherit; padding: .35rem .5rem; border: 1px solid var(--line);
                                       border-radius: 6px; background: var(--bg); color: var(--fg); width: 100%;
                                       box-sizing: border-box; }}
textarea {{ font: .85rem/1.4 ui-monospace, "SF Mono", Menlo, Consolas, monospace; margin-top: .6rem; resize: vertical; }}
h2.part {{ font-size: 1.15rem; margin: 2.25rem 0 .75rem; padding-top: 1.25rem; border-top: 1px solid var(--line); }}
h2.part:first-of-type {{ border-top: 0; padding-top: 0; margin-top: 0; }}
.buttons {{ display: flex; gap: .75rem; flex-wrap: wrap; margin-top: 1.25rem; }}
button {{ font: inherit; padding: .5rem 1.1rem; border-radius: 6px; border: 1px solid var(--accent);
          cursor: pointer; background: var(--box); color: var(--accent); }}
button.go {{ background: var(--accent); color: var(--bg); }}
.result {{ margin-top: 2rem; }}
.result h2 {{ font-size: 1.1rem; margin: 0 0 .5rem; }}
.ok {{ color: var(--ok); }} .bad {{ color: var(--bad); }}
pre {{ background: var(--box); border: 1px solid var(--line); border-radius: 8px; padding: .75rem 1rem;
       overflow-x: auto; font-size: .85rem; line-height: 1.4; white-space: pre-wrap; }}
code {{ font-size: .9em; }}
</style></head>
<body><main>
<h1>Quizzes on Brightspace</h1>
<p class="who">{who}</p>
<h2 class="part">Make a quiz from its file</h2>
<form method="post" action="/setup-quiz">
<input type="hidden" name="token" value="{token}">
<input type="hidden" name="filename" value="{filename}">
<fieldset><legend>The quiz</legend>
<p class="hint">A quiz written for bs-yaml-quiz, whose <code>brightspace:</code> block says which course it
goes in, when it runs and which quiz it copies its settings from. Choose its file, or paste it below. Its grade
item is made with it; its questions go in afterwards, from the CSV this hands back.</p>
<input type="file" accept=".yml,.yaml" id="pick">
<textarea name="yaml" rows="14" spellcheck="false" required>{yaml}</textarea>
</fieldset>
<div class="buttons">
<button name="action" value="plan">Check, and send nothing</button>
<button name="action" value="go" class="go">Make it</button>
<button name="action" value="check">Compare with Brightspace</button>
</div>
</form>
{made}
<h2 class="part">Copy a quiz into several sections</h2>
<form method="post" action="/copy-quiz">
<input type="hidden" name="token" value="{token}">
<fieldset><legend>From</legend>
<p class="hint">A shell: a course of yours with no students, holding exactly the quiz to copy.</p>
<select name="shell" required>{shells}</select>
</fieldset>
<fieldset><legend>Into</legend>
<p class="hint">Every section that should receive it, the one you made it for included.</p>
{sections}
</fieldset>
<fieldset><legend>Grade item</legend>
<p class="hint">A copied quiz loses its grade item, so each copy is attached to its section's
item of the quiz's own name. Where a section has none, a new one is made shaped like this one,
such as the last quiz's. Leave it empty to make none.</p>
<input type="text" name="item_like" value="{item_like}" placeholder="Quiz 2">
<label><input type="checkbox" name="clear" {clear}> Afterwards, delete the quiz from the shell,
ready for the next one</label>
</fieldset>
<div class="buttons">
<button name="action" value="check">Check, and send nothing</button>
<button name="action" value="copy" class="go">Copy</button>
</div>
</form>
{copied}
</main>
<script>
// Reads the chosen file into the box, so that each button sends the same text.
document.getElementById("pick").addEventListener("change", function () {{
  var file = this.files[0], form = this.form;
  if (file) file.text().then(function (text) {{ form.yaml.value = text; form.filename.value = file.name; }});
}});
</script>
</body></html>
"""

REFUSED = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Brightspace: not here</title></head>
<body style="font: 16px/1.5 system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem">
<h1 style="font-size: 1.3rem">Not here</h1><p>{why}</p></body></html>
"""


def known(user):
    """(a line saying who the page acts as, [(label, org unit id, its name)])."""
    session, courses = (user["session"], user["courses"]) if user else (None, None)
    names, who = {}, ""
    try:
        s = B.open_session(argparse.Namespace(base_url=None, json=False), session=session)
        me = B.whoami(s)
        who = f"Acting as {me.get('FirstName')} {me.get('LastName')} ({me.get('UniqueName')}) in Brightspace."
        _, lp = s.versions()
        for item in s.paged(f"/d2l/api/lp/{lp}/enrollments/myenrollments/", {"orgUnitTypeId": "3"}):
            ou = item.get("OrgUnit") or {}
            names[ou.get("Id")] = " ".join((ou.get("Name") or "").split())
    except (B.Failed, B.NotLoggedIn) as e:
        who = f"Not logged in to Brightspace: {e}"
    try:
        labels = list(B.course_entries(courses))
    except B.Failed as e:
        return f"{who} The courses file is broken: {e}", []
    rows = []
    for label in labels:
        try:
            ou = B.course_site(label, courses).ou
        except (OSError, B.Failed):
            ou = None
        rows.append((label, ou, names.get(ou, "")))
    if not rows:
        who += f" No courses are listed in {courses or B.COURSES_FILE}."
    return who, rows


def page(user, form=None, made="", copied=""):
    form = form or {}
    who, rows = known(user)
    esc = html.escape
    shells = "".join(f'<option value="{esc(label)}"{" selected" if form.get("shell") == label else ""}>'
                     f'{esc(label)}{" — " + esc(name) if name else ""}</option>' for label, _, name in rows)
    chosen = set(form.get("to", []))
    sections = "".join(f'<label><input type="checkbox" name="to" value="{esc(label)}"'
                       f'{" checked" if label in chosen else ""}> {esc(label)} '
                       f'<span class="name">{esc(name)}</span></label>' for label, _, name in rows)
    return PAGE.format(who=esc(who), token=TOKEN, shells=shells, sections=sections or "<p>No courses yet.</p>",
                       item_like=esc(form.get("item_like", "")), clear="checked" if form.get("clear") else "",
                       yaml=esc(form.get("yaml", "")), filename=esc(form.get("filename", "")),
                       made=made, copied=copied)


def run_copy(form, labels, user):
    """copy-quiz for a submitted form, run as the user: (the command line, its exit status, its output)."""
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
    return command(argv, user)


def command(argv, user, cwd=None):
    """brightspace.py with these arguments, as the user: (the command line, exit status, output)."""
    env = dict(os.environ)
    if user:
        env |= {"BRIGHTSPACE_SESSION": user["session"], "BRIGHTSPACE_COURSES": str(user["courses"])}
    r = subprocess.run([sys.executable, str(HERE / "brightspace.py"), *argv],
                       capture_output=True, text=True, timeout=1800, env=env, cwd=cwd)
    return "brightspace.py " + " ".join(map(shell_word, argv)), r.returncode, (r.stdout + r.stderr).strip()


def run_setup(form, user):
    """setup-quiz for a quiz file sent from the page, run as the user, in a
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
        line, code, output = command(argv, user, cwd=tmp)
    return line, code, output, csv


def shell_word(arg):
    """An argument as a shell would need it typed, a --key=value's quotes on the value."""
    if re.fullmatch(r"[\w.=:/-]+", arg):
        return arg
    quote = lambda s: "'" + s.replace("'", "'\\''") + "'"
    m = re.fullmatch(r"(--[\w-]+=)(.*)", arg, re.S)
    return m.group(1) + quote(m.group(2)) if m else quote(arg)


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

    def user(self):
        """Who the request acts as, or Refused: addressed to the name served, from
        a form of this page if a browser says where it came from, and, behind
        Access, carrying a token Access signed for someone web.ini lists."""
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
            return None
        email, settings = self.access.who(self.headers)
        return dict(settings, email=email)

    def do_GET(self):
        try:
            user = self.user()
        except (Refused, OSError, ValueError, B.Failed) as e:
            return self.reply(403, REFUSED.format(why=html.escape(str(e))))
        if urllib.parse.urlsplit(self.path).path != "/":
            return self.reply(404, REFUSED.format(why="Nothing is at that address."))
        self.reply(200, page(user))

    def do_POST(self):
        try:
            user = self.user()
        except (Refused, OSError, ValueError, B.Failed) as e:
            return self.reply(403, REFUSED.format(why=html.escape(str(e))))
        where = urllib.parse.urlsplit(self.path).path
        if where not in ("/copy-quiz", "/setup-quiz"):
            return self.reply(404, REFUSED.format(why="Nothing is at that address."))
        size = int(self.headers.get("Content-Length") or 0)
        if size > 2_000_000:
            return self.reply(413, REFUSED.format(why="That is more than a quiz file; nothing was run."))
        fields = urllib.parse.parse_qs(self.rfile.read(size).decode("utf-8"), keep_blank_values=True)
        form = {k: (v if k == "to" else v[0]) for k, v in fields.items()}
        if not secrets.compare_digest(form.get("token", ""), TOKEN):
            return self.reply(403, REFUSED.format(why="That form is from an earlier run of this page, or from "
                                                      "somewhere else. Reload the page and try again."))
        slot = "made" if where == "/setup-quiz" else "copied"
        try:
            if where == "/setup-quiz":
                line, code, output, csv = run_setup(form, user)
            else:
                (line, code, output), csv = run_copy(form, [label for label, _, _ in known(user)[1]], user), None
        except ValueError as e:
            result = f'<section class="result"><h2 class="bad">Not run</h2><p>{html.escape(str(e))}</p></section>'
            return self.reply(200, page(user, form, **{slot: result}))
        except subprocess.TimeoutExpired:
            result = ('<section class="result"><h2 class="bad">Still running after half an hour</h2>'
                      "<p>Look in Brightspace before trying again.</p></section>")
            return self.reply(200, page(user, form, **{slot: result}))
        action = form.get("action")
        if where == "/copy-quiz":
            verdict = "Stopped" if code else "Copied" if action == "copy" else "Checked: nothing sent"
        elif code:
            verdict = "Not what the file says, or not compared: below is why" if action == "check" else "Stopped"
        else:
            verdict = {"go": "Made", "check": "Brightspace has what the file says"}.get(action, "Checked: nothing sent")
        download = ""
        if csv and not code:
            data = base64.b64encode(csv[1].encode("utf-8")).decode()
            download = (f'<p><a download="{html.escape(csv[0])}" href="data:text/csv;charset=utf-8;base64,{data}">'
                        f"Download {html.escape(csv[0])}</a>, the questions for the Question Library's "
                        "Import, made from the file.</p>")
        result = (f'<section class="result"><h2 class="{"ok" if code == 0 else "bad"}">{verdict}</h2>'
                  f"<p><code>{html.escape(line)}</code></p>{download}<pre>{html.escape(output)}</pre></section>")
        self.reply(200, page(user, form, **{slot: result}))


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
