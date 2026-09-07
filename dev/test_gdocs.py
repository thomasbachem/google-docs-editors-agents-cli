#!/usr/bin/env python3
"""Drive gdocs.py's command branches against a fake Docs API.

No network and no Google account: the token is a throwaway file, so this also
covers `create`, which would otherwise leave an undeletable document behind
(the tool holds no Drive scope).
"""

import contextlib
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
from googleapiclient.errors import HttpError  # noqa: E402

import gauth  # noqa: E402
import gdocs  # noqa: E402

calls = []
fails = []
retries = []
sent = []

# run() swaps gdocs.api for a fake, so the real one is kept to test its preflight.
REAL_API = gdocs.api


class Exec:
    """Stands in for an HttpRequest: what --dry-run prints comes off these."""

    def __init__(self, result, body=None):
        self.result = result
        self.body = json.dumps(body) if body is not None else None
        self.uri = "https://fake/"
        self.method = "POST"

    def execute(self, **kw):
        retries.append(kw.get("num_retries"))
        sent.append(self.body)      # None for reads, so `any(sent)` means a write
        return self.result


def para(start, end, text, style="NORMAL_TEXT"):
    return {"startIndex": start, "endIndex": end,
            "paragraph": {"elements": [{"textRun": {"content": text}}],
                          "paragraphStyle": {"namedStyleType": style}}}


# One tab with a child tab; indices match the text lengths exactly.
TAB_DOC = {
    "title": "Testdoc",
    "tabs": [{
        "tabProperties": {"tabId": "t.0", "title": "Erste"},
        "documentTab": {"body": {"content": [
            {"startIndex": 0, "endIndex": 1, "sectionBreak": {}},
            para(1, 7, "Titel\n", "HEADING_1"),
            para(7, 19, "Absatz eins\n"),
        ]}},
        "childTabs": [{
            "tabProperties": {"tabId": "t.1", "title": "Kind"},
            "documentTab": {"body": {"content": [para(1, 6, "Kind\n")]}},
        }],
    }],
}

# A document from before tabs existed: content hangs off the top-level body.
LEGACY_DOC = {"title": "Alt", "body": {"content": [
    {"startIndex": 0, "endIndex": 1, "sectionBreak": {}},
    para(1, 4, "Hi\n"),
]}}

# Text inside a table must come out too, or `get` silently loses columns.
TABLE_DOC = {"title": "Mit Tabelle", "body": {"content": [
    para(1, 5, "Vor\n"),
    {"startIndex": 5, "endIndex": 17, "table": {"tableRows": [{"tableCells": [
        {"content": [para(7, 11, "A1\n")]},
        {"content": [para(11, 15, "B1\n")]},
    ]}]}},
]}}


class Docs:
    def __init__(self, doc):
        self.doc = doc

    def create(self, body=None):
        calls.append(("create", body))
        return Exec({"documentId": "FAKEDOC", "title": (body or {}).get("title")}, body=body)

    def get(self, **kw):
        calls.append(("get", kw))
        return Exec(self.doc)

    def batchUpdate(self, **kw):
        calls.append(("batchUpdate", kw))
        return Exec({"replies": [{"replaceAllText": {"occurrencesChanged": 3}}]},
                    body=kw.get("body"))


