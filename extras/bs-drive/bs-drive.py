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
    bs-drive.py shot     COURSE QUIZ FILE.png

COURSE is a label from ~/.config/brightspace/courses.ini or an org unit id;
QUIZ is a quiz id or its exact name. Needs chromium on PATH and python3 with
websocket-client (nix-shell -p chromium python3Packages.websocket-client).
The README beside this file says what each page looks like underneath and
what was learned driving it.
"""
import argparse, base64, configparser, json, os, pathlib, re, shutil, subprocess, sys, tempfile, time, urllib.request

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
            stdout=subprocess.DEVNULL, stderr=open(pathlib.Path(self.profile) / "chromium.err", "w"))
        port_file = pathlib.Path(self.profile) / "DevToolsActivePort"
        for _ in range(150):
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.2)
        else:
            self.close(); sys.exit("chromium did not start (is it on PATH?)")
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
        self.b.eval(HELPERS)

    def find(self, sel, text, exact=True, last=True):
        test = f"t === {json.dumps(text)}" if exact else f"t.includes({json.dumps(text)})"
        pick = "es[es.length - 1]" if last else "es[0]"
        return self.b.eval(f"""(() => {{
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
        r = self.b.eval(f"""(() => {{ const es = findAll({pred_js}).filter(e => visible(e)); if (!es.length) return null;
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


def org_unit(course):
    """An org unit id as given, or read off the course's site for a label, the
    way brightspace.py reads it: courses.ini names the site, whose _quarto.yml
    has urls.brightspace with ou= in it."""
    ini = configparser.ConfigParser()
    ini.read(CONFIG / "courses.ini")
    if course not in ini:                      # a label is often digits too, "120": the file is asked first
        if re.fullmatch(r"\d+", str(course)):
            return int(course)
        sys.exit(f"{course!r} is not an org unit id or a label in {CONFIG / 'courses.ini'}")
    site = pathlib.Path(ini[course]["site"])
    if not site.is_absolute():
        site = (CONFIG / "courses.ini").resolve().parent / site
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
    b.cookies(load_cookies())
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
    if not a.off and not domains:
        sys.exit("--domains 'a.org, b.edu' (one comma-separated line, as exam.py header prints it), or --off")
    b, p = session(ou, qi, height=1400, respondus=True)
    try:
        rows = respondus_row(p, name)
        if not rows:
            sys.exit(f"no row for {name!r} on the LockDown dashboard")
        print("row now:", rows[0])
        open_settings(p, name)
        have = read_lockdown(p)
        print(f"now: required={have['required']} external domains allowed={have['allow']} domains={have['domains']!r}")
        if "\n" in (have["domains"] or ""):
            print("  (stored with newlines: LockDown then opens each site's first page and nothing behind it)")
        if not a.go:
            print(f"would set: required={not a.off} domains={domains!r}; nothing written; add --go"); return
        p.click_pred(f"""e => e.tagName === 'INPUT' && e.name === 'ldbEnabled' && e.value === {json.dumps('false' if a.off else 'true')}""", settle=2, what="the Require radio")
        if not a.off:
            if not have["allow"]:
                p.click_pred("""e => e.tagName === 'INPUT' && e.id === 'allowExternalDomains'""", settle=2, what="allow external domains")
            r = b.eval("""(() => { const e = findAll(e => e.tagName === 'TEXTAREA' && e.id === 'externalDomains')[0]; if (!e) return null;
                          e.scrollIntoView({block: 'center'}); e.focus(); e.select(); return absOf(e); })()""")
            if not r:
                sys.exit("the external domains box did not appear")
            b.click_xy(r["x"], r["y"]); time.sleep(0.5)
            b.eval("""(() => { const e = findAll(e => e.tagName === 'TEXTAREA' && e.id === 'externalDomains')[0]; e.value = ''; e.focus(); })()""")
            b.insert(domains); time.sleep(1)
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
    ok = (got["required"] is (not a.off)) and (a.off or (got["domains"] == domains and got["allow"]))
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
    s.add_argument("--off", action="store_true", help="LockDown not required"); s.set_defaults(fn=verb_lockdown)
    s = sub.add_parser("special-access", help="put one user on the quiz's special access list"); common(s)
    s.add_argument("--user", required=True, help="the name as the classlist shows it, 'First Last'")
    s.add_argument("--only", action="store_true", help="and allow only users with special access"); s.set_defaults(fn=verb_special_access)
    s = sub.add_parser("shot", help="a screenshot of the editor, for a look"); common(s)
    s.add_argument("file"); s.set_defaults(fn=verb_shot)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
