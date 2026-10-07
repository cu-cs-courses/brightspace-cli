"""A fake Brightspace, and brightspace.py run against it as a subprocess.

    nix-shell -p 'python3.withPackages(ps: [ps.lz4 ps.pyyaml])' --run 'python3 test_brightspace.py'
    MOCK_COOKIES_OK=0 ... same ...                 # /d2l/api/ refuses cookies: the token fallback

lz4 is needed because the Firefox cases build a real session store, and because
the interpreter running this is the one the tool is invoked with, bypassing its
shebang. PyYAML is for setup-quiz, which reads a quiz's YAML; without it those
cases are skipped and say so.

The JSON here is the shape the Valence docs give for each route, cut down to
the fields the tool reads; the mock is the record of what was assumed.
"""
import base64, datetime as dt, hashlib, json, os, pathlib, random, re, subprocess, sys, tempfile, threading
import urllib.error, urllib.parse, urllib.request
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


def refused_file_types(body):
    """As the live folder routes, measured 2026-10-03: CustomAllowableFileTypes
    as any list is a binding error, and both file-type fields are otherwise
    taken and ignored."""
    return isinstance(body.get("CustomAllowableFileTypes"), list)


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
VALUES = {}   # (grade item id, user id) -> the value the API returns
NEWS = [{"Id": 900, "Title": "Class 9, Closing Journal", "StartDate": "2026-09-23T19:21:00.000Z",
         "IsPublished": True, "Body": {"Text": "The dot", "Html": "<h2>The dot</h2>"}},
        {"Id": 901, "Title": "Scratch", "StartDate": "2026-12-31T05:01:00.000Z",
         "IsPublished": False, "Body": {"Text": "", "Html": ""}}]
# A quiz's four rich-text fields in the read shape, as the live Quiz 2 returns them.
RICH = {k: {"Text": {"Text": "", "Html": ""}, "IsDisplayed": k != "Instructions"}
        for k in ("Instructions", "Description", "Header", "Footer")}
NEXT = {"folder": 88, "item": 777, "quiz": 5100, "made": 77, "checklist": 899, "checklist part": 9000}
# A shell course for copy-quiz: no students and one quiz, which a copy job puts
# into OU with its questions and without its grade item, as the live one does.
SHELL = 1000500
SHELLS = {"quizzes": [{"QuizId": 5001, "Name": "Quiz 9", "IsActive": False, "DueDate": None,
                       "StartDate": None, "EndDate": "2026-10-09T03:59:00.000Z", **RICH, "Shuffle": True,
                       "IsAutoSetGraded": True, "GradeItemId": 4321,
                       "AttemptsAllowed": {"IsUnlimited": False, "NumberOfAttemptsAllowed": 2}}],
          "people": [{"DisplayName": "Ada L", "ClasslistRoleDisplayName": "Instructor"}], "questions": 3}
JOBS, COPIED = {}, {}                       # copy jobs by token; questions of each copied quiz
ACCESS_KEYS = {"keys": []}                  # Cloudflare Access's public keys, as brightspace-web fetches them
COPYING = {"slow": False, "fail": False}    # a job that reads PROCESSING once; one that ends FAILED
# Checklists by org unit, any org unit: each {"ChecklistId", "Name", "Description",
# "categories": [...], "items": [...]}, the two lists kept in the read shape.
CHECKLISTS = {}
CHECKLISTS_DOWN = set()    # org units whose checklists answer 500, as a course might

def checklist_rich(v):
    """A checklist's rich text as D2L keeps it, measured on a sandbox 2026-10-07:
    RichTextInput or nothing, its HTML rewritten -- a link gains rel="noopener"
    and &mdash; becomes the dash -- and plain text kept as its own HTML."""
    if not (isinstance(v, dict) and "Content" in v and v.get("Type") in ("Text", "Html")):
        return None
    c = v["Content"]
    if v["Type"] == "Html":
        c = c.replace("<a href=", '<a rel="noopener" href=').replace("&mdash;", "\u2014")
    return {"Text": re.sub(r"<[^>]+>", "", c), "Html": c}

