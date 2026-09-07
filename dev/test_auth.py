#!/usr/bin/env python3
"""Exercise auth.py's branches with a faked OAuth flow – no browser, no network.

Covers the cases that are otherwise only reachable by actually re-authorizing,
including the refusal that protects the token when the consent lands on a
different Google account.
"""

import base64
import contextlib
import importlib
import io
import json
import os
import shutil
import sys
import tempfile

from google_auth_oauthlib.flow import WSGITimeoutError
from googleapiclient.errors import HttpError

TIMED_OUT = object()   # a consent nobody ever completed

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, os.pardir, "auth.py")
GAUTH = os.path.join(HERE, os.pardir, "gauth.py")
fails = []


def jwt(email):
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'RS256'})}.{seg({'email': email})}.sig"


class FakeCreds:
    def __init__(self, id_token):
        self.id_token = id_token

    def to_json(self):
        return json.dumps(
            {"token": "at", "refresh_token": "rt", "scopes": ["s"], "account": ""}
        )


def http_error(status, message):
    """A real HttpError, so the check matches the way it will in production."""
    resp = type("R", (), {"status": status, "reason": "x"})()
    return HttpError(resp, json.dumps({"error": {"code": status, "message": message}}).encode(),
                     uri="https://example/probe")


class StubService:
    """Answers any probe with one prepared error – no network in the suite."""

    def __init__(self, err):
        self._err = err

    def _endpoint(self):
        outer = self

        class E:
            def get(self, **kw):
                return type("Req", (), {"execute": lambda _s: (_ for _ in ()).throw(outer._err)})()

        return E()

    spreadsheets = documents = _endpoint


def stub_build(disabled=(), err=None):
    def _build(name, version, credentials=None):
        if err is not None:
            return StubService(err)
        if name in disabled:
            return StubService(http_error(
                403, f"{name} API has not been used in project 42 before or it is "
                     "disabled. Enable it by visiting https://console.cloud.google.com/"
                     f"apis/api/{name}.googleapis.com/overview?project=42 then retry."))
        return StubService(http_error(404, "Requested entity was not found."))
    return _build


class FakeFlow:
    last_kwargs = {}

    def __init__(self, creds):
        self._creds = creds

    def run_local_server(self, **kw):
        FakeFlow.last_kwargs = kw
        if self._creds is TIMED_OUT:
            raise WSGITimeoutError("no response")
        return self._creds


def run_case(name, prior_token, id_token, expect_exit, expect_email, expect_source,
             client={"installed": {}}):
    d = tempfile.mkdtemp()
    shutil.copy(SRC, os.path.join(d, "auth.py"))
    shutil.copy(GAUTH, os.path.join(d, "gauth.py"))
    json.dump(client, open(os.path.join(d, "client_secret.json"), "w"))
    tok = os.path.join(d, "token.json")
    if prior_token is not None:
        json.dump(prior_token, open(tok, "w"))
        os.chmod(tok, 0o600)

    sys.path.insert(0, d)
    sys.modules.pop("auth", None)
    sys.modules.pop("gauth", None)
    auth = importlib.import_module("auth")
    auth.InstalledAppFlow = type("F", (), {
        "from_client_secrets_file": staticmethod(
            lambda p, s: FakeFlow(TIMED_OUT if id_token is TIMED_OUT else FakeCreds(id_token)))
    })
    auth.build = stub_build()      # the post-consent probe must not reach the network

    code = 0
    try:
        auth.main()
    except SystemExit as e:
        code = 1 if e.code else 0
    finally:
        sys.path.remove(d)
        sys.modules.pop("auth", None)
        sys.modules.pop("gauth", None)

    got = json.load(open(tok)) if os.path.exists(tok) else {}
    mode = oct(os.stat(tok).st_mode & 0o777) if os.path.exists(tok) else "-"
    ok = (
        code == expect_exit
        and got.get("email") == expect_email
        and got.get("email_source") == expect_source
        and (mode == "0o600" or not got)
    )
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    print(f"        exit={code} email={got.get('email')!r} "
          f"source={got.get('email_source')!r} mode={mode}")
    if not ok:
        fails.append(name)
    shutil.rmtree(d, ignore_errors=True)


print("=== auth.py branches (faked flow) ===")
# A Web client gets as far as the consent and dies on a redirect-URI mismatch that
# names nothing, so it is refused here instead – the type is in the JSON's one key.
run_case("a Web client is refused before the flow starts",
         None, jwt("a@b.de"), 1, None, None, client={"web": {}})
run_case("an unreadable client JSON is refused too",
         None, jwt("a@b.de"), 1, None, None, client=[])
run_case("fresh auth, id_token present",
         None, jwt("a@b.de"), 0, "a@b.de", "id_token")
run_case("re-auth, same account",
         {"email": "a@b.de", "email_source": "id_token"}, jwt("a@b.de"), 0, "a@b.de", "id_token")
