#!/usr/bin/env python3
"""Shared Google credential handling for the gsheets and gdocs CLIs.

One OAuth client, one token.json: authorize once with auth.py and both tools
work. It lives in one file because the refresh path is easy to get subtly wrong
in ways that only show up later – see write_token() and credentials().
"""

import atexit
import errno
import json
import os
import queue
import re
import socket
import sys
import tempfile
import threading
import time
import warnings
from collections import namedtuple
from urllib.parse import urlsplit

# A stock Mac's python3 is Apple's 3.9.6, and on it google-api-core, google-auth
# (twice) and urllib3 each warn at import – eight lines of stderr on every call,
# none of them a failure. Hidden before the first Google import below, which is
# why every module imports gauth before anything of Google's; any other warning
# still shows.
warnings.filterwarnings("ignore", message=r"You are using a .*Python version",
                        category=FutureWarning, module=r"google\.")
warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL", module="urllib3")

from google.auth.exceptions import TransportError  # noqa: E402
from google.auth.transport.requests import Request  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402
from googleapiclient.http import HttpRequest  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.path.join(HERE, "token.json")
SECRETS = os.path.join(HERE, "client_secret.json")

# The backoff sleeps rand() * 2**attempt seconds, so the cost roughly doubles
# each time: 3 attempts cost at most ~14s, 10 would cost ~17 minutes on the last
# sleep alone. Hence the ceiling.
RETRY_DEFAULT = 3
RETRY_CEILING = 10

# The prefix was GTOOLS_ while this lived in a directory of that name, which is
# gone – and a name pointing at a place that no longer exists sends a reader
# looking for it. The old spelling still works, so a caller that set it does not
# silently lose the setting, and any message it provokes names what it set.
PREFIX = "GAPI_"
LEGACY_PREFIX = "GTOOLS_"


def env(name, default=None):
    """A setting and the variable it came from, new spelling winning."""
    for prefix in (PREFIX, LEGACY_PREFIX):
        raw = os.environ.get(prefix + name)
        if raw is not None:
            return raw, prefix + name
    return default, PREFIX + name


def retry_count():
    """How hard to retry transient failures, overridable per run.

    The client library retries 429 and 5xx, but only when asked – its own
    default is none at all, which is how a one-off 503 becomes a write that
    simply did not happen. A bad value here must not take the tool down over a
    convenience knob, so every rejection falls back and says so.
    """
    raw, var = env("RETRIES")
    if raw is None:
        return RETRY_DEFAULT
    try:
        n = int(raw)
    except ValueError:
        print(f"{var}={raw!r} is not a number – using {RETRY_DEFAULT}",
              file=sys.stderr)
        return RETRY_DEFAULT
    if n < 0:
        # range(num_retries + 1) would be empty, so the request never runs at all
        print(f"{var}={n} is negative – using 0", file=sys.stderr)
        return 0
    if n > RETRY_CEILING:
        print(f"{var}={n} exceeds the {RETRY_CEILING} cap – using "
              f"{RETRY_CEILING}", file=sys.stderr)
        return RETRY_CEILING
    return n


RETRIES = retry_count()

# Sitting out the per-minute quota is a different job from riding out a blip,
# and no RETRIES value does both: the jittered backoff never reliably outlasts
# the sliding 60-second window (at 5 it never reaches it, at 6 barely half the
# time), and the jitter exists to spread contending clients apart – a limit of
# 60 calls per minute per USER has no contention to spread. It is a clock, not
# a crowd, so these waits are fixed and deliberately sum past the window.
QUOTA_WAITS_DEFAULT = "20,40"


