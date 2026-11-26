#!/usr/bin/env python3
"""The quiz settings the API has no route for, set through the web page the way
a hand does: headless Chromium, driven over the DevTools protocol with the
session brightspace.py keeps, one verb per setting. Each verb opens the quiz's
editor, prints what is there, and with --go changes it and reads it back from
a fresh load; without --go nothing is written.

    bs-drive.py header   COURSE QUIZ [--header FILE | --header-html HTML] [--footer FILE | --footer-html HTML] [--json] [--go]
    bs-drive.py visible  COURSE QUIZ [--hidden] [--go]
    bs-drive.py lockdown COURSE QUIZ --domains 'a.org, b.edu' [--off] [--[no-]monitor] [--[no-]second-camera] [--[no-]proctored-lab] [--lockdown-password PW]
                         [--[no-]lock-in] [--calculator standard|scientific|none]
                         [--monitor-steps LIST] [--face-detection LIST] [--live-proctoring SYSTEM --live-code CODE] [--go]
    bs-drive.py special-access COURSE QUIZ --user 'First Last' [--only] [--go]
    bs-drive.py import   COURSE FILE.csv --section NAME [--go]
    bs-drive.py blanks   COURSE FILE.yml --section NAME [--go]
    bs-drive.py shuffle  COURSE FILE.yml --section NAME [--go]
    bs-drive.py add      COURSE QUIZ FILE.csv --section NAME [--go]
    bs-drive.py shot     COURSE QUIZ FILE.png

COURSE is a label from ~/.config/brightspace/courses.ini or an org unit id;
QUIZ is a quiz id or its exact name. Needs chromium on PATH and python3 with
websocket-client (nix-shell -p chromium python3Packages.websocket-client).
The README beside this file says what each page looks like underneath and
what was learned driving it.
"""
import argparse, base64, configparser, importlib.util, json, os, pathlib, re, shutil, subprocess, sys, tempfile, time, urllib.request

try:
    import websocket
except ImportError:
    sys.exit("bs-drive needs websocket-client: nix-shell -p chromium python3Packages.websocket-client")

HOST = os.environ.get("BS_HOST", "commonwealthu.brightspace.com")
STATE = pathlib.Path(os.environ.get("BRIGHTSPACE_STATE") or
                     pathlib.Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser() / "brightspace")
CONFIG = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "brightspace"
HERE = pathlib.Path(__file__).resolve().parent
BRIGHTSPACE_PY = HERE.parent.parent / "brightspace.py"


# ---------------------------------------------------------------- the browser

