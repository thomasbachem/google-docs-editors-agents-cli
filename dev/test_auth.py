#!/usr/bin/env python3
"""Exercise auth.py's branches with a faked OAuth flow – no browser, no network.

Covers the cases that are otherwise only reachable by actually re-authorizing,
including the refusal that protects the token when the consent lands on a
different Google account.
"""

import base64
import importlib
import json
import os
import shutil
import sys
import tempfile

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


class FakeFlow:
    def __init__(self, creds):
        self._creds = creds

    def run_local_server(self, port=0):
        return self._creds


def run_case(name, prior_token, id_token, expect_exit, expect_email, expect_source):
    d = tempfile.mkdtemp()
    shutil.copy(SRC, os.path.join(d, "auth.py"))
    shutil.copy(GAUTH, os.path.join(d, "gauth.py"))
    open(os.path.join(d, "client_secret.json"), "w").write("{}")
    tok = os.path.join(d, "token.json")
    if prior_token is not None:
        json.dump(prior_token, open(tok, "w"))
        os.chmod(tok, 0o600)

    sys.path.insert(0, d)
    sys.modules.pop("auth", None)
    sys.modules.pop("gauth", None)
    auth = importlib.import_module("auth")
    auth.InstalledAppFlow = type("F", (), {
        "from_client_secrets_file": staticmethod(lambda p, s: FakeFlow(FakeCreds(id_token)))
    })

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

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