def quota_waits():
    """Pauses used to sit out a per-minute quota, from GAPI_QUOTA_WAIT.

    Empty or 0 turns the waiting off – a person at a terminal usually wants the
    error now, where a build script wants the write to land.
    """
    raw, var = env("QUOTA_WAIT", QUOTA_WAITS_DEFAULT)
    raw = raw.strip()
    fallback = tuple(int(p) for p in QUOTA_WAITS_DEFAULT.split(","))
    if raw in ("", "0"):
        return ()
    try:
        waits = tuple(int(p) for p in raw.split(","))
    except ValueError:
        print(f"{var}={raw!r} is not a comma-separated list of seconds – "
              f"using {QUOTA_WAITS_DEFAULT}", file=sys.stderr)
        return fallback
    if any(w < 0 for w in waits):
        print(f"{var}={raw!r} has a negative wait – using "
              f"{QUOTA_WAITS_DEFAULT}", file=sys.stderr)
        return fallback
    return waits


QUOTA_WAITS = quota_waits()


def minute_limit(err):
    """Whether an HttpError is the per-minute quota, not a passing 429.

    Measured against 19 real ones: every single one was HTTP 429 with reason
    RATE_LIMIT_EXCEEDED and a body naming "Read"/"Write requests per minute per
    user". A transient 429 names no per-minute quota and wants the short
    jittered backoff instead, not a wait long enough to clear a window.
    """
    if getattr(getattr(err, "resp", None), "status", None) != 429:
        return False
    return "per minute" in (getattr(err, "content", b"") or b"").decode("utf-8", "replace")


def quota_limit_name(err):
    """The limit a quota error names, for saying which one was hit."""
    body = (getattr(err, "content", b"") or b"").decode("utf-8", "replace")
    found = re.search(r"and limit '([^']+)'", body)
    return found.group(1) if found else "per-minute quota"


def stats_target():
    """What GAPI_STATS asks for, as (summary wanted, file to collect into).

    A boolean word keeps the count inside one process. Anything else names a
    file every process appends to, which is the only way the peak means
    anything once a build fans out: eleven generators finishing inside 45
    seconds each see a fraction of the window Google is actually metering, and
    it is their sum that trips the limit. The file is the caller's to remove
    between runs – it is appended to, never truncated here, since a process
    that truncated it would take its siblings' entries with it.
    """
    raw, _ = env("STATS", "")
    raw = raw.strip()
    if raw.lower() in ("", "0", "off", "false"):
        return False, None
    if raw.lower() in ("1", "on", "true", "yes"):
        return True, None
    return True, os.path.expanduser(raw)


STATS, STATS_FILE = stats_target()


def human_size(count):
    """Bytes at the scale the payload cap is discussed in – 414 KB, ~2 MB."""
    if count >= 1 << 20:
        return f"{count / (1 << 20):.1f} MB"
    if count >= 1 << 10:
        return f"{count / (1 << 10):.0f} KB"
    return f"{count} B"


def call_kind(uri, method):
    """Which quota a request spends: (api, "read"|"write").

    Reads and writes are counted apart because Google meters them apart, and
    the HTTP method decides it exactly: values.get is a GET, values.update a
    PUT, every batchUpdate a POST.

    Sheets and Docs have a host each, Drive does not: it answers on
    www.googleapis.com/drive/v3/…, so there the path names the API – or a run
    reading comments reports its Drive calls as "www".
    """
    parts = urlsplit(uri)
    api = (parts.hostname or "").split(".")[0] or "api"
    if api == "www":
        api = parts.path.strip("/").split("/")[0] or api
    return api, ("read" if method.upper() in ("GET", "HEAD") else "write")


# Wall clock, not monotonic: these records are merged across processes, and
# Python only defines a monotonic reading against others from the same one.
# Over a 60-second window a clock step is not a risk worth the ambiguity.
Call = namedtuple("Call", "at api kind size pid")


