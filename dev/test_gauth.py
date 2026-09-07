#!/usr/bin/env python3
"""Cover the shared credential core, which both CLIs now depend on.

The refresh path is the one that has broken before: Credentials.to_json() drops
keys it does not know, so the account recorded in token.json vanished on the
first refresh. Nothing here touches the network or the real token.
"""

import json
import os
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
import contextlib  # noqa: E402
import io  # noqa: E402

from googleapiclient.errors import HttpError  # noqa: E402

import gauth  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{('  ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


class FakeCreds:
    """Stands in for google.oauth2.credentials.Credentials."""

    refreshed = False

    def __init__(self, data, valid):
        self.data = data
        self.valid = valid

    @staticmethod
    def from_authorized_user_info(data):
        return FakeCreds(data, FakeCreds.start_valid)

    def refresh(self, request):
        FakeCreds.refreshed = True
        self.valid = True

    def to_json(self):
        # Deliberately lossy, exactly like the real one: no email, no email_source.
        return json.dumps({"token": "fresh", "refresh_token": "r",
                           "scopes": [gauth.SHEETS_SCOPE, gauth.DOCS_SCOPE]})


def use_token(**extra):
    fd, path = tempfile.mkstemp(suffix=".json")
    data = {"token": "old", "refresh_token": "r", "email": "tester@example.com",
            "email_source": "id_token", "scopes": [gauth.SHEETS_SCOPE]}
    data.update(extra)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    gauth.TOKEN = path
    return path


print("=== gauth.py credential core (no network) ===")
gauth.Credentials = FakeCreds
gauth.Request = lambda: "request"

# an expired token must refresh AND keep the keys to_json() throws away
path = use_token()
FakeCreds.start_valid, FakeCreds.refreshed = False, False
gauth.credentials()
saved = json.load(open(path))
check("an invalid token is refreshed", FakeCreds.refreshed)
check("the refreshed token is written", saved["token"] == "fresh", saved["token"])
check("email survives the refresh", saved.get("email") == "tester@example.com",
      repr(saved.get("email")))
check("email_source survives the refresh", saved.get("email_source") == "id_token",
      repr(saved.get("email_source")))
check("the token file stays private",
      stat.S_IMODE(os.stat(path).st_mode) == 0o600, oct(stat.S_IMODE(os.stat(path).st_mode)))
os.unlink(path)

# a still-valid token must not be rewritten at all
path = use_token(token="untouched")
FakeCreds.start_valid, FakeCreds.refreshed = True, False
gauth.credentials()
check("a valid token is left alone",
      not FakeCreds.refreshed and json.load(open(path))["token"] == "untouched")
os.unlink(path)

# a failed write must not leave the directory littered with temp files
path = use_token()
before = set(os.listdir(os.path.dirname(path)))
try:
    gauth.write_token({"bad": {1, 2}})  # a set is not JSON-serialisable
    check("an unserialisable write raises", False, "no exception")
except TypeError:
    check("an unserialisable write raises", True)
leaked = [f for f in set(os.listdir(os.path.dirname(path))) - before
          if f.startswith(".token.")]
check("a failed write leaves no temp file", not leaked, str(leaked))
check("a failed write leaves the old token intact",
      json.load(open(path))["token"] == "old")
os.unlink(path)

# the scope preflight, and the account label's provenance
path = use_token()
try:
    gauth.require_scope(gauth.DOCS_SCOPE, "Google Docs")
    msg = None
except gauth.ToolError as e:
    msg = str(e)
check("a missing scope is refused with the fix", msg and "auth.py" in msg, str(msg)[:50])
gauth.require_scope(gauth.SHEETS_SCOPE, "Sheets")
check("a present scope passes silently", True)
check("an attested account prints bare", gauth.account() == "tester@example.com")
os.unlink(path)

path = use_token(email_source="inferred")
check("an inferred account is labelled", "inferred" in gauth.account(), gauth.account())
os.unlink(path)

# split_flags: a dropped flag is how a mistyped --dry-run writes for real
def flags_of(argv, valued=("tab",), known=("--dry-run",)):
    try:
        return gauth.split_flags(argv, valued, known), None
    except gauth.ToolError as e:
        return None, str(e)


(values, flags, rest), err = flags_of(["t", "cmd", "--dry-run", "ID"])
check("a known bare flag is kept", flags == {"--dry-run"} and rest == ["t", "cmd", "ID"])
check("an absent valued flag reads as None", values["tab"] is None, repr(values["tab"]))

(values, _, _), err = flags_of(["t", "cmd", "--tab=t.1", "ID"])
check("a valued flag keeps its value", values["tab"] == "t.1")

(values, _, _), err = flags_of(["t", "cmd", "--tab=", "ID"])
check("an empty value stays distinct from absent", values["tab"] == "", repr(values["tab"]))

_, err = flags_of(["t", "cmd", "--dryrun", "ID"])
check("a mistyped flag is refused, not dropped",
      err and "unknown option: --dryrun" in err, str(err)[:50])
check("the refusal lists what is accepted", err and "--dry-run" in err and "--tab=" in err)

_, err = flags_of(["t", "cmd", "--nope=1", "ID"])
check("an unknown valued flag is refused too",
      err and "--nope=1" in err, str(err)[:40])

(values, flags, rest), err = flags_of(["t", "cmd", "--", "--dryrun", "--tab=x"])
check("everything after -- is positional",
      err is None and rest == ["t", "cmd", "--dryrun", "--tab=x"] and not flags, str(rest))

# GTOOLS_RETRIES: a convenience knob must never take the tool down
def with_env(value):
    """Run retry_count() with GTOOLS_RETRIES set (or unset for None)."""
    import io, contextlib
    old = os.environ.get("GTOOLS_RETRIES")
    if value is None:
        os.environ.pop("GTOOLS_RETRIES", None)
    else:
        os.environ["GTOOLS_RETRIES"] = value
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            return gauth.retry_count(), err.getvalue()
    finally:
        if old is None:
            os.environ.pop("GTOOLS_RETRIES", None)
        else:
            os.environ["GTOOLS_RETRIES"] = old


n, err = with_env(None)
check("unset means the default", n == gauth.RETRY_DEFAULT == 3 and not err, f"{n} {err!r}")

n, err = with_env("7")
check("a valid override is honoured", n == 7 and not err, f"{n} {err!r}")

n, err = with_env("0")
check("zero disables retries", n == 0 and not err, f"{n} {err!r}")

n, err = with_env("nope")
check("a non-numeric value falls back, loudly",
      n == gauth.RETRY_DEFAULT and "not a number" in err, f"{n} {err.strip()!r}")

# range(num_retries + 1) would be empty, so a negative would skip the request
n, err = with_env("-1")
check("a negative value clamps to 0, not to never-run",
      n == 0 and "negative" in err, f"{n} {err.strip()!r}")

n, err = with_env("500")
check("an absurd value is capped",
      n == gauth.RETRY_CEILING and "cap" in err, f"{n} {err.strip()!r}")

check("the ceiling keeps the worst case sane", gauth.RETRY_CEILING <= 10)

# a missing token must say what to run, not raise
gauth.TOKEN = "/nonexistent/token.json"
try:
    gauth.token_data()
    msg = None
except gauth.ToolError as e:
    msg = str(e)
check("a missing token names auth.py", msg and "auth.py" in msg, str(msg)[:40])

# --- the per-minute quota: a second policy, not a bigger number ------------
# Body shaped like the 19 real ones: 429, RATE_LIMIT_EXCEEDED, names the limit.
QUOTA_BODY = (b'{"error": {"code": 429, "message": "Quota exceeded for quota metric '
              b"'Read requests' and limit 'Read requests per minute per user' of service "
              b'\'sheets.googleapis.com\' for consumer \'project_number:1\'."}}')
BLIP_BODY = b'{"error": {"code": 429, "message": "Too many requests"}}'


class Resp:
    def __init__(self, status):
        self.status = status
        self.reason = "x"


print("\n--- per-minute quota ---")
check("a quota 429 is recognised", gauth.minute_limit(HttpError(Resp(429), QUOTA_BODY)))
check("a bare 429 is not – it wants the short backoff",
      not gauth.minute_limit(HttpError(Resp(429), BLIP_BODY)))
check("a 5xx is not", not gauth.minute_limit(HttpError(Resp(503), QUOTA_BODY)))
check("a 403 naming a quota is not either",
      not gauth.minute_limit(HttpError(Resp(403), QUOTA_BODY)))
check("the limit is named for the message",
      gauth.quota_limit_name(HttpError(Resp(429), QUOTA_BODY))
      == "Read requests per minute per user",
      gauth.quota_limit_name(HttpError(Resp(429), QUOTA_BODY)))


def with_quota_env(value):
    old = os.environ.get("GTOOLS_QUOTA_WAIT")
    if value is None:
        os.environ.pop("GTOOLS_QUOTA_WAIT", None)
    else:
        os.environ["GTOOLS_QUOTA_WAIT"] = value
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            return gauth.quota_waits(), err.getvalue()
    finally:
        if old is None:
            os.environ.pop("GTOOLS_QUOTA_WAIT", None)
        else:
            os.environ["GTOOLS_QUOTA_WAIT"] = old


waits, err = with_quota_env(None)
check("the default sums past a 60s window", waits == (20, 40) and sum(waits) >= 60, str(waits))
check("a custom list is honoured", with_quota_env("5,10,15")[0] == (5, 10, 15))
check("0 turns the waiting off", with_quota_env("0")[0] == ())
check("empty turns it off too", with_quota_env("")[0] == ())
waits, err = with_quota_env("nope")
check("garbage falls back, loudly", waits == (20, 40) and "comma-separated" in err, err.strip()[:40])
waits, err = with_quota_env("20,-5")
check("a negative wait falls back", waits == (20, 40) and "negative" in err, err.strip()[:40])


def run_request(waits, failures):
    """Drive RetryingRequest.execute with a scripted sequence of failures."""
    import googleapiclient.http as ghttp
    attempts, slept = [], []

    def fake(self, http=None, num_retries=0):
        attempts.append(num_retries)
        if len(attempts) <= failures[0]:
            raise HttpError(Resp(429), failures[1])
        return "ok"

    real_exec, real_sleep, real_waits = ghttp.HttpRequest.execute, gauth.time.sleep, gauth.QUOTA_WAITS
    ghttp.HttpRequest.execute = fake
    gauth.time.sleep = lambda s: slept.append(s)
    gauth.QUOTA_WAITS = waits
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            result = object.__new__(gauth.RetryingRequest).execute()
    except HttpError:
        result = "raised"
    finally:
        ghttp.HttpRequest.execute, gauth.time.sleep = real_exec, real_sleep
        gauth.QUOTA_WAITS = real_waits
    return result, attempts, slept, err.getvalue()


result, attempts, slept, err = run_request((20, 40), (2, QUOTA_BODY))
check("a quota 429 is waited out and retried", result == "ok" and len(attempts) == 3, str(attempts))
check("the waits are the fixed ones, in order", slept == [20, 40], str(slept))
check("each wait is announced, not silent", err.count("waiting") == 2, repr(err[:60]))
check("the announcement names the limit", "Read requests per minute" in err)
check("the short backoff is still asked for", attempts == [gauth.RETRIES] * 3, str(attempts))

result, attempts, slept, err = run_request((20, 40), (9, QUOTA_BODY))
check("it gives up after the last wait", result == "raised" and slept == [20, 40], str(slept))

result, attempts, slept, err = run_request((20, 40), (1, BLIP_BODY))
check("a bare 429 is not waited out", result == "raised" and slept == [], str(slept))

result, attempts, slept, err = run_request((), (1, QUOTA_BODY))
check("turning it off raises at once", result == "raised" and slept == [] and not err, str(slept))

# --- counting calls, the unit the quota is actually spent in ---------------
check("a read is told from a write by method",
      gauth.call_kind("https://sheets.googleapis.com/v4/spreadsheets/X", "GET")
      == ("sheets", "read")
      and gauth.call_kind("https://sheets.googleapis.com/v4/spreadsheets/X", "PUT")
      == ("sheets", "write"))
check("each API is counted apart, as Google meters them",
      gauth.call_kind("https://docs.googleapis.com/v1/documents/X", "POST") == ("docs", "write"))
check("an unparseable uri still counts as something",
      gauth.call_kind("", "POST") == ("api", "write"))

def call(at, kind="read", size=0, pid=1):
    return gauth.Call(1000.0 + at, "sheets", kind, size, pid)


counter = gauth.Calls()
counter.log = [call(t) for t in (0, 1, 2, 3, 4, 200, 201)] + [call(0, "write")]
check("totals are kept per quota",
      counter.counts() == {("sheets", "read"): 7, ("sheets", "write"): 1},
      str(counter.counts()))
# The total is the number that misleads: 7 reads is fine, 7 reads in 4s may not be
check("the peak is the busiest window, not the run",
      counter.peak("sheets", "read") == 5, str(counter.peak("sheets", "read")))
check("the report names both", "sheets read 7 (peak 5/60s)" in counter.report(),
      counter.report())
check("nothing called reports nothing", gauth.Calls().report() is None)

steady = gauth.Calls()
steady.log = [call(i) for i in range(70)]
# A closed window would say 61 here and cry wolf against a limit of exactly 60
check("a call exactly a window old has aged out", steady.peak("sheets", "read") == 60,
      str(steady.peak("sheets", "read")))

# payload bytes, the figure that decides whether a bundle needs splitting
sized = gauth.Calls()
sized.log = [call(0, "write", 400 * 1024), call(1, "write", 12 * 1024)]
check("the report carries the payload and the largest single one",
      "412 KB sent, largest 400 KB" in sized.report(), sized.report())
check("a read-only run says nothing about bytes", "sent" not in counter.report(),
      counter.report())
check("sizes are named at the scale the cap is",
      (gauth.human_size(900), gauth.human_size(414 * 1024), gauth.human_size(2 << 20))
      == ("900 B", "414 KB", "2.0 MB"))

# --- collecting across processes, which is the only way a peak means anything

def with_stats_env(value):
    old = os.environ.get("GTOOLS_STATS")
    if value is None:
        os.environ.pop("GTOOLS_STATS", None)
    else:
        os.environ["GTOOLS_STATS"] = value
    try:
        return gauth.stats_target()
    finally:
        if old is None:
            os.environ.pop("GTOOLS_STATS", None)
        else:
            os.environ["GTOOLS_STATS"] = old


check("stats are off unless asked for",
      with_stats_env(None) == (False, None) and with_stats_env("") == (False, None))
check("1 turns them on, in this process", with_stats_env("1") == (True, None))
check("0 and off keep them off",
      with_stats_env("0") == (False, None) and with_stats_env("off") == (False, None))
check("a path turns on collecting", with_stats_env("/tmp/x.jsonl") == (True, "/tmp/x.jsonl"))
check("~ is expanded, so a path from a shell survives",
      with_stats_env("~/x.jsonl")[1] == os.path.expanduser("~/x.jsonl"))

shared = tempfile.mkstemp(suffix=".jsonl")[1]
os.unlink(shared)
# Two "processes", each seeing only its own half of one 60-second window
first, second = gauth.Calls(shared), gauth.Calls(shared)
for i in range(4):
    first.log.append(call(i, pid=11))
    first._append(first.log[-1])
for i in range(3):
    second.log.append(call(i, pid=22))
    second._append(second.log[-1])
check("every process's calls survive concurrent appends",
      len(second.records()) == 7, str(len(second.records())))
# 3 alone would pass a budget the pipeline is actually blowing
check("the peak spans the whole run, not one process",
      second.peak("sheets", "read") == 7, str(second.peak("sheets", "read")))
check("the report says how many processes it covers",
      "7 API call(s) in 2 processes" in second.report(), second.report())

with open(shared, "a") as f:
    f.write("{ torn\n")
check("a torn line is skipped, not fatal", len(second.records()) == 7,
      str(len(second.records())))

blocked = gauth.Calls(os.path.join(shared, "nope", "x.jsonl"))
err = io.StringIO()
with contextlib.redirect_stderr(err):
    blocked.record("sheets", "read")
    blocked.record("sheets", "read")
check("an unusable path degrades to this process, loudly",
      len(blocked.records()) == 2 and "cannot collect" in err.getvalue(),
      err.getvalue().strip()[:50])
check("it complains once, not per call", err.getvalue().count("cannot collect") == 1)
os.unlink(shared)

# every attempt spends quota, so the quota waits must each be counted
before = len(gauth.calls.log)
run_request((20, 40), (2, QUOTA_BODY))
check("a waited-out retry counts as another call",
      len(gauth.calls.log) - before == 3, str(len(gauth.calls.log) - before))

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
