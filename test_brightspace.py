"""A fake Brightspace, and brightspace.py run against it as a subprocess.

    nix-shell -p 'python3.withPackages(ps: [ps.lz4])' --run 'python3 test_brightspace.py'
    MOCK_COOKIES_OK=0 ... same ...                 # /d2l/api/ refuses cookies: the token fallback

lz4 is needed because the Firefox cases build a real session store, and because
the interpreter running this is the one the tool is invoked with, bypassing its
shebang.

The JSON here is the shape the Valence docs give for each route, cut down to
the fields the tool reads; the mock is the record of what was assumed.
"""
import datetime as dt, json, os, pathlib, re, subprocess, sys, tempfile, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT = str(pathlib.Path(__file__).with_name("brightspace.py"))
XSRF, SECRET, BEARER, SECRET_VAL = "TOKEN123", "SESSIONSECRET", "BEARER1", "SESSIONVAL"
# The token the server currently expects, and whether /d2l/home hands it out.
# Both mutable, so a test can rotate the token under the tool or take it away.
TOKEN = {"now": XSRF}
HOME = {"xsrf": True}
# What the mock currently accepts, so the suite can expire a session on purpose.
ACCEPT = {"secure": SECRET}
DELETED = set()   # quiz ids a DELETE has removed


def alive(quizzes):
    return [x for x in quizzes if x.get("QuizId") not in DELETED]
COOKIES_OK = os.environ.get("MOCK_COOKIES_OK", "1") == "1"   # 0: /d2l/api/ wants the bearer
OU = 1000240
now = dt.datetime.now(dt.timezone.utc)
def z(d): return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
TODAY, OLD = z(now - dt.timedelta(minutes=5)), "2026-09-24T17:45:00.000Z"

FOLDERS = [
    {"Id": 7001, "Name": "My journal", "DueDate": None, "IsHidden": False, "TotalUsers": 10,
     "TotalUsersWithSubmissions": 9, "Availability": None},
    {"Id": 555, "Name": "Assignment 4", "DueDate": "2026-09-24T03:59:00.000Z", "IsHidden": False,
     "TotalUsers": 10, "TotalUsersWithSubmissions": 7, "Availability": {"StartDate": "2026-09-17T16:00:00.000Z", "EndDate": None}},
    {"Id": 556, "Name": "Midterm Part 1", "DueDate": "2026-09-29T17:25:00.000Z", "IsHidden": True,
     "TotalUsers": 10, "TotalUsersWithSubmissions": 0, "Availability": None},
]
JOURNAL = [
    {"Entity": {"EntityId": 100001, "EntityType": "User", "DisplayName": "Rita Solberg"}, "Status": 1, "Feedback": None,
     "Submissions": [
         {"Id": 1, "SubmissionDate": OLD, "Comment": {"Text": "old entry", "Html": "<p>old entry</p>"}, "Files": []},
         {"Id": 2, "SubmissionDate": TODAY, "Comment": {"Text": "", "Html": "<p>The <b>loop</b> part was new to me.<br>The rest was review.</p>"}, "Files": []}]},
    {"Entity": {"EntityId": 100002, "EntityType": "User", "DisplayName": "Tomas Weber"}, "Status": 1, "Feedback": None,
     "Submissions": [{"Id": 3, "SubmissionDate": TODAY, "Comment": {"Text": "Finished the drills.", "Html": "<p>Finished the drills.</p>"}, "Files": []}]},
    {"Entity": {"EntityId": 1, "EntityType": "User", "DisplayName": "Nobody Yet"}, "Status": 0, "Feedback": None, "Submissions": []},
]
A4 = [{"Entity": {"EntityId": 100002, "EntityType": "User", "DisplayName": "Tomas Weber"}, "Status": 1,
       "Feedback": {"Score": 95}, "Submissions": [{"Id": 9001, "SubmissionDate": OLD, "Comment": {"Text": "", "Html": ""},
       "Files": [{"FileId": 1, "FileName": "table.c", "Size": 12}, {"FileId": 2, "FileName": "Makefile", "Size": 8}]}]}]
FILES = {"1": b"int main(){}", "2": b"all:\n\tcc\n"}
CATS = [{"Id": 1, "Name": "Assignments", "ShortName": "", "Weight": 10.0, "MaxPoints": 10.0,
         "CanExceedMax": False, "ExcludeFromFinalGrade": False, "WeightDistributionType": 1,
         "NumberOfHighestToDrop": 0, "NumberOfLowestToDrop": 0}]
ITEMS = [{"Id": 500, "Name": "Assignment 1", "GradeType": "Numeric", "MaxPoints": 10.0,
          "Weight": 10.0, "CategoryId": 1, "GradeSchemeUrl": "/x", "IsHidden": False},
         {"Id": 501, "Name": "Midterm", "GradeType": "Numeric", "MaxPoints": 10.0,
          "Weight": 35.0, "CategoryId": 0, "GradeSchemeUrl": "/x", "IsHidden": False,
          "AssociatedTool": {"ToolId": 2000, "ToolItemId": 7002}}]
QUIZZES_MADE = []
NEWS = [{"Id": 900, "Title": "Class 9, Closing Journal", "StartDate": "2026-09-23T19:21:00.000Z",
         "IsPublished": True, "Body": {"Text": "The dot", "Html": "<h2>The dot</h2>"}},
        {"Id": 901, "Title": "Scratch", "StartDate": "2026-12-31T05:01:00.000Z",
         "IsPublished": False, "Body": {"Text": "", "Html": ""}}]
# A quiz's four rich-text fields in the read shape, as the live Quiz 2 returns them.
RICH = {k: {"Text": {"Text": "", "Html": ""}, "IsDisplayed": k != "Instructions"}
        for k in ("Instructions", "Description", "Header", "Footer")}
NEXT = {"folder": 88, "item": 777}

def stored_rich(v):
    """What D2L keeps of rich text sent to it: {"Content", "Type"} comes back as
    {"Text", "Html"}, and anything else -- the read shape sent straight back --
    is dropped without an error. Measured on the live instance 2026-09-27."""
    if isinstance(v, dict) and "Content" in v:
        c, is_html = v["Content"], v.get("Type") == "Html"
        return {"Text": re.sub(r"<[^>]+>", "", c) if is_html else c, "Html": c if is_html else ""}
    return {"Text": "", "Html": ""}

def enrolled(ou, name, role="Instructor", end="2099-12-26T04:59:00.000Z"):
    return {"OrgUnit": {"Id": ou, "Code": f"{ou}.202630", "Name": name},
            "Access": {"IsActive": True, "ClasslistRoleName": role, "EndDate": end}}
# myenrollments, two pages of it. A past term's course, a sandbox and a course
# taken rather than taught are what init has to leave out; the trailing space
# is one the live instance has.
ENROLLED = [
    [enrolled(OU, "CMSC-240-01-Fall 2026 - Parallel C"),
     enrolled(900240, "CMSC-240-01-Spring 2026 - Parallel C", end="2026-05-20T03:59:00.000Z"),
     enrolled(1000999, "Devshell Parallel C - Ada Lovelace", role="Staff", end=None)],
    [enrolled(1000120, "CMSC-120-01-Fall 2026 - OOP in Java "),
     enrolled(1000115, "CMSC-115-01-Fall 2026 - Python"),
     enrolled(1000116, "CMSC-115-02-Fall 2026 - Python"),
     enrolled(1000777, "Center for Teaching and Learning", role="Participant", end=None)]]