def use_token(email="tester@example.com", source="id_token", scopes=None):
    """Point the shared auth module at a throwaway token."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"email": email, "email_source": source,
                   "scopes": scopes if scopes is not None
                   else [gauth.SHEETS_SCOPE, gauth.DOCS_SCOPE]}, f)
    gauth.TOKEN = path
    fd, spath = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"installed": {"project_id": "test-project-42"}}, f)
    gauth.SECRETS = spath
    return path


LAST_EXIT = None


def run(argv, doc=TAB_DOC):
    global LAST_EXIT
    LAST_EXIT = None
    calls.clear()
    retries.clear()
    sent.clear()
    gdocs.api = lambda: Docs(doc)
    out, err = io.StringIO(), io.StringIO()
    sys.argv = argv
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            gdocs.main()
        except SystemExit as e:
            LAST_EXIT = e.code
    return out.getvalue(), err.getvalue()


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{('  ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


def last_request(kind):
    return calls[-1][1]["body"]["requests"][0][kind]


print("=== gdocs.py branches (fake API) ===")
token_path = use_token()

out, _ = run(["gdocs", "whoami"])
check("whoami prints the account", out.splitlines()[0] == "tester@example.com",
      out.splitlines()[0])
check("and the project, so nothing opens the credential file to learn it",
      "project: test-project-42" in out, out.splitlines()[-1])

out, err = run(["gdocs", "create", "Mein Dokument"])
check("create prints the document URL", "document/d/FAKEDOC/edit" in out, out.splitlines()[0])
# a peer session did url=$(gdocs create …) and got the account line instead
check("create keeps stdout to the URL alone",
      out.strip() == "https://docs.google.com/document/d/FAKEDOC/edit", repr(out))
check("create names the account on stderr",
      "created in the Drive of tester@example.com" in err, repr(err))
check("create passes the title verbatim", calls[0][1] == {"title": "Mein Dokument"})

# a URL argument must reduce to the bare id
check("doc_id strips a full URL",
      gdocs.doc_id("https://docs.google.com/document/d/ABC-123_x/edit#heading=h.9") == "ABC-123_x")
check("doc_id passes a bare id through", gdocs.doc_id("ABC-123_x") == "ABC-123_x")

out, _ = run(["gdocs", "info", "ID"])
check("info prints the title", out.startswith("Testdoc"), out.splitlines()[0])
check("info prints tab ids", "tabId=t.0" in out and "tabId=t.1" in out)
check("info prints the end index", "end index=19" in out, out)
check("info outlines headings only",
      "HEADING_1" in out and "Titel" in out and "Absatz eins" not in out, out)

out, _ = run(["gdocs", "get", "ID"])
check("get prints the first tab's text", out == "Titel\nAbsatz eins\n", repr(out))

out, _ = run(["gdocs", "get", "--tab=t.1", "ID"])
check("get --tab reads a child tab", out == "Kind\n", repr(out))

out, _ = run(["gdocs", "get", "ID"], doc=LEGACY_DOC)
check("get falls back to a tab-less body", out == "Hi\n", repr(out))

out, _ = run(["gdocs", "get", "ID"], doc=TABLE_DOC)
check("get follows table cells", out == "Vor\nA1\nB1\n", repr(out))

out, err = run(["gdocs", "index", "ID"])
check("index prints start and end", "1      7" in out and "7     19" in out, out)
check("index tags the heading", "HEADING_1" in out and "sectionBreak" in out)
check("index shows newlines as escapes", "'Titel\\n'" in out, out)

# a silent cut invites comparing a preview against the full text and missing it
long_doc = {"title": "Lang", "body": {"content": [para(1, 200, "W" * 198 + "\n")]}}
out, _ = run(["gdocs", "index", "ID"], doc=long_doc)
check("index marks a truncated preview", "…'" in out, out.strip()[-30:])
check("index does not mark a short one", "…" not in run(["gdocs", "index", "ID"])[0])
check("index hints the last insert index", "insert at 18" in err, err.strip())

out, _ = run(["gdocs", "json", "ID"])
check("json round-trips the document", json.loads(out)["title"] == "Testdoc")

# append has to find the end itself, and stop one short of the final newline
out, _ = run(["gdocs", "append", "ID", "Neu"])
req = last_request("insertText")
check("append inserts at end index - 1", req["location"] == {"index": 18}, str(req["location"]))
check("append sends the text unchanged", req["text"] == "Neu")
check("append reports the range", "3 chars at [18, 21)" in out, out.strip())
check("append reads the document first", calls[0][0] == "get")

out, _ = run(["gdocs", "append", "--tab=t.1", "ID", "Neu"])
req = last_request("insertText")
check("append --tab targets that tab and its own end",
      req["location"] == {"index": 5, "tabId": "t.1"}, str(req["location"]))

# indices count UTF-16 units, so an astral character counts twice
out, _ = run(["gdocs", "append", "ID", "a🙂b"])
check("append counts UTF-16 units, not code points", "4 chars at [18, 22)" in out, out.strip())
check("utf16_len agrees", gdocs.utf16_len("a🙂b") == 4 and len("a🙂b") == 3)

out, _ = run(["gdocs", "insert", "ID", "7", "X"])
req = last_request("insertText")
check("insert uses the given index", req["location"] == {"index": 7} and req["text"] == "X")
check("insert does not read the document first",
      not any(c[0] == "get" for c in calls), str([c[0] for c in calls]))

out, _ = run(["gdocs", "replace", "ID", "alt", "neu"])
req = last_request("replaceAllText")
check("replace matches case by default", req["containsText"] == {"text": "alt", "matchCase": True})
check("replace spans every tab by default", "tabsCriteria" not in req, str(req))
check("replace reports the count", "3 occurrence(s)" in out, out.strip())

run(["gdocs", "replace", "--tab=t.1", "ID", "alt", "neu"])
check("replace --tab narrows to that tab",
      last_request("replaceAllText")["tabsCriteria"] == {"tabIds": ["t.1"]})

out, _ = run(["gdocs", "delete", "ID", "7", "19"])
check("delete sends an end-exclusive range",
      last_request("deleteContentRange")["range"] == {"startIndex": 7, "endIndex": 19})
check("delete reports the range", "deleted [7, 19)" in out, out.strip())

out, _ = run(["gdocs", "style", "ID", "1", "7", "HEADING_2"])
req = last_request("updateParagraphStyle")
check("style sets the named style", req["paragraphStyle"] == {"namedStyleType": "HEADING_2"})
check("style touches only that field", req["fields"] == "namedStyleType")

out, _ = run(["gdocs", "format", "ID", "1", "7", '{"bold":true,"italic":false}'])
req = last_request("updateTextStyle")
check("format passes the style through", req["textStyle"] == {"bold": True, "italic": False})
check("format lists exactly the given fields", req["fields"] == "bold,italic", req["fields"])

run(["gdocs", "format", "ID", "1", "7", '{"link":{"url":"https://example.com"}}'])
req = last_request("updateTextStyle")
check("format writes a link as a text style",
      req["textStyle"]["link"] == {"url": "https://example.com"} and req["fields"] == "link")

run(["gdocs", "format", "ID", "1", "7", "{}"])
check("format refuses an empty style",
      isinstance(LAST_EXIT, str) and "non-empty style object" in LAST_EXIT, str(LAST_EXIT)[:60])
check("format writes nothing when refused", not any(c[0] == "batchUpdate" for c in calls))

# the batch escape hatch: rising indices are the classic silent mistake
_, err = run(["gdocs", "batch", "ID",
              '[{"insertText":{"location":{"index":5},"text":"a"}},'
              ' {"insertText":{"location":{"index":40},"text":"b"}}]'])
check("batch warns on rising indices", "WARNING" in err and "[5, 40]" in err, err.strip()[:70])

_, err = run(["gdocs", "batch", "ID",
              '[{"insertText":{"location":{"index":40},"text":"b"}},'
              ' {"insertText":{"location":{"index":5},"text":"a"}}]'])
check("back-to-front order is silent", "WARNING" not in err, err.strip()[:70])

_, err = run(["gdocs", "batch", "ID",
              '[{"deleteContentRange":{"range":{"startIndex":30,"endIndex":40}}},'
              ' {"updateTextStyle":{"range":{"startIndex":1,"endIndex":5},"textStyle":{},'
              '"fields":"bold"}}]'])
check("ranges count as indices too, descending is fine", "WARNING" not in err, err.strip()[:70])

_, err = run(["gdocs", "batch", "ID", '[{"updateDocumentStyle":{"documentStyle":{}}}]'])
check("index-free requests never warn", "WARNING" not in err)

# TabProperties.index is a tab's position among its siblings, not a document
# offset – counting it would warn about an ordering that does not exist
_, err = run(["gdocs", "batch", "ID",
              '[{"addDocumentTab":{"tabProperties":{"title":"A","index":0}}},'
              ' {"addDocumentTab":{"tabProperties":{"title":"B","index":1}}}]'])
check("a tab's own index is not a document position", "WARNING" not in err, err.strip()[:70])

# ...while a table request's nested location still counts
_, err = run(["gdocs", "batch", "ID",
              '[{"insertTableRow":{"tableCellLocation":{"tableStartLocation":{"index":5}}}},'
              ' {"insertText":{"location":{"index":90},"text":"x"}}]'])
check("a nested table location still counts", "WARNING" in err and "[5, 90]" in err,
      err.strip()[:70])

# an insert with no fixed index cannot be out of order
_, err = run(["gdocs", "batch", "ID",
              '[{"insertText":{"endOfSegmentLocation":{},"text":"a"}},'
              ' {"insertText":{"endOfSegmentLocation":{},"text":"b"}}]'])
check("end-of-segment inserts never warn", "WARNING" not in err, err.strip()[:70])

# a bare -- ends flag parsing, so content may start with --tab=
run(["gdocs", "replace", "ID", "--", "--tab=literal", "ersetzt"])
req = last_request("replaceAllText")
check("-- lets content start with --tab=",
      req["containsText"]["text"] == "--tab=literal" and req["replaceText"] == "ersetzt",
      str(req)[:60])
check("-- does not leak a tabId", "tabsCriteria" not in req)

run(["gdocs", "replace", "--tab=t.1", "ID", "--", "--tab=literal", "x"])
check("a real --tab still applies before the terminator",
      last_request("replaceAllText")["tabsCriteria"] == {"tabIds": ["t.1"]})

out, _ = run(["gdocs", "batch", "ID", '[{"insertText":{"location":{"index":5},"text":"a"}}]'])
check("batch prints the replies", json.loads(out)[0]["replaceAllText"]["occurrencesChanged"] == 3)

# a tab id that is not there must name the ones that are, not edit the wrong tab
run(["gdocs", "get", "--tab=nope", "ID"])
check("unknown tab is refused by name",
      isinstance(LAST_EXIT, str) and "'t.0'" in LAST_EXIT and "'t.1'" in LAST_EXIT,
      str(LAST_EXIT)[:60])

# missing arguments must explain themselves, not raise IndexError
for cmd, need in (("append", 1), ("insert", 2), ("replace", 2), ("delete", 2),
                  ("style", 3), ("format", 3), ("batch", 1)):
    run(["gdocs", cmd, "ID"])
    check(f"{cmd} reports missing arguments",
          isinstance(LAST_EXIT, str) and f"expected {need} argument" in LAST_EXIT,
          str(LAST_EXIT)[:50])

# --tab=$UNSET expands to --tab= : writing to the first tab is the wrong answer
run(["gdocs", "append", "--tab=", "ID", "x"])
check("an empty --tab= is refused, not treated as the first tab",
      isinstance(LAST_EXIT, str) and "is empty" in LAST_EXIT, str(LAST_EXIT)[:40])
check("nothing was written to any tab", not any(sent), str(sent))

run(["gdocs", "append", "--dryrun", "ID", "x"])
check("a mistyped --dry-run is refused here too",
      isinstance(LAST_EXIT, str) and "unknown option" in LAST_EXIT, str(LAST_EXIT)[:40])
check("and no edit went through", not any(sent), str(sent))

# --dry-run must show the index it really computed, not a guess
# the read path no longer passes num_retries per call – the service carries it,
# which is what covers in-process callers who never go through send()
import googleapiclient.http as _ghttp  # noqa: E402

_seen = []
_orig_execute = _ghttp.HttpRequest.execute
_ghttp.HttpRequest.execute = lambda self, http=None, num_retries=0: _seen.append(num_retries)
_probe = object.__new__(gauth.RetryingRequest)
_probe.execute()
_probe.execute(num_retries=0)
_ghttp.HttpRequest.execute = _orig_execute
check("a service request retries by default", _seen[0] == gauth.RETRIES, str(_seen))
check("an explicit num_retries still wins", _seen[1] == 0, str(_seen))

_built = {}
_real_build, _real_creds = gdocs.build, gdocs.credentials
gdocs.build = lambda *a, **kw: (_built.update(kw)
                                 or type("S", (), {"spreadsheets": lambda s: "svc",
                                                   "documents": lambda s: "svc"})())
gdocs.credentials = lambda: "creds"
REAL_API()
gdocs.build, gdocs.credentials = _real_build, _real_creds
check("api() hands out a retrying service",
      _built.get("requestBuilder") is gauth.RetryingRequest, str(_built.get("requestBuilder")))

out, _ = run(["gdocs", "append", "--dry-run", "ID", "Neu"])
shown = json.loads(out)
check("dry-run append shows the computed index",
      shown["body"]["requests"][0]["insertText"]["location"] == {"index": 18}, out[:80])
check("dry-run append sends nothing", not any(sent), str(sent))
check("dry-run append still read the document", any(c[0] == "get" for c in calls))

out, _ = run(["gdocs", "create", "--dry-run", "Neu"])
check("dry-run create leaves no document behind",
      json.loads(out)["body"] == {"title": "Neu"} and not any(sent), out[:50])

out, _ = run(["gdocs", "style", "--dry-run", "--tab=t.1", "ID", "1", "7", "HEADING_2"])
req = json.loads(out)["body"]["requests"][0]["updateParagraphStyle"]
check("dry-run keeps the tab in the previewed range",
      req["range"]["tabId"] == "t.1" and not any(sent), out[:60])

out, _ = run(["gdocs", "append", "ID", "Neu"])
check("without --dry-run the edit really goes", any(sent), str(sent))

# a hand-typed argument is normal input, so a bad one must not raise
run(["gdocs", "batch", "ID", "{oops"])
check("malformed JSON is one line",
      isinstance(LAST_EXIT, str) and "invalid JSON" in LAST_EXIT, str(LAST_EXIT)[:60])
run(["gdocs", "delete", "ID", "x", "5"])
check("a non-numeric index is one line",
      isinstance(LAST_EXIT, str) and "whole number" in LAST_EXIT, str(LAST_EXIT)[:60])
check("nothing was sent for either", not any(c[0] == "batchUpdate" for c in calls))

# a transient 503 must not become an edit that silently never happened
run(["gdocs", "append", "ID", "Neu"])
check("a gdocs write asks the library to retry",
      retries and retries[-1] == gauth.RETRIES, str(retries))

# an API error must stay one readable line: Google answers a bad id with a full
# HTML page, and a raw traceback would dump kilobytes into the caller's output
class Resp:
    status = 403
    reason = "Forbidden"


class Failing(Docs):
    def __init__(self, err):
        super().__init__(TAB_DOC)
        self.err = err

    def get(self, **kw):
        raise self.err


def run_failing(argv, err):
    global LAST_EXIT
    LAST_EXIT = None
    gdocs.api = lambda: Failing(err)
    out, errout = io.StringIO(), io.StringIO()
    sys.argv = argv
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errout):
        try:
            gdocs.main()
        except SystemExit as e:
            LAST_EXIT = e.code
    return out.getvalue(), errout.getvalue()


run_failing(["gdocs", "get", "ID"],
            HttpError(Resp(), b'{"error": {"message": "Docs API has not been used"}}'))
check("an API error exits with one line",
      isinstance(LAST_EXIT, str) and LAST_EXIT == "HTTP 403: Docs API has not been used",
      str(LAST_EXIT)[:70])

run_failing(["gdocs", "get", "ID"], HttpError(Resp(), b"<!DOCTYPE html><html>" + b"x" * 5000))
check("an HTML error page is summarised, not printed",
      isinstance(LAST_EXIT, str) and len(LAST_EXIT) < 200 and "HTML error page" in LAST_EXIT,
      f"{len(str(LAST_EXIT))} chars")

# the empty id that produced that HTML page in the first place
run(["gdocs", "index", ""])
check("an empty document id is refused up front",
      isinstance(LAST_EXIT, str) and "no document id given" in LAST_EXIT, str(LAST_EXIT)[:50])

# the scope preflight replaces Google's opaque 403 with the actual fix
sheets_only = use_token(scopes=[gauth.SHEETS_SCOPE])
gdocs.credentials = lambda: "creds"
gdocs.build = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("built without the scope"))
try:
    REAL_API()
    exit_msg = None
except gauth.ToolError as e:
    exit_msg = str(e)
check("api() refuses a token without the Docs scope",
      exit_msg is not None and "no Google Docs scope" in exit_msg, str(exit_msg)[:60])
check("the refusal names auth.py as the fix", "auth.py" in (exit_msg or ""))

both = use_token(scopes=[gauth.SHEETS_SCOPE, gauth.DOCS_SCOPE])


class Built:
    def documents(self):
        return "documents-resource"


gdocs.build = lambda *a, **kw: Built()
check("api() proceeds once the scope is there", REAL_API() == "documents-resource")

for path in (token_path, sheets_only, both):
    if os.path.exists(path):
        os.unlink(path)

# Same for Document – a mistyped tab id is the likeliest in-process mistake
def direct(fn):
    try:
        return "ok", fn()
    except gauth.ToolError as e:
        return "ToolError", str(e)
    except SystemExit as e:
        return "SystemExit", str(e.code)


print("\n--- Document, called directly ---")
use_token()
doc = gdocs.Document("ID", service=Docs(TAB_DOC))

kind, msg = direct(lambda: gdocs.Document("", service=Docs(TAB_DOC)))
check("an empty id raises instead of exiting", kind == "ToolError", f"{kind}: {str(msg)[:40]}")

wrong = gdocs.Document("ID", service=Docs(TAB_DOC), tab="t.nope")
kind, msg = direct(lambda: wrong.text())
check("a mistyped tab raises instead of exiting", kind == "ToolError", f"{kind}: {str(msg)[:40]}")

kind, msg = direct(lambda: doc.format(1, 5, {}))
check("an empty style raises instead of exiting", kind == "ToolError", f"{kind}: {str(msg)[:40]}")

check("text returns the selected tab's text", doc.text() == "Titel\nAbsatz eins\n", repr(doc.text()))
check("end returns the tab's end index", doc.end() == 19, str(doc.end()))
check("tabs lists the whole tree", [t[0] for t in doc.tabs()] == ["t.0", "t.1"])
check("append reports the range it wrote", doc.append("Neu") == (18, 21), str(doc.append("Neu")))

# --tab names a tab inside an existing document, so it means nothing to whoami or
# create. Swallowing it would suggest create had placed content in a tab.
for cmd, argv in (("whoami", ["gdocs", "whoami", "--tab=t.0"]),
                  ("create", ["gdocs", "create", "Titel", "--tab=t.0"])):
    run(argv)
    check(f"--tab is refused on `{cmd}`",
          isinstance(LAST_EXIT, str) and "--tab= does not apply" in LAST_EXIT,
          str(LAST_EXIT)[:60])
run(["gdocs", "whoami"])
check("whoami still works without it", LAST_EXIT is None, str(LAST_EXIT)[:60])

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