class Calls:
    """Every API call of a run, and when – across processes when collecting.

    A running total answers the wrong question. The limit is 60 calls per
    minute per user over a SLIDING window, so what decides whether a run is
    safe is its busiest 60 seconds, not how many calls it made in an hour.
    Hence a timestamp per call, and peak() beside the totals.

    One count is one call issued. A request the client library retried inside
    its own backoff spends the quota again without passing through here, so
    after a run that hit 429s the real usage is a little higher than reported –
    which is the safe direction to be wrong in for a budget.

    Given a path, each call is also appended there as one short JSON line, and
    every reading widens to the whole file. That is what makes the peak true
    for a build that fans out into subprocesses, where a per-process figure
    understates the only window that matters.
    """

    WINDOW = 60

    def __init__(self, path=None):
        self.log = []
        self.path = path
        self.handle = None
        self.broken = False

    def record(self, api, kind, size=0):
        self.log.append(Call(time.time(), api, kind, size, os.getpid()))
        self._append(self.log[-1])

    def _append(self, call):
        """Add one line to the shared file, or stop trying and say so.

        Append-only and one write per call, so that concurrent writers keep
        every entry – a read-modify-write would keep whichever process
        finished last and silently drop the rest.
        """
        if not self.path or self.broken:
            return
        try:
            if self.handle is None:
                self.handle = open(self.path, "a")
            self.handle.write(json.dumps(list(call)) + "\n")
            self.handle.flush()
        except OSError as err:
            self.broken = True
            print(f"{env('STATS')[1]}={self.path}: cannot collect ({err}) – counting this "
                  f"process only", file=sys.stderr)

    def records(self):
        """This run's calls: the shared file's where there is one, else ours."""
        if not self.path or self.broken:
            return list(self.log)
        found = []
        try:
            with open(self.path) as f:
                for line in f:
                    try:
                        found.append(Call(*json.loads(line)))
                    except (ValueError, TypeError):
                        continue      # a torn or foreign line is not worth failing over
        except OSError:
            return list(self.log)
        return found

    def counts(self, records=None):
        """Calls per quota, as {(api, kind): n} – e.g. {("sheets", "read"): 30}."""
        totals = {}
        for call in self.records() if records is None else records:
            totals[(call.api, call.kind)] = totals.get((call.api, call.kind), 0) + 1
        return totals

    def peak(self, api, kind, records=None, window=WINDOW):
        """The most calls of one kind inside any `window` seconds.

        This is the number the quota actually applies, and the reason a run
        that makes 90 calls in five minutes never trips it.
        """
        stamps = sorted(call.at for call in (self.records() if records is None else records)
                        if (call.api, call.kind) == (api, kind))
        most, first = 0, 0
        for last, when in enumerate(stamps):
            # Half-open, so a call exactly `window` old has already aged out –
            # closed, a steady 1/s would peak at 61 against a limit of 60
            while when - stamps[first] >= window:
                first += 1
            most = max(most, last - first + 1)
        return most

    def report(self):
        """One line per run, or None when nothing was called."""
        records = self.records()
        if not records:
            return None
        spent = ", ".join(
            f"{api} {kind} {n} (peak {self.peak(api, kind, records)}/{self.WINDOW}s)"
            for (api, kind), n in sorted(self.counts(records).items()))
        workers = {call.pid for call in records}
        where = f" in {len(workers)} processes" if len(workers) > 1 else ""
        line = f"gapi: {len(records)} API call(s){where} – {spent}"
        sent = sum(call.size for call in records)
        if sent:
            # The payload cap sits around 2 MB, so the largest single one is
            # what decides whether a bundle needs splitting – the total does not
            line += (f"; {human_size(sent)} sent, largest "
                     f"{human_size(max(call.size for call in records))}")
        return line


calls = Calls(STATS_FILE)


def print_stats():
    print(calls.report() or "gapi: no API calls", file=sys.stderr)


if STATS:
    atexit.register(print_stats)


class RetryingRequest(HttpRequest):
    """A request that rides out a blip, sits out the minute limit, and counts.

    Two retry policies, because they are two failures. The library's own
    backoff is short, jittered and asked for per call – it covers a 5xx or a
    passing 429. A per-minute quota needs a wait that outlasts the window, so
    that one is fixed, announced, and skipped entirely when the caller turns it
    off. Each pass through the quota loop is counted; the library's own retries
    happen inside one of them and are not – see Calls.
    """

    def execute(self, http=None, num_retries=None):
        tries = RETRIES if num_retries is None else num_retries
        body = getattr(self, "body", None)
        size = len(body.encode("utf-8") if isinstance(body, str) else body) if body else 0
        for wait in QUOTA_WAITS + (None,):
            calls.record(*call_kind(getattr(self, "uri", ""),
                                    getattr(self, "method", "GET")), size=size)
            try:
                return super().execute(http=http, num_retries=tries)
            except HttpError as err:
                if wait is None or not minute_limit(err):
                    raise
                # A silent minute reads as a hang, so say what is happening
                print(f"{quota_limit_name(err)} reached – waiting {wait}s, then retrying",
                      file=sys.stderr)
                time.sleep(wait)