def checklist_route(handler, method, ou, rest, body=None):
    """GET and POST under /d2l/api/le/1.99/<ou>/checklists/, as the live routes
    answer them: an item needs a category of its checklist, a due date (null
    will do) and a place; a name is 1 to 512 characters."""
    if ou in CHECKLISTS_DOWN:
        return handler.send(500, {"title": "Internal Server Error"})
    have = CHECKLISTS.setdefault(ou, [])
    binding = {"title": "JSON Binding Error", "status": 400}
    shown = lambda c: {k: c[k] for k in ("ChecklistId", "Name", "Description")}
    m = re.fullmatch(r"(?:(\d+)(?:/(categories|items)/)?)?", rest)
    if not m:
        return handler.send(404, {"Errors": [{"Message": "unknown " + rest}]})
    one = next((c for c in have if str(c["ChecklistId"]) == m.group(1)), None) if m.group(1) else None
    if m.group(1) and not one:
        return handler.send(404, {"title": "Not Found", "detail": f"Checklist {m.group(1)} not found"})
    if method == "GET":
        if not m.group(1):
            return handler.send(200, {"Objects": [shown(c) for c in have], "Next": None})
        if not m.group(2):
            return handler.send(200, shown(one))
        return handler.send(200, {"Objects": one[m.group(2)], "Next": None})
    rich = checklist_rich(body.get("Description"))
    name = body.get("Name")
    if rich is None or not isinstance(name, str):
        return handler.send(400, binding)
    if not name or len(name) > 512:
        return handler.send(400, {"title": "Validation Error", "detail": "Name cannot have more than 512 characters."})
    if not m.group(1):
        NEXT["checklist"] += 1
        have.append({"ChecklistId": NEXT["checklist"], "Name": name, "Description": rich, "categories": [], "items": []})
        return handler.send(200, shown(have[-1]))
    if "SortOrder" not in body:
        return handler.send(400, binding)
    NEXT["checklist part"] += 1
    if m.group(2) == "categories":
        one["categories"].append({"CategoryId": NEXT["checklist part"], "Name": name, "Description": rich,
                                  "SortOrder": body["SortOrder"]})
        return handler.send(200, one["categories"][-1])
    if not isinstance(body.get("CategoryId"), int) or "DueDate" not in body:
        return handler.send(400, binding)
    if body["CategoryId"] not in [c["CategoryId"] for c in one["categories"]]:
        return handler.send(404, {"title": "Not Found", "detail": f"CategoryId {body['CategoryId']} not found"})
    if "refuse me" in name:          # a write that fails halfway through, on purpose
        return handler.send(400, {"title": "Validation Error", "detail": "refused, as asked"})
    one["items"].append({"ChecklistItemId": NEXT["checklist part"], "CategoryId": body["CategoryId"],
                         "ChecklistId": one["ChecklistId"], "Name": name, "Description": rich,
                         "SortOrder": body["SortOrder"], "DueDate": body["DueDate"]})
    return handler.send(200, one["items"][-1])

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
        if p == "/cdn-cgi/access/certs":      # Cloudflare Access's published keys, for brightspace-web
            return self.send(200, ACCESS_KEYS)
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
        m = re.fullmatch(r"/d2l/api/le/1\.99/(\d+)/checklists/(.*)", p)
        if m: return checklist_route(self, "GET", int(m.group(1)), m.group(2))
        m = re.fullmatch(r"/d2l/api/le/1\.99/import/(\d+)/copy/(\w+)", p)
        if m:
            job = JOBS.get(m.group(2))
            if not job: return self.send(404, {"Errors": [{"Message": "Resource Not Found"}]})
            job["reads"] += 1
            if job["slow"] and job["reads"] == 1: return self.send(200, {"Status": "PROCESSING"})
            if not job["done"]:
                job["done"] = True
                for qz in ([] if job["status"] == "FAILED" else SHELLS["quizzes"]):
                    NEXT["quiz"] += 1
                    QUIZZES_MADE.append(dict(qz, QuizId=NEXT["quiz"], GradeItemId=None))
                    COPIED[NEXT["quiz"]] = SHELLS["questions"]
            return self.send(200, {"Status": job["status"]})
        sb = f"/d2l/api/le/1.99/{SHELL}/"
        if p.startswith(sb):
            r = p[len(sb):]
            if r == "quizzes/": return self.send(200, {"Objects": SHELLS["quizzes"], "Next": None})
            if r == "classlist/": return self.send(200, SHELLS["people"])
            if re.fullmatch(r"quizzes/\d+/questions/", r):
                return self.send(200, {"Objects": [{"QuestionTypeId": 8}] * SHELLS["questions"], "Next": None})
            if re.fullmatch(r"quizzes/\d+/attempts/", r): return self.send(200, {"Objects": [], "Next": None})
            return self.send(404, {"Errors": [{"Message": "unknown " + r}]})
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
            made = COPIED.get(int(re.search(r"\d+", r).group()), 0)
            return self.send(200, {"Objects": [{"QuestionTypeId": 8}] * made, "Next": None})
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
        if r == "classlist/": return self.send(200, [{"Identifier": "100001", "DisplayName": "Rita Solberg", "FirstName": "Rita", "LastName": "Solberg", "ClasslistRoleDisplayName": "Student", "Username": "rs", "Email": "rs@x"},
                                                      {"Identifier": "100002", "DisplayName": "Tomas Weber", "FirstName": "Tomas", "LastName": "Weber", "ClasslistRoleDisplayName": "Student", "Username": "tw", "Email": "tw@x"},
                                                      {"Identifier": "77", "DisplayName": "Ada L", "FirstName": "Ada", "LastName": "L", "ClasslistRoleDisplayName": "Instructor", "Username": "al", "Email": "al@x"}])
        m = re.fullmatch(r"grades/(\d+)/values/(\d+)", r)
        if m:
            got = VALUES.get((int(m.group(1)), m.group(2)))
            return self.send(200, got) if got else self.send(404, {"Errors": [{"Message": "Not Found"}]})
        # A quiz the run itself created has no attempts and no questions yet.
        m = re.fullmatch(r"quizzes/(\d+)/(attempts|questions)/", r)
        if m and int(m.group(1)) in {x["QuizId"] for x in QUIZZES_MADE}:
            return self.send(200, {"Objects": [], "Next": None})
        return self.send(404, {"Errors": [{"Message": "unknown " + r}]})
    def do_DELETE(self):
        base = f"/d2l/api/le/1.99/{OU}/"
        if not self.headers.get("X-Csrf-Token"):
            return self.send(403, "CSRF token required", "text/plain")
        sb = f"/d2l/api/le/1.99/{SHELL}/quizzes/"
        if self.path.startswith(sb):
            SHELLS["quizzes"] = [x for x in SHELLS["quizzes"] if str(x["QuizId"]) != self.path[len(sb):]]
            return self.send(200, "", "text/plain")
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
            if refused_file_types(body):
                return self.send(400, {"title": "JSON Binding Error"})
            body = {k: v for k, v in body.items() if k not in ("AllowableFileType", "CustomAllowableFileTypes")}
            for i, f in enumerate(FOLDERS):
                if f["Id"] == int(mf.group(1)):
                    FOLDERS[i] = body | {k: f[k] for k in ("Id", "TotalUsers", "TotalUsersWithSubmissions") if k in f} | {
                        "Attachments": [], "LinkAttachments": [],
                        "CustomInstructions": stored_rich(body.get("CustomInstructions"))}
                    return self.send(200, FOLDERS[i])
            return self.send(404, {"Errors": [{"Message": "no such folder"}]})
        mv = re.fullmatch(r"grades/(\d+)/values/(\d+)", self.path[len(base):]) if self.path.startswith(base) else None
        if mv:
            # Kept as the API returns it: comments in the read shape, and a
            # value sent in any other shape than RichTextInput dropped.
            if body.get("GradeObjectType") != 1 or not isinstance(body.get("PointsNumerator"), (int, float)):
                return self.send(400, {"Errors": [{"Message": "Invalid grade value"}]})
            VALUES[(int(mv.group(1)), mv.group(2))] = {
                "PointsNumerator": body["PointsNumerator"], "GradeObjectType": 1,
                "Comments": stored_rich(body.get("Comments")), "PrivateComments": stored_rich(body.get("PrivateComments"))}
            return self.send(200, "", "text/plain")
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
        m = re.fullmatch(r"/d2l/api/le/1\.99/(\d+)/checklists/(.*)", self.path)
        if m:
            if self.headers.get("X-Csrf-Token") != TOKEN["now"]:
                return self.send(403, "CSRF token required", "text/plain")
            if not self.api_ok():
                return self.send(403, '{ Errors: [ {Message: "Forbidden"} ] }', "text/html")
            return checklist_route(self, "POST", int(m.group(1)), m.group(2), json.loads(raw))
        m = re.fullmatch(r"/d2l/api/le/1\.99/import/(\d+)/copy/", self.path)
        if m:
            if self.headers.get("X-Csrf-Token") != TOKEN["now"]:
                return self.send(403, "CSRF token required", "text/plain")
            body = json.loads(raw)
            if int(m.group(1)) != OU or body != {"SourceOrgUnitId": SHELL, "Components": ["Quizzes"]}:
                return self.send(400, {"Errors": [{"Message": "not the copy this suite expects: " + json.dumps(body)}]})
            token = str(672000 + len(JOBS))
            JOBS[token] = {"reads": 0, "done": False, "slow": COPYING["slow"],
                           "status": "FAILED" if COPYING["fail"] else "COMPLETE"}
            return self.send(200, {"JobToken": token})
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
        if self.path == f"/d2l/api/le/1.99/{SHELL}/quizzes/":
            if self.headers.get("X-Csrf-Token") != TOKEN["now"]:
                return self.send(403, "CSRF token required", "text/plain")
            body = json.loads(raw)
            if "AttemptsAllowed" in body:
                return self.send(400, {"title": "JSON Binding Error"})
            made = {k: v for k, v in body.items() if k != "NumberOfAttemptsAllowed"} | {
                "QuizId": NEXT["made"], "AttemptsAllowed": {"IsUnlimited": False,
                                                            "NumberOfAttemptsAllowed": body.get("NumberOfAttemptsAllowed")}}
            for key in ("Instructions", "Description", "Header", "Footer"):
                if isinstance(body.get(key), dict):
                    made[key] = {"Text": stored_rich(body[key]["Text"]), "IsDisplayed": body[key].get("IsDisplayed")}
            NEXT["made"] += 1
            SHELLS["quizzes"].append(made)
            return self.send(200, made)
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
                    "QuizId": NEXT["made"], "AttemptsAllowed": {"IsUnlimited": body.get("NumberOfAttemptsAllowed") is None,
                                                      "NumberOfAttemptsAllowed": body.get("NumberOfAttemptsAllowed")}}
                for key in ("Instructions", "Description", "Header", "Footer"):
                    if isinstance(body.get(key), dict):
                        made[key] = {"Text": stored_rich(body[key]["Text"]), "IsDisplayed": body[key].get("IsDisplayed")}
                QUIZZES_MADE.append(made)
                NEXT["made"] += 1
                return self.send(200, made)
            if r == "dropbox/folders/":
                if refused_file_types(body):
                    return self.send(400, {"title": "JSON Binding Error"})
                body = {k: v for k, v in body.items()
                        if k not in ("AllowableFileType", "CustomAllowableFileTypes")}
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
                   f"[withsite]\nsite = {out / 'site'}\nou = {OU}\n\n[shell]\nou = {SHELL}\n")
