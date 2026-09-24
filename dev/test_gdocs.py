#!/usr/bin/env python3
"""Drive gdocs.py's command branches against a fake Docs API.

No network and no Google account: the token is a throwaway file, so this also
covers `create`, which would otherwise leave a document behind that no command
here deletes.
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
import gcomments  # noqa: E402
import gdocs  # noqa: E402

calls = []
fails = []
retries = []
sent = []

# run() swaps gdocs.api and gcomments.drive_api for fakes, so the real ones are kept to probe them
REAL_API = gdocs.api
REAL_DRIVE_API = gcomments.drive_api


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
# The Drive service run() hands out – None until the comment tests set one, so a
# Docs command that reached for Drive fails loudly instead of passing quietly.
DRIVE = [None]


def run(argv, doc=TAB_DOC):
    global LAST_EXIT
    LAST_EXIT = None
    calls.clear()
    retries.clear()
    sent.clear()
    gdocs.api = lambda: Docs(doc)
    gcomments.drive_api = lambda: DRIVE[0] or (_ for _ in ()).throw(
        AssertionError("a Docs command built the Drive service"))
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

DEAD = ("no connection to docs.googleapis.com over IPv6 or IPv4 within 10s – "
        "is the network down?")
run_failing(["gdocs", "get", "ID"], gauth.Unreachable(gauth.errno.EHOSTUNREACH, DEAD))
check("a dead network exits with one line, not a traceback", LAST_EXIT == DEAD, str(LAST_EXIT))

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
check("Document.api() hands back the service it drives", doc.api() is doc.service)

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

# --- comments: Drive's threads, placed through a DOCX export ---------------------
print("\n--- comments ---")
import zipfile  # noqa: E402
from xml.sax.saxutils import escape as xml_escape  # noqa: E402

WNS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
       'xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" '
       'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"')


def docx_export(body, comments=()):
    """A DOCX as Google writes one, reduced to what `comments` reads.

    body: a list of paragraphs, ("title", text) for the paragraph the export adds per
    tab, ("cell", paragraph) for a one-cell table, or ("S", id) for a range opened
    between paragraphs. A paragraph is a list of text, ("S"|"E", id), ("tab",), ("br",),
    ("page",), ("img",) an inline image, ("float",) a floating one and ("foot",) a
    footnote mark. comments: (id, date, text, paraId, parent paraId or None).
    """
    def paragraph(parts, style=""):
        # tab stops reuse the name w:tab, and must not read as a tab character
        xml = ['<w:p><w:pPr>' + (f'<w:pStyle w:val="{style}"/>' if style else "") +
               '<w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr>']
        for part in parts:
            if isinstance(part, str):
                xml.append('<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">'
                           f'{xml_escape(part)}</w:t></w:r>')
            elif part[0] in ("S", "E"):
                end = "Start" if part[0] == "S" else "End"
                xml.append(f'<w:commentRange{end} w:id="{part[1]}"/>')
            else:
                xml.append({"tab": "<w:r><w:tab/></w:r>", "br": "<w:r><w:br/></w:r>",
                            "page": '<w:r><w:br w:type="page"/></w:r>',
                            # whatever a drawing holds inside is not the paragraph's text
                            "img": "<w:r><w:drawing><wp:inline><w:t>Bildtitel</w:t></wp:inline>"
                                   "</w:drawing></w:r>",
                            # a floating image holds no index in the Docs API
                            "float": "<w:r><w:drawing><wp:anchor/></w:drawing></w:r>",
                            "foot": '<w:r><w:footnoteReference w:id="1"/></w:r>'}[part[0]])
        return "".join(xml) + "</w:p>"

    xml = []
    for item in body:
        if isinstance(item, list):
            xml.append(paragraph(item))
        elif item[0] == "title":
            xml.append(paragraph([item[1]], "Title"))
        elif item[0] == "cell":
            xml.append(f"<w:tbl><w:tblPr/><w:tr><w:tc><w:tcPr/>{paragraph(item[1])}"
                       "</w:tc></w:tr></w:tbl>")
        else:
            xml.append(f'<w:commentRangeStart w:id="{item[1]}"/>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   f"<w:document {WNS}><w:body>{''.join(xml)}</w:body></w:document>")
        z.writestr("word/comments.xml", f"<w:comments {WNS}>" + "".join(
            f'<w:comment w:id="{cid}" w:author="Tom" w:date="{date}Z"><w:p w14:paraId="{para}">'
            f'<w:r><w:t>{xml_escape(text)}</w:t></w:r></w:p></w:comment>'
            for cid, date, text, para, _ in comments) + "</w:comments>")
        z.writestr("word/commentsExtended.xml", f"<w15:commentsEx {WNS}>" + "".join(
            f'<w15:commentEx w15:paraId="{para}"'
            + (f' w15:paraIdParent="{parent}"' if parent else "") + ' w15:done="0"/>'
            for _, _, _, para, parent in comments) + "</w15:commentsEx>")
    return buf.getvalue()


def runs(start, *parts, style="NORMAL_TEXT"):
    """A paragraph of text runs and one-unit chips, indexed as the Docs API counts."""
    elements, at = [], start
    for part in parts:
        width = gdocs.utf16_len(part) if isinstance(part, str) else 1
        elements.append({"startIndex": at, "endIndex": at + width,
                         **({"textRun": {"content": part}} if isinstance(part, str) else part)})
        at += width
    return {"startIndex": start, "endIndex": at,
            "paragraph": {"elements": elements, "paragraphStyle": {"namedStyleType": style}}}


COMMENT_DOC = {"title": "Kommentare", "tabs": [
    {"tabProperties": {"tabId": "t.0", "title": "Eins"}, "documentTab": {"body": {"content": [
        {"startIndex": 0, "endIndex": 1, "sectionBreak": {}},
        runs(1, "Titel\n", style="HEADING_1"),                       # [1, 7)
        runs(7, "🚀 Ziel ist Wachstum.\n"),                           # Wachstum at [19, 27)
        runs(29, "Ende A\n"),                                        # A at 34
        runs(36, "Anfang B\n"),                                      # Anfang ends at 42
        {"startIndex": 45, "endIndex": 55, "table": {"tableRows": [{"tableCells": [
            {"content": [runs(47, "Kosten\n")]}]}]}},                # Kosten at [47, 53)
        runs(55, "Frag ", {"person": {"personProperties": {"name": "Kai"}}}, " dazu\n"),
        runs(67, "\n"),
    ]}}},
    {"tabProperties": {"tabId": "t.1", "title": "Zwei"}, "documentTab": {"body": {"content": [
        {"startIndex": 0, "endIndex": 1, "sectionBreak": {}},
        runs(1, "Text im zweiten Tab.\n"),                          # zweiten at [9, 16)
        runs(22, "Bild ", {"inlineObjectElement": {"inlineObjectId": "kix.1"}}, " hier\n"),
    ]}}},                                                            # image [27, 28), hier [29, 33)
]}
DOCX_DATA = docx_export(
    [("title", "Eins"), ("S", "5"), ["Titel", ("E", "5")],
     ["🚀 Ziel ist ", ("S", "0"), ("S", "6"), "Wachstum", ("E", "0"), ("E", "6"), "."],
     ["Ende ", ("S", "1"), "A"], ["Anfang", ("E", "1"), " B"],
     ("cell", [("S", "2"), "Kosten", ("E", "2")]),
     # a paragraph the export spells differently – a stand-in: a person chip, measured,
     # exports as the name the Docs API gives it too
     ["Frag kai@example.com ", ("S", "3"), "dazu", ("E", "3")], [],
     ("title", "Zwei"), ["Text im ", ("S", "4"), "zweiten", ("E", "4"), " Tab."],
     [("S", "10"), "Bild ", ("S", "9"), ("img",), ("E", "9"), ("E", "10"), " ",
      ("S", "11"), "hier", ("E", "11")]],
    [("0", "2026-09-14T10:00:00", "Nach Emoji", "10", None),
     ("1", "2026-09-14T10:01:00", "Über zwei", "11", None),
     ("2", "2026-09-14T10:02:00", "In Tabelle", "12", None),
     ("3", "2026-09-14T10:03:00", "Mit Chip", "13", None),
     ("4", "2026-09-14T10:04:00", "Zweiter Tab", "14", None),
     ("5", "2026-09-14T10:05:00", "Ganzer Titel", "15", None),
     ("6", "2026-09-14T10:08:00", "Antwort", "16", "10"),
     ("7", "2026-09-14T10:09:00", "Y", "17", None),
     ("8", "2026-09-14T10:09:00", "Z", "18", None),
     ("9", "2026-09-14T10:10:00", "Auf Bild", "19", None),
     ("10", "2026-09-14T10:11:00", "Bis Bild", "20", None),
     ("11", "2026-09-14T10:12:00", "Offen", "21", None)])


def thread(cid, created, text, quoted=None, **extra):
    return {"id": cid, "anchor": "kix.abc123", "content": text, "createdTime": created,
            "modifiedTime": created, "author": {"displayName": "Tom"},
            **({"quotedFileContent": {"value": quoted}} if quoted is not None else {}), **extra}


DOC_THREADS = [
    thread("emoji", "2026-09-14T10:00:00.100Z", "Nach Emoji", "Wachstum",
           replies=[{"id": "r1", "content": "Antwort", "createdTime": "2026-09-14T10:08:00.500Z",
                     "author": {"displayName": "Kai"}}]),
    thread("span", "2026-09-14T10:01:00.000Z", "Über zwei", "A\nAnfang"),
    thread("cell", "2026-09-14T10:02:00.000Z", "In Tabelle", "Kosten alt"),
    thread("chip", "2026-09-14T10:03:00.000Z", "Mit Chip", "dazu"),
    thread("second", "2026-09-14T10:04:00.000Z", "Zweiter Tab", "zweiten"),
    thread("between", "2026-09-14T10:05:00.000Z", "Ganzer Titel", "Titel"),
    thread("deleted", "2026-09-14T10:06:00.000Z", "Weg damit", "gel&#246;scht"),
    thread("done", "2026-09-14T10:07:00.000Z", "Erledigt", "Erl&#246;se", resolved=True),
    {"id": "file", "content": "Ohne Text", "createdTime": "2026-09-14T10:07:30.000Z",
     "author": {"displayName": "Tom"}},
    # the same second as the reply above, which must never be taken for it
    thread("late", "2026-09-14T10:08:00.000Z", "Spät", "spät"),
    # two threads in the export share its second and neither text is its own
    thread("blur", "2026-09-14T10:09:00.000Z", "X", "x"),
    thread("image", "2026-09-14T10:10:00.000Z", "Auf Bild"),
    thread("upto", "2026-09-14T10:11:00.000Z", "Bis Bild"),
    # one second, three threads, one entry – the resolved one and the deleted one are not in it
    thread("open2", "2026-09-14T10:12:00.100Z", "Offen", "hier"),
    thread("done2", "2026-09-14T10:12:00.500Z", "Anders", "hier", resolved=True),
    thread("gone2", "2026-09-14T10:12:00.900Z", "Weg auch", "weg"),
]


class FakeDrive:
    def __init__(self, found=DOC_THREADS, docx=DOCX_DATA, broken=False):
        self.found, self.docx, self.broken = list(found), docx, broken

    def comments(self):
        drive = self

        class Comments:
            def list(self, **kw):
                calls.append(("drive.comments.list", kw))
                return Exec({"comments": drive.found})

            def get(self, **kw):
                calls.append(("drive.comments.get", kw))
                found = next(c for c in drive.found if c["id"] == kw["commentId"])
                return Exec({"resolved": True} if found.get("resolved") else {})
        return Comments()

    def files(self):
        drive = self

        class Files:
            def export(self, **kw):
                calls.append(("drive.files.export", kw))
                if drive.broken:
                    raise HttpError(Resp(), b'{"error": {"message": "This file is too large '
                                            b'to be exported."}}')
                return Exec(drive.docx)
        return Files()

    def replies(self):
        class Replies:
            def create(self, **kw):
                calls.append(("drive.replies.create", kw))
                return Exec({"id": "reply-1", **kw["body"]}, body=kw["body"])
        return Replies()


DRIVE[0] = FakeDrive()


breaks = gdocs.docx_comments(docx_export([["a", ("tab",), "b"],
                                          ["x", ("br",), "y", ("page",), "z", ("img",),
                                           ("float",), ("foot",)]]))[0]
check("the DOCX reader skips tab stops, reads a tab and a line break, and an image, a footnote "
      "mark and a page break as one object character each – a floating image as none",
      breaks == ["a\tb", "x\u000by" + gdocs.OBJECT + "z" + 2 * gdocs.OBJECT], repr(breaks))

out, err = run(["gdocs", "comments", "--json", "--all", "ID"], doc=COMMENT_DOC)
placed = {c["id"]: c for c in json.loads(out)}


def where(cid):
    c = placed[cid]
    return c["placement"], c["tab_id"], c["start"], c["end"], c["passage"]


check("a thread after an emoji gets the range the Docs API counts, in UTF-16",
      where("emoji") == ("text", "t.0", 19, 27, "Wachstum"), str(where("emoji")))
check("a thread across two paragraphs spans both, the newline included",
      where("span") == ("text", "t.0", 34, 42, "A\nAnfang"), str(where("span")))
check("a thread in a table cell is found inside it",
      where("cell") == ("text", "t.0", 47, 53, "Kosten"), str(where("cell")))
check("a thread in another tab names that tab, the export's title paragraphs skipped",
      where("second") == ("text", "t.1", 9, 16, "zweiten")
      and placed["second"]["tab"] == "Zwei", str(where("second")))
check("a thread on an image alone covers the image's one index",
      where("image") == ("text", "t.1", 27, 28, gdocs.OBJECT), str(where("image")))
check("a thread ending right after an image takes the image in",
      where("upto") == ("text", "t.1", 22, 28, "Bild " + gdocs.OBJECT), str(where("upto")))
check("threads sharing a second with one entry: the entry goes to the one whose text it holds",
      where("open2") == ("text", "t.1", 29, 33, "hier"), str(where("open2")))
check("and the resolved one and the deleted one keep out of it",
      (placed["done2"]["placement"], placed["gone2"]["placement"]) == ("resolved", "deleted"),
      f"{placed['done2']['placement']} {placed['gone2']['placement']}")
twins = gcomments.match_threads(
    [thread("a", "2026-09-14T11:00:00.100Z", "Gleich"),
     thread("b", "2026-09-14T11:00:00.600Z", "Gleich")],
    [{"created": "2026-09-14T11:00:00", "text": "Gleich"}])
check("an entry two threads would both take goes to neither", twins == [None, None], str(twins))
check("a range opened between paragraphs starts at the next one",
      where("between") == ("text", "t.0", 1, 6, "Titel"), str(where("between")))
check("where a paragraph reads differently in the export, the range is the paragraph, said so",
      where("chip") == ("paragraph", "t.0", 55, 67, "dazu"), str(where("chip")))
lonely = gdocs.place_doc_comments(
    [thread("x", "2026-09-14T11:00:00.000Z", "Hier")],
    (["Anders als hier"], [{"created": "2026-09-14T11:00:00", "text": "Hier", "reply": False,
                            "start": (0, 0), "end": (0, 6)}], set()),
    {"title": "T", "body": {"content": [runs(1, "Völlig fremd\n")]}})[0]
check("a paragraph paired only by position, without the text in it, is not offered as the place",
      (lonely["placement"], lonely["start"]) == ("unknown", None), str(lonely)[:80])
empty = gdocs.place_doc_comments(
    [thread("x", "2026-09-14T11:00:00.000Z", "Hier")],
    (["Anders als hier"], [{"created": "2026-09-14T11:00:00", "text": "Hier", "reply": False,
                            "start": (0, 3), "end": (0, 3)}], set()),
    {"title": "T", "body": {"content": [runs(1, "Völlig fremd\n")]}})[0]
check("nor is one for a range holding no text, which every paragraph would contain",
      empty["placement"] == "unknown", str(empty)[:80])
# one empty paragraph closing a tab, two opening the next: aligned as one list, the next
# tab's pair lines up with this tab's paragraphs just as well
edges = gdocs.place_doc_comments(
    [thread("x", "2026-09-14T11:00:00.000Z", "Leer")],
    gdocs.docx_comments(docx_export(
        [("title", "A"), ["Eins"], [], ("title", "B"), [("S", "0"), ("E", "0")], [], ["Zwei"]],
        [("0", "2026-09-14T11:00:00", "Leer", "10", None)])),
    {"title": "T", "tabs": [
        {"tabProperties": {"tabId": "t.0", "title": "A"}, "documentTab": {"body": {"content": [
            runs(1, "Eins\n"), runs(6, "\n")]}}},
        {"tabProperties": {"tabId": "t.1", "title": "B"}, "documentTab": {"body": {"content": [
            runs(1, "\n"), runs(2, "\n"), runs(3, "Zwei\n")]}}}]})[0]
check("an empty paragraph opening a tab is placed in that tab, not the one before",
      (edges["placement"], edges["tab_id"], edges["start"]) == ("text", "t.1", 1),
      f"{edges['placement']} {edges['tab_id']} {edges['start']}")

# a link the export spells otherwise, beside a paragraph reading like the document's version of it
LINKED = {"title": "T", "tabs": [{"tabProperties": {"tabId": "t.0", "title": "T"},
                                  "documentTab": {"body": {"content": [
    runs(1, "Siehe ", {"richLink": {"richLinkProperties": {"title": "Plan"}}}, "\n"),
    runs(9, "Siehe Plan\n"), runs(20, "Ende\n")]}}}]}
on_plan = [{"created": "2026-09-14T11:00:00", "text": "Leer", "reply": False,
            "start": (1, 6), "end": (1, 10)}]
twin = gdocs.place_doc_comments([thread("x", "2026-09-14T11:00:00.000Z", "Leer")],
                                (["Siehe Plan.docx", "Siehe Plan", "Ende"], on_plan, set()),
                                LINKED)[0]
check("paragraphs of a stretch as long on both sides pair by position, not by a lookalike",
      (twin["placement"], twin["start"], twin["end"]) == ("text", 15, 19),
      f"{twin['placement']} {twin['start']} {twin['end']}")
twin = gdocs.place_doc_comments([thread("x", "2026-09-14T11:00:00.000Z", "Leer")],
                                (["Siehe Plan.docx", "Siehe Plan", "Extra", "Ende"], on_plan,
                                 set()), LINKED)[0]
check("in a stretch of different lengths, a text there twice is no pairing at all",
      twin["placement"] == "unknown", f"{twin['placement']} {twin['start']}")

# the document's own Title paragraph carrying the next tab's name, where the counts disagree
split = gdocs.tab_stretches(["A", "a1", "B", "Fazit", "", "", "B", "Fazit ", "neu"], {0, 2, 6},
                            gdocs.paragraphs_of({"tabs": [
    {"tabProperties": {"tabId": "t.0", "title": "A"}, "documentTab": {"body": {"content": [
        runs(1, "a1\n"), runs(4, "B\n"), runs(6, "Fazit\n"), runs(12, "\n")]}}},
    {"tabProperties": {"tabId": "t.1", "title": "B"}, "documentTab": {"body": {"content": [
        runs(1, "Fazit\n"), runs(7, "neu\n")]}}}]}))
check("a tab's title is taken nearest where the counts put it, not at the first lookalike",
      split == [([1, 2, 3, 4, 5], [0, 1, 2, 3]), ([7, 8], [4, 5])], str(split))
check("a document of one tab pairs without the title paragraph its export opens with",
      gdocs.tab_stretches(["Tab 1", "x"], {0}, [("t.0", "Tab 1", 1, 3, [("x", 1, True)])])
      == [([1], [0])])

# a resolved thread is never in the export, so it takes no entry from an open one with its text
same = gdocs.place_doc_comments(
    [thread("open", "2026-09-14T11:00:00.100Z", "A"),
     thread("gone", "2026-09-14T11:00:00.200Z", "G"),
     thread("res", "2026-09-14T11:00:00.300Z", "A", resolved=True)],
    (["x"], [{"created": "2026-09-14T11:00:00", "text": "A", "reply": False,
              "start": (0, 0), "end": (0, 1)}], set()),
    {"title": "T", "body": {"content": [runs(1, "x\n")]}})
check("a resolved thread with an open one's text leaves it its place, and the deleted one "
      "its absence", [c["placement"] for c in same] == ["text", "deleted", "resolved"],
      str([c["placement"] for c in same]))
check("an open thread the export lacks had its text deleted",
      where("deleted") == ("deleted", None, None, None, None), str(where("deleted")))
check("a resolved thread, which no export carries, claims no place",
      placed["done"]["placement"] == "resolved" and placed["done"]["start"] is None)
check("a comment without an anchor claims no text", placed["file"]["placement"] == "none")
check("a reply in the export is never taken for a thread made in its second",
      placed["late"]["placement"] == "deleted", placed["late"]["placement"])
check("a thread tied within a second with no text to decide it is unknown, not deleted",
      placed["blur"]["placement"] == "unknown", placed["blur"]["placement"])
check("the quote is unescaped, and replies ride along",
      placed["deleted"]["quoted"] == "gelöscht"
      and [r["id"] for r in placed["emoji"]["replies"]] == ["r1"])
kinds = [(name, kw.get("mimeType")) for name, kw in calls]
check("a listing costs the threads, one DOCX export and one read of the document",
      kinds == [("drive.comments.list", None), ("drive.files.export", gdocs.DOCX), ("get", None)],
      str(kinds))
check("and the read takes every tab's content", calls[-1][1].get("includeTabsContent") is True)
check("a listing writes nothing", not any(sent), str(sent))

out, err = run(["gdocs", "comments", "ID"], doc=COMMENT_DOC)
check("the listing names the tab the way --tab takes it, then the range",
      "'Eins' (--tab=t.0) [19, 27)  open  id=emoji" in out, out[:60])
check("it prints the text a thread is on, and what it was made on where that differs",
      "on 'Kosten', made on 'Kosten alt'" in out and "on 'Wachstum'\n" in out,
      out[out.find("id=cell"):][:80])
check("a deleted thread says so, and what it was made on",
      "(its text was deleted)  open  id=deleted" in out and "made on 'gelöscht'" in out,
      out[out.find("id=deleted") - 25:][:80])
check("resolved threads are left out by default, and counted on stderr",
      "id=done" not in out and "2 resolved thread(s) not shown" in err, err.strip())

DRIVE[0] = FakeDrive(found=[t for t in DOC_THREADS if t["id"] in ("done", "file")])
out, _ = run(["gdocs", "comments", "--all", "ID"], doc=COMMENT_DOC)
check("with no open anchored thread there is nothing to export or read",
      [name for name, _ in calls] == ["drive.comments.list"]
      and "a resolved thread leaves the export" in out, str([name for name, _ in calls]))

DRIVE[0] = FakeDrive(broken=True)
out, err = run(["gdocs", "comments", "--json", "--all", "ID"], doc=COMMENT_DOC)
placed = {c["id"]: c for c in json.loads(out)}
check("a failed export still lists the threads, the open ones unknown rather than deleted",
      LAST_EXIT is None and placed["deleted"]["placement"] == "unknown"
      and placed["done"]["placement"] == "resolved", str(LAST_EXIT))
check("and says why", "DOCX export could not be read" in err and "too large" in err,
      err.strip()[:80])

DRIVE[0] = FakeDrive()
out, _ = run(["gdocs", "reply", "ID", "emoji", "Passt", "so"])
check("reply answers the thread on this document through Drive",
      (calls[-1][0], calls[-1][1]["fileId"], calls[-1][1]["body"]) ==
      ("drive.replies.create", "ID", {"content": "Passt so"})
      and out.strip() == "replied to comment emoji", str(calls[-1]))
out, _ = run(["gdocs", "resolve", "ID", "done"])
check("resolving a resolved thread sends nothing, as in gsheets",
      "already resolved – nothing sent" in out
      and not any(n == "drive.replies.create" for n, _ in calls), out.strip())
for argv, why in ((["gdocs", "comments", "--tab=t.1", "ID"], "--tab= does not apply to `comments`"),
                  (["gdocs", "reply", "--tab=t.1", "ID", "emoji", "x"],
                   "--tab= does not apply to `reply`"),
                  (["gdocs", "get", "--json", "ID"], "--json applies to `comments`"),
                  (["gdocs", "get", "--all", "ID"], "--all applies to `comments`"),
                  (["gdocs", "resolve", "ID"], "expected 1 argument")):
    run(argv)
    check(f"{' '.join(argv[1:3])} is refused: {why}",
          isinstance(LAST_EXIT, str) and why in LAST_EXIT, str(LAST_EXIT)[:60])

DRIVE[0] = None
run(["gdocs", "get", "ID"])
check("a Docs command never builds the Drive service", LAST_EXIT is None, str(LAST_EXIT)[:60])
kind, got = direct(lambda: gdocs.Document("ID", service=Docs(COMMENT_DOC),
                                          drive=FakeDrive()).comments())
check("Document.comments() returns the placed threads in-process",
      kind == "ok" and {c["id"]: c["placement"] for c in got}["span"] == "text", f"{kind}")
gcomments.drive_api = REAL_DRIVE_API

# Generated documents, each written twice – as the Docs API describes it and as the DOCX export
# would – with the true tab and index of every export position known, so a thread placed a tab or
# an index off fails even where its text is empty. Empty paragraphs are frequent on purpose: runs
# of them at tab edges are what paired paragraphs across a boundary once.
import random  # noqa: E402

ALPHABET = ["a", "Umsatz", " ", "ü", "🚀", "\t", chr(11), "𝔘"]


def generated(rng):
    # known: per export paragraph, None or (tab, index at each offset)
    tabs, body, known = [], [], []
    for t in range(rng.randint(1, 3)):
        tab, content, at = f"t.{t}", [], 1
        body.append(("title", f"Tab {t}"))
        known.append(None)
        for _ in range(rng.randint(1, 6)):
            in_cell = rng.random() < 0.15
            if in_cell:
                table_at, at = at, at + 3          # the table, its row and its cell open
            tokens = [] if rng.random() < 0.35 else [
                rng.choice(ALPHABET) if rng.random() < 0.85 else rng.choice(["img", "page"])
                for _ in range(rng.randint(1, 8))]
            parts, xml, idx = [], [], [at]     # idx: the index at each character offset
            for token in tokens:
                if token in ("img", "page"):
                    parts.append({"inlineObjectElement" if token == "img" else "pageBreak": {}})
                    xml.append((token,))
                    idx.append(idx[-1] + 1)
                else:
                    parts.append(token)
                    xml.append({"\t": ("tab",), chr(11): ("br",)}.get(token, token))
                    idx += [idx[-1] + gdocs.utf16_len(token[:n + 1]) for n in range(len(token))]
            para = runs(at, *parts, "\n")
            at = para["endIndex"]
            if in_cell:
                at += 1
                content.append({"startIndex": table_at, "endIndex": at, "table": {"tableRows": [
                    {"tableCells": [{"content": [para]}]}]}})
            else:
                content.append(para)
            body.append(("cell", xml) if in_cell else xml)
            known.append((tab, idx))
        tabs.append({"tabProperties": {"tabId": tab, "title": f"Tab {t}"},
                     "documentTab": {"body": {"content": content}}})
    return {"title": "Generiert", "tabs": tabs}, body, known


def with_marks(item, marks):
    """A body item with ("S"|"E", id) parts put in at their character offsets."""
    xml = item[1] if isinstance(item, tuple) else item
    out, offset = [], 0
    for part in xml:
        out += marks.get(offset, [])
        width = len(part) if isinstance(part, str) else 1
        if isinstance(part, str) and width > 1:
            for n, ch in enumerate(part):
                out += marks.get(offset + n, []) if n else []
                out.append(ch)
        else:
            out.append(part)
        offset += width
    out += marks.get(offset, [])
    return ("cell", out) if isinstance(item, tuple) else out


misplaced, unplaced, total = [], 0, 0
for seed in range(400):
    rng = random.Random(seed)
    generated_doc, body, known = generated(rng)
    real = [n for n, k in enumerate(known) if k is not None]
    marks, entries, threads, want = {}, [], [], {}
    for cid in range(rng.randint(1, 4)):
        tab = known[rng.choice(real)][0]
        mine = [n for n in real if known[n][0] == tab]
        p0, p1 = (sorted(rng.sample(mine, 2)) if len(mine) > 1 and rng.random() < 0.3
                  else [rng.choice(mine)] * 2)
        o0 = rng.randint(0, len(known[p0][1]) - 1)
        o1 = rng.randint(o0 if p0 == p1 else 0, len(known[p1][1]) - 1)
        marks.setdefault(p0, {}).setdefault(o0, []).append(("S", str(cid)))
        marks.setdefault(p1, {}).setdefault(o1, []).append(("E", str(cid)))
        created = f"2026-09-14T12:{cid:02d}:00"
        entries.append((str(cid), created, f"g{cid}", f"{cid + 100}", None))
        threads.append(thread(f"g{cid}", created + ".000Z", f"g{cid}"))
        want[f"g{cid}"] = (tab, known[p0][1][o0], known[p1][1][o1])
    marked = [item if known[n] is None else with_marks(item, marks.get(n, {}))
              for n, item in enumerate(body)]
    for c in gdocs.place_doc_comments(threads, gdocs.docx_comments(docx_export(marked, entries)),
                                      generated_doc):
        total += 1
        if c["placement"] != "text":
            unplaced += 1
        elif (c["tab_id"], c["start"], c["end"]) != want[c["id"]]:
            misplaced.append((seed, c["id"], (c["tab_id"], c["start"], c["end"]), want[c["id"]]))
check(f"on {total} threads in 400 generated documents, every one lands on the tab and index "
      f"it was put on", not misplaced and not unplaced,
      f"{len(misplaced)} misplaced, {unplaced} unplaced, first {misplaced[:1]}")

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