# An IPv6 route that swallows packets instead of refusing them – a mobile
# connection, 2026-09-24 – hung every call for minutes. The resolver listed six
# IPv6 addresses of sheets.googleapis.com before the first IPv4 one; httplib2
# gives up on the first after the client library's 60s timeout without trying
# the next, and each of three retries starts over at IPv6. A token refresh
# reached IPv4, but only after 76s. So a Google host's addresses come back in
# the order of a race, as a browser orders them (RFC 8305): the family the
# resolver lists first starts, the other joins after a head start, and the one
# that completes a handshake first is listed first. Installed on import, so a
# script driving Client or Document gets it too.
HEAD_START = 0.25     # RFC 8305's suggested delay before the next family starts
GIVE_UP = 10          # seconds without a handshake on any family
REMEMBER = 60         # seconds a fallback holds for its host – see getaddrinfo()
GOOGLE = (".googleapis.com", ".google.com")
FAMILY_NAMES = {socket.AF_INET6: "IPv6", socket.AF_INET: "IPv4"}


class Unreachable(OSError):
    """A Google host that completed no handshake in time, on any address.

    Raised with EHOSTUNREACH, which neither httplib2 nor the client library
    retries, so a dead network ends the call after one race rather than four.
    """

    def __str__(self):
        return self.strerror or super().__str__()


class Unresolvable(socket.gaierror):
    """A Google host whose name did not resolve – offline, as a rule.

    Still a gaierror, so every library treats it exactly as before, retries
    included; it only carries a line worth printing, and network_failure()
    finds it under whatever the libraries wrap around it.
    """

    def __str__(self):
        return self.strerror or super().__str__()


def network_failure(err):
    """The Unreachable or Unresolvable behind an error, or None.

    Measured with the network gone: the Sheets call raises httplib2's
    ServerNotFoundError, a refresh google-auth's TransportError around three
    layers from requests and urllib3, a refresh mid-run a TransportError around
    httplib2's – each with the reason at the bottom. A chain can loop, as the
    one credentials() re-raises does, so each link is visited once.
    """
    seen = set()
    while err is not None and id(err) not in seen:
        if isinstance(err, (Unreachable, Unresolvable)):
            return err
        seen.add(id(err))
        err = err.__cause__ or err.__context__
    return None


def handshake(family, address, timeout):
    """Open one TCP connection and close it again – all a race needs to know."""
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(address)


def race(infos, head_start=HEAD_START, give_up=GIVE_UP):
    """The address family that completes a handshake first, or None.

    One address per family, the resolver's first of each, in the resolver's
    order – which is where a system's own preference for IPv4 shows. The first
    family starts; the other starts once the head start is over, or at once
    when the first fails. A loser runs on in its daemon thread until its own
    timeout, and nothing waits on it.
    """
    firsts = {}
    for family, _, _, _, address in infos:
        if family in FAMILY_NAMES:
            firsts.setdefault(family, address)
    order = list(firsts.items())
    verdicts = queue.Queue()

    def attempt(family, address):
        try:
            handshake(family, address, give_up)
            verdicts.put(family)
        except OSError:
            verdicts.put(None)

    deadline = time.monotonic() + give_up
    pending = 0
    for n, (family, address) in enumerate(order, 1):
        threading.Thread(target=attempt, args=(family, address), daemon=True).start()
        pending += 1
        last = n == len(order)
        while pending:
            until = deadline if last else min(deadline, time.monotonic() + head_start)
            try:
                won = verdicts.get(timeout=max(until - time.monotonic(), 0))
            except queue.Empty:
                break
            pending -= 1
            if won is not None:
                return won
            if not last:
                break
    return None


