#!/usr/bin/env python3
"""The quiz settings the API has no route for, set through the web page the way
a hand does: headless Chromium, driven over the DevTools protocol with the
session brightspace.py keeps, one verb per setting. Each verb opens the quiz's
editor, prints what is there, and with --go changes it and reads it back from
a fresh load; without --go nothing is written.

    bs-drive.py header   COURSE QUIZ --header FILE [--footer FILE] [--go]
    bs-drive.py visible  COURSE QUIZ [--hidden] [--go]
    bs-drive.py lockdown COURSE QUIZ --domains 'a.org, b.edu' [--off] [--go]
    bs-drive.py special-access COURSE QUIZ --user 'First Last' [--only] [--go]
    bs-drive.py import   COURSE FILE.csv --section NAME [--go]
    bs-drive.py blanks   COURSE FILE.yml --section NAME [--go]
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
        if str(q.get("QuizId")) == str(which) or name == which or name == which + LDB_SUFFIX:
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

def read_header(p):
    return p.b.eval("Object.fromEntries(deepAll('d2l-htmleditor').map(e => [e.getAttribute('label'), e.html || '']))")


def verb_header(a):
    ou = org_unit(a.course); qi, name = quiz(ou, a.quiz)
    want = {"Header": pathlib.Path(a.header).read_text()}
    if a.footer:
        want["Footer"] = pathlib.Path(a.footer).read_text()
    b, p = session(ou, qi)
    try:
        p.click("button, d2l-button, d2l-button-subtle", "Timing & Display")
        p.click("button, d2l-button, d2l-button-subtle", "Manage Header and Footer")
        have = read_header(p)
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
        same = got.get(k, "").replace("\n", "") == v.replace("\n", "")
        ok &= same
        print(f"checked on the server: {k} {len(got.get(k, ''))} chars, {'identical to the file' if same else 'DIFFERS from the file'}")
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
    return p.b.eval(f"""findAll(e => e.tagName === 'TR' && (() => {{ const t = (e.innerText || '').replace(/\\s+/g, ' ').trim();
        return t.startsWith({json.dumps(name)} + ' ') || t.startsWith({json.dumps(name)} + '-') || t === {json.dumps(name)}; }})())
        .map(e => e.innerText.replace(/\\s+/g, ' ').trim().slice(0, 140))""")


def open_settings(p, name):
    pat = json.dumps(name)
    p.click_pred(f"""e => e.tagName === 'A' && e.closest('tr') && ((e.closest('tr').innerText || '').replace(/\\s+/g, ' ').trim().startsWith({pat}))""",
                 settle=3, what=f"the row of {name!r} on the LockDown dashboard")
    p.click_pred("""e => e.tagName === 'A' && e.textContent.trim() === 'Settings'""", settle=8, what="the row's Settings")


def read_lockdown(p):
    p.click_pred("""e => e.tagName === 'A' && /Advanced Settings/i.test(e.textContent || '')""", settle=3, must=False)
    return p.b.eval("""({required: (findAll(e => e.tagName === 'INPUT' && e.name === 'ldbEnabled' && e.value === 'true')[0] || {}).checked,
                        allow: (findAll(e => e.tagName === 'INPUT' && e.id === 'allowExternalDomains')[0] || {}).checked,
                        domains: (findAll(e => e.tagName === 'TEXTAREA' && e.id === 'externalDomains')[0] || {}).value})""")


def verb_lockdown(a):
    ou = org_unit(a.course); qi, name = quiz(ou, a.quiz)
    domains = ", ".join(d.strip() for d in re.split(r"[,\n]", a.domains or "") if d.strip())
    if a.off and domains:
        sys.exit("--off takes no --domains")
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
        print(f"now: required={have['required']} external domains allowed={have['allow']} domains={have['domains']!r}")
        if "\n" in (have["domains"] or ""):
            print("  (stored with newlines: LockDown then opens each site's first page and nothing behind it)")
        if not a.go:
            print(f"would set: required={not a.off} domains={domains!r}; nothing written; add --go"); return
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
    print(f"checked on the server: required={got['required']} external domains allowed={got['allow']} domains={got['domains']!r}")
    ok = (got["required"] is (not a.off)) and (a.off or (got["domains"] == domains and got["allow"] if domains
                                                    else not got["allow"]))
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


def wanted_blanks(path):
    """[(title, [[answer, ...] per blank])] for every MSA question in a quiz file,
    the answers as the CSV would write them."""
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
                odd = [r[2] for r in rows if r[1] != "100" or r[3]]
                if odd:
                    sys.exit(f"{where}, blank {i}: {odd} has a weight or regexp, which the blank's answer field "
                             "has no place for; add that blank by hand")
                blanks.append([r[2] for r in rows])
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


def verb_blanks(a):
    ou = org_unit(a.course)
    want = wanted_blanks(a.file)
    if not want:
        sys.exit(f"{a.file} has no MSA question")
    bad = False
    for title, blanks in want:
        b, p = open_question(ou, a.section, title)
        try:
            have = [x["answers"] for x in p.js(BLANKS)]
            print(f"{title}: {len(have)} of {len(blanks)} blanks now")
            if have == blanks:
                print("  whole already; nothing to change"); continue
            if len(have) != 1 or have[0] != blanks[0]:
                print(f"  it holds {have}, not the first blank as imported, {blanks[0]}: left alone"); bad = True; continue
            for k, answers in enumerate(blanks[1:], 2):
                print(f"  would add blank {k}: {', '.join(answers)}")
            if not a.go:
                print("  nothing written; add --go"); continue
            for k in range(2, len(blanks) + 1):      # every blank first: a click after typing lands on a moved button
                p.click("d2l-button, button", "Add Blank", settle=3)
            if len(p.js(BLANKS)) != len(blanks):
                sys.exit(f"Add Blank made {len(p.js(BLANKS))} blanks, not {len(blanks)}; nothing saved")
            for k, answers in enumerate(blanks[1:], 2):
                r = p.js(f"""(() => {{ const e = findAll(e => e.tagName === 'INPUT' && /Answers for blank {k} of/.test(labelOf(e)))[0];
                             e.scrollIntoView({{block: 'center'}}); return absOf(e); }})()""")
                b.click_xy(r["x"], r["y"])
                for ans in answers:
                    b.insert(ans); b.key("Enter")
            typed = [x["answers"] for x in p.js(BLANKS)]
            if typed != blanks:
                sys.exit(f"  the fields read {typed} before saving, not {blanks}; nothing saved")
            # A mouse click on Save sends nothing here; Enter on the focused button submits the form.
            p.js("findAll(e => e.tagName === 'BUTTON' && visible(e) && textOf(e) === 'Save')[0].focus()")
            b.key("Enter"); time.sleep(8)
        finally:
            b.close()
        b, p = open_question(ou, a.section, title)
        try:
            got = [x["answers"] for x in p.js(BLANKS)]
        finally:
            b.close()
        ok = got == blanks
        bad |= not ok
        print(f"  checked on the server: {len(got)} blanks, " + ("as the file has them" if ok else f"DIFFERENT: {got}"))
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

    s = sub.add_parser("header", help="paste the header, and the footer, as HTML files"); common(s)
    s.add_argument("--header", required=True); s.add_argument("--footer"); s.set_defaults(fn=verb_header)
    s = sub.add_parser("visible", help="the visibility switch at the foot of the editor"); common(s)
    s.add_argument("--hidden", action="store_true", help="hide it (the default makes it visible)"); s.set_defaults(fn=verb_visible)
    s = sub.add_parser("lockdown", help="require LockDown Browser, with its allowed domains"); common(s)
    s.add_argument("--domains", help="one comma-separated line, as exam.py header prints it")
    s.add_argument("--off", action="store_true", help="LockDown not required")
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