ATTEMPTS = [{"AttemptId": 1, "UserId": 10, "Completed": OLD}, {"AttemptId": 2, "UserId": 10, "Completed": OLD},
            {"AttemptId": 3, "UserId": 11, "Completed": OLD}, {"AttemptId": 4, "UserId": 12, "Completed": None}]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def cookie_ok(self):
        return f"d2lSecureSessionVal={ACCEPT['secure']}" in self.headers.get("Cookie", "")
    def loginFailed(self):
        return self.path.startswith("/d2l/lp/auth/login/loginFailed.d2l")
    def api_ok(self):
        if COOKIES_OK and self.cookie_ok(): return True
        return self.headers.get("Authorization") == f"Bearer {BEARER}"
    def send(self, code, body, ctype="application/json", extra=()):
        if isinstance(body, (dict, list)): body = json.dumps(body).encode()
        elif isinstance(body, str): body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        for k, v in extra: self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)
    def redirect(self, to, extra=()):
        self.send_response(302); self.send_header("Location", to); self.send_header("Content-Length", "0")
        for k, v in extra: self.send_header(k, v)
        self.end_headers()
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path); q = urllib.parse.parse_qs(u.query); p = u.path
        port = self.server.server_address[1]
        if p == "/d2l/login":
            sso = "" if os.environ.get("MOCK_NO_SSO") else "<button class='d2l-button d2l-button-sso-1'>Commonwealth User Login</button>"
            return self.send(200, f"<html>{sso}<form data-location='/d2l/lp/auth/login/login.d2l'><input name='userName'><input name='password'></form>"
                             "<script>localStorage.setItem('XSRF.Token','');</script></html>", "text/html")
        if self.loginFailed():
            return self.send(200, "<html>Login failed</html>", "text/html")
        if p == "/d2l/home":
            if not self.cookie_ok(): return self.redirect("/d2l/login?sessionExpired=1")
            if not HOME["xsrf"]:
                return self.send(200, "<html><script>localStorage.setItem('XSRF.Token','');</script></html>", "text/html")
            return self.send(200, '<html><script>var x = {"2":"{\\"_type\\":\\"func\\",\\"N\\":\\"D2L.LP.Web.Authentication.Xsrf.Init\\",\\"P\\":[\\"d2l_referrer\\",\\"' + TOKEN["now"] + '\\",0]}"};</script></html>', "text/html")
        if p == "/d2l/api/versions/":
            return self.send(200, [{"ProductCode": "le", "LatestVersion": "1.99"}, {"ProductCode": "lp", "LatestVersion": "1.63"}])
        if not p.startswith("/d2l/api/"): return self.send(404, "nope", "text/plain")
        if not self.api_ok(): return self.send(403, '{ Errors: [ {Message: "Forbidden"} ] }', "text/html")
        if p == "/d2l/api/lp/1.63/users/whoami":
            return self.send(200, {"Identifier": "77", "FirstName": "Ada", "LastName": "Lovelace", "UniqueName": "alovelace"})
        if p == "/d2l/api/lp/1.63/enrollments/myenrollments/":
            assert q.get("orgUnitTypeId") == ["3"], q
            last = q.get("bookmark") == ["b1"]
            return self.send(200, {"PagingInfo": {"Bookmark": "b2" if last else "b1", "HasMoreItems": not last},
                                   "Items": ENROLLED[1 if last else 0]})
        base = f"/d2l/api/le/1.99/{OU}/"
        if not p.startswith(base): return self.send(404, {"Errors": [{"Message": "no such org unit"}]})
        r = p[len(base):]
        if r == "dropbox/folders/": return self.send(200, FOLDERS)
        if r == "dropbox/folders/7001/submissions/": return self.send(200, JOURNAL)
        if r == "dropbox/folders/555/submissions/": return self.send(200, A4)
        m = re.fullmatch(r"dropbox/folders/555/submissions/9001/files/(\d+)", r)
        if m: return self.send(200, FILES[m.group(1)], "application/octet-stream")
        if r == "quizzes/":
            if q.get("page") == ["2"]: return self.send(200, {"Objects": alive([{"QuizId": 2, "Name": "Quiz 2", "IsActive": False, "DueDate": None, "EndDate": OLD, **RICH,
                "Shuffle": True, "IsAutoSetGraded": True}] + QUIZZES_MADE), "Next": None})
            return self.send(200, {"Objects": [{
                "QuizId": 1, "Name": "Quiz 1", "IsActive": True, "DueDate": OLD,
                "RestrictIPAddressRange": [{"IPRangeStart": "148.137.0.0", "IPRangeEnd": "148.137.255.255"}],
                "Password": "hunter2", "NumberOfAttemptsAllowed": 2, "GradeItemId": 999,
                "Shuffle": False, "DisplayInCalendar": True,
                # read shape, as the live Quiz 2 returns it (2026-09-27)
                "Instructions": {"Text": {"Text": "", "Html": ""}, "IsDisplayed": False},
                "Description": {"Text": {"Text": "Old words", "Html": "<p>Old words</p>"}, "IsDisplayed": True},
                "Header": {"Text": {"Text": "", "Html": ""}, "IsDisplayed": True},
                "Footer": {"Text": {"Text": "", "Html": ""}, "IsDisplayed": True},
                "SubmissionTimeLimit": {"TimeLimitValue": 50}}],
                "Next": f"http://127.0.0.1:{port}{base}quizzes/?page=2"})
        if r == "quizzes/1/questions/":
            return self.send(200, {"Objects": [
                {"QuestionTypeId": 8, "QuestionText": {"Text": "Upload your four programs here."}}], "Next": None})
        if re.fullmatch(r"quizzes/\d+/questions/", r):
            return self.send(200, {"Objects": [], "Next": None})
        if r == "quizzes/1/attempts/": return self.send(200, {"Objects": ATTEMPTS, "Next": None})
        if r == "quizzes/2/attempts/": return self.send(200, {"Objects": [], "Next": None})
        if r == "grades/categories/":
            return self.send(200, [c | {"Grades": []} for c in CATS])
        if r == "news/": return self.send(200, NEWS)
        m = re.fullmatch(r"news/(\d+)", r)
        if m:
            hit = [n for n in NEWS if n["Id"] == int(m.group(1))]
            return self.send(200, hit[0]) if hit else self.send(404, {"Errors": [{"Message": "no such news item"}]})
        if r == "grades/":
            return self.send(200, ITEMS)
        m = re.fullmatch(r"grades/(\d+)", r)
        if m:
            hit = [i for i in ITEMS if i["Id"] == int(m.group(1))]
            return self.send(200, hit[0]) if hit else self.send(404, {"Errors": [{"Message": "no item"}]})
        if r == "classlist/": return self.send(200, [{"DisplayName": "Rita Solberg", "FirstName": "Rita", "LastName": "Solberg", "ClasslistRoleDisplayName": "Student", "Username": "rs", "Email": "rs@x"},
                                                      {"DisplayName": "Ada L", "FirstName": "Ada", "LastName": "L", "ClasslistRoleDisplayName": "Instructor", "Username": "al", "Email": "al@x"}])
        # A quiz the run itself created has no attempts and no questions yet.
        m = re.fullmatch(r"quizzes/(\d+)/(attempts|questions)/", r)
        if m and int(m.group(1)) in {x["QuizId"] for x in QUIZZES_MADE}:
            return self.send(200, {"Objects": [], "Next": None})
        return self.send(404, {"Errors": [{"Message": "unknown " + r}]})
    def do_DELETE(self):
        base = f"/d2l/api/le/1.99/{OU}/"
        if not self.headers.get("X-Csrf-Token"):
            return self.send(403, "CSRF token required", "text/plain")
        m = re.fullmatch(r"quizzes/(\d+)", self.path[len(base):]) if self.path.startswith(base) else None
        if not m or int(m.group(1)) in DELETED:
            return self.send(404, {"Errors": [{"Message": "no such quiz"}]})
        DELETED.add(int(m.group(1)))
        return self.send(200, "", "text/plain")

    def do_PUT(self):
        base = f"/d2l/api/le/1.99/{OU}/"
        if self.headers.get("X-Csrf-Token") != TOKEN["now"]:
            return self.send(403, "CSRF token required", "text/plain")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        mq = re.fullmatch(r"quizzes/(\d+)", self.path[len(base):]) if self.path.startswith(base) else None
        if mq:
            if "AttemptsAllowed" in body:   # the read shape, refused as on a create
                return self.send(400, {"title": "JSON Binding Error"})
            for i, q in enumerate(QUIZZES_MADE):
                if q["QuizId"] == int(mq.group(1)):
                    n = body.get("NumberOfAttemptsAllowed")
                    kept = {k: v for k, v in body.items() if k != "NumberOfAttemptsAllowed"} | {
                        "QuizId": q["QuizId"], "AttemptsAllowed": {"IsUnlimited": n is None, "NumberOfAttemptsAllowed": n}}
                    for key in ("Instructions", "Description", "Header", "Footer"):
                        if isinstance(body.get(key), dict):
                            kept[key] = {"Text": stored_rich(body[key]["Text"]), "IsDisplayed": body[key].get("IsDisplayed")}
                    QUIZZES_MADE[i] = kept
                    return self.send(200, kept)
            return self.send(404, {"Errors": [{"Message": "no such quiz"}]})
        mf = re.fullmatch(r"dropbox/folders/(\d+)", self.path[len(base):]) if self.path.startswith(base) else None
        if mf:
            for i, f in enumerate(FOLDERS):
                if f["Id"] == int(mf.group(1)):
                    FOLDERS[i] = body | {k: f[k] for k in ("Id", "TotalUsers", "TotalUsersWithSubmissions") if k in f} | {
                        "Attachments": [], "LinkAttachments": [],
                        "CustomInstructions": stored_rich(body.get("CustomInstructions"))}
                    return self.send(200, FOLDERS[i])
            return self.send(404, {"Errors": [{"Message": "no such folder"}]})
        m = re.fullmatch(r"grades/(\d+)", self.path[len(base):]) if self.path.startswith(base) else None
        if not m:
            return self.send(404, {"Errors": [{"Message": "no PUT route"}]})
        for i, item in enumerate(ITEMS):
            if item["Id"] == int(m.group(1)):
                # Both behaviours measured on a weighted sandbox, 2026-09-27: a
                # weight sent for an item inside a category is refused, and the
                # update replaces the whole object, so a tool left out is gone.
                if (body.get("CategoryId") or 0) and "Weight" in body:
                    return self.send(400, {"Errors": [{"Message": "Cannot set grade weight directly "
                                                       "when weight is specified by grade category"}]})
                ITEMS[i] = body | {"Id": item["Id"]}
                return self.send(200, ITEMS[i])
        return self.send(404, {"Errors": [{"Message": "no such item"}]})

    def do_POST(self):
        # The body can only be read once, so read it here and let each branch parse it.
        n = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(n)
        form = urllib.parse.parse_qs(raw.decode())
        if self.path == "/d2l/lp/auth/login/login.d2l":
            assert form.get("loginPath") == ["/d2l/login"], form
            if form.get("userName") == ["ada"] and form.get("password") == ["pw"]:
                return self.redirect("/d2l/home", [("Set-Cookie", "d2lSessionVal=abc; Path=/; HttpOnly"),
                                                   ("Set-Cookie", f"d2lSecureSessionVal={ACCEPT['secure']}; Path=/; HttpOnly")])
            # Measured on the live instance 2026-09-26: a refused login still sets both
            # session cookies, so their presence proves nothing.
            return self.redirect("/d2l/lp/auth/login/loginFailed.d2l?status=1",
                                 [("Set-Cookie", "d2lSessionVal=dead; Path=/; HttpOnly"),
                                  ("Set-Cookie", "d2lSecureSessionVal=dead; Path=/; HttpOnly")])
        base = f"/d2l/api/le/1.99/{OU}/"
        if self.path.startswith(base):
            if self.headers.get("X-Csrf-Token") != TOKEN["now"]:
                return self.send(403, "CSRF token required", "text/plain")
            if not self.api_ok():
                return self.send(403, '{ Errors: [ {Message: "Forbidden"} ] }', "text/html")
            r = self.path[len(base):]
            body = json.loads(raw)
            if r == "grades/categories/":
                made = body | {"Id": 99}
                CATS.append(made)
                return self.send(200, made)
            if r == "quizzes/":
                if "AttemptsAllowed" in body:   # the read shape; live answer 2026-09-27
                    return self.send(400, {"title": "JSON Binding Error"})
                for key in ("Instructions", "Description", "Header", "Footer"):
                    v = body.get(key)
                    if isinstance(v, dict) and not isinstance(v.get("Text"), dict):
                        return self.send(400, {"Errors": [{"Message": f"{key}.Text: expected RichTextInput"}]})
                made = {k: v for k, v in body.items() if k != "NumberOfAttemptsAllowed"} | {
                    "QuizId": 77, "AttemptsAllowed": {"IsUnlimited": body.get("NumberOfAttemptsAllowed") is None,
                                                      "NumberOfAttemptsAllowed": body.get("NumberOfAttemptsAllowed")}}
                for key in ("Instructions", "Description", "Header", "Footer"):
                    if isinstance(body.get(key), dict):
                        made[key] = {"Text": stored_rich(body[key]["Text"]), "IsDisplayed": body[key].get("IsDisplayed")}
                QUIZZES_MADE.append(made)
                return self.send(200, made)
            if r == "dropbox/folders/":
                made = body | {"Id": NEXT["folder"], "LinkAttachments": [],
                               "CustomInstructions": stored_rich(body.get("CustomInstructions"))}
                NEXT["folder"] += 1
                FOLDERS.append(made)
                return self.send(200, made)
            if r == "grades/":
                if body.get("CategoryId") and "Weight" in body:   # the live answer, 2026-09-27
                    return self.send(400, {"Errors": [{"Message": "Cannot set grade weight directly when weight is specified by grade category"}]})
                made = body | {"Id": NEXT["item"]}
                NEXT["item"] += 1
                ITEMS.append(made)
                tool = body.get("AssociatedTool") or {}
                if tool.get("ToolId") == 2000:   # linked from the item's side: the folder follows
                    for f in FOLDERS:
                        if f["Id"] == tool.get("ToolItemId"):
                            f["GradeItemId"] = made["Id"]
                return self.send(200, made)
            return self.send(404, {"Errors": [{"Message": "no write route " + r}]})
        if self.path == "/d2l/lp/auth/oauth2/token":
            if self.cookie_ok() and self.headers.get("X-Csrf-Token") == TOKEN["now"] and form.get("scope") == ["*:*:*"]:
                return self.send(200, {"access_token": BEARER, "expires_at": 0})
            return self.send(200, "<html>redirect to login</html>", "text/html")
        return self.send(404, "nope", "text/plain")