plain_getaddrinfo = getattr(socket.getaddrinfo, "__wrapped__", socket.getaddrinfo)
fallbacks = {}


def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """socket.getaddrinfo, with a Google host's families in the order they answer.

    Every address is still returned, the losing family's last, so a caller that
    tries them in turn keeps its own fallback. Any other host, a call asking for
    one family, and one not asking for TCP – which a race of TCP handshakes says
    nothing about – are passed through untouched.

    Only a fallback is remembered, a race the resolver's first family lost: kept
    too long, it merely costs that family's preference. A win by the first one
    is raced again at the next connection instead – a network that turns on it
    meanwhile, Wi-Fi to a phone's hotspot, would otherwise send that connection
    straight into the hang. Connections are reused, so that is rarely a cost.
    """
    raced = (family == socket.AF_UNSPEC and type in (0, socket.SOCK_STREAM) and port
             and isinstance(host, str) and host.lower().rstrip(".").endswith(GOOGLE))
    try:
        found = plain_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror as err:
        if not raced:
            raise
        raise Unresolvable(err.errno, f"cannot resolve {host} – is the network down?") from err
    if not raced:
        return found
    families = [f for f in FAMILY_NAMES if any(info[0] == f for info in found)]
    if not families:
        return found
    key = (host.lower(), port)
    first, when = fallbacks.get(key, (None, None))
    if when is None or time.monotonic() - when > REMEMBER:
        first = race(found)
        if first is None:
            over = " or ".join(FAMILY_NAMES[f] for f in families)
            raise Unreachable(errno.EHOSTUNREACH, f"no connection to {host} over {over} within "
                                                  f"{GIVE_UP}s – is the network down?")
        preferred = next(info[0] for info in found if info[0] in FAMILY_NAMES)
        if first == preferred:
            fallbacks.pop(key, None)
        else:
            fallbacks[key] = (first, time.monotonic())
    return sorted(found, key=lambda info: info[0] != first)


getaddrinfo.__wrapped__ = plain_getaddrinfo
socket.getaddrinfo = getaddrinfo


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DOCS_SCOPE = "https://www.googleapis.com/auth/documents"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


def token_data():
    if not os.path.exists(TOKEN):
        raise ToolError("no token.json – run auth.py once to authorize")
    with open(TOKEN) as f:
        return json.load(f)


def account():
    """Account label, marked when it is an inference rather than Google-attested."""
    data = token_data()
    email = data.get("email", "")
    if email and data.get("email_source") != "id_token":
        return f"{email} (inferred – run auth.py to confirm with Google)"
    return email


def project():
    """Project id behind the OAuth client. Read from the client file so nothing –
    an agent least of all – has to open a credential to answer "which project?"."""
    try:
        with open(SECRETS) as f:
            return json.load(f).get("installed", {}).get("project_id", "")
    except (OSError, ValueError):
        return ""


def write_token(data, path=None):
    """Replace token.json atomically – parallel sessions refresh concurrently,
    and a half-written credential file would cost a browser re-authorization.
    The temp name must be unique per process: with a shared one, the loser of
    the race calls os.replace() on a file the winner already renamed away."""
    target = path or TOKEN
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target) or ".",
                               prefix=".token.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def credentials():
    data = token_data()
    # No scopes argument: the stored scopes are authoritative, so adding scopes
    # in auth.py cannot cause a scope-mismatch error here.
    creds = Credentials.from_authorized_user_info(data)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except TransportError as err:
            # the reason is all a caller needs, not the four layers around it
            dead = network_failure(err)
            if dead is None:
                raise
            raise dead from None
        refreshed = json.loads(creds.to_json())
        # to_json() drops unknown keys, so both are re-added on every refresh
        refreshed["email"] = data.get("email", "")
        refreshed["email_source"] = data.get("email_source", "")
        write_token(refreshed)
    return creds


class ToolError(Exception):
    """A refusal the caller should see as a message, not a stack trace.

    Raised rather than exited so the same guards work in-process: a library
    that calls sys.exit() on bad input takes its caller's build down with it.
    The CLIs turn these back into a one-line exit.
    """