class Browser:
    """Headless Chromium with a throwaway profile, spoken to over one page's
    DevTools socket. The profile is removed with the browser."""

    def __init__(self, width=1400, height=1200):
        self.profile = tempfile.mkdtemp(prefix="bs-drive-")
        self.proc = subprocess.Popen(
            ["chromium", "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
             f"--window-size={width},{height}", f"--user-data-dir={self.profile}",
             "--remote-debugging-port=0", "--remote-allow-origins=*", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=open(pathlib.Path(self.profile) / "chromium.err", "w"),
            # A Nix-built Chromium finds its libraries by its own paths; a desktop session's
            # LD_LIBRARY_PATH (a newer alsa-lib, on 2026-11-18) overrides them and it dies at load.
            env={k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"})
        port_file = pathlib.Path(self.profile) / "DevToolsActivePort"
        for _ in range(150):
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.2)
        else:
            err = (pathlib.Path(self.profile) / "chromium.err").read_text(errors="replace").strip().splitlines()
            self.close(); sys.exit("chromium did not start: " + (err[-1] if err else "is it on PATH?"))
        port = port_file.read_text().split()[0]
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
        page = next(t for t in targets if t["type"] == "page")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True)
        self.ws.settimeout(90)
        self.n = 0
        for d in ("Page", "Runtime", "Network", "DOM"):
            self.send(f"{d}.enable")

    def send(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    def cookies(self, pairs):
        for name, value in pairs:
            self.send("Network.setCookie", name=name, value=value, domain=HOST, path="/", secure=True, httpOnly=True)

    def goto(self, url, settle=3.0):
        self.send("Page.navigate", url=url)
        for _ in range(120):
            if self.eval("document.readyState") == "complete":
                break
            time.sleep(0.5)
        time.sleep(settle)

    def eval(self, js):
        r = self.send("Runtime.evaluate", expression=js, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", str(r["exceptionDetails"])))
        return r.get("result", {}).get("value")

    def shot(self, path):
        r = self.send("Page.captureScreenshot", format="png")
        pathlib.Path(path).write_bytes(base64.b64decode(r["data"]))
        return path

    def click_xy(self, x, y):
        for t in ("mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", clickCount=1)
        time.sleep(0.5)

    def insert(self, text):
        self.send("Input.insertText", text=text)

    def key(self, key, code=None, keycode=None):
        """One key pressed and released, Enter by default the way a chip input wants it."""
        p = {"key": key, "code": code or key, "windowsVirtualKeyCode": keycode or {"Enter": 13, "Tab": 9}.get(key, 0)}
        if key == "Enter":
            p["text"] = "\r"
        self.send("Input.dispatchKeyEvent", type="keyDown", **p)
        p.pop("text", None)
        self.send("Input.dispatchKeyEvent", type="keyUp", **p)
        time.sleep(0.3)

    def frame_urls(self, needle):
        return [t["url"] for t in self.send("Target.getTargets")["targetInfos"] if t["type"] == "iframe" and needle in t["url"]]

    def close(self):
        try:
            self.send("Browser.close")
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except Exception:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


# Helpers installed into the page: D2L's editor is web components behind shadow
# roots, and its dialogs load same-origin iframes, nested; so a lookup walks
# shadow roots (deepAll), every frame (findAll), and reports a click point in
# the top window's coordinates (absOf), which is what the mouse event takes.
HELPERS = r"""
window.deepAll = (sel, root = document) => {
  const out = [];
  const walk = (node) => {
    if (node.querySelectorAll) for (const e of node.querySelectorAll(sel)) out.push(e);
    const kids = node.querySelectorAll ? node.querySelectorAll('*') : [];
    for (const e of kids) if (e.shadowRoot) walk(e.shadowRoot);
  };
  walk(root);
  return out;
};
window.deep = (sel, root = document) => deepAll(sel, root)[0] || null;
window.rectOf = (el) => { const r = el.getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height}; };
window.textsOf = (sel) => deepAll(sel).map(e => (e.innerText || e.textContent || '').trim()).filter(Boolean);
window.allDocs = () => { const out = [document]; const walk = (d) => { for (const f of deepAll('iframe', d)) { try { if (f.contentDocument && f.contentDocument.body) { out.push(f.contentDocument); walk(f.contentDocument); } } catch (e) {} } }; walk(document); return out; };
window.findAll = (pred) => { const out = []; for (const d of allDocs()) for (const e of deepAll('*', d)) if (pred(e)) out.push(e); return out; };
window.absOf = (e) => { let r = e.getBoundingClientRect(); let x = r.x + r.width / 2, y = r.y + r.height / 2; let w = e.ownerDocument.defaultView;
  while (w && w !== window.top && w.frameElement) { const fr = w.frameElement.getBoundingClientRect(); x += fr.x; y += fr.y; w = w.parent; } return {x, y, w: r.width, h: r.height}; };
window.labelOf = (e) => ((e.getAttribute && e.getAttribute('aria-label')) || (e.labels && e.labels[0] ? e.labels[0].textContent : '') || '').trim();
window.textOf = (e) => (labelOf(e) || e.innerText || e.textContent || e.value || '').trim();
window.visible = (e) => e.getBoundingClientRect().width > 0 || (e.labels && e.labels[0]);
true
"""

LDB_SUFFIX = "- Requires Respondus LockDown Browser"
# Respondus renames a quiz it guards, and renames it again as the settings change:
# "Quiz 9- Requires Respondus LockDown Browser", and with Monitor "Quiz 9 - Requires
# Respondus LockDown Browser + Webcam" (2026-11-24). A quiz is known by the name
# with that taken off.
RESPONDUS_SUFFIX = re.compile(r"\s*-\s*Requires Respondus LockDown Browser(\s*\+\s*Webcam)?\s*$")


def base_name(name):
    return RESPONDUS_SUFFIX.sub("", name or "").strip()


# A dashboard row is a quiz's whole name, whichever suffix it has, then its LockDown
# column, Required or Not Required: so Quiz 1 is never Quiz 10's row.
ROW_JS = r"""((t, base) => { const esc = base.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp('^' + esc + '(\\s*-\\s*Requires Respondus LockDown Browser(\\s*\\+\\s*Webcam)?)?\\s+(Not )?Required\\b').test(t); })"""
EDITOR = "https://{host}/d2l/lms/quizzing/admin/modify/quiz_newedit_properties.d2l?qi={qi}&ou={ou}"
RESPONDUS = "https://{host}/d2l/lms/quizzing/admin/modify/quiz_newedit_respondus.d2l?ou={ou}"


class Page:
    """One editor page: the browser, the helpers, and clicks by what a thing
    says, searched in every frame, the innermost last so a dialog wins."""

    def __init__(self, b):
        self.b = b

    def helpers(self):
        """Install the helpers, again after every navigation: a new document
        has none of them, and a click that loads a page is a navigation."""
        self.b.eval(HELPERS)

    def js(self, code):
        if self.b.eval("typeof findAll") != "function":
            self.helpers()
        return self.b.eval(code)

    def find(self, sel, text, exact=True, last=True):
        test = f"t === {json.dumps(text)}" if exact else f"t.includes({json.dumps(text)})"
        pick = "es[es.length - 1]" if last else "es[0]"
        return self.js(f"""(() => {{
          const es = findAll(e => e.matches && e.matches({json.dumps(sel)}) && visible(e) && (() => {{ const t = textOf(e); return {test}; }})());
          if (!es.length) return null; const e = {pick};
          let target = e; if (e.tagName === 'INPUT' && e.getBoundingClientRect().width < 4 && e.labels && e.labels[0]) target = e.labels[0];
          target.scrollIntoView({{block: 'center'}}); return absOf(target); }})()""")

    def click(self, sel, text, settle=3, exact=True, last=True, must=True):
        r = self.find(sel, text, exact, last)
        if not r:
            if must:
                raise SystemExit(f"nothing on the page says {text!r} (looked in {sel})")
            return None
        self.b.click_xy(r["x"], r["y"])
        time.sleep(settle)
        return r

    def click_pred(self, pred_js, settle=3, must=True, what=""):
        r = self.js(f"""(() => {{ const es = findAll({pred_js}).filter(e => visible(e)); if (!es.length) return null;
                            const e = es[0]; e.scrollIntoView({{block: 'center'}}); return absOf(e); }})()""")
        if not r:
            if must:
                raise SystemExit(f"nothing on the page matches {what or pred_js}")
            return None
        self.b.click_xy(r["x"], r["y"])
        time.sleep(settle)
        return r


# ------------------------------------------------------------ course and quiz

def load_cookies(path=STATE / "cookies.txt"):
    """The session cookies out of brightspace.py's LWP cookie jar."""
    if not pathlib.Path(path).exists():
        sys.exit(f"no session at {path}: run brightspace.py session first")
    pairs = []
    for line in pathlib.Path(path).read_text().splitlines():
        if line.startswith("Set-Cookie3:"):
            first = line[len("Set-Cookie3:"):].strip().split(";")[0]
            name, _, value = first.partition("=")
            pairs.append((name.strip(), value.strip().strip('"')))
    if not pairs:
        sys.exit(f"{path} holds no cookies")
    return pairs


def brightspace_module():
    spec = importlib.util.spec_from_file_location("brightspace", BRIGHTSPACE_PY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def session_cookies():
    """The cookies of the session to act as: with $BRIGHTSPACE_SESSION, that
    [name]'s entry in the keep-alive file, read by brightspace.py's own parser,
    as brightspace.py acts for it; without, the jar `brightspace.py session`
    fills."""
    named = os.environ.get("BRIGHTSPACE_SESSION")
    if not named:
        return load_cookies()
    bp = brightspace_module()
    try:
        entries = bp.keepalive_entries(bp.KEEPALIVE_FILE)
    except bp.Failed as e:
        sys.exit(str(e))
    for name, state, cookies in entries:
        if name == named:
            if cookies:
                return [(c, cookies[c]) for c in bp.SESSION_COOKIES]
            return load_cookies(pathlib.Path(state).expanduser() / "cookies.txt")
    sys.exit(f"no session [{named}] in {bp.KEEPALIVE_FILE}")


def org_unit(course):
    """An org unit id as given, or read off the course's site for a label, the
    way brightspace.py reads it: courses.ini names the site, whose _quarto.yml
    has urls.brightspace with ou= in it."""
    path = pathlib.Path(os.environ.get("BRIGHTSPACE_COURSES") or CONFIG / "courses.ini").expanduser()
    ini = configparser.ConfigParser()
    ini.read(path)
    if course not in ini:                      # a label is often digits too, "120": the file is asked first
        if re.fullmatch(r"\d+", str(course)):
            return int(course)
        sys.exit(f"{course!r} is not an org unit id or a label in {path}")
    if ini[course].get("ou"):
        return int(ini[course]["ou"])
    site = pathlib.Path(ini[course]["site"]).expanduser()
    if not site.is_absolute():
        site = path.resolve().parent / site
    text = (site / "_quarto.yml").read_text()
    m = re.search(r"brightspace:\s*[\"']?https?://[^\s\"']*?/(\d+)[\"']?\s*$", text, flags=re.M)
    if not m:
        sys.exit(f"{site / '_quarto.yml'} has no urls.brightspace ending in the org unit id")
    return int(m.group(1))


def quiz(ou, which):
    """(id, name) of a quiz given by id or by its exact name, from the API
    through brightspace.py, which also proves the session is alive."""
    out = subprocess.run([sys.executable, str(BRIGHTSPACE_PY), "--json", "quizzes", str(ou)],
                         capture_output=True, text=True)
    if out.returncode:
        sys.exit(out.stderr.strip() or "brightspace.py quizzes failed")
    data = json.loads(out.stdout)
    data = data if isinstance(data, list) else data.get("Objects", data)
    # Respondus renames a quiz it guards, "<name>- Requires Respondus LockDown Browser",
    # so the name given may be the one before that.
    for q in data:
        name = q.get("Name", "")
        if str(q.get("QuizId")) == str(which) or name == which or base_name(name) == base_name(which):
            return int(q["QuizId"]), name
    sys.exit(f"no quiz {which!r} in org unit {ou}; the names are: " + "; ".join(q.get("Name", "?") for q in data))


def session(ou, qi, width=1400, height=1200, respondus=False):
    b = Browser(width, height)
    b.cookies(session_cookies())
    if respondus:
        b.goto(RESPONDUS.format(host=HOST, ou=ou), settle=10)
        frames = b.frame_urls("respondus")
        if not frames:
            b.close(); sys.exit("the LockDown tab did not load its Respondus dashboard")
        b.goto(frames[0], settle=25)
    else:
        b.goto(EDITOR.format(host=HOST, qi=qi, ou=ou), settle=6)
    p = Page(b)
    p.helpers()
    title = b.eval("document.title") or ""
    if "Edit Quiz" not in title and not respondus:
        b.close(); sys.exit(f"the editor did not open (page title {title!r}): is the session alive?")
    return b, p


def save_page(p):
    p.click("button, d2l-button", "Save", settle=6)


# ------------------------------------------------------------------- the verbs

def same_html(a, b):
    """Whether two pieces of HTML are the same. The quiz editor keeps the text
    and the tags and rewrites the rest: <br /> comes back <br>, & as &amp;,
    font-family:monospace; as font-family: monospace;, a link with
    rel="noopener", and a newline between blocks (2026-11-25)."""
    def norm(t):
        t = re.sub(r"\s*/>", ">", re.sub(r">\s+<", "><", (t or "").replace("\n", " ").replace("&amp;", "&")))
        t = re.sub(r'\s+rel="noopener(?: noreferrer)?"', "", t)
        return re.sub(r'style="([^"]*)"', lambda m: 'style="' + re.sub(r"\s*([:;])\s*", r"\1", m[1]).strip() + '"',
                      t).strip()
    return norm(a) == norm(b)


def read_header(p):
    return p.b.eval("Object.fromEntries(deepAll('d2l-htmleditor').map(e => [e.getAttribute('label'), e.html || '']))")


def verb_header(a):
    """The header and footer, from HTML files or as HTML on the command line
    (setup-quiz's, made from a quiz file's Markdown), through the quiz editor:
    saved through the API, {OrgDefinedId} would become the saver's own id."""
    ou = org_unit(a.course); qi, name = quiz(ou, a.quiz)
    want = {}
    for key, file, html in (("Header", a.header, a.header_html), ("Footer", a.footer, a.footer_html)):
        if file and html is not None:
            sys.exit(f"--{key.lower()} and --{key.lower()}-html are two ways to give one {key.lower()}: take one")
        if file or html is not None:
            want[key] = pathlib.Path(file).read_text() if file else html
    if not want and not a.json:
        sys.exit("nothing to paste: --header, --footer, --header-html or --footer-html")
    b, p = session(ou, qi)
    try:
        p.click("button, d2l-button, d2l-button-subtle", "Timing & Display")
        p.click("button, d2l-button, d2l-button-subtle", "Manage Header and Footer")
        have = read_header(p)
        if a.json:
            print(json.dumps({k.lower(): have.get(k, "") for k in ("Header", "Footer")})); return
        for k in ("Header", "Footer"):
            print(f"{name}: {k} now {len(have.get(k, ''))} chars" + (f", would become {len(want[k])}" if k in want else ""))
        if not a.go:
            print("nothing written; add --go"); return
        # D2L's editor is TinyMCE inside a d2l-htmleditor: set both, and tell the component.
        out = b.eval(f"""(() => {{ const want = {json.dumps(want)}; const out = [];
          for (const ed of deepAll('d2l-htmleditor')) {{
            const label = ed.getAttribute('label'); if (!(label in want)) continue;
            const inst = (window.tinymce ? window.tinymce.get() : []).find(t => (ed.shadowRoot && ed.shadowRoot.contains(t.getElement())) || ed.contains(t.getElement()));
            if (inst) {{ inst.setContent(want[label]); inst.fire('change'); inst.fire('input'); }}
            ed.html = want[label];
            ed.dispatchEvent(new CustomEvent('d2l-htmleditor-update', {{bubbles: true, composed: true, detail: {{html: want[label]}}}}));
            out.push(label); }} return out; }})()""")
        if sorted(out) != sorted(want):
            sys.exit(f"the dialog has editors for {out}, not {sorted(want)}")
        time.sleep(1)
        p.click("d2l-dialog[opened] button, d2l-dialog[opened] d2l-button", "Add")   # the dialog's button says Add, not Save
        save_page(p)
    finally:
        b.close()
    b, p = session(ou, qi)
    try:
        p.click("button, d2l-button, d2l-button-subtle", "Timing & Display")
        p.click("button, d2l-button, d2l-button-subtle", "Manage Header and Footer")
        got = read_header(p)
    finally:
        b.close()
    ok = True
    for k, v in want.items():
        same = same_html(got.get(k, ""), v)
        ok &= same
        print(f"checked on the server: {k} {len(got.get(k, ''))} chars, {'identical to what was given' if same else 'DIFFERS from what was given'}")
        if not same:
            print(f"  given: {v.strip()}\n  held:  {got.get(k, '').strip()}")
    if not ok:
        sys.exit(1)


def read_visible(p):
    return p.b.eval("(() => { const e = deep('d2l-switch-visibility, d2l-switch'); return e ? e.on : null; })()")


def verb_visible(a):
    ou = org_unit(a.course); qi, name = quiz(ou, a.quiz)
    want = not a.hidden
    b, p = session(ou, qi)
    try:
        on = read_visible(p)
        if on is None:
            sys.exit("no visibility switch on the editor page")
        print(f"{name}: {'visible' if on else 'hidden'} now; wanted {'visible' if want else 'hidden'}")
        if on == want:
            print("nothing to change"); return
        if not a.go:
            print("nothing written; add --go"); return
        r = b.eval("rectOf(deep('d2l-switch-visibility, d2l-switch'))")
        b.click_xy(r["x"], r["y"]); time.sleep(2)
        save_page(p)
    finally:
        b.close()
    b, p = session(ou, qi)
    try:
        on = read_visible(p)
    finally:
        b.close()
    print(f"checked on the server: {'visible' if on else 'hidden'}")
    if on != want:
        sys.exit(1)


def respondus_row(p, name):
    """The quiz's row on the Respondus dashboard, by its exact name or the
    name with Respondus's own suffix."""
    return p.b.eval(f"""findAll(e => e.tagName === 'TR' && {ROW_JS}((e.innerText || '').replace(/\\s+/g, ' ').trim(), {json.dumps(base_name(name))}))
        .map(e => e.innerText.replace(/\\s+/g, ' ').trim().slice(0, 140))""")


def open_settings(p, name):
    pat = json.dumps(base_name(name))
    p.click_pred(f"""e => e.tagName === 'A' && e.closest('tr') && {ROW_JS}((e.closest('tr').innerText || '').replace(/\\s+/g, ' ').trim(), {pat})""",
                 settle=3, what=f"the row of {name!r} on the LockDown dashboard")
    p.click_pred("""e => e.tagName === 'A' && e.textContent.trim() === 'Settings'""", settle=8, what="the row's Settings")


# Respondus Monitor, below LockDown on the same page: one radio group, monitorEnabled,
# whose ids start monitorEnabledTrue_ (required), monitorEnabledFalse_ (not) and
# liveProctoringEnabled_ (an instructor proctoring over Zoom, Teams and the like);
# and, folded under Monitor's own Advanced Settings, the box requiring a second
# camera, a phone that scans a QR code. Read off 100's Progress Exam 1, 2026-11-24.
MONITOR = "findAll(e => e.tagName === 'INPUT' && e.name === 'monitorEnabled' && e.type === 'radio' && e.id.startsWith({}))[0]"
SECOND = "findAll(e => e.tagName === 'INPUT' && e.id === 'secondCameraEnabled')[0]"
# The startup sequence's steps that can be left out, by the names the YAML gives
# them; the webcam check and face detection are always there. Face detection's
# two options: stop a student starting, and tell one during the exam, when the
# face cannot be seen. Live proctoring's video systems, as its menu names them.
STEPS = {"instructions": "ADDITIONAL_INSTRUCTIONS_ENABLED", "guidelines": "VIDEO_GUIDELINES_ENABLED",
         "photo": "FACE_PIC_CHECK_ENABLED", "id": "ID_CHECK_ENABLED", "environment": "ENV_CHECK_ENABLED"}
FACE = {"block": "forceFacialDetection", "notify": "notifyFacialDetectionLoss"}
SYSTEMS = {"zoom": "ZOOM", "teams": "TEAMS", "google meet": "GOOGLEMEET", "collaborate": "BBCOLLABORATE",
           "webex": "WEBEX", "other": "OTHER"}
BOX = "findAll(e => e.tagName === 'INPUT' && e.type === 'checkbox' && e.id === {})[0]"
# Monitor's "Allow this exam to additionally be delivered in a proctored lab (no
# webcam recording)", folded under its Advanced Settings: a sitting with a person
# in the room instead of the recording. Its id ends in the quiz's own key.
LAB = "findAll(e => e.tagName === 'INPUT' && e.type === 'checkbox' && e.id.startsWith('monitorOptional_'))[0]"
# LockDown Browser's own "Password to access exam", under its Password Settings: not
# the quiz's Brightspace password, which reads none beside it. Optional, until the
# proctored lab is ticked, which Respondus will not save without one: the proctor
# in the room types it in.
PASSWORD = "findAll(e => e.tagName === 'INPUT' && e.id === 'testPassword')[0]"
# Under LockDown's own Advanced Settings: "Lock students into the browser until
# exam is completed", and "Enable Calculator on the toolbar", which once ticked
# offers a Standard and a Scientific one (2026-11-26).
LOCK_IN = "findAll(e => e.tagName === 'INPUT' && e.id === 'lockIntoLDB')[0]"
CALC = "findAll(e => e.tagName === 'INPUT' && e.id === 'enableCalc')[0]"
CALC_TYPE = {"standard": "basicCalc", "scientific": "scientificCalc"}
BY_ID = "findAll(e => e.tagName === 'INPUT' && e.id === {})[0]"


def names_list(text, known, what):
    """A comma-separated list of names from known, or none for an empty list; exits on another."""
    names = [x.strip().lower() for x in (text or "").split(",") if x.strip()]
    if names == ["none"]:
        return []
    bad = [x for x in names if x not in known]
    if bad:
        sys.exit(f"{what}: {', '.join(bad)} is not one of {', '.join(known)}, or none")
    return names


def read_lockdown(p):
    p.click_pred("""e => e.tagName === 'A' && /Advanced Settings/i.test(e.textContent || '')""", settle=3, must=False)
    got = p.b.eval(f"""({{required: (findAll(e => e.tagName === 'INPUT' && e.name === 'ldbEnabled' && e.value === 'true')[0] || {{}}).checked,
                        allow: (findAll(e => e.tagName === 'INPUT' && e.id === 'allowExternalDomains')[0] || {{}}).checked,
                        domains: (findAll(e => e.tagName === 'TEXTAREA' && e.id === 'externalDomains')[0] || {{}}).value,
                        monitor: (() => {{ const e = findAll(e => e.tagName === 'INPUT' && e.name === 'monitorEnabled' && e.type === 'radio' && e.checked)[0];
                                          return !e ? null : e.id.startsWith('monitorEnabledTrue_') ? 'required'
                                                 : e.id.startsWith('liveProctoringEnabled_') ? 'live proctoring' : 'not required'; }})(),
                        second_camera: !!({SECOND} || {{}}).checked,
                        lab: !!({LAB} || {{}}).checked,
                        lock_in: !!({LOCK_IN} || {{}}).checked,
                        calculator: !({CALC} || {{}}).checked ? 'none' :
                                    (({BY_ID.format(json.dumps(CALC_TYPE["scientific"]))} || {{}}).checked ? 'scientific' : 'standard'),
                        password: ({PASSWORD} || {{}}).value || '',
                        steps: {json.dumps(STEPS)}, face: {json.dumps(FACE)},
                        system: (findAll(e => e.tagName === 'SELECT' && e.id === 'liveProctoringSystem')[0] || {{}}).value || '',
                        code: (findAll(e => e.tagName === 'INPUT' && e.id === 'liveProctoringCode')[0] || {{}}).value || '',
                        form: Object.fromEntries(findAll(e => /^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName) && (e.id || e.name) &&
                                                       !/^(hidden|submit|button|reset)$/.test(e.type))
                          .map(e => [(e.id || e.name).replace(/_[0-9a-f]{{32}}$/, ''),
                                     [e.type === 'checkbox' || e.type === 'radio' ? e.checked : e.value,
                                      ((e.labels && e.labels[0] && e.labels[0].innerText) || '').replace(/\\s+/g, ' ').trim().slice(0, 90)]]))}})""")
    # form: every control on the settings page, its id less the quiz's own key, as
    # [value, label], so that two quizzes' settings compare whole, those the
    # verb has no flag for included.
    # the boxes' ids above, turned into the names of those ticked
    for key in ("steps", "face"):
        got[key] = [n for n, i in got[key].items() if p.b.eval(f"!!({BOX.format(json.dumps(i))} || {{}}).checked")]
    got["system"] = next((n for n, v in SYSTEMS.items() if v == got["system"]), "")
    return got


def set_monitor(b, p, mode, second=None, steps=None, face=None, system=None, code=None, lab=None):
    """Proctoring on the settings page as it stands, nothing saved: mode is "required"
    (Respondus Monitor), "not required", "live" (an instructor over a video system)
    or None to leave it; with Monitor the startup steps and face detection, each a
    list of the names on, and the second camera; with live proctoring the system and
    the start code. Exits, with nothing saved, when anything will not change."""
    shown = lambda js: b.eval(f"(() => {{ const e = {js}; if (!e) return false; const r = e.getBoundingClientRect(); "
                              "return r.width > 0 && r.height > 0 && e.offsetParent !== null; })()")

    def tick(js, want, what):
        if bool(b.eval(f"({js} || {{}}).checked")) == want:
            return
        r = b.eval(f"(() => {{ const e = {js}; if (!e) return null; e.scrollIntoView({{block: 'center'}}); return absOf(e); }})()")
        if not r:
            sys.exit(f"no box for {what} on the page; nothing saved")
        b.click_xy(r["x"], r["y"]); time.sleep(1)
        if bool(b.eval(f"({js} || {{}}).checked")) != want:
            sys.exit(f"the box for {what} did not change; nothing saved")

    if mode is not None:
        prefix = {"required": "monitorEnabledTrue_", "not required": "monitorEnabledFalse_", "live": "liveProctoringEnabled_"}[mode]
        radio = MONITOR.format(json.dumps(prefix))
        if not b.eval(f"({radio} || {{}}).checked"):
            r = b.eval(f"(() => {{ const e = {radio}; if (!e) return null; e.scrollIntoView({{block: 'center'}}); return absOf(e); }})()")
            if not r:
                sys.exit("no Respondus Monitor radio on the page; nothing saved")
            b.click_xy(r["x"], r["y"]); time.sleep(2)
            # Monitor chosen on a course for the first time opens Respondus's introduction
            # over the page, ending "Continue to Respondus Monitor >>": until it is closed
            # every click lands on it. Its "Don't show this message again" is left alone.
            p.click_pred("e => /^(BUTTON|INPUT|A)$/.test(e.tagName) && /^Continue to Respondus Monitor/i.test((e.value || e.textContent || '').trim())"
                         " && e.getBoundingClientRect().width > 0", settle=2, must=False)
            if not b.eval(f"({radio} || {{}}).checked"):
                sys.exit(f"proctoring did not become {mode}; nothing saved")
    for names, boxes, what in ((steps, STEPS, "the startup step"), (face, FACE, "face detection's")):
        for name, box in boxes.items():
            if names is not None:
                tick(BOX.format(json.dumps(box)), name in names, f"{what} {name}")
    if system is not None:
        SELECT = "findAll(e => e.tagName === 'SELECT' && e.id === 'liveProctoringSystem')[0]"
        b.eval(f"""(() => {{ const e = {SELECT}; e.value = {json.dumps(SYSTEMS[system])};
                   for (const t of ['input', 'change']) e.dispatchEvent(new Event(t, {{bubbles: true}})); }})()""")
        time.sleep(1)
        if b.eval(f"({SELECT} || {{}}).value") != SYSTEMS[system]:
            sys.exit(f"the video system did not become {system}; nothing saved")
    if code is not None:
        CODE = "findAll(e => e.tagName === 'INPUT' && e.id === 'liveProctoringCode')[0]"
        r = b.eval(f"(() => {{ const e = {CODE}; e.scrollIntoView({{block: 'center'}}); e.value = ''; e.focus(); return absOf(e); }})()")
        b.click_xy(r["x"], r["y"]); b.insert(code); time.sleep(1)
        if b.eval(f"{CODE}.value") != code:
            sys.exit(f"the start code reads {b.eval(f'{CODE}.value')!r}, not {code!r}; nothing saved")
    after = ("(e.compareDocumentPosition(findAll(x => x.tagName === 'INPUT' && x.name === 'monitorEnabled')[0])"
             " & Node.DOCUMENT_POSITION_PRECEDING)")
    if lab is not None:
        # Folded under Monitor's own Advanced Settings, the link of that name after the
        # Monitor radios (LockDown's comes first).
        if not shown(LAB):
            p.click_pred(f"e => e.tagName === 'A' && (e.textContent || '').trim() === 'Advanced Settings' && "
                         f"e.getBoundingClientRect().width > 0 && {after}", settle=2, must=False)
        if not shown(LAB):
            sys.exit("the box for a proctored lab is not on the screen; nothing saved")
        if lab and not b.eval(f"({PASSWORD} || {{}}).value"):
            sys.exit("a sitting in a proctored lab needs LockDown Browser's exam password, which the proctor types "
                     "in, and Respondus will not save it without one: --lockdown-password; nothing saved")
        tick(LAB, lab, "a proctored lab")
    if second is not None:
        # Folded under Monitor's own Advanced Settings and there under Second Camera.
        for link in ("Advanced Settings", "Second Camera"):
            if shown(SECOND):
                break
            p.click_pred(f"e => e.tagName === 'A' && (e.textContent || '').trim() === {json.dumps(link)} && "
                         f"e.getBoundingClientRect().width > 0 && {after}", settle=2, must=False)
        if not shown(SECOND):
            sys.exit("the box requiring a second camera is not on the screen; nothing saved")
        tick(SECOND, second, "a second camera")


def verb_lockdown(a):
    ou = org_unit(a.course); qi, name = quiz(ou, a.quiz)
    domains = ", ".join(d.strip() for d in re.split(r"[,\n]", a.domains or "") if d.strip())
    if a.off and domains:
        sys.exit("--off takes no --domains")
    steps = None if a.monitor_steps is None else names_list(a.monitor_steps, STEPS, "--monitor-steps")
    face = None if a.face_detection is None else names_list(a.face_detection, FACE, "--face-detection")
    system = None if a.live_proctoring is None else a.live_proctoring.strip().lower()
    if system is not None and system not in SYSTEMS:
        sys.exit(f"--live-proctoring: {a.live_proctoring!r} is not one of {', '.join(SYSTEMS)}")
    monitors = a.monitor or a.second_camera or a.proctored_lab or steps is not None or face is not None
    if a.off and (monitors or system):
        sys.exit("Respondus Monitor and live proctoring run inside LockDown Browser, so --off takes neither")
    if monitors and a.monitor is False:
        sys.exit("--second-camera, --proctored-lab, --monitor-steps and --face-detection are Respondus Monitor's: "
                 "not with --no-monitor")
    if system and monitors:
        sys.exit("live proctoring is instead of Respondus Monitor: --live-proctoring takes none of Monitor's flags")
    if a.off and a.lockdown_password is not None:
        sys.exit("--lockdown-password is LockDown Browser's, so --off takes none")
    if a.off and (a.lock_in is not None or a.calculator is not None):
        sys.exit("--lock-in and --calculator are LockDown Browser's, so --off takes neither")
    if a.calculator is not None and a.calculator not in ("none", *CALC_TYPE):
        sys.exit(f"--calculator: is standard, scientific or none, not {a.calculator!r}")
    if a.live_code is not None and not system:
        sys.exit("--live-code is live proctoring's start code: it takes --live-proctoring")
    mode = "live" if system else "required" if monitors else "not required" if a.monitor is False else None
    monitor = {"required": True, "not required": False}.get(mode)
    b, p = session(ou, qi, height=1400, respondus=True)
    try:
        rows = respondus_row(p, name)
        if not rows:
            sys.exit(f"no row for {name!r} on the LockDown dashboard")
        print("row now:", rows[0])
        open_settings(p, name)
        have = read_lockdown(p)
        if a.json:
            print(json.dumps(have)); return
        print(f"now: required={have['required']} external domains allowed={have['allow']} domains={have['domains']!r} "
              f"monitor={have['monitor']} second camera={have['second_camera']} proctored lab={have['lab']}"
              f" exam password={have['password']!r} lock in={have['lock_in']} calculator={have['calculator']}"
              f" steps={','.join(have['steps']) or 'none'} face={','.join(have['face']) or 'none'}"
              + (f" system={have['system']} code={have['code']!r}" if have['monitor'] == 'live proctoring' else ""))
        if "\n" in (have["domains"] or ""):
            print("  (stored with newlines: LockDown then opens each site's first page and nothing behind it)")
        if not a.go:
            print(f"would set: required={not a.off} domains={domains!r}"
                  + (f" proctoring={mode}" if mode else "")
                  + (f" second camera={a.second_camera}" if a.second_camera is not None else "")
                  + (f" proctored lab={a.proctored_lab}" if a.proctored_lab is not None else "")
                  + (f" exam password={a.lockdown_password!r}" if a.lockdown_password is not None else "")
                  + (f" lock in={a.lock_in}" if a.lock_in is not None else "")
                  + (f" calculator={a.calculator}" if a.calculator is not None else "")
                  + (f" steps={','.join(steps) or 'none'}" if steps is not None else "")
                  + (f" face={','.join(face) or 'none'}" if face is not None else "")
                  + (f" system={system}" if system else "") + (f" code={a.live_code!r}" if a.live_code is not None else "")
                  + "; nothing written; add --go"); return
        p.click_pred(f"""e => e.tagName === 'INPUT' && e.name === 'ldbEnabled' && e.value === {json.dumps('false' if a.off else 'true')}""", settle=2, what="the Require radio")
        if not a.off:
            # On a quiz never required before, Advanced Settings appears only now,
            # folded, and its boxes are in the page but not on the screen: a click
            # on one then lands on nothing. So it is opened until the box is shown.
            ALLOW = "findAll(e => e.tagName === 'INPUT' && e.id === 'allowExternalDomains')[0]"
            shown = lambda js: b.eval(f"(() => {{ const e = {js}; if (!e) return false; const r = e.getBoundingClientRect(); "
                                      "return r.width > 0 && r.height > 0 && e.offsetParent !== null; })()")
            for _ in range(2):
                if shown(ALLOW):
                    break
                p.click_pred("""e => e.tagName === 'A' && /Advanced Settings/i.test(e.textContent || '') && e.getBoundingClientRect().width > 0""",
                             settle=3, must=False)
            if not shown(ALLOW):
                sys.exit("the box allowing other sites is not on the screen; nothing saved")
            ticked = b.eval(f"{ALLOW}.checked")
            if bool(domains) != bool(ticked):     # ticked for sites, unticked for none
                r = b.eval(f"(() => {{ const e = {ALLOW}; e.scrollIntoView({{block: 'center'}}); return absOf(e); }})()")
                b.click_xy(r["x"], r["y"]); time.sleep(2)
                if bool(b.eval(f"{ALLOW}.checked")) != bool(domains):
                    sys.exit("the box allowing other sites did not change; nothing saved")
        if not a.off and domains:
            BOX = "findAll(e => e.tagName === 'TEXTAREA' && e.id === 'externalDomains')[0]"
            if not shown(BOX):
                sys.exit("the box for the sites did not appear; nothing saved")
            r = b.eval(f"""(() => {{ const e = {BOX}; e.scrollIntoView({{block: 'center'}}); e.focus(); e.select(); return absOf(e); }})()""")
            b.click_xy(r["x"], r["y"]); time.sleep(0.5)
            b.eval("""(() => { const e = findAll(e => e.tagName === 'TEXTAREA' && e.id === 'externalDomains')[0]; e.value = ''; e.focus(); })()""")
            b.insert(domains); time.sleep(1)
            if b.eval(f"{BOX}.value").strip() != domains:
                sys.exit(f"the sites box reads {b.eval(f'{BOX}.value')!r}, not {domains!r}; nothing saved")
        def tick(js, want, what):
            if bool(b.eval(f"({js} || {{}}).checked")) == want:
                return
            r = b.eval(f"(() => {{ const e = {js}; if (!e) return null; e.scrollIntoView({{block: 'center'}}); return absOf(e); }})()")
            if not r:
                sys.exit(f"no box for {what} on the page; nothing saved")
            b.click_xy(r["x"], r["y"]); time.sleep(1)
            if bool(b.eval(f"({js} || {{}}).checked")) != want:
                sys.exit(f"the box for {what} did not change; nothing saved")
        if not a.off and a.lock_in is not None:
            tick(LOCK_IN, a.lock_in, "locking students in until the exam is completed")
        if not a.off and a.calculator is not None:
            tick(CALC, a.calculator != "none", "the calculator")
            if a.calculator != "none":
                tick(BY_ID.format(json.dumps(CALC_TYPE[a.calculator])), True, f"the {a.calculator} calculator")
        if not a.off and a.lockdown_password is not None:
            r = b.eval(f"(() => {{ const e = {PASSWORD}; if (!e) return null; e.scrollIntoView({{block: 'center'}}); "
                       "e.value = ''; e.focus(); return absOf(e); })()")
            if not r:
                sys.exit("no box for LockDown Browser's exam password on the page; nothing saved")
            b.click_xy(r["x"], r["y"]); time.sleep(0.5)
            b.eval(f"(() => {{ const e = {PASSWORD}; e.value = ''; e.focus(); }})()")
            if a.lockdown_password:
                b.insert(a.lockdown_password)
            b.eval(f"(() => {{ const e = {PASSWORD}; for (const t of ['input', 'change']) e.dispatchEvent(new Event(t, {{bubbles: true}})); }})()")
            time.sleep(1)
            if b.eval(f"{PASSWORD}.value") != a.lockdown_password:
                sys.exit(f"the exam password reads {b.eval(f'{PASSWORD}.value')!r}, not {a.lockdown_password!r}; nothing saved")
        if not a.off and mode:
            set_monitor(b, p, mode, a.second_camera, steps, face, system, a.live_code, a.proctored_lab)
        p.click_pred("""e => (e.tagName === 'INPUT' || e.tagName === 'BUTTON' || e.tagName === 'A') && /Save \\+ Close/.test(e.value || e.textContent || '')""", settle=10, what="Save + Close")
        print("row after save:", (respondus_row(p, name) or ["?"])[0])
    finally:
        b.close()
    b, p = session(ou, qi, height=1400, respondus=True)
    try:
        print("checked on the server, row:", (respondus_row(p, name) or ["?"])[0])
        open_settings(p, name)
        got = read_lockdown(p)
    finally:
        b.close()
    print(f"checked on the server: required={got['required']} external domains allowed={got['allow']} domains={got['domains']!r} "
          f"monitor={got['monitor']} second camera={got['second_camera']} proctored lab={got['lab']}"
          f" exam password={got['password']!r} lock in={got['lock_in']} calculator={got['calculator']}"
          f" steps={','.join(got['steps']) or 'none'} face={','.join(got['face']) or 'none'}"
          + (f" system={got['system']} code={got['code']!r}" if got['monitor'] == 'live proctoring' else ""))
    ok = (got["required"] is (not a.off)) and (a.off or (got["domains"] == domains and got["allow"] if domains
                                                    else not got["allow"]))
    if not a.off and mode:
        ok &= got["monitor"] == {"live": "live proctoring"}.get(mode, mode)
    if not a.off and a.proctored_lab is not None:
        ok &= got["lab"] is a.proctored_lab
    if not a.off and a.lockdown_password is not None:
        ok &= got["password"] == a.lockdown_password
    if not a.off and a.lock_in is not None:
        ok &= got["lock_in"] is a.lock_in
    if not a.off and a.calculator is not None:
        ok &= got["calculator"] == a.calculator
    if not a.off and steps is not None:
        ok &= sorted(got["steps"]) == sorted(steps)
    if not a.off and face is not None:
        ok &= sorted(got["face"]) == sorted(face)
    if not a.off and system:
        ok &= got["system"] == system
    if not a.off and a.live_code is not None:
        ok &= got["code"] == a.live_code
    if not a.off and a.second_camera is not None:
        ok &= got["second_camera"] is a.second_camera
    if not ok:
        sys.exit(1)


def open_special_access(p):
    p.click("button, d2l-button, d2l-button-subtle", "Availability Dates & Conditions", settle=2)
    p.click("button, d2l-button, d2l-button-subtle", "Manage Special Access", settle=8)


def read_special_access(p, user):
    radios = p.b.eval("""findAll(e => e.tagName === 'INPUT' && e.type === 'radio' && /spType/.test(e.id + e.name)).map(e => ({id: e.id, checked: e.checked, label: e.labels && e.labels[0] ? e.labels[0].textContent.trim().slice(0, 70) : ''}))""")
    rows = p.b.eval(f"""findAll(e => (e.tagName === 'TR' || e.tagName === 'LI') && (e.innerText || '').includes({json.dumps(user)})).map(e => e.innerText.replace(/\\s+/g, ' ').trim().slice(0, 140))""")
    return radios, rows


def verb_special_access(a):
    ou = org_unit(a.course); qi, name = quiz(ou, a.quiz)
    user = a.user.strip()
    b, p = session(ou, qi)
    try:
        open_special_access(p)
        radios, rows = read_special_access(p, user)
        print(f"{name}: access is " + "; ".join(f"{'[x]' if r['checked'] else '[ ]'} {r['label'] or r['id']}" for r in radios))
        print(f"{user!r} on the special access list: {'yes' if rows else 'no'}")
        if rows and not a.only:
            print("nothing to change"); return
        if not a.go:
            print("nothing written; add --go"); return
        if a.only:
            p.click_pred("""e => e.tagName === 'INPUT' && e.type === 'radio' && /restrict/i.test(e.id + e.value)""", settle=2, what="the only-users-with-special-access choice")
        if not rows:
            p.click("button, d2l-button, input[type=button], a", "Add Users to Special Access", settle=8)
            box = p.find("input[type=checkbox]", f"Select {user}")
            if not box:
                names = b.eval("""findAll(e => e.tagName === 'INPUT' && e.type === 'checkbox' && /^Select /.test(labelOf(e))).map(e => labelOf(e).slice(7))""")
                sys.exit(f"no {user!r} on the users page; it lists: {', '.join(names) or 'nobody'}. Nothing written.")
            b.click_xy(box["x"], box["y"]); time.sleep(2)
            checked = b.eval(f"""findAll(e => e.tagName === 'INPUT' && e.type === 'checkbox' && labelOf(e) === {json.dumps('Select ' + user)}).map(e => e.checked)""")
            if checked != [True]:
                sys.exit(f"the box for {user!r} did not tick ({checked}); nothing saved")
            p.click("button, d2l-button, input[type=submit], input[type=button]", "Save", settle=6)
        p.click("button, d2l-button, input[type=submit], input[type=button]", "Save and Close", settle=6)
        save_page(p)
    finally:
        b.close()
    b, p = session(ou, qi)
    try:
        open_special_access(p)
        radios, rows = read_special_access(p, user)
    finally:
        b.close()
    print("checked on the server: " + "; ".join(f"{'[x]' if r['checked'] else '[ ]'} {r['label'] or r['id']}" for r in radios))
    print(f"checked on the server: {user!r} on the list: {'yes' if rows else 'NO'}")
    if not rows:
        sys.exit(1)


# ------------------------------------------------------------ the Question Library

LIBRARY = "https://{host}/d2l/lms/qc/main_frame.d2l?ctid=1&ti=51000&ou={ou}"


# The library's upload dialog keeps its file input in a closed shadow root, which no
# script in the page can reach; the DevTools DOM domain can, with pierce. A node id
# lasts until the next getDocument, so each search is used at once.
def dom_find(b, query):
    b.send("DOM.getDocument", depth=-1, pierce=True)
    r = b.send("DOM.performSearch", query=query, includeUserAgentShadowDOM=True)
    ids = (b.send("DOM.getSearchResults", searchId=r["searchId"], fromIndex=0, toIndex=r["resultCount"])["nodeIds"]
           if r["resultCount"] else [])
    b.send("DOM.discardSearchResults", searchId=r["searchId"])
    return ids


def dom_text(b, nid):
    try:
        obj = b.send("DOM.resolveNode", nodeId=nid)["object"]["objectId"]
        r = b.send("Runtime.callFunctionOn", objectId=obj, returnByValue=True, functionDeclaration=
                   "function () { const e = this.nodeType === 3 ? this.parentNode : this;"
                   " return ((e && (e.innerText || e.textContent)) || '').trim(); }")
        return r["result"].get("value") or ""
    except RuntimeError:
        return ""


def dom_center(b, nid):
    try:
        q = b.send("DOM.getBoxModel", nodeId=nid)["model"]["content"]
    except RuntimeError:
        return None
    return ((q[0] + q[4]) / 2, (q[1] + q[5]) / 2) if q[4] - q[0] > 0 else None


def dom_wait(b, query, secs=60, shown=True):
    """The first node matching query, shown on the screen unless shown is False,
    looked for once a second until secs have passed."""
    end = time.time() + secs
    while time.time() < end:
        for nid in dom_find(b, query):
            if not shown or dom_center(b, nid):
                return nid
        time.sleep(1)
    return None


# The list frame's rows: each one's name and type, a section's type being "-".
ROWS = r"""(() => { const out = [];
  for (const d of allDocs().filter(d => /main_frame_list/.test((d.location || {}).href || ''))) {
    let ni = -1, ti = -1;
    for (const tr of deepAll('tr', d)) {
      const cells = [...tr.cells].map(c => (c.innerText || '').replace(/\s+/g, ' ').trim());
      if (ni < 0) { ni = cells.indexOf('Name'); ti = cells.indexOf('Type'); continue; }
      const a = [...tr.querySelectorAll('a')].find(a => (a.textContent || '').trim());
      if (a && ti >= 0 && cells.length > ti) out.push({name: a.textContent.trim(), type: cells[ti]});
    } }
  return out; })()"""


def library(ou, section, create=False, go=False):
    """A browser on the Question Library with the section open, made first when
    create and go say so. Returns (browser, page), or (None, None) when the
    section is missing and nothing may be written."""
    b = Browser(1400, 1600)
    b.cookies(session_cookies())
    b.goto(LIBRARY.format(host=HOST, ou=ou), settle=8)
    p = Page(b)
    if "Question Library" not in (b.eval("document.title") or ""):
        b.close(); sys.exit("the Question Library did not open: is the session alive?")
    names = [r["name"] for r in p.js(ROWS) if r["type"] == "-" and not r["name"].startswith(STAGING)]
    if section not in names:
        if not create:
            b.close(); sys.exit(f"no section {section!r} in the library; it has: {', '.join(names) or 'none'}")
        if not go:
            print(f"no section {section!r} yet; --go makes it")
            b.close(); return None, None
        p.click("button, d2l-button, d2l-dropdown-button", "New", settle=3)
        p.click("d2l-menu-item", "Section", settle=8)
        r = p.js("""(() => { const e = findAll(e => e.tagName === 'INPUT' && e.name === 'section.name')[0]; if (!e) return null;
                    e.scrollIntoView({block: 'center'}); e.focus(); return absOf(e); })()""")
        if not r:
            b.close(); sys.exit("the new section's form did not open")
        b.click_xy(r["x"], r["y"]); b.insert(section); time.sleep(1)
        p.click("button, d2l-button", "Save", settle=8)
        names = [r["name"] for r in p.js(ROWS) if r["type"] == "-"]
        if section not in names:
            b.close(); sys.exit(f"section {section!r} was not made; the library has: {', '.join(names)}")
        print(f"made section {section!r}")
    p.click("a", section, settle=8)            # the list's link, not the tree's: the last one on the page
    if not p.js(f"findAll(e => /^H[12]$/.test(e.tagName) && textOf(e) === {json.dumps(section)}).length"):
        b.close(); sys.exit(f"section {section!r} did not open")
    return b, p


STAGING = "TemporaryQuestionsImportSection_"


def cancel(b):
    """The upload dialog's Cancel, which an upload that is not imported needs: the
    upload has already put its questions in a section of its own, named STAGING
    and a GUID, and only Import All or Cancel takes them out of it."""
    for nid in dom_find(b, "d2l-button"):
        if dom_text(b, nid) == "Cancel" and dom_center(b, nid):
            b.click_xy(*dom_center(b, nid)); time.sleep(5); return


def csv_titles(path):
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        return [row[1] for row in csv.reader(f) if len(row) > 1 and row[0] == "Title"]


def verb_import(a):
    ou = org_unit(a.course)
    path = pathlib.Path(a.file).resolve()
    titles = csv_titles(path)
    if not titles:
        sys.exit(f"{a.file} has no Title rows: is it a question-import CSV?")
    if len(set(titles)) != len(titles):
        sys.exit(f"{a.file} has two questions of one title; the readback goes by title")
    b, p = library(ou, a.section, create=True, go=a.go)
    if b is None:
        print(f"would import {len(titles)} questions: {'; '.join(titles)}"); return
    try:
        rows = p.js(ROWS)
        have = [r["name"] for r in rows if r["type"] != "-"]
        staged = [r["name"] for r in rows if r["name"].startswith(STAGING)]
        print(f"{a.section}: {len(have)} questions now")
        if staged:
            print(f"  and {len(staged)} section(s) an upload left behind without importing: {', '.join(staged)}")
        dup = [t for t in titles if t in have]
        if dup:
            sys.exit("already in the section, and an import would make a second copy: " + "; ".join(dup)
                     + ". Delete those in the library first.")
        print(f"would import {len(titles)} questions: {'; '.join(titles)}")
        if not a.go:
            print("nothing written; add --go"); return
        p.click("button, d2l-button, d2l-button-subtle, d2l-dropdown-button, d2l-dropdown-button-subtle", "Import")
        p.click("d2l-menu-item", "Upload a File", settle=2)
        nid = dom_wait(b, 'input[type="file"]', 60, shown=False)
        if not nid:
            sys.exit("the upload dialog did not open")
        b.send("DOM.setFileInputFiles", files=[str(path)], nodeId=nid)
        btn = dom_wait(b, "#review-import-all-button", 90)
        said = [dom_text(b, n) for n in dom_find(b, "Found")]
        found = next((int(m.group(1)) for t in said for m in [re.search(r"Found\s+(\d+)\s+question", t)] if m), None)
        if not btn:
            errs = [t for t in (dom_text(b, n) for n in dom_find(b, "rror")) if t][:3]
            cancel(b)
            sys.exit("the upload did not come back with Import All" + (": " + " / ".join(errs) if errs else ""))
        if found != len(titles):
            cancel(b)
            sys.exit(f"Brightspace found {found} questions in the file, which has {len(titles)}; nothing imported")
        btn = dom_wait(b, "#review-import-all-button", 10)     # the count's search made the old node id stale
        b.click_xy(*dom_center(b, btn))
        for _ in range(60):
            time.sleep(1)
            if not dom_find(b, "#review-import-all-button"):
                break
        time.sleep(3)
    finally:
        b.close()
    b, p = library(ou, a.section)
    try:
        rows = p.js(ROWS)
        got = {r["name"]: r["type"] for r in rows if r["type"] != "-"}
        staged = [r["name"] for r in rows if r["name"].startswith(STAGING)]
    finally:
        b.close()
    missing = [t for t in titles if t not in got]
    print(f"checked on the server: {a.section} holds {len(got)} questions; "
          + (f"MISSING {'; '.join(missing)}" if missing else f"all {len(titles)} imported"))
    if staged:
        print(f"  and a staging section is still there: {', '.join(staged)}")
    if missing:
        sys.exit(1)


# One blank's answers, read off its tag field: the input's label says how many and
# which blank, and the tags before it are the answers.
BLANKS = r"""findAll(e => e.tagName === 'INPUT' && /Answers for blank \d+ of \d+/.test(labelOf(e))).map(e => {
  const m = labelOf(e).match(/(\d+) Answers for blank (\d+) of (\d+)/);
  return {blank: +m[2], of: +m[3], answers: [...e.parentElement.children].filter(c => c !== e)
    .map(c => (c.innerText || c.textContent || '').trim()).filter(Boolean)}; })"""


def yaml_quiz():
    spec = importlib.util.spec_from_file_location("bs_yaml_quiz", HERE.parent / "bs-yaml-quiz" / "bs-yaml-quiz.py")
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except ImportError as e:
        sys.exit(f"blanks reads the quiz with bs-yaml-quiz, which needs {e.name}: nix-shell -p python3Packages.pyyaml")
    return m


# A blank's answer type, as the editor's button names it: "Answer Type for Blank 2,
# Regular Expression Selected". Every answer of a blank is of its type, so a
# blank is a regular expression only when all its answers are.
BLANK_TYPES = r"""Object.fromEntries(findAll(e => e.tagName === 'BUTTON' && /Answer Type for Blank \d+, .* Selected/.test(e.innerText || ''))
  .map(e => { const m = (e.innerText || '').match(/Answer Type for Blank (\d+), (.*) Selected/); return [m[1], m[2].trim()]; }))"""


def wanted_blanks(path):
    """[(title, [([answer, ...], type) per blank])] for every MSA question in a quiz
    file, the answers as the CSV would write them and the type "Text" or "Regular
    Expression". bs-yaml-quiz has refused what a blank cannot hold, part credit
    and a blank of both kinds; the checks here stay for a file that reaches this
    verb some other way."""
    yq = yaml_quiz()
    try:
        data = yq.load(pathlib.Path(path).read_text())
        out = []
        for n, q in enumerate(data["questions"], 1):
            if not (isinstance(q, dict) and q.get("type") == "MSA"):
                continue
            where = f"{path}, question {n}"
            blanks = []
            for i, (_, answers) in enumerate(yq.msa_blanks(q, where), 1):
                rows = yq.answer_rows(answers, f"{where}, blank {i}")
                regexp = {bool(r[3]) for r in rows}
                if [r for r in rows if r[1] != "100"] or len(regexp) > 1:
                    sys.exit(f"{where}, blank {i}: part credit, or regular expressions beside plain answers, "
                             "which a blank cannot hold")
                blanks.append(([r[2] for r in rows], "Regular Expression" if regexp == {True} else "Text"))
            out.append((q.get("title"), blanks))
    except yq.Failed as e:
        sys.exit(str(e))
    return out


def open_question(ou, section, title):
    b, p = library(ou, section)
    names = [r["name"] for r in p.js(ROWS) if r["type"] != "-"]
    if names.count(title) != 1:
        b.close(); sys.exit(f"{section!r} holds {names.count(title)} questions titled {title!r}; import it first, once")
    p.click("a", title, settle=10)
    if b.eval("document.title") != "Question Editor":
        b.close(); sys.exit(f"{title!r} did not open in the question editor")
    return b, p


def set_type(b, p, k, kind):
    """Blank k's answer type, through its button's menu: Text, Case-Sensitive Text
    or Regular Expression. True once the button says so. The menu draws a moment
    after the click, a new blank's later than the first's, so each step waits."""
    item = (f"e => e.tagName === 'DIV' && e.children.length === 0 && (e.innerText || '').trim() === {json.dumps(kind)}"
            " && e.getBoundingClientRect().width > 0")
    p.click_pred(f"e => e.tagName === 'BUTTON' && /Answer Type for Blank {k},/.test(e.innerText || '')", settle=1,
                 what=f"blank {k}'s answer type")
    if not wait_for(p, f"findAll({item}).length", secs=10):
        return False
    # Clicked where it is: scrolling it into view first closes the menu, and the
    # click then lands on nothing (2026-11-24, a third blank's menu low on the page).
    r = p.js(f"(() => {{ const e = findAll({item})[0]; return e ? absOf(e) : null; }})()")
    if not r:
        return False
    b.click_xy(r["x"], r["y"]); time.sleep(1)
    return bool(wait_for(p, f"({BLANK_TYPES})[{json.dumps(str(k))}] === {json.dumps(kind)}", secs=10))


def verb_blanks(a):
    ou = org_unit(a.course)
    want = wanted_blanks(a.file)
    if not want:
        sys.exit(f"{a.file} has no MSA question")
    bad = False
    for title, blanks in want:
        texts = [answers for answers, _ in blanks]
        types = {str(k): kind for k, (_, kind) in enumerate(blanks, 1)}
        b, p = open_question(ou, a.section, title)
        try:
            have = [x["answers"] for x in p.js(BLANKS)]
            have_types = p.js(BLANK_TYPES)
            print(f"{title}: {len(have)} of {len(blanks)} blanks now")
            if have == texts and have_types == types:
                print("  whole already; nothing to change"); continue
            if len(have) != 1 or have[0] != texts[0]:
                print(f"  it holds {have}, not the first blank as imported, {texts[0]}: left alone"); bad = True; continue
            for k, (answers, kind) in enumerate(blanks[1:], 2):
                print(f"  would add blank {k}" + (" as a regular expression" if kind != "Text" else "")
                      + f": {', '.join(answers)}")
            if not a.go:
                print("  nothing written; add --go"); continue
            for k in range(2, len(blanks) + 1):      # every blank first: a click after typing lands on a moved button
                p.click("d2l-button, button", "Add Blank", settle=3)
            if len(p.js(BLANKS)) != len(blanks):
                sys.exit(f"Add Blank made {len(p.js(BLANKS))} blanks, not {len(blanks)}; nothing saved")
            # Every blank's type before any answer: once a blank's answer field has the
            # focus, the next click on a type button only takes it away, and the menu
            # never opens (2026-11-24, blank 3 after blank 2's answers).
            for k, (answers, kind) in enumerate(blanks, 1):
                if p.js(BLANK_TYPES).get(str(k)) != kind and not set_type(b, p, k, kind):
                    sys.exit(f"  blank {k}'s answer type did not become {kind}; nothing saved")
            for k, (answers, kind) in enumerate(blanks[1:], 2):
                r = p.js(f"""(() => {{ const e = findAll(e => e.tagName === 'INPUT' && /Answers for blank {k} of/.test(labelOf(e)))[0];
                             e.scrollIntoView({{block: 'center'}}); return absOf(e); }})()""")
                b.click_xy(r["x"], r["y"])
                for ans in answers:
                    b.insert(ans); b.key("Enter")
            typed = [x["answers"] for x in p.js(BLANKS)]
            if typed != texts:
                sys.exit(f"  the fields read {typed} before saving, not {texts}; nothing saved")
            # A mouse click on Save sends nothing here; Enter on the focused button submits the form.
            p.js("findAll(e => e.tagName === 'BUTTON' && visible(e) && textOf(e) === 'Save')[0].focus()")
            b.key("Enter"); time.sleep(8)
        finally:
            b.close()
        b, p = open_question(ou, a.section, title)
        try:
            got = [x["answers"] for x in p.js(BLANKS)]
            got_types = p.js(BLANK_TYPES)
        finally:
            b.close()
        ok = got == texts and got_types == types
        bad |= not ok
        print(f"  checked on the server: {len(got)} blanks, " + ("as the file has them" if ok else
              f"DIFFERENT: {got}, types {got_types}"))
    if bad:
        sys.exit(1)


# The editor's "Randomize answer order", the same box on an MC and an MS question.
SHUFFLE = "findAll(e => e.tagName === 'INPUT' && e.name === 'question.choiceInteraction.shuffle')"


def wait_for(p, js, secs=30):
    """js's value once it is truthy, looked for once a second until secs have passed."""
    end = time.time() + secs
    while time.time() < end:
        got = p.js(js)
        if got:
            return got
        time.sleep(1)
    return None


def shuffle_box(p, title):
    """Open title from the section's list, which the page is showing, and read
    its box: True or False, or None when the editor shows no such box."""
    p.click("a", title, settle=2)
    got = wait_for(p, f"(() => {{ const es = {SHUFFLE}; return es.length ? [es[0].checked] : null; }})()")
    return got[0] if got else None


# The page Save goes to when the question is in a quiz already: Add Existing put
# a copy there, and a box per place says which copies the change goes to, the
# library's ticked and every quiz's not. The verb ticks them all.
PLACES = "findAll(e => e.tagName === 'INPUT' && e.type === 'checkbox' && e.name === 'gridObjects_cb')"


def save_question(b, p, title):
    """Save the open question, every copy of it in a quiz included, and come back
    to the section's list. Returns the quizzes whose copies the change went to.
    Called inside a try whose finally closes the browser, as back_to_list is."""
    # A mouse click on Save sends nothing here; Enter on the focused button submits the form.
    p.js("findAll(e => e.tagName === 'BUTTON' && visible(e) && textOf(e) === 'Save')[0].focus()")
    b.key("Enter")
    listed = f"{ROWS}.some(r => r.name === {json.dumps(title)})"
    landed = wait_for(p, f"({listed} && 'list') || ({PLACES}.length && 'in use')")
    if landed == "list":
        return []
    if landed != "in use":
        sys.exit(f"{title}: Save did not go back to the section's list")
    places = p.js(f"{PLACES}.map(e => labelOf(e).replace(/^Select /, ''))")
    quizzes = [x for x in places if x != "Question Library"]
    print(f"  it is in {'; '.join(quizzes)} too, whose copies the change goes to as well")
    for k in range(len(places)):
        if not p.js(f"{PLACES}[{k}].checked"):
            r = p.js(f"(() => {{ const e = {PLACES}[{k}]; e.scrollIntoView({{block: 'center'}}); return absOf(e); }})()")
            b.click_xy(r["x"], r["y"]); time.sleep(0.5)
    if not p.js(f"{PLACES}.every(e => e.checked)"):
        sys.exit(f"{title}: not every place it is used in could be ticked; nothing saved")
    p.click_pred("e => e.tagName === 'BUTTON' && textOf(e) === 'Save' && /shared_question/.test(e.ownerDocument.location.href)",
                 settle=1, what="the Save of the page that lists where the question is used")
    if not wait_for(p, listed):
        sys.exit(f"{title}: Save did not go back to the section's list")
    return quizzes


def quiz_shuffles(ou, name):
    """{question name: whether its options are shuffled} in a quiz, read through
    the API: QuestionInfo's Randomize, on a multiple choice and a multi-select."""
    qi, _ = quiz(ou, name)
    bp = brightspace_module()
    s = bp.open_session(argparse.Namespace(base_url=None, course=None))
    r = s.api(f"/d2l/api/le/{s.versions()[0]}/{ou}/quizzes/{qi}/questions/")
    return {q.get("Name"): bool((q.get("QuestionInfo") or {}).get("Randomize"))
            for q in (r.get("Objects", []) if isinstance(r, dict) else r) if q.get("QuestionTypeId") in (1, 4)}


def back_to_list(b, p, title):
    """The editor's Cancel, which goes back to the list without saving."""
    p.click("button, d2l-button", "Cancel", settle=2)
    if not wait_for(p, f"{ROWS}.some(r => r.name === {json.dumps(title)})"):
        sys.exit(f"Cancel on {title!r} did not go back to the section's list")


def verb_shuffle(a):
    """Every MC and MS question's "Randomize answer order" as the quiz file has
    it, on unless a question says shuffle: false. One browser goes from question
    to question, since Save and Cancel both land on the section's list; a fresh
    one reads back those it changed, and the API each quiz's copy it changed:
    a question already in a quiz is changed there too."""
    ou = org_unit(a.course)
    yq = yaml_quiz()
    try:
        want = yq.shuffled(yq.load(pathlib.Path(a.file).read_text()), a.file)
    except yq.Failed as e:
        sys.exit(str(e))
    if not want:
        sys.exit(f"{a.file} has no MC or MS question")
    untitled = [str(n) for n, t, _ in want if not t]
    if untitled:
        sys.exit(f"{a.file}: question {', '.join(untitled)} has no title, which is how the library finds it")
    b, p = library(ou, a.section)
    changed = []
    try:
        names = [r["name"] for r in p.js(ROWS) if r["type"] != "-"]
        bad = [t for _, t, _ in want if names.count(t) != 1]
        if bad:
            sys.exit(f"{a.section!r} does not hold exactly one of each: {'; '.join(bad)}; import it first, once")
        for _, title, on in want:
            have = shuffle_box(p, title)
            if have is None:
                sys.exit(f"{title}: the editor shows no Randomize answer order box")
            print(f"{title}: options {'shuffled' if have else 'in order'}"
                  + ("" if have == on else f"; the file has them {'shuffled' if on else 'in order'}"))
            if have == on or not a.go:
                back_to_list(b, p, title); continue
            r = p.js(f"(() => {{ const e = {SHUFFLE}[0]; e.scrollIntoView({{block: 'center'}}); return absOf(e); }})()")
            b.click_xy(r["x"], r["y"]); time.sleep(1)
            if p.js(f"{SHUFFLE}[0].checked") != on:
                sys.exit(f"{title}: the box did not change; nothing saved")
            changed.append((title, on, save_question(b, p, title)))
    finally:
        b.close()
    if not changed:
        print("nothing written" + ("" if a.go else "; add --go")); return
    b, p = library(ou, a.section)
    bad = False
    try:
        for title, on, _ in changed:
            got = shuffle_box(p, title)
            back_to_list(b, p, title)
            bad |= got != on
            print(f"  checked on the server: {title}, options "
                  + ("shuffled" if got else "in order") + ("" if got == on else ", NOT as the file has them"))
    finally:
        b.close()
    for name in sorted({x for _, _, quizzes in changed for x in quizzes}):
        held = quiz_shuffles(ou, name)
        for title, on, quizzes in changed:
            if name in quizzes:
                got = held.get(title)
                bad |= got != on
                print(f"  checked on the server: {title} in {name}, options "
                      + ("missing" if got is None else "shuffled" if got else "in order")
                      + ("" if got == on else ", NOT as the file has them"))
    if bad:
        sys.exit(1)


# Add Existing's dialog draws its rows in closed shadow roots, so a box is found
# through the DOM domain and its row read from the box itself: the row of a
# question reads "<title> <type> <n> points", a section's "<name> Section".
BOX_ROW = """function () {
  let r = this;
  for (let i = 0; i < 8 && r; i++) {
    r = r.parentElement || (r.getRootNode && r.getRootNode().host) || null;
    if (r && (r.innerText || '').trim().split('\\n').length > 1) break;
  }
  return {id: this.id, checked: this.checked, row: r ? (r.innerText || '').replace(/\\s+/g, ' ').trim() : ''};
}"""
TYPES = ("Short Answer|Multi-Short Answer|Multiple Choice|Multi-Select|True or False|Written Response|"
         "Matching|Ordering|Fill in the Blanks|Likert|Arithmetic|Significant Figures")


def dialog_boxes(b):
    out = []
    for n in dom_find(b, 'input[type="checkbox"]'):
        try:
            obj = b.send("DOM.resolveNode", nodeId=n)["object"]["objectId"]
            out.append(b.send("Runtime.callFunctionOn", objectId=obj, returnByValue=True,
                              functionDeclaration=BOX_ROW)["result"]["value"])
        except RuntimeError:
            pass
    return out


def click_node(b, query):
    """Scroll the first node matching query into view, then click it where it is."""
    nid = dom_wait(b, query, 10, shown=False)
    if not nid:
        return False
    obj = b.send("DOM.resolveNode", nodeId=nid)["object"]["objectId"]
    b.send("Runtime.callFunctionOn", objectId=obj, functionDeclaration="function () { this.scrollIntoView({block: 'center'}); }")
    time.sleep(0.4)
    c = dom_center(b, dom_wait(b, query, 5, shown=False))
    if not c:
        return False
    b.click_xy(*c); time.sleep(0.5)
    return True


def quiz_questions(ou, qi):
    """The names of a quiz's questions, read through the API, as the session acts."""
    bp = brightspace_module()
    s = bp.open_session(argparse.Namespace(base_url=None, course=None))
    le, _ = s.versions()
    r = s.api(f"/d2l/api/le/{le}/{ou}/quizzes/{qi}/questions/")
    return [q.get("Name", "") for q in (r.get("Objects", []) if isinstance(r, dict) else r)]


def verb_add(a):
    ou = org_unit(a.course); qi, name = quiz(ou, a.quiz)
    titles = csv_titles(a.file)
    if not titles:
        sys.exit(f"{a.file} has no Title rows")
    have = quiz_questions(ou, qi)
    there = [t for t in titles if t in have]
    print(f"{name}: {len(have)} questions now")
    if len(there) == len(titles):
        print("all of the file's questions are in it already; nothing to add"); return
    if there:
        sys.exit("some are in it already, and Add Existing would add them a second time: " + "; ".join(there))
    print(f"would add {len(titles)} from section {a.section!r}: {'; '.join(titles)}")
    if not a.go:
        print("nothing written; add --go"); return
    b, p = session(ou, qi)
    try:
        p.click("d2l-button", "Add Existing", settle=3)
        p.click("d2l-menu-item", "Browse Question Library", settle=10)
        if not dom_wait(b, 'input[type="checkbox"][id^="sectionId_"]', 60):
            sys.exit("the library did not open in the dialog")
        sec = [x for x in dialog_boxes(b) if x["id"].startswith("sectionId_") and x["row"] == f"{a.section} Section"]
        if len(sec) != 1:
            sys.exit(f"{len(sec)} sections named {a.section!r} in the dialog; nothing added")
        if not click_node(b, f'button[aria-label="Expand {a.section} section"]'):
            sys.exit(f"no way to open section {a.section!r} in the dialog")
        time.sleep(5)
        rows = [x for x in dialog_boxes(b) if x["id"].startswith("questionId_")]
        want = {}
        for t in titles:
            hit = [x["id"] for x in rows if re.fullmatch(re.escape(t) + rf" ({TYPES}) [\d.]+ points?", x["row"])]
            if len(hit) != 1:
                sys.exit(f"{len(hit)} questions titled {t!r} in section {a.section!r}; nothing added")
            want[t] = hit[0]
        for t, box in want.items():
            if not click_node(b, f"#{box}"):
                sys.exit(f"the box of {t!r} could not be clicked; nothing added")
        on = sorted(x["id"] for x in dialog_boxes(b) if x["checked"] and x["id"].startswith(("questionId_", "sectionId_")))
        if on != sorted(want.values()):
            sys.exit(f"ticked {on}, not the {len(want)} questions alone; nothing added")
        imp = [n for n in dom_find(b, "button") if dom_text(b, n) == "Import" and dom_center(b, n)]
        if not imp:
            sys.exit("no Import button in the dialog; nothing added")
        b.click_xy(*dom_center(b, imp[0]))
        for _ in range(60):
            time.sleep(1)
            if not dom_find(b, 'input[type="checkbox"][id^="questionId_"]'):
                break
        time.sleep(3)
    finally:
        b.close()
    got = quiz_questions(ou, qi)
    missing = [t for t in titles if got.count(t) != 1]
    print(f"checked on the server: {name} holds {len(got)} questions; "
          + (f"NOT EXACTLY ONCE: {'; '.join(missing)}" if missing else f"each of the {len(titles)} once"))
    if missing:
        sys.exit(1)


def verb_shot(a):
    ou = org_unit(a.course); qi, name = quiz(ou, a.quiz)
    b, p = session(ou, qi)
    try:
        print(b.shot(a.file), "of", name)
    finally:
        b.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)

    def common(s):
        s.add_argument("course", help="a label from courses.ini, or an org unit id")
        s.add_argument("quiz", help="a quiz id, or its exact name")
        s.add_argument("--go", action="store_true", help="write it; without this, only read")

    s = sub.add_parser("header", help="paste the header, and the footer, as HTML"); common(s)
    s.add_argument("--header", metavar="FILE", help="the header, an HTML file")
    s.add_argument("--footer", metavar="FILE", help="the footer, an HTML file")
    s.add_argument("--header-html", dest="header_html", metavar="HTML", help="the header as HTML; '' empties it")
    s.add_argument("--footer-html", dest="footer_html", metavar="HTML", help="the footer as HTML; '' empties it")
    s.add_argument("--json", action="store_true", help="print both as the editor holds them, and write nothing")
    s.set_defaults(fn=verb_header)
    s = sub.add_parser("visible", help="the visibility switch at the foot of the editor"); common(s)
    s.add_argument("--hidden", action="store_true", help="hide it (the default makes it visible)"); s.set_defaults(fn=verb_visible)
    s = sub.add_parser("lockdown", help="require LockDown Browser, with its allowed domains"); common(s)
    s.add_argument("--domains", help="one comma-separated line, as exam.py header prints it")
    s.add_argument("--off", action="store_true", help="LockDown not required")
    s.add_argument("--monitor", action=argparse.BooleanOptionalAction,
                   help="require Respondus Monitor, or with --no-monitor not; left alone without either")
    s.add_argument("--second-camera", dest="second_camera", action=argparse.BooleanOptionalAction,
                   help="require a second camera, which is Monitor's; left alone without either")
    s.add_argument("--proctored-lab", dest="proctored_lab", action=argparse.BooleanOptionalAction,
                   help="also allow a sitting in a proctored lab, no webcam recording, which is Monitor's "
                        "and needs the exam password")
    s.add_argument("--lock-in", dest="lock_in", action=argparse.BooleanOptionalAction,
                   help="lock students into the browser until the exam is completed")
    s.add_argument("--calculator", metavar="standard|scientific|none", help="the calculator on LockDown's toolbar")
    s.add_argument("--lockdown-password", dest="lockdown_password", metavar="PASSWORD",
                   help="LockDown Browser's own password to access the exam, which the lab's proctor types in; "
                        "'' clears it")
    s.add_argument("--monitor-steps", dest="monitor_steps", metavar="'instructions, environment'",
                   help="the startup steps on, the rest off: " + ", ".join(STEPS) + "; or none")
    s.add_argument("--face-detection", dest="face_detection", metavar="'block, notify'",
                   help="face detection's options on, the rest off: block (no start), notify (during); or none")
    s.add_argument("--live-proctoring", dest="live_proctoring", metavar="SYSTEM",
                   help="an instructor proctoring over " + ", ".join(SYSTEMS) + ", instead of Monitor")
    s.add_argument("--live-code", dest="live_code", metavar="CODE", help="live proctoring's start code")
    s.add_argument("--json", action="store_true", help="print the setting as JSON and change nothing")
    s.set_defaults(fn=verb_lockdown)
    s = sub.add_parser("special-access", help="put one user on the quiz's special access list"); common(s)
    s.add_argument("--user", required=True, help="the name as the classlist shows it, 'First Last'")
    s.add_argument("--only", action="store_true", help="and allow only users with special access"); s.set_defaults(fn=verb_special_access)
    s = sub.add_parser("import", help="a question-import CSV into a section of the Question Library")
    s.add_argument("course"); s.add_argument("file", help="the CSV bs-yaml-quiz.py writes")
    s.add_argument("--section", required=True, help="made at the library's top if it is not there")
    s.add_argument("--go", action="store_true"); s.set_defaults(fn=verb_import)
    s = sub.add_parser("blanks", help="the blanks after the first of each MSA question, from its quiz YAML")
    s.add_argument("course"); s.add_argument("file", help="the quiz's YAML, whose CSV was imported with --msa-as-sa")
    s.add_argument("--section", required=True, help="the section the questions were imported into")
    s.add_argument("--go", action="store_true"); s.set_defaults(fn=verb_blanks)
    s = sub.add_parser("shuffle", help="each MC and MS question's Randomize answer order, as its quiz YAML has it")
    s.add_argument("course"); s.add_argument("file", help="the quiz's YAML; an MC or MS question is shuffled unless it says shuffle: false")
    s.add_argument("--section", required=True, help="the section the questions were imported into")
    s.add_argument("--go", action="store_true"); s.set_defaults(fn=verb_shuffle)
    s = sub.add_parser("add", help="Add Existing: a section's questions, by the CSV's titles, into a quiz"); common(s)
    s.add_argument("file", help="the CSV whose titles say which questions")
    s.add_argument("--section", required=True, help="the library section they were imported into")
    s.set_defaults(fn=verb_add)
    s = sub.add_parser("shot", help="a screenshot of the editor, for a look"); common(s)
    s.add_argument("file"); s.set_defaults(fn=verb_shot)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