DEFAULTS = conf / "quiz-defaults.yml"
env = dict(os.environ, BRIGHTSPACE_STATE_DIR=str(state), BRIGHTSPACE_URL=f"http://127.0.0.1:{port}",
           BRIGHTSPACE_USER="ada", BRIGHTSPACE_COURSES=str(COURSES), BRIGHTSPACE_QUIZ_DEFAULTS=str(DEFAULTS))
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
run("folders", "nope", ok=False, has=["unknown course 'nope'", "240, 120, same, login-only, withsite, shell"])
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
    run("set-item", "240", "Midterm Part 1", "--points", "9",
        has=["MaxPoints:", "-> 9.0", "checked on the server: maxpoints, still attached to"])
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

    # copy-quiz: the one quiz in a shell course, into a section, whole.
    run("copy-quiz", "shell", "--to", "240",
        has=["'Quiz 9' in shell: 3 questions, hidden, end 2026-10-08 23:59",
             "into 240: no grade item, since it has none called 'Quiz 9'", "nothing sent"])
    run("copy-quiz", "shell", "--to", "240", "--item-like", "nope", ok=False, has=["240 has no grade item 'nope'"])
    run("copy-quiz", "shell", "--to", "shell", ok=False, has=["shell is the shell itself"])
    SHELLS["people"].append({"DisplayName": "Una S", "ClasslistRoleDisplayName": "Student"})
    run("copy-quiz", "shell", "--to", "240", ok=False, has=["shell has 1 students, so it is a section"])
    SHELLS["people"].pop()
    SHELLS["quizzes"].append(dict(SHELLS["quizzes"][0], QuizId=5002, Name="Quiz 10"))
    run("copy-quiz", "shell", "--to", "240", ok=False,
        has=["shell holds 2 quizzes ('Quiz 9', 'Quiz 10')", "exactly the one to copy"])
    SHELLS["quizzes"].pop()
    COPYING["slow"] = True      # the job reads PROCESSING once, so the wait is exercised
    run("copy-quiz", "shell", "--to", "240", "--item-like", "Assignment 1", "--go",
        has=["into 240: attached to a new grade item 'Quiz 9', shaped like 'Assignment 1'",
             "copied, quiz 5101 (job 672000)", "made grade item 'Quiz 9'", "shaped like 'Assignment 1'",
             "checked on the server: name, questions, shown, dates, grade item",
             "'Quiz 9' is in 240; the shell still holds it, and --clear empties it"])
    COPYING["slow"] = False
    fails += QUIZZES_MADE[-1]["GradeItemId"] != ITEMS[-1]["Id"] or ITEMS[-1]["Name"] != "Quiz 9"
    print("   the copy sends its scores to the new item:", QUIZZES_MADE[-1]["GradeItemId"] == ITEMS[-1]["Id"])
    run("copy-quiz", "shell", "--to", "240", "--go", ok=False,
        has=["240 already has a quiz named 'Quiz 9'", "nothing has been copied anywhere"])
    # An item already named like the quiz is the one it attaches to; --clear empties the shell.
    run("new-item", "240", "Quiz 11", "--like", "Assignment 1")
    SHELLS["quizzes"][0]["Name"] = "Quiz 11"
    run("copy-quiz", "shell", "--to", "240", "--go", "--clear",
        has=["into 240: attached to its grade item 'Quiz 11'", "checked on the server: gone from the shell",
             "'Quiz 11' is in 240; the shell is empty again"], lacks=["made grade item"])
    fails += SHELLS["quizzes"] != []; print("   the shell is empty:", SHELLS["quizzes"] == [])
    run("copy-quiz", "shell", "--to", "240", ok=False, has=["shell holds 0 quizzes"])
    # A job that fails leaves the section as it was and says so.
    SHELLS["quizzes"].append({"QuizId": 5003, "Name": "Quiz 12", "IsActive": True, **RICH,
                              "AttemptsAllowed": {"IsUnlimited": False, "NumberOfAttemptsAllowed": 1}})
    COPYING["fail"] = True
    run("copy-quiz", "shell", "--to", "240", "--go", "--clear", ok=False,
        has=["ended FAILED, so nothing else was done here", "not done in 240; the shell keeps its quiz"])
    COPYING["fail"] = False
    fails += len(SHELLS["quizzes"]) != 1; print("   the shell keeps its quiz after a failure:", len(SHELLS["quizzes"]) == 1)

    # --- brightspace-web: the page over copy-quiz ------------------------------------
    WEB = str(pathlib.Path(SCRIPT).with_name("brightspace-web.py"))
    webconf = pathlib.Path(tempfile.mkdtemp())
    (webconf / "brightspace").mkdir(mode=0o700)
    (webconf / "brightspace" / "keepalive.ini").write_text(f"[me]\nstate = {state}\n")
    (webconf / "brightspace" / "keepalive.ini").chmod(0o600)
    webenv = dict(env, XDG_CONFIG_HOME=str(webconf))
    run("whoami", extra={"XDG_CONFIG_HOME": str(webconf), "BRIGHTSPACE_SESSION": "me"},
        has=["Ada Lovelace (alovelace)"])
    run("whoami", extra={"XDG_CONFIG_HOME": str(webconf), "BRIGHTSPACE_SESSION": "nobody"}, ok=False,
        has=["no session [nobody] in"])

    def start_web(*args):
        proc = subprocess.Popen([sys.executable, WEB, "--port", "0", "--no-browser", *args], env=webenv,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        first = proc.stdout.readline()
        return proc, int(re.search(r"127\.0\.0\.1:(\d+)", first).group(1)), first

    def fetch(port, host, path="/", form=None, headers=None):
        data = urllib.parse.urlencode(form, doseq=True).encode() if form is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers={"Host": host, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def web_check(label, got, code, has=(), lacks=()):
        global fails
        good = got[0] == code and all(h in got[1] for h in has) and not any(x in got[1] for x in lacks)
        print(("ok  " if good else "FAIL"), label, f"({got[0]})")
        if not good:
            fails += 1; print(got[1][-1500:])

    proc, wport, first = start_web()
    here = f"127.0.0.1:{wport}"
    got = fetch(wport, here)
    web_check("web, on one's own machine: the page, acting as the session", got, 200,
              has=["Acting as Ada Lovelace (alovelace)", 'value="shell"', 'value="240"'])
    token = re.search(r'name="token" value="([^"]+)"', got[1]).group(1)
    web_check("web: a request under another name is refused", fetch(wport, "evil.example"), 403, has=["not served under that name"])
    web_check("web: a form without the page's token is refused",
              fetch(wport, here, "/copy-quiz", {"shell": "shell", "to": "240", "action": "check"}), 403,
              has=["Reload the page"])
    web_check("web: a form from another site is refused",
              fetch(wport, here, "/copy-quiz", {"token": token, "shell": "shell", "to": "240"},
                    {"Origin": "https://evil.example"}), 403, has=["came from another site"])
    web_check("web: a course is one of the courses file's, never an option",
              fetch(wport, here, "/copy-quiz", {"token": token, "shell": "--help", "to": "240", "action": "check"}),
              200, has=["Not run", "is not one of your courses"])
    web_check("web: Check runs copy-quiz without --go and shows what it printed",
              fetch(wport, here, "/copy-quiz", {"token": token, "shell": "shell", "to": ["240"],
                                                "item_like": "Assignment 1", "action": "check"}), 200,
              has=["Checked: nothing sent", "brightspace.py copy-quiz shell --to 240 --item-like=&#x27;Assignment 1&#x27;",
                   "into 240: attached to a new grade item", "nothing sent; --go copies it"])
    proc.terminate(); proc.wait()

    # Behind Cloudflare Access: a token Access signed, checked here. An RSA key of
    # the suite's own stands in for Access's, its public half served as Access
    # serves it.
    def prime(bits):
        while True:
            c = random.getrandbits(bits) | (1 << (bits - 1)) | 1
            if all(c % sp for sp in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)):
                d, r = c - 1, 0
                while d % 2 == 0:
                    d, r = d // 2, r + 1
                for _ in range(24):
                    x = pow(random.randrange(2, c - 1), d, c)
                    if x in (1, c - 1):
                        continue
                    for _ in range(r - 1):
                        x = pow(x, 2, c)
                        if x == c - 1:
                            break
                    else:
                        break
                else:
                    return c

    def rsa():
        while True:
            p_, q_ = prime(512), prime(512)
            phi = (p_ - 1) * (q_ - 1)
            if phi % 65537:
                return p_ * q_, pow(65537, -1, phi)

    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    (N, D), (N2, D2) = rsa(), rsa()

    def jwt(claims, n=N, d=D, kid="k1"):
        head = b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
        body = b64(json.dumps(claims).encode())
        k = (n.bit_length() + 7) // 8
        info = bytes.fromhex("3031300d060960864801650304020105000420")
        em = b"\x00\x01" + b"\xff" * (k - 3 - len(info) - 32) + b"\x00" + info + hashlib.sha256(f"{head}.{body}".encode()).digest()
        return f"{head}.{body}.{b64(pow(int.from_bytes(em, 'big'), d, n).to_bytes(k, 'big'))}"
    ACCESS_KEYS["keys"] = [{"kid": "k1", "kty": "RSA", "alg": "RS256",
                            "n": b64(N.to_bytes(128, "big")), "e": b64((65537).to_bytes(3, "big"))}]
    webini = webconf / "web.ini"
    webini.write_text(f"[access]\nteam = test.cloudflareaccess.com\naud = the-aud\nhost = bs.example.test\n"
                      f"certs = http://127.0.0.1:{port}/cdn-cgi/access/certs\n\n"
                      f"[user ada@example.edu]\nsession = me\ncourses = {COURSES}\n")
    proc, wport, first = start_web("--access", str(webini))
    good = {"email": "ada@example.edu", "aud": ["the-aud"], "iss": "https://test.cloudflareaccess.com",
            "exp": int(dt.datetime.now().timestamp()) + 600}
    asks = lambda claims=good, **kw: {"Cf-Access-Jwt-Assertion": jwt(claims, **kw)}
    web_check("web behind Access: no token, no page", fetch(wport, "bs.example.test"), 403,
              has=["only reached through Cloudflare Access"])
    got = fetch(wport, "bs.example.test", headers=asks())
    web_check("web behind Access: a token Access signed for a listed user", got, 200,
              has=["Acting as Ada Lovelace (alovelace)", 'value="240"'])
    token = re.search(r'name="token" value="([^"]+)"', got[1]).group(1)
    web_check("web behind Access: a token for another application", 
              fetch(wport, "bs.example.test", headers=asks(dict(good, aud=["other"]))), 403, has=["another application"])
    web_check("web behind Access: a token signed by a key Access does not publish",
              fetch(wport, "bs.example.test", headers=asks(n=N2, d=D2, kid="k9")), 403, has=["not signed by a key"])
    web_check("web behind Access: a token signed by the wrong key under the right name",
              fetch(wport, "bs.example.test", headers=asks(n=N2, d=D2)), 403, has=["signature does not check out"])
    web_check("web behind Access: an expired token",
              fetch(wport, "bs.example.test", headers=asks(dict(good, exp=1))), 403, has=["expired"])
    web_check("web behind Access: someone Access let in who is not listed",
              fetch(wport, "bs.example.test", headers=asks(dict(good, email="bob@example.edu"))), 403,
              has=["you signed in as bob@example.edu, who is not listed"])
    web_check("web behind Access: under the loopback name, refused",
              fetch(wport, f"127.0.0.1:{wport}", headers=asks()), 403, has=["not served under that name"])
    web_check("web behind Access: Check, run as the user's own session and courses",
              fetch(wport, "bs.example.test", "/copy-quiz",
                    {"token": token, "shell": "shell", "to": "240", "action": "check"},
                    {**asks(), "Origin": "https://bs.example.test"}), 200,
              has=["Checked: nothing sent", "brightspace.py copy-quiz shell --to 240"])
    proc.terminate(); proc.wait()
    webini.write_text(f"[user ada@example.edu]\nsession = me\ncourses = {COURSES}\n")
    proc, wport, first = start_web("--access", str(webini))
    web_check("web behind Access, before web.ini names the application: everything refused",
              fetch(wport, "bs.example.test", headers=asks()), 403, has=["does not yet say which Access application"])
    print(("ok  " if "so everything is refused" in first else "FAIL"), "web: it says on starting that it refuses everything")
    fails += "so everything is refused" not in first
    proc.terminate(); proc.wait()
    # Announcements are read, not written: see brightspace.py for why.
    run("announcements", "240", has=["Class 9, Closing Journal", "2026-09-23 15:21", "Scratch", "draft"])
    run("new-folder", "240", "Assignment 5", ok=False, has=["already exists"])
    # A folder restricted to some file types reads them back as a list, which a
    # write refuses outright; the clone goes without them, and says so.
    FOLDERS.append({"Id": 557, "Name": "Only arr", "IsHidden": False, "DueDate": None,
                    "Availability": None, "AllowableFileType": 5,
                    "CustomAllowableFileTypes": [".arr"], "TotalUsers": 10})
    run("new-folder", "240", "Practice", "--like", "Only arr", "--dry-run",
        has=["takes .arr files only, which the API cannot set", "nothing sent"],
        lacks=['"CustomAllowableFileTypes"', '"AllowableFileType"'])
    run("new-folder", "240", "Practice", "--like", "Only arr",
        has=["created folder 'Practice'", "checked on the server"])
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
    # What a second submission does, alone or beside showing and hiding.
    run("set-folder", "240", "Assignment 8", "--submissions", "keep-all", "--dry-run",
        has=['"SubmissionsRule": 2', "-> keep-all", "nothing sent"], lacks=["hidden from students"])
    run("set-folder", "240", "Assignment 8", "--submissions", "keep-all",
        has=["-> keep-all", "grade item, points, submission type, resubmissions"])
    run("set-folder", "240", "Assignment 8", "--submissions", "keep-all", ok=False, has=["already keep-all"])
    run("set-folder", "240", "Assignment 8", "--hide", "--submissions", "overwrite",
        has=["hidden from students no -> yes; a second submission keep-all -> overwrite", "checked on the server"])
    run("set-folder", "240", "Assignment 8", "--show", "--submissions", "overwrite",
        has=["yes -> no"], lacks=["a second submission"])
    run("setup", "240", "a8", "--site", str(site.parent / "nowhere"), ok=False,
        has=["is not a course's website repo"], lacks=["Traceback"])
    run("set-folder", "240", "Assignment 8", ok=False, has=["say what to change: --show, --hide or --submissions"])
    FOLDERS.append({"Id": 4999, "Name": "Linked", "IsHidden": True, "DueDate": None, "Availability": None,
                    "LinkAttachments": [{"LinkId": 1, "LinkName": "page", "Href": "https://example.edu/a.html"}]})
    run("set-folder", "240", "Linked", "--show", ok=False, has=["carries an attachment"])
    run("set-folder", "240", "Only arr", "--hide", ok=False,
        has=["takes .arr files only, which the API cannot send back"])

    # setup-quiz: a quiz and its grade item, from the brightspace: block in the
    # quiz's own YAML, which bs-yaml-quiz reads for it.
    try:
        import yaml  # noqa: F401
    except ImportError:
        yaml = None
        print("SKIP setup-quiz and its web form: this Python has no PyYAML; run the suite as its docstring says")
    if yaml:
        qdir = out / "quiz"
        qdir.mkdir()
        QUESTIONS = ('questions:\n- title: Q1 What it prints\n  type: SA\n'
                     '  text: What does `printf("%d", 2 + 3)` print?\n  answers:\n  - 5\n  - five\n'
                     '- title: Q2 Why\n  type: MC\n  points: 2\n  text: Why?\n'
                     '  options:\n  - 100: Because.\n  - 0: Because not.\n')

        def quiz_yml(fname, questions=QUESTIONS, description=True, **keys):
            keys = {"course": "240", "name": "Quiz 13", "like": "Quiz 11", "start": "2026-10-13 12:30",
                    "end": "2026-10-13 14:00", "minutes": "8", "attempts": "2", "points": "8"} | keys
            text = "brightspace:\n" + "".join(f"  {k}: {v}\n" for k, v in keys.items() if v is not None)
            if description:
                text += "  description: |\n    Two questions on **pointers**.\n\n    *Two attempts*, both today.\n"
            (qdir / fname).write_text(text + "\n" + questions)
            return str(qdir / fname)

        q13 = quiz_yml("q13.yml")
        before = (len(QUIZZES_MADE), len(ITEMS))
        run("setup-quiz", q13,
            has=["Quiz 13 in 240: 2 questions from q13.yml, worth 3",
                 "opens 2026-10-13 12:30, closes 2026-10-13 14:00; 8 minutes, 2 attempts",
                 "settings copied from 'Quiz 11'; scores to a grade item 'Quiz 13', out of 8; a description",
                 '"Name": "Quiz 13"', '"MaxPoints": 8.0', '"NumberOfAttemptsAllowed": 2', '"TimeLimitValue": 8',
                 "<p>Two questions on <strong>pointers</strong>.</p><p><em>Two attempts</em>, both today.</p>",
                 "modelled on 'Quiz 11', with its instructions and header emptied and its description written",
                 "no grade item: --go attaches the one made above", "nothing sent",
                 "Upload a File -> q13.csv, which bs-yaml-quiz.py", "Quiz 13 -> Add Existing -> the 2 questions",
                 "none came from 'Quiz 11'", "--check reads it all back"])
        fails += (len(QUIZZES_MADE), len(ITEMS)) != before
        print("   a plan sends nothing:", (len(QUIZZES_MADE), len(ITEMS)) == before)
        # The CSV beside the file, once made, is named without a word; stale, it is said.
        subprocess.run([sys.executable, str(pathlib.Path(SCRIPT).parent / "extras" / "bs-yaml-quiz" / "bs-yaml-quiz.py"),
                        q13], check=True, capture_output=True)
        run("setup-quiz", q13, has=["Upload a File -> q13.csv.\n"])
        run("setup-quiz", q13, "--go",
            has=["created grade item 'Quiz 13'", "created quiz 'Quiz 13'",
                 "checked on the server: name, start, end, attempts, time limit, IP range, password, grade item, "
                 "description"], lacks=["no grade item:"])
        made = QUIZZES_MADE[-1]
        fine = made["GradeItemId"] == ITEMS[-1]["Id"] and ITEMS[-1]["MaxPoints"] == 8.0 and made["AutoExportToGrades"]
        fails += not fine; print("   the quiz sends its scores to the new item, out of 8:", fine)
        # Its questions go in by hand, so --check finds none until they do.
        t = run("setup-quiz", q13, "--check", ok=False, has=["DIFFERS"])
        fails += not re.search(r"\nquestions +2 +0 +DIFFERS\n", t)
        print("   the questions not added yet are the one difference:", t.count("DIFFERS") == 1)
        COPIED[made["QuizId"]] = 2
        run("setup-quiz", q13, "--check", has=["2026-10-13 14:00", "8 minutes", "Quiz 13", "q13.csv", "current"],
            lacks=["DIFFERS"])
        # A date moved in the file and not on Brightspace, and a question changed
        # after the CSV was made, are what --check is for.
        quiz_yml("q13.yml", end="2026-10-13 14:15", questions=QUESTIONS.replace("- five", "- five\n  - 5.0"))
        run("setup-quiz", q13, "--check", ok=False, has=["2026-10-13 14:15", "2026-10-13 14:00", "stale", "DIFFERS"])
        run("setup-quiz", q13, "--go", ok=False, has=["240 already has a quiz called 'Quiz 13'", "--check reads it back"])
        run("setup-quiz", q13, "--go", "--check", ok=False, has=["--go or --check, not both"])
        # An item of the quiz's name already there is the one it goes to.
        run("new-item", "240", "Quiz 14", "--like", "Assignment 1")
        run("setup-quiz", quiz_yml("q14.yml", name="Quiz 14"), "--go",
            has=["The grade item 'Quiz 14' is already there, out of 10; the quiz goes to it",
                 "It is not out of 8, as the plan says", "created quiz 'Quiz 14'"], lacks=["created grade item"])
        # A quiz copied from one with no grade item needs one made first, or the file to say none.
        run("setup-quiz", quiz_yml("q15.yml", name="Quiz 15", like="Quiz 2", points=None), ok=False,
            has=["'Quiz 2' sends its scores to no grade item", "grade_item: none"])
        run("setup-quiz", quiz_yml("q15.yml", name="Quiz 15", like="Quiz 2", points=None, grade_item="none"), "--go",
            has=["no grade item: the plan says grade_item: none", "created quiz 'Quiz 15'"],
            lacks=["created grade item"])
        # What the block refuses, each before anything is read off Brightspace.
        for keys, why in [({"atempts": "2"}, "brightspace: has atempts, which setup-quiz does not know"),
                          ({"name": None}, "brightspace: has no name"),
                          ({"start": "tomorrow"}, "start: 'tomorrow' is not a date and time"),
                          ({"minutes": "eight"}, "minutes: is a whole number, not 'eight'"),
                          ({"ip": "campus"}, "ip: is a range like"),
                          ({"grade_item": "Quiz 13"}, "grade_item: is none, or left out"),
                          ({"grade_item": "none"}, "points: is what its grade item is out of"),
                          ({"course": "nope"}, "nope")]:
            run("setup-quiz", quiz_yml("bad.yml", **({"name": "Quiz 16"} | keys)), ok=False, has=[why],
                lacks=["Traceback"])
        broken = pathlib.Path(quiz_yml("bad.yml", name="Quiz 16", description=False))
        broken.write_text(broken.read_text().replace("\n\nquestions:", "\n  description: |\n    ```\n    open\n\nquestions:"))
        run("setup-quiz", str(broken), ok=False, has=["description: a block opened with ``` is never closed"],
            lacks=["Traceback"])
        (qdir / "bare.yml").write_text(QUESTIONS)
        run("setup-quiz", str(qdir / "bare.yml"), ok=False, has=["has no brightspace: block"])
        run("setup-quiz", quiz_yml("bad.yml", name="Quiz 16", questions=QUESTIONS.replace("type: MC", "type: XX")),
            ok=False, has=["question 2"], lacks=["Traceback"])
        run("setup-quiz", str(qdir / "nowhere.yml"), ok=False, has=["nowhere.yml"], lacks=["Traceback"])

        # The same file on the page: pasted or chosen, then checked, made or compared.
        proc, wport, first = start_web()
        here = f"127.0.0.1:{wport}"
        got = fetch(wport, here, "/quizzes")
        web_check("web: the page has both forms", got, 200,
                  has=['action="/setup-quiz#', 'name="yaml"', 'action="/copy-quiz#', 'action="/make#', 'action="/defaults#'])
        token = re.search(r'name="token" value="([^"]+)"', got[1]).group(1)
        text16 = pathlib.Path(quiz_yml("q16.yml", name="Quiz 16")).read_text().replace("\n", "\r\n")
        web_check("web: a quiz file without the page's token is refused",
                  fetch(wport, here, "/setup-quiz", {"yaml": text16, "action": "plan"}), 403, has=["Reload the page"])
        before = len(QUIZZES_MADE)
        got = fetch(wport, here, "/setup-quiz", {"token": token, "yaml": text16, "filename": "q16.yml", "action": "plan"})
        web_check("web: Check runs setup-quiz without --go, with the CSV to download", got, 200,
                  has=["Checked: nothing sent", "brightspace.py setup-quiz q16.yml",
                       "Quiz 16 in 240: 2 questions from q16.yml", 'download="q16.csv"', "data:text/csv;",
                       "Upload a File -&gt; q16.csv.", "Two questions on **pointers**."],
                  lacks=["setup-quiz-"])
        csv16 = base64.b64decode(re.search(r"base64,([A-Za-z0-9+/=]+)", got[1]).group(1)).decode()
        fails += not csv16.startswith("NewQuestion,SA") or "\r\n" not in csv16
        print("   the download is the question-import CSV:", csv16.startswith("NewQuestion,SA"))
        fails += len(QUIZZES_MADE) != before; print("   checking sent nothing:", len(QUIZZES_MADE) == before)
        web_check("web: Make it makes the quiz and its grade item",
                  fetch(wport, here, "/setup-quiz", {"token": token, "yaml": text16, "filename": "q16.yml",
                                                     "action": "go"}), 200,
                  has=["Made", "brightspace.py setup-quiz q16.yml --go", "created quiz &#x27;Quiz 16&#x27;"])
        web_check("web: Compare reads it back, the questions not in yet",
                  fetch(wport, here, "/setup-quiz", {"token": token, "yaml": text16, "filename": "q16.yml",
                                                     "action": "check"}), 200,
                  has=["Not what the plan says", "--check", "DIFFERS"])
        web_check("web: a file name that could be an option is not used as one",
                  fetch(wport, here, "/setup-quiz", {"token": token, "yaml": text16, "filename": "-x.yml",
                                                     "action": "plan"}), 200,
                  has=["brightspace.py setup-quiz quiz.yml", "already has a quiz called &#x27;Quiz 16&#x27;"])
        web_check("web: a file with no block says why, and offers no CSV",
                  fetch(wport, here, "/setup-quiz", {"token": token, "yaml": QUESTIONS, "action": "plan"}), 200,
                  has=["Stopped", "has no brightspace: block"], lacks=["download="])
        web_check("web: an empty box is not run",
                  fetch(wport, here, "/setup-quiz", {"token": token, "yaml": " ", "action": "plan"}), 200,
                  has=["Not run", "paste it into the box"])
        proc.terminate(); proc.wait()

        # quiz-defaults: a block per course, in the keys of a quiz's block; the
        # lines of the keys it is given change, and no others.
        run("quiz-defaults", has=["no defaults in"])
        DEFAULTS.write_text("# Ours, with a comment that must stay.\n\n120:\n  # why eight\n  minutes: 8\n")
        run("quiz-defaults", "240", "--set", "start=12:30", "--set", "end=14:00", "--set", "minutes=8",
            "--set", "attempts=2", "--set", "points=8",
            has=["wrote", "240's start set, end set, minutes set, attempts set, points set",
                 "240     start", "12:30"], lacks=["120     minutes"])
        text = DEFAULTS.read_text()
        fine = text.startswith("# Ours, with a comment that must stay.\n\n120:\n  # why eight\n  minutes: 8\n")
        fails += not fine; print("   the file's own lines and comments stay as they were:", fine)
        run("quiz-defaults", "240", "--set", "description=Two **attempts**.\n\nBoth today.", "--unset", "points",
            has=["description set", "points removed", "Two **attempts**. …"], lacks=["240     points"])
        fails += "  description: |\n    Two **attempts**.\n\n    Both today.\n" not in DEFAULTS.read_text()
        print("   a description is a block of its own lines:", "    Both today." in DEFAULTS.read_text())
        for argv, why in [(["240", "--set", "start=noon"], "start: is a time of day like 14:00"),
                          (["240", "--set", "colour=red"], "colour: is not a default"),
                          (["240", "--set", "minutes=eight"], "minutes: is a whole number"),
                          (["240", "--set", "sections=240 nowhere"], "unknown course 'nowhere'"),
                          (["240", "--set", "minutes="], "--unset minutes removes it"),
                          (["--set", "minutes=8"], "name it")]:
            run("quiz-defaults", *argv, ok=False, has=[why], lacks=["Traceback"])
        fails += "colour" in DEFAULTS.read_text(); print("   a refused change writes nothing:", "colour" not in DEFAULTS.read_text())

        # setup-quiz with no file: the flags, and the course's defaults for the rest.
        before = (len(QUIZZES_MADE), len(ITEMS))
        run("setup-quiz", "--course", "240", "--name", "Quiz 10", "--date", "2026-10-20",
            has=["Quiz 10 in 240: its questions written in Brightspace",
                 "opens 2026-10-20 12:30, closes 2026-10-20 14:00; 8 minutes, 2 attempts",
                 "settings copied from 'Quiz 9'; scores to a grade item 'Quiz 10'; a description",
                 "from 240's defaults: start, end, minutes, attempts, description",
                 '"StartDate": "2026-10-20T16:30:00.000Z"', "modelled on 'Quiz 9'",
                 "Its questions: Quiz 10 -> Add/Edit Questions", "the same setup-quiz with --check"])
        fails += (len(QUIZZES_MADE), len(ITEMS)) != before
        print("   a plan from the flags sends nothing:", (len(QUIZZES_MADE), len(ITEMS)) == before)
        run("setup-quiz", "--course", "240", "--name", "Quiz 10", "--date", "2026-10-20", "--minutes", "12",
            "--like", "Quiz 2", "--no-grade-item", has=["12 minutes", "settings copied from 'Quiz 2'; no grade item",
                                                        "from 240's defaults: start, end, attempts, description"])
        run("setup-quiz", "--course", "240", "--name", "Quiz 10", "--start", "12:30", ok=False,
            has=["start: 12:30 is a time of day, and there is no date to put it on"])
        run("setup-quiz", "--course", "120", "--name", "Quiz 10", "--date", "2026-10-20", ok=False,
            has=["date: 2026-10-20 needs a time to open and one to close"])
        run("setup-quiz", "--course", "240", "--name", "Quiz 10", "--date", "2026-10-20", "--go",
            has=["created grade item 'Quiz 10'", "modelled on 'Quiz 9'", "created quiz 'Quiz 10'"])
        run("setup-quiz", "--course", "240", "--name", "Quiz 10", "--date", "2026-10-20", "--check",
            has=["2026-10-20 14:00", "8 minutes"], lacks=["DIFFERS"])

        # The shell: a course taught as sections has its quiz made in an empty
        # shell, settings from the first section, and copied into each after.
        run("quiz-defaults", "grp", "--set", "sections=240", "--set", "start=09:00", "--set", "end=10:00",
            "--set", "attempts=2", "--set", "item_like=previous", has=["grp's sections set"])
        run("setup-quiz", "--course", "grp", "--name", "Quiz 17", "--date", "2026-10-21", ok=False,
            has=["grp is taught as sections (240)", "give a shell"])
        run("quiz-defaults", "grp", "--set", "shell=shell", has=["grp's shell set"])
        run("setup-quiz", "--course", "grp", "--name", "Quiz 17", "--date", "2026-10-21", ok=False,
            has=["shell holds 1 quizzes ('Quiz 12')", "a shell starts empty"])
        SHELLS["quizzes"].clear()
        QUIZZES_MADE[[q["Name"] for q in QUIZZES_MADE].index("Quiz 16")]["CategoryId"] = 3
        run("setup-quiz", "--course", "grp", "--name", "Quiz 17", "--date", "2026-10-21", "--points", "5",
            ok=False, has=["a shell's quiz has none"])
        run("setup-quiz", "--course", "grp", "--name", "Quiz 17", "--date", "2026-10-21",
            has=["Quiz 17 made in shell, then copied into 240", "settings copied from 'Quiz 16' in 240",
                 "no grade item in the shell", "modelled on 'Quiz 16' in 240", '"CategoryId": null',
                 "Then by hand, in shell", "copy-quiz shell --to 240 --item-like previous --go --clear"])
        fails += SHELLS["quizzes"] != []; print("   a plan makes nothing in the shell:", SHELLS["quizzes"] == [])
        run("setup-quiz", "--course", "grp", "--name", "Quiz 17", "--date", "2026-10-21", "--go",
            has=["created quiz 'Quiz 17'", "checked on the server"], lacks=["created grade item"])
        made = SHELLS["quizzes"][-1] if SHELLS["quizzes"] else {}
        fine = made.get("Name") == "Quiz 17" and made.get("CategoryId") is None and made.get("StartDate") == "2026-10-21T13:00:00.000Z"
        fails += not fine; print("   the shell's quiz, its category left behind with its course:", fine)
        run("copy-quiz", "shell", "--to", "240", "--item-like", "previous", "--go", "--clear",
            has=["attached to a new grade item 'Quiz 17', shaped like 'Quiz 16'", "the shell is empty again"])
        run("copy-quiz", "shell", "--to", "240", "--item-like", "previous", ok=False, has=["shell holds 0 quizzes"])

        # The page over all three: made in a course or in a shell, copied, and
        # each course's defaults, the empty fields of a quiz being its course's.
        proc, wport, first = start_web()
        here = f"127.0.0.1:{wport}"
        got = fetch(wport, here, "/quizzes")
        token = re.search(r'name="token" value="([^"]+)"', got[1]).group(1)
        web_check("web: the make form's empty fields show the course's defaults", got, 200,
                  has=['name="start" value="" placeholder="12:30"', 'option value="grp"', "sections 240",
                       '<p class="hint" id="gist">240&#x27;s defaults: opens 12:30, closes 14:00; 8 minutes',
                       '<td>240</td>', "opens 12:30, closes 14:00; 8 minutes; 2 attempts; a description"],
                  lacks=['class="as"'])
        web_check("web: Check runs setup-quiz with the fields filled in, and nothing else",
                  fetch(wport, here, "/make", {"token": token, "as": "you", "course": "240", "route": "course",
                                               "name": "Quiz 18", "date": "2026-10-27", "start": "", "minutes": "",
                                               "grade_item": "", "action": "plan"}), 200,
                  has=["Checked: nothing sent", "brightspace.py setup-quiz --course 240 --name=&#x27;Quiz 18&#x27; "
                       "--date=2026-10-27</code>", "from 240&#x27;s defaults: start, end, minutes, attempts"])
        web_check("web: a shell that is not one of the courses is refused",
                  fetch(wport, here, "/make", {"token": token, "course": "grp", "route": "shell", "shell": "--help",
                                               "name": "Quiz 18", "action": "plan"}), 200,
                  has=["Not run", "is not one of your courses"])
        got = fetch(wport, here, "/make", {"token": token, "course": "grp", "route": "shell", "shell": "shell",
                                           "name": "Quiz 18", "date": "2026-10-28", "grade_item": "", "action": "go"})
        web_check("web: Make it in the shell makes it there, and readies step 2", got, 200,
                  has=["Made", "--course grp --shell shell --name=&#x27;Quiz 18&#x27;", "created quiz &#x27;Quiz 18&#x27;",
                       "step 2 below copies it", '<input type="checkbox" name="to" value="240" checked>',
                       'name="item_like" value="previous"', '<input type="checkbox" name="clear" checked>'])
        web_check("web: step 2 copies it into the sections and empties the shell",
                  fetch(wport, here, "/copy-quiz", {"token": token, "shell": "shell", "to": "240",
                                                    "item_like": "previous", "clear": "on", "action": "copy"}), 200,
                  has=["Copied", "shaped like &#x27;Quiz 17&#x27;", "the shell is empty again"])
        got = fetch(wport, here, "/quizzes?edit=240")
        web_check("web: a course's defaults, to edit, as they are", got, 200,
                  has=['id="dcourse" list="courses" value="240"', 'name="d_start" value="12:30"',
                       'name="d_minutes" value="8"'])
        before = DEFAULTS.read_text()
        got = fetch(wport, here, "/defaults", {"token": token, "dcourse": "240", "action": "save", "d_start": "",
                                               "d_minutes": "", "d_attempts": ""})
        web_check("web: Save on a form that never held the course's defaults clears nothing, and loads them",
                  got, 200, has=["Not run", "did not hold 240&#x27;s saved defaults", 'name="d_start" value="12:30"',
                                 'name="d_loaded" value="240"'])
        fails += DEFAULTS.read_text() != before
        print("   the file is as it was:", DEFAULTS.read_text() == before)
        web_check("web: Load fills the form with a course's saved defaults, and runs nothing",
                  fetch(wport, here, "/defaults", {"token": token, "dcourse": "240", "action": "load"}), 200,
                  has=['name="d_start" value="12:30"', 'name="d_minutes" value="8"', 'name="d_loaded" value="240"'],
                  lacks=["Saved", "Not run"])
        form = {"token": token, "dcourse": "240", "d_loaded": "240", "action": "save", "d_start": "12:30",
                "d_end": "14:00", "d_minutes": "10", "d_attempts": "",
                "d_description": "Two **attempts**.\r\n\r\nBoth today.", "d_shell": ""}
        web_check("web: Save sets what changed and unsets what was emptied",
                  fetch(wport, here, "/defaults", form), 200,
                  has=["Saved", "quiz-defaults 240 --set=minutes=10 --unset=attempts</code>",
                       'name="d_minutes" value="10"'])
        left = yaml.load(DEFAULTS.read_text(), Loader=yaml.BaseLoader)["240"]
        fails += "attempts" in left or left.get("minutes") != "10"
        print("   the file has 240's minutes changed and its attempts gone:", "attempts" not in left and left.get("minutes") == "10")
        web_check("web: Save with nothing changed runs nothing",
                  fetch(wport, here, "/defaults", form | {"d_attempts": ""}), 200,
                  has=["Not run", "nothing to change in 240"])
        web_check("web: a defaults name that could be anything is refused",
                  fetch(wport, here, "/defaults", {"token": token, "dcourse": "../x", "d_minutes": "8"}), 200,
                  has=["Not run", "a course is one of your labels"])
        web_check("web: on one's own machine there is no one else to act as",
                  fetch(wport, here, "/make", {"token": token, "as": "other", "course": "240", "name": "Q",
                                               "action": "plan"}), 403, has=["You may not act as other"])
        proc.terminate(); proc.wait()

        # Behind Access, whose session: each person picks among the ones web.ini gives them.
        (webconf / "brightspace" / "keepalive.ini").write_text(f"[me]\nstate = {state}\n\n[other]\nstate = {state}\n")
        (webconf / "brightspace" / "keepalive.ini").chmod(0o600)
        theirs = webconf / "other-courses.ini"
        theirs.write_text(f"[240]\nou = {OU}\n\n[shell]\nou = {SHELL}\n")
        webini.write_text(f"[access]\nteam = test.cloudflareaccess.com\naud = the-aud\nhost = bs.example.test\n"
                          f"certs = http://127.0.0.1:{port}/cdn-cgi/access/certs\n\n"
                          f"[session me]\ncourses = {COURSES}\ndefaults = {DEFAULTS}\n\n"
                          f"[session other]\ncourses = {theirs}\nowner = Bob Smith\n\n"
                          f"[user ada@example.edu]\nsessions = me, other\n\n[user bob@example.edu]\nsessions = other\n")
        proc, wport, first = start_web("--access", str(webini))
        got = fetch(wport, "bs.example.test", "/quizzes", headers=asks())
        web_check("web behind Access: the sessions one may act as, the first chosen", got, 200,
                  has=['<option value="me" selected>me</option><option value="other">other</option>',
                       'value="120"', "<td>240</td>"])
        token = re.search(r'name="token" value="([^"]+)"', got[1]).group(1)
        web_check("web behind Access: the other session, with its own courses and its own defaults",
                  fetch(wport, "bs.example.test", "/quizzes?as=other", headers=asks()), 200,
                  has=['<option value="other" selected>other</option>', 'value="shell"'],
                  lacks=['value="120"', "<td>240</td>"])
        web_check("web behind Access: a session's owner as web.ini names them, beside Brightspace's username",
                  fetch(wport, "bs.example.test", "/?as=other", headers=asks()), 200,
                  has=["Acting as Bob Smith (alovelace) in Brightspace."], lacks=["Ada Lovelace"])
        web_check("web behind Access: a session with no owner given, as Brightspace names them",
                  fetch(wport, "bs.example.test", "/?as=me", headers=asks()), 200,
                  has=["Acting as Ada Lovelace (alovelace) in Brightspace."])
        web_check("web behind Access: someone given one session gets no choice",
                  fetch(wport, "bs.example.test", "/quizzes", headers=asks(dict(good, email="bob@example.edu"))), 200,
                  lacks=['class="as"', 'value="120"'])
        web_check("web behind Access: a session not given is refused",
                  fetch(wport, "bs.example.test", "/make", {"token": token, "as": "me", "course": "240",
                                                           "name": "Q", "action": "plan"},
                        {**asks(dict(good, email="bob@example.edu")), "Origin": "https://bs.example.test"}), 403,
                  has=["You may not act as me"])
        web_check("web behind Access: a Save writes the session's own defaults file",
                  fetch(wport, "bs.example.test", "/defaults",
                        {"token": token, "as": "other", "dcourse": "240", "d_minutes": "9"},
                        {**asks(), "Origin": "https://bs.example.test"}), 200, has=["Saved", "--set=minutes=9"])
        own = webini.parent / "quiz-defaults-other.yml"
        good_file = own.exists() and "minutes: 9" in own.read_text() and "minutes: 9" not in DEFAULTS.read_text()
        fails += not good_file; print("   it went to quiz-defaults-other.yml beside web.ini, and nowhere else:", good_file)
        proc.terminate(); proc.wait()


    # grade: named students' mark in one grade item, its full marks by default.
    ITEMS.append({"Id": 600, "Name": "Presentation", "GradeType": "Numeric", "MaxPoints": 10.0,
                  "Weight": 5.0, "CategoryId": 0, "GradeSchemeUrl": "/x", "IsHidden": False})
    VALUES[(600, "100002")] = {"PointsNumerator": 8.0, "GradeObjectType": 1,
                               "Comments": {"Text": "Clear and well paced.", "Html": "<p>Clear and well paced.</p>"},
                               "PrivateComments": {"Text": "", "Html": ""}}
    run("grade", "240", "Presentation", "--max", "Rita Solberg", "tomas weber", "Nobody Here",
        has=["'Presentation' in 240, out of 10: 10 for 2 students", "Rita Solberg", "none -> 10",
             "Tomas Weber", "8 -> 10", "not graded: Nobody Here (not on the classlist)",
             "nothing sent; --go sets the 2 that differ"])
    fails += VALUES.get((600, "100001")) is not None; print("   a plan sets nothing:", VALUES.get((600, "100001")) is None)
    run("grade", "240", "Presentation", "--max", "Rita Solberg", "100002", "--go",
        has=["checked on the server: Rita Solberg's mark, Rita Solberg's comment",
             "checked on the server: Tomas Weber's mark, Tomas Weber's comment", "2 set"])
    fine = VALUES[(600, "100002")]["Comments"]["Text"] == "Clear and well paced." and VALUES[(600, "100001")]["PointsNumerator"] == 10.0
    fails += not fine; print("   the marks are in, and the comment a student had is still there:", fine)
    run("grade", "240", "Presentation", "--max", "Rita Solberg", "--go", has=["has 10 already", "nothing to change"])
    run("grade", "240", "Presentation", "--points", "11", "Rita Solberg", ok=False, has=["is out of 10, so 11 cannot go in it"])
    run("grade", "240", "Presentation", "Rita Solberg", ok=False, has=["--max or --points N"])
    run("grade", "240", "Presentation", "--max", "--points", "3", "Rita Solberg", ok=False, has=["--max or --points N"])

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

# --- checklists: one checklist, the same in several courses ------------------
try:
    import yaml  # noqa: F401
except ImportError:
    yaml = None
if COOKIES_OK and not yaml:
    print("SKIP checklists and their tab: an item's Markdown is bs-yaml-quiz's, which needs PyYAML")
if COOKIES_OK and yaml:
    run("checklists", "240", has=["id", "name", "items", "due"], lacks=["Week"])
    WEEK7 = ["--name", "Week 7",
             "--item", "Read [chapter 3](https://example.edu/ch3)\nThe *notes* help &mdash; a lot.", "--due", "2026-10-13 23:59",
             "--item", "Quiz 6 opens in class", "--due", "2026-10-13 12:30",
             "--item", "Bring questions\n\nAny part of A5."]
    run("new-checklist", "240", "120", *WEEK7,
        has=["'Week 7', 3 items:", "1. Read chapter 3  (due 2026-10-13 23:59)", "3. Bring questions  (no due date)",
             '"DueDate": null', "\"CategoryId\": \"<the category's id>\"",
             "into 240: 0 checklists there now, none of this name", "into 120: 0 checklists",
             "nothing sent; --go posts it"])
    fails += any(CHECKLISTS.get(ou) for ou in (OU, 1000120))
    print("   checking sent nothing:", not any(CHECKLISTS.get(ou) for ou in (OU, 1000120)))
    run("new-checklist", "240", "120", *WEEK7, "--go",
        has=["240:\n  made checklist 900, with 3 items", "120:\n  made checklist 901, with 3 items",
             "checked on the server: name, items, descriptions, due dates", "'Week 7' is in 240, 120"])
    made = CHECKLISTS[1000120][0]
    due = dt.datetime.fromisoformat("2026-10-13 23:59").astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    good = ([c["Name"] for c in made["categories"]] == ["Week 7"]
            and [i["Name"] for i in made["items"]] == ["Read chapter 3", "Quiz 6 opens in class", "Bring questions"]
            and made["items"][0]["DueDate"] == due and made["items"][2]["DueDate"] is None
            and 'rel="noopener" href="https://example.edu/ch3"' in made["items"][0]["Description"]["Html"])
    fails += not good
    print("   one category of its name, the items in order, due in UTC, the link in the description:", good)
    run("new-checklist", "120", "--name", "week 7", "--item", "x", ok=False,
        has=["into 120: it already has a checklist of this name",
             "120 already has a checklist named 'week 7'; nothing has been sent anywhere"])
    # Read back, each item as new-checklist takes it: the link and the emphasis
    # survive Brightspace's rewriting of the HTML, and the dash its entity.
    run("checklists", "120", has=["901", "Week 7", "3", "2026-10-13 12:30 to 2026-10-13 23:59"])
    run("checklists", "120", "week", has=[
        "'Week 7', checklist 901: 3 items", "1. due 2026-10-13 23:59",
        "   Read [chapter 3](https://example.edu/ch3)\n\n   The *notes* help — a lot.",
        "2. due 2026-10-13 12:30\n   Quiz 6 opens in class", "3. no due date\n   Bring questions\n   Any part of A5."])
    run("--json", "checklists", "120", "901", has=['"ChecklistItemId"', '"checklist"'])
    run("checklists", "120", "nope", ok=False, has=["no checklists match 'nope': 901 'Week 7'"])
    run("new-checklist", "240", "--name", "Bad", "--item", "x", "--due", "tomorrow", ok=False,
        has=["item 1: 'tomorrow' is not a date and a time, as '2026-10-13 23:59'"])
    run("new-checklist", "240", "--name", "Bad", "--due", "2026-10-13 23:59", ok=False,
        has=["each --due follows the --item"])
    run("new-checklist", "240", "--name", "Bad", "--item", "x", "--due", "2026-10-13 23:59",
        "--due", "2026-10-14 23:59", ok=False, has=["each --due follows the --item"])
    run("new-checklist", "240", "--name", "Bad", ok=False, has=["an item at least"])
    run("new-checklist", "240", "--name", " ", "--item", "x", ok=False, has=["a checklist needs a name"])
    run("new-checklist", "240", "--name", "Bad", "--item", "```\ncode\n```", ok=False,
        has=["item 1: its first line is its name, and cannot open a block of code"])
    run("new-checklist", "240", "--name", "Bad", "--item", "ok", "--item", "x" * 513, ok=False,
        has=["item 2: its first line is its name, 513 characters long"])
    run("new-checklist", "nope", "--name", "Bad", "--item", "x", ok=False, has=["unknown course 'nope'"])

    # The tab: first of the two, filled in from the newest checklist in the
    # courses file's courses, ticking each that has one of its name.
    proc, wport, first = start_web()
    here = f"127.0.0.1:{wport}"
    got = fetch(wport, here)
    token = re.search(r'name="token" value="([^"]+)"', got[1]).group(1)
    web_check("web: the checklist tab comes first, filled in from the newest checklist", got, 200,
              has=['<a href="/?as=you" aria-current="page">Checklists</a><a href="/quizzes?as=you">Quizzes</a>',
                   "Acting as Ada Lovelace (alovelace)", "Filled in from \u2018Week 7\u2019, the newest checklist here, in 240, 120, same and withsite.", 'value="Week 7"',
                   ">Read [chapter 3](https://example.edu/ch3)\n\nThe *notes* help — a lot.</textarea>",
                   'value="2026-10-13T23:59"', 'value="2026-10-13T12:30"', 'name="item_due" value=""',
                   'name="to" value="120" checked', 'name="to" value="240" checked', 'name="to" value="shell">'],
              lacks=['id="make-it"', 'class="as"'])
    week8 = {"token": token, "name": "Week 8", "to": ["240", "120"],
             "item_text": ["Read chapter 4", "", "Quiz 7\r\nIn class."], "item_due": ["2026-10-20T23:59", "", ""]}
    web_check("web: Check runs new-checklist without --go, and keeps the form as it was sent",
              fetch(wport, here, "/checklist", week8 | {"action": "plan"}), 200,
              has=["Checked: nothing sent", "brightspace.py new-checklist 240 120 --name=&#x27;Week 8&#x27; "
                   "--item=&#x27;Read chapter 4&#x27; --due=&#x27;2026-10-20 23:59&#x27; --item=&#x27;Quiz 7",
                   "nothing sent; --go posts it", 'value="Week 8"', ">Quiz 7\nIn class.</textarea>"],
              lacks=[" --go</code>"])
    web_check("web: Post it posts it, into every course ticked",
              fetch(wport, here, "/checklist", week8 | {"action": "go"}), 200,
              has=["Posted", "made checklist", "checked on the server", "&#x27;Week 8&#x27; is in 240, 120"])
    fine = [c["Name"] for c in CHECKLISTS[1000120]] == ["Week 7", "Week 8"]
    fails += not fine; print("   Week 8 is in 120 beside Week 7:", fine)
    web_check("web: the next one starts from the one just posted", fetch(wport, here), 200,
              has=["Filled in from \u2018Week 8\u2019", 'value="2026-10-20T23:59"'])
    CHECKLISTS_DOWN.add(SHELL)
    web_check("web: a course that cannot be read is left out, and named",
              fetch(wport, here), 200, has=["Filled in from \u2018Week 8\u2019",
                                            "The checklists of shell could not be read."])
    CHECKLISTS_DOWN.clear()
    web_check("web: what the command refuses, the page shows",
              fetch(wport, here, "/checklist", week8 | {"to": ["240"], "action": "go"}), 200,
              has=["Stopped", "240 already has a checklist named &#x27;Week 8&#x27;"])
    web_check("web: a checklist goes into a course of the courses file, never an option",
              fetch(wport, here, "/checklist", week8 | {"to": ["--help"], "action": "plan"}), 200,
              has=["Not run", "is not one of your courses"])
    web_check("web: a checklist with no course ticked is not run",
              fetch(wport, here, "/checklist", {k: v for k, v in week8.items() if k != "to"} | {"action": "plan"}), 200,
              has=["Not run", "tick at least one course"])
    web_check("web: a due date with no item is not run",
              fetch(wport, here, "/checklist", week8 | {"item_text": ["", "x"], "item_due": ["2026-10-20T23:59", ""],
                                                        "action": "plan"}), 200,
              has=["Not run", "an item has a due date and no text"])
    web_check("web: the quiz tab is a page of its own", fetch(wport, here, "/quizzes?as=you"), 200,
              has=['<a href="/quizzes?as=you" aria-current="page">Quizzes</a>', 'id="make-it"'],
              lacks=['id="items"'])
    web_check("web: nothing else is a page", fetch(wport, here, "/checklists"), 404)
    proc.terminate(); proc.wait()

    # A write refused halfway leaves its checklist, and says where.
    run("new-checklist", "240", "--name", "Partial", "--item", "fine", "--item", "refuse me", "--go", ok=False,
        has=["Checklist 904 is there, made in part: delete it in Brightspace", "not done in 240"])

run("logout", has=["forgot the session"])
assert not (state / "session.json").exists()
run("whoami", ok=False, has=["no session"])
print("FAILURES:", fails); sys.exit(1 if fails else 0)