class DryRun(Exception):
    """Unwinds a command that must not write. Raised by send() in a dry run."""


def split_flags(argv, valued=(), known=()):
    """Separate flags from positional arguments, with a bare -- as terminator.

    Positional arguments are unbounded in both tools, so a value like a field
    mask or a tab id cannot also be positional without the two turning
    ambiguous. Returns the --name=value pairs (None where the flag was absent,
    so an empty value stays distinguishable), the bare flags, and the rest.

    An unrecognised option is refused rather than dropped: a mistyped --dry-run
    would otherwise be swallowed and the write would go through for real.
    """
    cut = argv.index("--") if "--" in argv else len(argv)
    head, tail = argv[:cut], argv[cut + 1:]
    values = {}
    for name in valued:
        given = next((a for a in head if a.startswith(f"--{name}=")), None)
        values[name] = given.split("=", 1)[1] if given is not None else None
    flags = {a for a in head if a.startswith("--") and "=" not in a}
    unknown = sorted({a for a in flags if a not in known} |
                     {a for a in head if a.startswith("--") and "=" in a
                      and a.split("=", 1)[0][2:] not in valued})
    if unknown:
        allowed = ", ".join(sorted([f"--{name}=" for name in valued] + list(known)))
        raise ToolError(f"unknown option: {' '.join(unknown)}\n"
                 f"this tool takes {allowed or 'no options'} – an argument that really "
                 f"starts with -- goes after a bare --")
    return values, flags, [a for a in head if not a.startswith("--")] + tail


def send(request, dry):
    """Execute a mutating request – or, in a dry run, show it and send nothing.

    Reads still run in a dry run, deliberately: `gdocs append` cannot say what
    it would insert without first learning where the document ends, and a
    preview computed from a guess would be worth less than no preview.
    """
    if not dry:
        return request.execute(num_retries=RETRIES)
    body = getattr(request, "body", None)
    print(json.dumps({"dry-run": True,
                      "method": getattr(request, "method", "POST"),
                      "uri": getattr(request, "uri", ""),
                      "body": json.loads(body) if body else None},
                     ensure_ascii=False, indent=1))
    # Unwinding beats returning a fake result: the command's success line would
    # otherwise report cells that were never written.
    raise DryRun()


def load_json(text, what):
    """Parse a JSON argument, failing with the position instead of a traceback.

    These arguments are typed or generated by hand, so a malformed one is a
    normal outcome, not a bug worth a stack trace.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ToolError(
            f"{what}: invalid JSON at line {e.lineno} column {e.colno} – {e.msg}") from None


def as_int(text, what):
    """Parse a numeric argument the same way."""
    try:
        return int(text)
    except ValueError:
        raise ToolError(f"{what}: expected a whole number, got {text!r}") from None


def http_error_message(err):
    """Reduce a googleapiclient HttpError to one readable line.

    Google answers some requests with a full HTML page rather than JSON – a bad
    document id gets Drive's "Seite nicht gefunden" – and letting that escape
    dumps kilobytes of markup into whatever is reading this tool's output.
    """
    status = getattr(getattr(err, "resp", None), "status", "?")
    body = (getattr(err, "content", b"") or b"").decode("utf-8", "replace")
    try:
        detail = json.loads(body)["error"]["message"]
    except Exception:
        detail = ("the API replied with an HTML error page, which usually means a "
                  "malformed or empty id" if body.lstrip().startswith("<")
                  else body[:300] or "no detail given")
    return f"HTTP {status}: {detail}"


def require_scope(scope, what):
    """Fail with the fix rather than Google's opaque 403.

    A token predating a scope keeps working for everything it already covers, so
    what is missing is one browser consent, not a reinstall.
    """
    if scope not in (token_data().get("scopes") or []):
        raise ToolError(
            f"this token carries no {what} scope.\n"
            f"Run  {os.path.join(HERE, 'auth.py')}  once and consent again – same\n"
            "account, existing access kept, nothing else to change."
        )