srv = ThreadingHTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
state = pathlib.Path(tempfile.mkdtemp()) / "state"; out = pathlib.Path(tempfile.mkdtemp())

# The suite's own courses file and sites, so it never moves with a real course.
# 240's site carries a section's brightspace: under meetings:, ahead of urls:,
# which is not the course's and must not be taken for it. Paths are relative,
# so they are read from the file's own directory.
conf = pathlib.Path(tempfile.mkdtemp())
def course_dir(name, brightspace, before=""):
    (conf / name).mkdir()
    (conf / name / "_quarto.yml").write_text(
        before + f'urls:\n  site:  &site-url  "https://example.edu/{name}"\n  brightspace: "{brightspace}"\n'
        '  sections:\n    - tag: "02"\n      brightspace: "https://example.brightspace.com/d2l/home/998"\n')
course_dir("site240", f"https://example.brightspace.com/d2l/home/{OU}",
           before='meetings:\n  sections:\n    - tag: "01"\n'
                  '      brightspace: "https://example.brightspace.com/d2l/home/999"\n')
course_dir("site120", "https://example.brightspace.com/d2l/home/1000120")
course_dir("login", "https://example.brightspace.com/d2l/login")
COURSES = conf / "courses.ini"
COURSES.write_text("# the suite's own\n\n[240]\nsite = site240\ndropbox = dropbox240\n\n"
                   f"[120]\nsite = site120\n\n[same]\nou = {OU}\n\n[login-only]\nsite = login\n\n"
                   f"[withsite]\nsite = {out / 'site'}\nou = {OU}\n")