run_case("re-auth, different account -> REFUSE, token kept",
         {"email": "a@b.de", "email_source": "id_token"}, jwt("evil@corp.com"), 1,
         "a@b.de", "id_token")
run_case("no id_token returned -> keep prior, do not blank",
         {"email": "a@b.de", "email_source": "id_token"}, None, 0, "a@b.de", "inferred")
run_case("inferred -> attested upgrade",
         {"email": "a@b.de", "email_source": "inferred"}, jwt("a@b.de"), 0, "a@b.de", "id_token")
# An unattended run must end rather than wait forever for a browser nobody opened,
# and a run that ends that way must leave the working token alone.
run_case("a consent nobody completes times out, token untouched",
         {"email": "a@b.de", "email_source": "id_token"}, TIMED_OUT, 1, "a@b.de", "id_token")

check_timeout = FakeFlow.last_kwargs.get("timeout_seconds")
print(f"  {'ok  ' if check_timeout else 'FAIL'} the consent wait is bounded, not open-ended"
      f"  timeout_seconds={check_timeout}")
if not check_timeout:
    fails.append("the consent wait is bounded, not open-ended")

print("\n=== auth.py: adopting the downloaded client ===")


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{('  ' + str(detail)) if detail else ''}")
    if not cond:
        fails.append(name)


def fresh_auth(home):
    """Import auth.py with HOME pointed at a throwaway tree – DOWNLOADS is resolved
    at import, so it has to be set before, not after."""
    d = tempfile.mkdtemp()
    shutil.copy(SRC, os.path.join(d, "auth.py"))
    shutil.copy(GAUTH, os.path.join(d, "gauth.py"))
    os.environ["HOME"] = home
    sys.path.insert(0, d)
    sys.modules.pop("auth", None)
    sys.modules.pop("gauth", None)
    mod = importlib.import_module("auth")
    sys.path.remove(d)
    return mod, d


real_home = os.environ.get("HOME", "")
try:
    home = tempfile.mkdtemp()
    dl = os.path.join(home, "Downloads")
    os.makedirs(dl)
    one = os.path.join(dl, "client_secret_111-aaa.apps.googleusercontent.com.json")
    json.dump({"installed": {}}, open(one, "w"))

    mod, d = fresh_auth(home)
    dest = os.path.join(d, "client_secret.json")
    check("a single download is taken without being asked for",
          mod.adopt_client(dest) and os.path.exists(dest))
    check("and tightened to 600 – Downloads is world-readable",
          oct(os.stat(dest).st_mode & 0o777) == "0o600",
          oct(os.stat(dest).st_mode & 0o777))

    # Replacing a botched client is the repair; losing the old one costs a trip
    # back to the Console.
    mod.adopt_client(dest)
    check("replacing keeps the previous client as .bak", os.path.exists(dest + ".bak"))

    two = os.path.join(dl, "client_secret_222-bbb.apps.googleusercontent.com.json")
    json.dump({"installed": {}}, open(two, "w"))
    try:
        mod.adopt_client(os.path.join(d, "other.json"))
        check("several downloads are refused, never guessed between", False, "no exit")
    except SystemExit as e:
        check("several downloads are refused, never guessed between",
              "name one" in str(e) and one in str(e) and two in str(e))

    mod.DOWNLOADS = os.path.join(home, "nothing")
    check("nothing to take reports back, so main() can explain",
          mod.adopt_client(os.path.join(d, "third.json")) is False)

    try:
        mod.adopt_client(dest, given=os.path.join(home, "nope.json"))
        check("a named file that is not there is refused", False, "no exit")
    except SystemExit:
        check("a named file that is not there is refused", True)

    # Consent succeeds whether or not the project has the APIs turned on, so the
    # 403 otherwise arrives at first real use. Measured live: an enabled API
    # answers 404 to a probe for an id that cannot exist.
    def warned(build):
        mod.build = build
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            mod.warn_disabled_apis(None)
        return buf.getvalue()

    out = warned(stub_build(disabled=("sheets",)))
    check("a disabled API is named during setup, not at first use",
          "Sheets API is not enabled" in out and "console.cloud.google.com" in out,
          out.strip().splitlines()[-1][:70] if out.strip() else "silent")
    check("both are probed, so the second one is not missed",
          "Docs API is not enabled" in warned(stub_build(disabled=("docs",))))
    check("an enabled API stays silent", warned(stub_build()) == "")
    # Measured against Drive: no scope answers 403 before the service check runs,
    # so a bare status match would send the caller to the wrong Console page.
    check("a scope 403 is not mistaken for a disabled API",
          warned(stub_build(err=http_error(
              403, "Request had insufficient authentication scopes."))) == "")
    check("a probe that fails outright cannot fail the authorization",
          warned(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down"))) == "")
finally:
    if real_home:
        os.environ["HOME"] = real_home
    sys.modules.pop("auth", None)
    sys.modules.pop("gauth", None)

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