env = dict(os.environ, BRIGHTSPACE_STATE_DIR=str(state), BRIGHTSPACE_URL=f"http://127.0.0.1:{port}",
           BRIGHTSPACE_USER="ada", BRIGHTSPACE_COURSES=str(COURSES))
fails = 0
def run(*args, password="pw", ok=True, has=(), lacks=(), extra=None):
    global fails
    r = subprocess.run([sys.executable, SCRIPT, *args], env=dict(env, BRIGHTSPACE_PASSWORD=password, **(extra or {})),
                       capture_output=True, text=True,
                       # No controlling terminal and no stdin: a prompt then fails fast
                       # rather than waiting on a tty the suite cannot answer.
                       stdin=subprocess.DEVNULL, start_new_session=True, timeout=60)
    text = r.stdout + r.stderr; good = (r.returncode == 0) == ok and all(h in text for h in has) and not any(l in text for l in lacks)
    print(("ok  " if good else "FAIL"), " ".join(args), f"(exit {r.returncode})")
    if not good:
        fails += 1; print(text)
    return text
print(f"cookies accepted by /d2l/api/: {COOKIES_OK}")

# The prompting commands are driven in-process: getpass reads /dev/tty when there
# is one, so a pipe on stdin is not a reliable way to answer it.
def inproc(label, argv, answers, ok=True, has=(), extra=None, patch=None):
    global fails
    import io, contextlib, importlib, getpass as G, builtins
    saved = dict(os.environ)
    for k, v in {**env, **(extra or {})}.items():
        os.environ[k] = v
    sys.path.insert(0, str(pathlib.Path(SCRIPT).parent))
    b = importlib.reload(importlib.import_module("brightspace"))
    if patch:
        patch(b)
    fed = list(answers)
    G.getpass = lambda *a, **k: fed.pop(0)
    b.getpass.getpass = G.getpass
    real_input, builtins.input = builtins.input, lambda *a, **k: fed.pop(0) if fed else ""
    buf, code, msg = io.StringIO(), 0, ""
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            b.main(argv)
    except SystemExit as e:
        # sys.exit("message") carries the message as .code and means failure.
        msg = e.code if isinstance(e.code, str) else ""
        code = 1 if msg else (e.code or 0)
    finally:
        builtins.input = real_input
        os.environ.clear()
        os.environ.update(saved)
    text = buf.getvalue() + msg
    good = (code == 0) == ok and all(h in text for h in has)
    print(("ok  " if good else "FAIL"), label, f"(exit {code})")
    if not good:
        fails += 1; print(text)
    return text

inproc("login refuses where SSO is offered", ["login", "--user", "ada"], ["pw"], ok=False,
       has=["single sign-on", "brightspace.py session"])
os.environ["MOCK_NO_SSO"] = env["MOCK_NO_SSO"] = "1"
inproc("login --force on a refused password", ["login", "--user", "ada"], ["bad"], ok=False,
       has=["the login was refused", "loginFailed"])
assert not (state / "cookies.txt").exists(), "a refused login must not leave a session behind"
print("ok   a refused login leaves no session")
# With the cookies refused, a cookie session can only work by minting a token from
# them, which needs the XSRF value — so that mode asks for it and feeds it.
CARGS = [] if COOKIES_OK else ["--xsrf"]
CANS = [] if COOKIES_OK else [XSRF]

inproc("session, two pastes", ["session", "--paste", *CARGS], [SECRET_VAL, SECRET, *CANS],
       has=["read from the paste", "accepted: Ada Lovelace (alovelace)", "user id 77",
            "browser's own session"])
curl = ("curl 'https://commonwealthu.brightspace.com/d2l/home' -H 'cookie: d2lSessionVal=%s; "
        "d2lSecureSessionVal=%s; d2lSameSiteCanaryA=1' --compressed" % (SECRET_VAL, SECRET))
inproc("session, one paste of a Copy-as-cURL", ["session", "--paste", *CARGS], [curl, *CANS],
       has=["read from the paste", "accepted:"])
inproc("session, a paste missing one cookie", ["session", "--paste"], ["d2lSessionVal=%s" % SECRET_VAL, ""],
       ok=False, has=["both cookies are needed"])
inproc("session --token", ["session", "--token"], [BEARER],
       has=["read from token", "accepted: Ada Lovelace", "lasts about an hour"])
assert json.loads((state / "session.json").read_text()).get("bearer") == BEARER
print("ok   a pasted token is the whole session, and is saved as one")
run("whoami", has=["Ada Lovelace (alovelace), user id 77"])
inproc("session again, after a token was in play", ["session", "--paste", *CARGS],
       [SECRET_VAL, SECRET, *CANS], has=["read from the paste"])
assert "bearer" not in json.loads((state / "session.json").read_text()), "the old token must not survive"
print("ok   replacing a session drops the token it left behind")
run("login", "--force", has=["logged in as Ada Lovelace (alovelace)", "le 1.99, lp 1.63"])
modes = {p.name: oct(p.stat().st_mode & 0o777) for p in state.iterdir()}
print("   state files:", modes); fails += modes != {"cookies.txt": "0o600", "session.json": "0o600"}
meta = json.loads((state / "session.json").read_text()); fails += "bearer" in meta
print("   bearer kept out of a cookie session:", "bearer" not in meta)
run("whoami", has=["Ada Lovelace (alovelace), user id 77"])
run("courses", has=["1000240  240", "1000120  120", "Instructor"], lacks=["No " + str(COURSES)])
# --- the courses file ---------------------------------------------------------
# 240 resolving at all is the check on the section decoys in its _quarto.yml:
# taken for the course, either would answer "no such org unit".
run("folders", "nope", ok=False, has=["unknown course 'nope'", "240, 120, same, login-only, withsite"])
run("folders", "login-only", ok=False, has=["no urls.brightspace of the form", "[login-only]", "needs an ou"])
run("folders", "same", has=["7001  My journal"])
bad = conf / "bad.ini"
bad.write_text("[240]\nsit = site240\n")
run("folders", "240", ok=False, extra={"BRIGHTSPACE_COURSES": str(bad)}, has=["[240]: no such key sit"])
# A broken courses file stops what needs a course and nothing else.
run("whoami", extra={"BRIGHTSPACE_COURSES": str(bad)}, has=["Ada Lovelace (alovelace)"])
bad.write_text("[240]\nou = CMSC-240\n")
run("folders", "240", ok=False, extra={"BRIGHTSPACE_COURSES": str(bad)}, has=["ou is a number"])
run("courses", extra={"BRIGHTSPACE_COURSES": str(conf / "none.ini")},
    has=["1000240", "No " + str(conf / "none.ini") + " yet"])
run("folders", "cmsc240", extra={"BRIGHTSPACE_COURSES": str(conf / "none.ini")}, ok=False,
    has=["unknown course 'cmsc240'", "which does not exist", "brightspace.py courses"])
# The host, with no --base-url and no $BRIGHTSPACE_URL: the first course's site
# names it, and with no courses file it is Commonwealth's.
def host_with(**extra):
    e = {k: v for k, v in env.items() if k != "BRIGHTSPACE_URL"} | extra
    return subprocess.run([sys.executable, "-c", "import argparse, brightspace as b; "
                           "print(b.base_url(argparse.Namespace(base_url=None)))"],
                          cwd=pathlib.Path(SCRIPT).parent, env=e, capture_output=True, text=True).stdout.strip()
hosts = host_with(), host_with(BRIGHTSPACE_COURSES=str(conf / "none.ini"))
print("   the host, from the first site and then the default:", hosts)
fails += hosts != ("https://example.brightspace.com", "https://commonwealthu.brightspace.com")

# --- init: the two files, written from the enrollments --------------------------
# A tree of sites to search: a worktree naming the same course with a longer
# path, a site naming its sections only, and three a search must not reach.
fresh = pathlib.Path(tempfile.mkdtemp())
tree = fresh / "courses"
def a_site(where, brightspace, extra=""):
    (where).mkdir(parents=True)
    (where / "_quarto.yml").write_text(
        f'urls:\n  site: "https://example.edu/x"\n  brightspace: "{brightspace}"\n{extra}')
home = "https://example.brightspace.com/d2l/home/"
a_site(tree / "cmsc-240" / "website", home + str(OU))
(tree / "cmsc-240" / "dropbox").mkdir()
a_site(tree / "cmsc-240-a-worktree" / "website", home + str(OU))
a_site(tree / "cmsc-115" / "site", "https://example.brightspace.com/d2l/login",
       f'  sections:\n    - tag: "01"\n      brightspace: "{home}1000115"\n'
       f'    - tag: "02"\n      brightspace: "{home}1000116"\n')
a_site(tree / ".hidden" / "website", home + "1000120")
a_site(tree / "a" / "b" / "c" / "website", home + "1000120")
elsewhere = pathlib.Path(tempfile.mkdtemp())
a_site(elsewhere / "website", home + "1000120")
(tree / "linked").symlink_to(elsewhere)
made = fresh / "conf" / "brightspace"
init_env = {"XDG_CONFIG_HOME": str(fresh / "conf"), "BRIGHTSPACE_COURSES": str(made / "courses.ini")}
want = ["# CMSC-240-01-Fall 2026 - Parallel C\n[240]\nsite = " + str(tree / "cmsc-240" / "website")
        + "\ndropbox = " + str(tree / "cmsc-240" / "dropbox") + "\n\n",
        "# CMSC-120-01-Fall 2026 - OOP in Java\n[120]\nou = 1000120\n\n",
        "[115-01]\nsite = " + str(tree / "cmsc-115" / "site") + "\nou = 1000115\n",
        "[115-02]\nsite = " + str(tree / "cmsc-115" / "site") + "\nou = 1000116\n",
        f"[ada]\nstate = {state}\n"]
gone = ["900240", "Spring", "Devshell", "Teaching", "worktree", ".hidden", "/a/b/c", "linked", str(elsewhere)]
run("init", "--sites", str(tree), "--dry-run", extra=init_env,
    has=want + ["keepalive.ini (only if this machine keeps your session alive)"], lacks=gone)
fails += made.exists(); print("   --dry-run wrote nothing:", not made.exists())
# The keepalive file is only for a machine that runs the keep-alive, so init
# asks; with nobody at the terminal and no flag, it writes none.
run("init", "--sites", str(tree), extra=init_env,
    has=[f"wrote {made / 'courses.ini'}: 240, 120, 115-01, 115-02, 3 with a site",
         f"no {made / 'keepalive.ini'} written: there was nobody to ask", "--keepalive says it does"])
run("init", "--no-keepalive", extra=init_env,
    has=["courses.ini exists, left alone", "No keepalive file, then", "session --export"])
a_person = lambda b: setattr(b, "interactive", lambda: True)
inproc("init, answered no at the terminal", ["init"], ["n"], extra=init_env, patch=a_person,
       has=["No keepalive file, then"])
fails += (made / "keepalive.ini").exists()
print("   no keepalive file without a yes:", not (made / "keepalive.ini").exists())
inproc("init, answered yes at the terminal", ["init"], ["y"], extra=init_env, patch=a_person,
       has=[f"wrote {made / 'keepalive.ini'}, mode 0600: this session, as [ada]", "Keeping sessions alive"])
fails += oct((made / "keepalive.ini").stat().st_mode & 0o777) != "0o600" or oct(made.stat().st_mode & 0o777) != "0o700"
print("   keepalive.ini 0600 in a 0700 directory:", oct((made / "keepalive.ini").stat().st_mode & 0o777), oct(made.stat().st_mode & 0o777))
fails += not all(w in (made / "courses.ini").read_text() + (made / "keepalive.ini").read_text() for w in want)
# What init wrote is what the commands read.
run("courses", extra=init_env, has=[f"{OU}  240", "1000115  115-01", "1000116  115-02", "1000120  120"])
run("folders", "240", extra=init_env, has=["7001  My journal"])
run("keepalive", extra=init_env, has=["ada  alive: alovelace"])
# A second run touches neither, and says what each lacks.
run("init", "--sites", str(tree), extra=init_env,
    has=["courses.ini exists, left alone; it names every course you teach",
         "keepalive.ini exists, left alone; it has this session"])
(made / "courses.ini").write_text((made / "courses.ini").read_text().replace("[120]\nou = 1000120\n", ""))
(made / "keepalive.ini").write_text("[colleague]\ncookies = d2lSessionVal=a; d2lSecureSessionVal=b\n")
run("init", extra=init_env,
    has=["not in it yet:\n\n# CMSC-120-01-Fall 2026 - OOP in Java\n[120]\nou = 1000120",
         f"this session is not in it:\n\n[ada]\nstate = {state}"])
run("folders", "240", has=["7001  My journal", "555   Assignment 4", "hidden", "7/10", "2026-09-23 23:59"])
run("--json", "folders", "240", has=['"TotalUsersWithSubmissions": 7'])
t = run("journal", "240", "--no-write", has=["Rita Solberg", "The loop part was new to me.", "The rest was review.", "Tomas Weber", "2 entries from 2 students"], lacks=["old entry"])
t = run("journal", "240", "--all", "--out", str(out / "j"), has=["old entry", "3 entries from 2 students", "3 files under"])
run("journal", "240", "--all", has=["3 files under " + str(conf / "dropbox240" / "journals" / "My journal API ")])
run("journal", "same", "--all", ok=False, has=["same has no dropbox in", "--out or --no-write"])
run("submissions", "same", "555", "--download", ok=False, has=["same has no dropbox in", "pass --out"])
files = sorted(str(p.relative_to(out / "j")) for p in (out / "j").rglob("*.html")); print("   ", files)
fails += not any(re.fullmatch(r"100001-7001 - Rita-Solberg/My journal-Sep 24, 2026 145 PM\.html", f) for f in files)
fails += not any(f.startswith("100002-7001 - Tomas-Weber/My journal-") for f in files)
run("journal", "240", "--on", "2026-09-24", "--no-write", has=["1 entries from 1 students, on 2026-09-24"], lacks=["Finished the drills"])
run("submissions", "240", "assignment 4", has=["Tomas Weber", "submitted", "table.c, Makefile", "95", "due 2026-09-23 23:59"])
run("submissions", "240", "nothing like it", ok=False, has=["no entries match"])
run("submissions", "240", "555", "--out", str(out / "s"), has=["2 files under"])
got = {p.name: p.read_bytes() for p in (out / "s").rglob("*") if p.is_file()}; print("   ", {k: v[:12] for k, v in got.items()})
fails += got != {"table.c": FILES["1"], "Makefile": FILES["2"]}
fails += not (out / "s" / "100002-555 - Tomas-Weber").is_dir()
run("quizzes", "240", has=["1   Quiz 1  active", "2   Quiz 2"])
t = run("quizzes", "240"); row = [l for l in t.splitlines() if l.startswith("1 ")][0]; print("   ", row)
fails += not re.search(r"\b2\s+1\s+3\s+1$", row)   # students 2, twice 1, attempts 3, unfinished 1
run("quiz", "240", "Quiz 1", has=["148.137.0.0 – 148.137.255.255", "password", "set",
                                  "attempts allowed", "2", "time limit", "50",
                                  "Upload your four programs here."])
run("quiz", "240", "Quiz 2", has=["IP restriction", "none — anyone with the link"])
run("quiz", "240", "Quiz", ok=False, has=["several quizzes match"])
run("ping", has=["alive: alovelace"])
run("ping", "--quiet")
run("classlist", "240", has=["Rita Solberg  Student     rs"], lacks=["rs@x"])
run("classlist", "240", "--emails", has=["rs@x"])
run("folders", "999999", ok=False, has=["no such org unit"])

# --- reading the session out of a Firefox profile -------------------------------
def write_store(profile, secure, value=SECRET_VAL, rel="sessionstore-backups/recovery.jsonlz4"):
    """A real mozlz4 session store: the magic, the size, one raw LZ4 block."""
    import lz4.block
    import struct
    body = json.dumps({"cookies": [
        {"host": "127.0.0.1", "name": "d2lSessionVal", "value": value, "path": "/", "httponly": True},
        {"host": "127.0.0.1", "name": "d2lSecureSessionVal", "value": secure, "path": "/", "httponly": True},
        {"host": "elsewhere.example", "name": "d2lSessionVal", "value": "another site", "path": "/"},
        {"host": "127.0.0.1", "name": "d2lSameSiteCanaryA", "value": "1", "path": "/"},
    ]}).encode()
    path = profile / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"mozLz40\0" + struct.pack("<I", len(body))
                     + lz4.block.compress(body, store_size=False))
    return path

if COOKIES_OK:   # a Firefox session carries no XSRF, so it cannot mint; see --paste --xsrf
    prof = pathlib.Path(tempfile.mkdtemp()) / "abc123.default"
    empty = pathlib.Path(tempfile.mkdtemp()) / "nothing.default"
    (empty / "sessionstore-backups").mkdir(parents=True)
    write_store(prof, SECRET)
    run("session", "--profile", str(prof), has=["read from Firefox", "recovery.jsonlz4", "s old",
                                                "accepted: Ada Lovelace", "last time you run this"])
    meta = json.loads((state / "session.json").read_text())
    fails += meta.get("source") != "firefox" or meta.get("profile") != str(prof)
    print("   recorded source:", meta.get("source"), "| profile recorded:", meta.get("profile") == str(prof))
    run("whoami", has=["Ada Lovelace"])

    run("session", "--from-firefox", "--profile", str(empty), ok=False,
        has=["no session store", "Brightspace in"])
    write_store(empty, SECRET, rel="sessionstore-backups/previous.jsonlz4")
    run("session", "--from-firefox", "--profile", str(empty), has=["previous.jsonlz4"])
    run("session", "--profile", str(pathlib.Path(tempfile.mkdtemp()) / "gone"), ok=False,
        has=["no such Firefox profile"])

    # Unnamed, it looks in every place Firefox keeps profiles. A move to the snap
    # copies them over and leaves the originals to go stale, so the same name is in
    # two places and only the newest store holds the session.
    home = pathlib.Path(tempfile.mkdtemp())
    at_home = {"HOME": str(home)}
    stale = write_store(home / ".mozilla/firefox/abc.default-release", "")
    years = stale.stat().st_mtime - 1460 * 86400
    os.utime(stale, (years, years))
    write_store(home / ".var/app/org.mozilla.firefox/.mozilla/firefox/xyz.default", "")
    run("session", "--from-firefox", ok=False, extra=at_home,
        has=["no 127.0.0.1 session in Firefox",
             "~/.var/app/org.mozilla.firefox/.mozilla/firefox/xyz.default: recovery.jsonlz4 ",
             "~/.mozilla/firefox/abc.default-release: recovery.jsonlz4 1460d old"])
    write_store(home / "snap/firefox/common/.mozilla/firefox/abc.default-release", SECRET)
    for named in [(), ("--profile", "abc.default-release")]:
        run("session", "--from-firefox", *named, extra=at_home,
            has=["read from Firefox (~/snap/firefox/common/.mozilla/firefox/abc.default-release, "
                 "recovery.jsonlz4"])

    # The whole point: a session that expires is re-read without being asked.
    run("session", "--profile", str(prof), has=["read from Firefox"])
    ACCEPT["secure"] = "ROTATED"
    run("whoami", ok=False, has=["403", "expired", "still holds the same one"])
    write_store(prof, "ROTATED")
    t = run("whoami", has=["re-read from Firefox", "Ada Lovelace"])
    print("   re-read happened on its own and the command still produced its answer")
    ACCEPT["secure"] = SECRET
    write_store(prof, SECRET)
    run("session", "--profile", str(prof), has=["read from Firefox"])

    # --export: the entry for someone else's keepalive file, alone on stdout so
    # it can go to a clipboard, and a keepalive file exactly as it stands.
    r = subprocess.run([sys.executable, SCRIPT, "session", "--profile", str(prof), "--export"], env=env,
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, start_new_session=True, timeout=60)
    good = (r.returncode == 0
            and r.stdout == f"[ada]\ncookies = d2lSessionVal={SECRET_VAL}; d2lSecureSessionVal={SECRET}\n"
            and all(w in r.stderr for w in ["read from Firefox", "accepted: Ada Lovelace", "send it the way you would send a password"]))
    print(("ok  " if good else "FAIL"), "session --export", f"(exit {r.returncode})")
    if not good:
        fails += 1; print(r.stdout + r.stderr)
    exported = conf / "exported.ini"
    exported.write_text(r.stdout)
    exported.chmod(0o600)
    run("keepalive", "--file", str(exported), has=["ada  alive: alovelace"])
    run("session", "--token", "--export", ok=False, has=["a token is not them"])

# --- the keepalive file: every session in it, each by name -------------------
# Nothing a line holds is ever printed, so each failure below is checked for the
# value it was given as well as for the message.
if COOKIES_OK:
    ka = conf / "keepalive.ini"
    run("keepalive", "--file", str(ka), ok=False, has=[f"no {ka}", "keepalive --help"])
    ka.write_text(f"[me]\nstate = {state}\n\n[colleague]\n"
                  f"cookies = d2lSessionVal={SECRET_VAL}; d2lSecureSessionVal={SECRET}\n")
    ka.chmod(0o644)
    run("keepalive", "--file", str(ka), ok=False, has=["can be read by others", f"chmod 600 {ka}"])
    ka.chmod(0o600)
    run("keepalive", "--file", str(ka), has=["me         alive: alovelace", "colleague  alive: alovelace"])
    t = run("keepalive", "--file", str(ka), "--quiet")
    fails += bool(t.strip()); print("   --quiet says nothing when every session is alive:", not t.strip())
    # A dead session is reported and the rest are still pinged, after it.
    ka.write_text("# a Cookie header pasted whole\n[gone]\n"
                  f"cookies = Cookie: d2lSessionVal={SECRET_VAL}; d2lSecureSessionVal=STALE999\n\n"
                  f"[me]\nstate = {state}\n")
    run("keepalive", "--file", str(ka), "--quiet", ok=False,
        has=[f"gone  dead: Brightspace refused it; put fresh cookies under [gone] in {ka}"],
        lacks=["STALE999", "alive:"])
    run("keepalive", "--file", str(ka), ok=False, has=["gone  dead:", "me    alive: alovelace"], lacks=["STALE999"])
    # A session taken from Firefox repairs itself here as under any command.
    ACCEPT["secure"] = "ROTATED2"
    write_store(prof, "ROTATED2")
    ka.write_text(f"[me]\nstate = {state}\n")
    run("keepalive", "--file", str(ka), "--quiet", has=["re-read from Firefox"], lacks=["dead"])
    ACCEPT["secure"] = SECRET
    write_store(prof, SECRET)
    run("session", "--profile", str(prof), has=["read from Firefox"])
    nowhere, broken = conf / "nostate", conf / "broken"
    broken.mkdir()
    (broken / "session.json").write_text('{"bearer": "LEAKME123"')
    ka.write_text(f"[elsewhere]\nstate = {nowhere}\n\n[broken]\nstate = {broken}\n\n[me]\nstate = {state}\n")
    run("keepalive", "--file", str(ka), ok=False,
        has=[f"elsewhere  dead: no session in {nowhere}; run `brightspace.py session` with BRIGHTSPACE_STATE_DIR={nowhere}",
             f"broken     unreadable: the session in {broken} (JSONDecodeError)", "me         alive"],
        lacks=["LEAKME123"])
    for text, says in [
            ("d2lSecureSessionVal=LEAKME123\n", "line 1: not a [name] or a key = value"),
            ("[x]\ncookie = d2lSessionVal=a; d2lSecureSessionVal=LEAKME123\n", "[x]: no such key cookie"),
            ("[x]\ncookies = d2lSessionVal=LEAKME123\n", "[x]: its cookies have no d2lSecureSessionVal"),
            (f"[x]\nstate = {state}\ncookies = d2lSessionVal=a; d2lSecureSessionVal=LEAKME123\n",
             "[x]: a state or cookies, not both"),
            ("[x]\n", "[x]: a state or cookies, not neither"),
            ("[x]\ncookies = d2lSessionVal=a; d2lSecureSessionVal=LEAKME123\n[x]\n", "line 3: [x] is there twice"),
            ("[x]\ncookies = a\ncookies = d2lSessionVal=a; d2lSecureSessionVal=LEAKME123\n",
             "line 3: cookies is set twice in [x]"),
            ("# nobody yet\n", "names no sessions")]:
        ka.write_text(text)
        run("keepalive", "--file", str(ka), ok=False, has=[says], lacks=["LEAKME123"])

# --- writing ---------------------------------------------------------------
if COOKIES_OK:
    run("new-category", "240", "Midterm", "--weight", "35", "--dry-run",
        has=['"Weight": 35.0', "modelled on the existing category 'Assignments'",
             "weights total 45", "after: 80", "nothing sent"])
    # The token is read off /d2l/home, so first take it off the page: a session
    # that finds none must refuse the write and say how to add one by hand --
    # the console line, since the Storage panel is not where it is found.
    HOME["xsrf"] = False
    run("session", "--profile", str(prof), has=["write token: not found on /d2l/home"])
    run("new-category", "240", "Midterm", "--weight", "35", ok=False,
        has=["Writing needs the XSRF token", "/d2l/home did not carry one",
             "localStorage['XSRF.Token']", "session --xsrf"])
    # The manual way in still works, and asks for the token alone.
    inproc("session --xsrf pastes the token and nothing else",
           ["session", "--profile", str(prof), "--xsrf"], [XSRF],
           has=["read from Firefox", "write token: pasted"])
    HOME["xsrf"] = True
    # The normal case: a Firefox session, handed nothing, finds the token itself
    # and writes. The value is never printed.
    run("session", "--profile", str(prof),
        has=["read from Firefox", "write token: read off /d2l/home"], lacks=[XSRF])
    run("new-category", "240", "Midterm", "--weight", "35",
        has=["created category 'Midterm', id 99"])
    # A token outlives nothing but its session. Rotate it under the tool: the
    # stored one is now wrong, the write is refused once, read again, and lands.
    TOKEN["now"] = "TOKEN456"
    run("new-category", "240", "Midterm", "--weight", "35", ok=False, has=["already exists"])
    run("set-item", "240", "Midterm", "--name", "Midterm Part 1", "--category", "Midterm",
        has=["attached to 7002", "'Midterm' -> 'Midterm Part 1'", "CategoryId: 0 -> 99",
             "now 'Midterm Part 1', category 99"])
    run("set-item", "240", "Midterm Part 1", ok=False, has=["nothing to change"])
    run("set-item", "240", "nope", ok=False, has=["no grade items match"])
    t = run("new-quiz", "240", "Midterm Part 1", "--start", "2026-09-29 12:30",
            "--end", "2026-09-29 13:25", "--minutes", "50", "--attempts", "1",
            "--ip", "148.137.150.0-148.137.150.255",
            has=['"IPRangeStart": "148.137.150.0"', '"IPRangeEnd": "148.137.150.255"',
                 '"NumberOfAttemptsAllowed": 1', '"TimeLimitValue": 50', '"IsActive": false',
                 "created quiz 'Midterm Part 1', id 77", "no API route creates a question",
                 "checked on the server: name, start, end, attempts, time limit, IP range, password, grade item",
                 '"Content": ""', '"Shuffle": true', '"IsAutoSetGraded": true'])
    fails += "2026-09-29T16:30:00.000Z" not in t   # 12:30 EDT is 16:30 UTC
    print("   local start time converted to UTC correctly:", "2026-09-29T16:30:00.000Z" in t)
    run("new-quiz", "240", "Quiz 1", ok=False, has=["already exists"])
    # set-quiz: only the named settings move; everything else, and the questions, stay.
    run("set-quiz", "240", "Midterm Part 1", "--no-shuffle", "--dry-run",
        has=["Shuffle: True -> False", "nothing sent"])
    run("set-quiz", "240", "Midterm Part 1", "--no-shuffle", "--no-auto-publish", "--name", "Midterm P1",
        has=["Name: 'Midterm Part 1' -> 'Midterm P1'", "auto-publish attempt results: True -> False",
             "checked on the server: Name, Shuffle, auto-publish attempt results, StartDate", "questions"])
    run("set-quiz", "240", "Midterm P1", "--shuffle", "--auto-publish", "--name", "Midterm Part 1",
        has=["Shuffle: False -> True", "checked on the server"])
    run("set-quiz", "240", "Midterm Part 1", "--shuffle", ok=False, has=["nothing to change"])
    header = out / "header.html"
    header.write_text("<h4>Available resources</h4><ul><li>the course site</li></ul>")
    run("set-quiz", "240", "Midterm Part 1", "--header", str(header),
        has=["header: 0 chars -> ", "chars, displayed", "checked on the server: header, StartDate"])
    run("new-quiz", "240", "Whatever", "--ip", "nonsense", ok=False, has=["wants a range like"])
    t = run("new-folder", "240", "Assignment 5", "--like", "Assignment 4",
            "--open", "2026-09-30 00:01", "--due", "2026-10-06 23:59:59",
            "--link", "https://example.edu/a5.html", "--dry-run",
            has=['"Name": "Assignment 5"', '"IsHidden": true',
                 '"StartDateAvailabilityType": 0',
                 # the link markup, as JSON escapes it inside the payload
                 'https://example.edu/a5.html</a>',
                 "modelled on 'Assignment 4'", "no grade item", "nothing sent"],
            # The counters are facts about the template, not settings to carry over.
            lacks=["TotalUsersWithSubmissions", '"Id": 555'])
    fails += "2026-10-07T03:59:59.000Z" not in t   # 23:59:59 EDT is 03:59:59 UTC next day
    print("   local due time converted to UTC correctly:", "2026-10-07T03:59:59.000Z" in t)
    run("new-folder", "240", "Assignment 5", "--due", "2026-10-06 23:59:59", "--active",
        has=['"IsHidden": false', "created folder 'Assignment 5', id 88",
             "checked on the server: name, hidden, points, due"])
    # The page link lands in the instructions, in the shape the API accepts; the
    # fake drops the read shape and every link attachment, the way the live one does.
    run("new-folder", "240", "Assignment 7", "--like", "Assignment 4", "--due", "2026-10-20 23:59:59",
        "--link", "https://example.edu/a7.html",
        has=["created folder 'Assignment 7', id 89", "checked on the server: name, hidden, points, due, link"],
        lacks=["LinkAttachments"])
    # A grade item: cloned from a named one, attached to its folder from its own side,
    # so the folder is never rewritten.
    run("new-item", "240", "Assignment 7", "--like", "Assignment 1", "--folder", "Assignment 7", "--dry-run",
        lacks=['"Id": 500', "GradeSchemeUrl", '"Weight"'],
        has=['"Name": "Assignment 7"', '"ToolItemId": 89',
             "modelled on 'Assignment 1', attached to the folder 'Assignment 7'", "nothing sent"])
    run("new-item", "240", "Assignment 7", "--like", "Assignment 1", "--folder", "Assignment 7",
        has=["created grade item 'Assignment 7', id 777",
             "checked on the server: name, points, category, folder's grade item",
             "'Assignments' (10) holds"])
    run("new-item", "240", "Assignment 7", "--like", "Assignment 1", ok=False, has=["already exists"])
    run("new-item", "240", "Assignment 8", "--like", "Assignment 1", "--folder", "Assignment 7",
        ok=False, has=["already has grade item 777"])
    run("new-quiz", "240", "Quiz 3", "--grade-item", "Assignment 7", "--dry-run",
        has=['"GradeItemId": 777', '"AutoExportToGrades": true', '"Content": ""'])
    # Announcements are read, not written: see brightspace.py for why.
    run("announcements", "240", has=["Class 9, Closing Journal", "2026-09-23 15:21", "Scratch", "draft"])
    run("new-folder", "240", "Assignment 5", ok=False, has=["already exists"])
    run("new-folder", "240", "Assignment 6", "--like", "nope", ok=False, has=["no entries match"])
    run("new-folder", "240", "Assignment 6", "--grade-item", "nope", ok=False,
        has=["no grade items match"])

    # The tool must not have dropped the dropbox association while renaming.
    t = run("--json", "set-item", "240", "Midterm Part 1", "--name", "Midterm Part 1", "--dry-run") \
        if False else run("set-item", "240", "Midterm Part 1", "--weight", "35", "--dry-run")
    fails += "attached to 7002" not in t
    print("   the tool it is attached to survived the rename:", "attached to 7002" in t)

    # setup: an assignment's folder and item, with every value read off the site's
    # own files. A site of its own here, so the suite does not move with a course.
    site = out / "site"
    (site / "config").mkdir(parents=True)
    (site / "assignments").mkdir()
    (site / "_quarto.yml").write_text('urls:\n  site:  &site-url  "https://example.edu/f2026"\n')
    listing = site / "assignments.yml"
    listing.write_text('- path: assignments/a8-lists.qmd\n  published: "2026-09-01"\n  due: "2026-12-01"\n'
                       '- path: assignments/a1-intro.qmd\n- path: assignments/a2-maps.qmd\n'
                       '- path: assignments/a9-later.qmd\n  published: "2099-01-01"\n  due: "2099-01-08"\n')
    for page in ("a8-lists", "a1-intro", "a2-maps", "a9-later"):
        (site / "assignments" / f"{page}.qmd").write_text('---\ntitle: "Assignment"\n---\n')
    policy = site / "config" / "brightspace.yml"
    folders_policy = ('assignments:\n  folder: true\n  name: "Assignment {n}"\n  opens: "00:01"\n'
                      '  closes: "23:59:59"\n  visible: true   # shown from its published: day\n')
    policy.write_text(folders_policy)
    # Settings come from the assignment before, whose folder and item both exist.
    run("setup", "240", "a8", "--site", str(site),
        has=['"Name": "Assignment 8"', "https://example.edu/f2026/assignments/a8-lists.html</a>",
             '"IsHidden": false', "modelled on 'Assignment 7'", "nothing sent",
             "Nothing is left to do by hand for the folder and its column"])
    run("setup", "240", "a1", "--site", str(site), ok=False, has=["no assignment before it", "--like"])
    run("setup", "240", "a3", "--site", str(site), ok=False, has=["no entries for a3"])
    run("setup", "240", "a2", "--site", str(site), ok=False, has=["no quoted published: date"])
    # A course that shows its folders still hides one made before its day.
    run("setup", "240", "a9", "--site", str(site), "--like", "Assignment 7",
        has=['"IsHidden": true', "on 2099-01-01, when Assignment 9 goes out",
             "set-folder 240 'Assignment 9' --show"])
    run("setup", "240", "a8", "--site", str(site), "--go",
        has=["created folder 'Assignment 8'", "checked on the server: name, hidden, points, due, opens, link",
             "created grade item 'Assignment 8'", "folder's grade item"])
    run("setup", "240", "a8", "--site", str(site), "--check", lacks=["DIFFERS"])
    # The site can come from the courses file instead of --site.
    run("setup", "withsite", "a8", "--check", lacks=["DIFFERS"])
    run("setup", "same", "a8", ok=False, has=["same has no site in", "pass --site"])
    # A deadline moved on the site and not on Brightspace is what --check is for.
    listing.write_text(listing.read_text().replace("2026-12-01", "2026-12-02"))
    run("setup", "240", "a8", "--site", str(site), "--check", ok=False, has=["2026-12-02 23:59", "DIFFERS"])
    run("setup", "240", "a8", "--site", str(site), "--go", ok=False, has=["already exists"])
    # A course that hands in elsewhere gets the grade item alone, and no dates.
    policy.write_text('assignments:\n  folder: false\n  name: "Assignment {n}"\n')
    run("setup", "240", "a2", "--site", str(site),
        has=['"Name": "Assignment 2"', "modelled on 'Assignment 1'", "a grade item only",
             "Nothing is left to do by hand for the column"], lacks=['"DueDate"'])
    policy.write_text('assignments:\n  folder: true\n  name: "Assignment"\n')
    run("setup", "240", "a8", "--site", str(site), ok=False, has=["has no opens, closes, visible"])

    # set-folder: shown or hidden, and nothing else touched. The fake drops the
    # instructions if they go back in the shape they were read, as D2L does.
    run("set-folder", "240", "Assignment 8", "--hide", "--dry-run",
        has=["hidden from students no -> yes", '"Type": "Html"', "nothing sent"],
        lacks=['"TotalUsers"', '"LinkAttachments"'])
    run("set-folder", "240", "Assignment 8", "--hide",
        has=["checked on the server: hidden, name, due, opens, closes, instructions, grade item, points"])
    run("set-folder", "240", "Assignment 8", "--hide", ok=False, has=["already hidden"])
    run("set-folder", "240", "Assignment 8", "--show", has=["yes -> no", "checked on the server"])
    run("setup", "240", "a8", "--site", str(site.parent / "nowhere"), ok=False,
        has=["is not a course's website repo"], lacks=["Traceback"])
    run("set-folder", "240", "Assignment 8", ok=False, has=["one of the arguments --show --hide is required"])
    FOLDERS.append({"Id": 4999, "Name": "Linked", "IsHidden": True, "DueDate": None, "Availability": None,
                    "LinkAttachments": [{"LinkId": 1, "LinkName": "page", "Href": "https://example.edu/a.html"}]})
    run("set-folder", "240", "Linked", "--show", ok=False, has=["carries an attachment"])

# --- deleting: only a quiz with nothing in it --------------------------------
if COOKIES_OK:
    run("delete-quiz", "240", "Quiz 1", ok=False,
        has=["not deleting", "4 attempts, which are student work", "1 questions",
             "visible to students", "grade item 999"])
    run("delete-quiz", "240", "Quiz 2", has=["0 attempts, 0 questions", "may go; --go deletes it"])
    run("quizzes", "240", has=["Quiz 2"])            # checking deleted nothing
    run("delete-quiz", "240", "Quiz 2", "--go",
        has=["checked on the server: gone from the quiz list", "deleted 'Quiz 2'"])
    run("quizzes", "240", lacks=["Quiz 2"])
    run("delete-quiz", "240", "Quiz 2", "--go", ok=False, has=["no quizzes match"])

run("logout", has=["forgot the session"])
assert not (state / "session.json").exists()
run("whoami", ok=False, has=["no session"])
print("FAILURES:", fails); sys.exit(1 if fails else 0)
