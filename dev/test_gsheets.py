#!/usr/bin/env python3
"""Drive gsheets.py's command branches against a fake Sheets API.

No network and no Google account: the token is a throwaway file, so this also
covers `create`, which would otherwise leave an undeletable spreadsheet behind
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
import gsheets  # noqa: E402

calls = []
fails = []
retries = []
sent = []

# run() swaps gsheets.api for a fake, so the real one is kept to probe it
REAL_API = gsheets.api


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


class Values:
    def update(self, **kw):
        calls.append(("values.update", kw))
        return Exec({"updatedCells": 2}, body=kw.get("body"))

    def append(self, **kw):
        calls.append(("values.append", kw))
        return Exec({"updates": {"updatedCells": 2}}, body=kw.get("body"))

    def get(self, **kw):
        calls.append(("values.get", kw))
        return Exec({"range": "Tab1!A5:C9", "values": [["x"], [], ["y"]]})

    def batchClear(self, **kw):
        calls.append(("values.batchClear", kw))
        return Exec({"clearedRanges": kw["body"]["ranges"]}, body=kw.get("body"))

    def batchGet(self, **kw):
        calls.append(("values.batchGet", kw))
        return Exec({"valueRanges": [{"range": r, "values": [["v", r]]}
                                     for r in kw.get("ranges", [])]})

    def batchUpdate(self, **kw):
        calls.append(("values.batchUpdate", kw))
        cells = sum(len(row) for d in kw["body"]["data"] for row in d.get("values", []))
        return Exec({"totalUpdatedCells": cells}, body=kw.get("body"))


def rule(kind):
    return {"ranges": [{"startRowIndex": 1, "endRowIndex": 10,
                        "startColumnIndex": 0, "endColumnIndex": 1}],
            "booleanRule": {"condition": {"type": kind}}}


A, B, C = rule("NUMBER_GREATER"), rule("NUMBER_LESS"), rule("TEXT_CONTAINS")
PROTECT = {"description": "P", "range": {"startRowIndex": 0, "endRowIndex": 5,
                                         "startColumnIndex": 2, "endColumnIndex": 4}}
# A DimensionGroup's range is a DimensionRange (startIndex/endIndex), never a
# GridRange. The fixture used to spell it the wrong way, agreeing with the bug.
GROUP = {"range": {"dimension": "ROWS", "startIndex": 0, "endIndex": 4}, "depth": 1}

# Objects stacked as a generator re-run leaves them – both kinds verified to
# stack for real on a live sheet. The rules are in the arrangement that makes
# delete order matter: the repeats sit at 1 and 3, so removing them front to
# back would take a B and then C. The duplicate rowGroup stands in for a
# duplicate no delete request can express. One tab is empty.
OBJECT_DOC = {"sheets": [
    {"properties": {"sheetId": 0, "title": "Tab1"},
     "merges": [{"startRowIndex": 0, "endRowIndex": 1,
                 "startColumnIndex": 0, "endColumnIndex": 2}],
     "protectedRanges": [{"protectedRangeId": 7, **PROTECT},
                         {"protectedRangeId": 8, **PROTECT}],
     "conditionalFormats": [A, A, B, B, C],
     "bandedRanges": [{"bandedRangeId": 1, "range": {"startRowIndex": 0, "endRowIndex": 9}}],
     "rowGroups": [GROUP, GROUP],
     "data": [{"rowMetadata": [{"pixelSize": 21}, {"pixelSize": 21},
                               {"pixelSize": 45}, {"pixelSize": 21},
                               {"pixelSize": 21, "hiddenByUser": True}],
               "columnMetadata": [{"pixelSize": 100}, {"pixelSize": 100}]}]},
    {"properties": {"sheetId": 1, "title": "Leer"}},
]}


class Sheets:
    def __init__(self, locale="de_DE"):
        self.locale = locale

    def create(self, body=None):
        calls.append(("create", body))
        return Exec({"spreadsheetUrl": "https://docs.google.com/spreadsheets/d/FAKEID/edit"},
                    body=body)

    def get(self, **kw):
        calls.append(("get", kw))
        if "protectedRanges" in (kw.get("fields") or ""):
            return Exec(OBJECT_DOC)
        if kw.get("includeGridData"):
            cell = {"formattedValue": "Beleg",
                    "hyperlink": "https://example.com/a",
                    "userEnteredFormat": {"textFormat": {
                        "link": {"uri": "https://example.com/a"}}},
                    "textFormatRuns": None}
            return Exec({"sheets": [{
                "properties": {"title": "Tab1"},
                "data": [{"startRow": i * 10, "startColumn": 0,
                          "rowData": [{"values": [cell]}]}
                         for i in range(len(kw.get("ranges") or ["A1"]))],
            }]})
        return Exec({
            "properties": {"title": "T", "locale": self.locale},
            "sheets": [{"properties": {"title": "Tab1", "sheetId": 7,
                                       "gridProperties": {"rowCount": 10, "columnCount": 3}}},
                       {"properties": {"title": "Tab2", "sheetId": 8,
                                       "gridProperties": {"rowCount": 10, "columnCount": 3}}}],
        })

    def values(self):
        return Values()

    def batchUpdate(self, **kw):
        calls.append(("batchUpdate", kw))
        return Exec({"replies": [{}]}, body=kw.get("body"))


def use_token(email="tester@example.com", source="id_token"):
    """Point the shared auth module at a throwaway token, so tests never read
    the real one – gsheets reads the account through gauth."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"email": email, "email_source": source}, f)
    gauth.TOKEN = path
    fd, spath = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump({"installed": {"project_id": "test-project-42"}}, f)
    gauth.SECRETS = spath
    return path


LAST_EXIT = None


def run(argv, locale="de_DE"):
    global LAST_EXIT
    LAST_EXIT = None
    calls.clear()
    retries.clear()
    sent.clear()
    gsheets.api = lambda: Sheets(locale)
    out, err = io.StringIO(), io.StringIO()
    sys.argv = argv
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            gsheets.main()
        except SystemExit as e:
            LAST_EXIT = e.code
    return out.getvalue(), err.getvalue()


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{('  ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


print("=== gsheets.py branches (fake API) ===")
token_path = use_token()

out, _ = run(["gsheets", "whoami"])
check("whoami prints the account", out.splitlines()[0] == "tester@example.com",
      out.splitlines()[0])
check("and the project, so nothing opens the credential file to learn it",
      "project: test-project-42" in out, out.splitlines()[-1])

out, err = run(["gsheets", "create", "My Sheet"])
check("create prints the URL", "spreadsheets/d/FAKEID" in out)
# a caller doing `url=$(gsheets create …)` must get the URL and nothing else
check("create keeps stdout to the URL alone",
      out.strip() == "https://docs.google.com/spreadsheets/d/FAKEID/edit", repr(out))
check("create names the account on stderr",
      "created in the Drive of tester@example.com" in err, repr(err))
check("create passes the title verbatim", calls[0][1] == {"properties": {"title": "My Sheet"}})

out, _ = run(["gsheets", "info", "ID"])
check("info prints the locale", "locale=de_DE" in out)
check("info prints tab and sheetId", "'Tab1'" in out and "sheetId=7" in out)

out, err = run(["gsheets", "update", "ID", "A1", '[["0.125"]]'])
check("warns on a dotted decimal (de_DE)", "WARNING" in err and "0.125" in err)
check("still writes after warning", any(c[0] == "values.update" for c in calls))

_, err = run(["gsheets", "update", "ID", "A1", '[["0.125"]]'], locale="en_US")
check("no warning on en_US", "WARNING" not in err)

_, err = run(["gsheets", "update", "ID", "A1", '[[0.125, "text"]]'])
check("no locale lookup when values are safe",
      not any(c[0] == "get" for c in calls), f"calls={[c[0] for c in calls]}")

_, err = run(["gsheets", "append", "ID", "A1", '[["1.234"]]'])
check("append is guarded too", "WARNING" in err)

run(["gsheets", "delete-rows", "ID", "Tab1", "3", "4"])
rng = calls[-1][1]["body"]["requests"][0]["deleteDimension"]["range"]
check("delete-rows -> 0-based, end-exclusive",
      rng["startIndex"] == 2 and rng["endIndex"] == 4, str(rng))
check("delete-rows resolves the tab name", rng["sheetId"] == 7)

# reset: what a content overwrite leaves standing
out, _ = run(["gsheets", "reset", "ID", "Tab1"])
requests = calls[-1][1]["body"]["requests"]
mask = requests[0]["updateCells"]["fields"]
# Bounds read before a generator ran would miss everything below them; naming
# only the sheet means the whole grid at whatever size it is now
check("reset covers the whole current grid, not computed bounds",
      requests[0]["updateCells"]["range"] == {"sheetId": 7},
      str(requests[0]["updateCells"]["range"]))
check("reset clears each thing that survives an overwrite",
      all(f in mask for f in ("userEnteredFormat", "dataValidation", "note",
                              "textFormatRuns")), mask)
# a write under a stale merge is discarded silently, so these matter most
check("reset drops merges too", "unmergeCells" in requests[1], str(requests[1]))
# updateCells carrying no rows resets the masked fields and writes no content
check("reset keeps values", "rows" not in requests[0]["updateCells"],
      str(requests[0]["updateCells"].keys()))
check("reset leaves sizes alone by default",
      not any("updateDimensionProperties" in r for r in requests), str(len(requests)))
check("reset says what it did", "values kept" in out, out.strip())

out, _ = run(["gsheets", "reset", "--sizes", "ID", "Tab1"])
sized = [r["updateDimensionProperties"] for r in calls[-1][1]["body"]["requests"]
         if "updateDimensionProperties" in r]
check("--sizes restores rows and columns", len(sized) == 2, str(len(sized)))
check("--sizes unhides as well as resizes",
      all(d["properties"]["hiddenByUser"] is False for d in sized), str(sized))
# Sheets' own defaults, not the sheet's modal size, which a generator has moved
check("--sizes uses the API defaults, not what the sheet drifted to",
      {d["properties"]["pixelSize"] for d in sized} == {21, 100}, str(sized))
check("--sizes is announced", "sizes and hidden flags" in out, out.strip())

run(["gsheets", "reset", "ID", "Nichtda"])
check("reset refuses an unknown tab",
      isinstance(LAST_EXIT, str) and "no tab named 'Nichtda'" in LAST_EXIT, str(LAST_EXIT)[:50])

# clear and reset batch, because the quota counts calls and not ranges
out, _ = run(["gsheets", "clear", "ID", "A!A1:B2"])
check("clear always goes through batchClear", calls[-1][0] == "values.batchClear",
      calls[-1][0])

out, _ = run(["gsheets", "clear", "ID", "A!A1", "B!B2", "C!C3"])
check("clear takes several ranges in one call",
      calls[-1][1]["body"]["ranges"] == ["A!A1", "B!B2", "C!C3"], str(calls[-1][1]["body"]))
check("clear reports each range it cleared", "A!A1, B!B2, C!C3" in out, out.strip())
check("clear says it batched", "in 1 call" in out, out.strip())

out, _ = run(["gsheets", "reset", "ID", "Tab1", "Tab2"])
requests = calls[-1][1]["body"]["requests"]
check("reset covers every tab named",
      {r["updateCells"]["range"]["sheetId"] for r in requests if "updateCells" in r} == {7, 8},
      str(sorted(r["updateCells"]["range"]["sheetId"] for r in requests if "updateCells" in r)))
# one tab at a time would be two reads and two writes, for the same work
check("reset resolves every tab from one metadata read",
      len([c for c in calls if c[0] == "get"]) == 1, str([c[0] for c in calls]))
check("reset sends one write however many tabs",
      len([c for c in calls if c[0] == "batchUpdate"]) == 1, str([c[0] for c in calls]))

# the requests on their own, for a caller who already has a batchUpdate to fill
built = gsheets.reset_requests(7, 8)
check("reset_requests covers each sheet given",
      [r["updateCells"]["range"]["sheetId"] for r in built if "updateCells" in r] == [7, 8],
      str(built)[:60])
check("reset_requests is exactly what reset sends",
      built == calls[-1][1]["body"]["requests"], str(len(built)))
check("reset_requests carries sizes when asked",
      len(gsheets.reset_requests(7, sizes=True)) == 4,
      str(len(gsheets.reset_requests(7, sizes=True))))
sent.clear()
gsheets.reset_requests(7, 8, sizes=True)
check("reset_requests sends nothing itself", not sent, str(sent))

run(["gsheets", "reset", "ID", "Tab1", "Nichtda"])
check("an unknown tab fails the whole reset, naming only the unknown one",
      isinstance(LAST_EXIT, str) and "'Nichtda'" in LAST_EXIT and "Tab1" not in LAST_EXIT,
      str(LAST_EXIT)[:60])

run(["gsheets", "clear", "--sizes", "ID", "A1"])
check("--sizes is refused off `reset`",
      isinstance(LAST_EXIT, str) and "applies to `reset`" in LAST_EXIT, str(LAST_EXIT)[:50])

out, _ = run(["gsheets", "reset", "--dry-run", "ID", "Tab1"])
check("reset previews instead of sending", not any(sent) and "unmergeCells" in out,
      out.strip()[:60])

# a German date is a correct de_DE value, not a broken decimal
_, err = run(["gsheets", "update", "ID", "A1", '[["26.05.2023"],["04.10.2023"]]'])
check("German dates do not warn", "WARNING" not in err, err.strip()[:60])

# ...but a real decimal mixed in with dates still does, and is counted alone
_, err = run(["gsheets", "update", "ID", "A1",
              '[["26.05.2023"],["04.10.2023"],["0.125"]]'])
check("real decimal among dates still warns", "WARNING" in err and "'0.125'" in err)
check("dates excluded from the count", "1 value(s)" in err, err[err.find("and"):][:24])

# formulas: the other half of the same locale trap, and it points the other way
_, err = run(["gsheets", "update", "ID", "A1", '[["=IF(B7<=12,0,C7)"]]'])
check("a comma-separated formula warns", "WARNING" in err and "formula(s)" in err,
      err.strip()[:60])
check("the warning names the failure it causes", "#ERROR!" in err and ";" in err)

# a de_DE decimal INSIDE a formula is the same convention the value check
# demands – flagging it had the two halves contradict each other, 65 times
# across 7923 real formulas
for formula, flagged, why in (
        ("=0,035*B91", False, "a decimal comma"),
        ("=RUNDEN(1,5;0)", False, "a decimal inside a call"),
        ("=A1*0,5", False, "a decimal after an operator"),
        ("=IF(A1<=12,0,B1)", True, "an argument separator before a reference"),
        ("=SUM(A1, B1)", True, "a space is not a digit"),
        ("=MAX(A1,2)", True, "a reference cannot precede a decimal point"),
        ("=SUM(A1:B2,3)", True, "a range then a numeric argument"),
        ("=SUM($A$1,2)", True, "an absolute reference"),
        ("=LOG10(A1,2)", True, "a function name ending in a digit"),
        ("=MAX(12,5)", False, "undecidable, and read as a decimal on purpose"),
        # a sheet name is single-quoted, and a tab may legitimately hold a comma
        ("='Test, Komma'!A1", False, "a comma in a single-quoted sheet name"),
        ("=SUM('Plan, alt'!A1:A9)", False, "the same inside a call"),
        ("=SUM('Plan, alt'!A1,B1)", True, "a real separator after a quoted sheet name"),
        ("=CONCATENATE(\"it's\";A1,B1)", True, "an apostrophe inside a literal")):
    check(f"{why}: {formula}", gsheets.comma_separated_formula(formula) is flagged,
          formula)

_, err = run(["gsheets", "update", "ID", "A1", '[["=IF(B7<=12;0;C7)"]]'])
check("a semicolon formula is silent", "WARNING" not in err, err.strip()[:40])

# a comma inside a string literal is data, not an argument separator
_, err = run(["gsheets", "update", "ID", "A1", '[["=CONCATENATE(\"a,b\";C1)"]]'])
check("a comma inside a literal is not flagged", "WARNING" not in err, err.strip()[:60])

_, err = run(["gsheets", "update", "ID", "A1", '[["Text, mit Komma"]]'])
check("plain text with a comma is not a formula", "WARNING" not in err, err.strip()[:40])

_, err = run(["gsheets", "update", "ID", "A1", '[["=SUM(A1,B1)"],["0.125"]]'])
check("both halves warn independently", err.count("WARNING") == 2, str(err.count("WARNING")))

_, err = run(["gsheets", "update", "ID", "A1", '[["=SUM(A1,B1)"]]'], locale="en_US")
check("an en_US sheet is left alone", "WARNING" not in err, err.strip()[:40])

# one metadata read covers both checks, against a quota that counts calls
run(["gsheets", "update", "ID", "A1", '[["=SUM(A1,B1)"],["0.125"]]'])
check("both checks share one locale read",
      len([c for c in calls if c[0] == "get"]) == 1, str([c[0] for c in calls]))

# long lists are capped so the message stays readable
_, err = run(["gsheets", "update", "ID", "A1",
              '[["0.125"],["0.250"],["0.375"],["0.500"],["1.234"]]'])
check("warning caps the sample", "and 2 more" in err)

# `cells` exposes metadata that get/json cannot reach
out, _ = run(["gsheets", "cells", "ID", "C1:C2"])
data = json.loads(out)
check("cells returns CellData rows", isinstance(data, list) and data and isinstance(data[0], list))
check("cells surfaces the link", data[0][0]["hyperlink"] == "https://example.com/a")
check("cells asks for grid data", any(c[0] == "get" and c[1].get("includeGridData") for c in calls))

# several ranges must collapse into a single request – the quota counts calls
out, _ = run(["gsheets", "json", "ID", "A!A1:B2", "B!C1:D2"])
data = json.loads(out)
check("json with several ranges returns valueRanges",
      isinstance(data, list) and len(data) == 2 and data[0]["range"] == "A!A1:B2", str(data)[:60])
check("several ranges cost one batchGet",
      [c[0] for c in calls] == ["values.batchGet"], str([c[0] for c in calls]))

out, _ = run(["gsheets", "json", "ID", "A!A1:B2"])
check("one range keeps the bare value grid", json.loads(out) == [["x"], [], ["y"]], out.strip())
check("one range still uses values.get", any(c[0] == "values.get" for c in calls))

out, _ = run(["gsheets", "get", "ID", "A!A1", "B!A1"])
check("get labels each block with its range",
      "=== A!A1 ===" in out and "=== B!A1 ===" in out, repr(out[:40]))

out, _ = run(["gsheets", "get", "ID", "A!A1"])
check("one range prints bare TSV", "===" not in out, repr(out))

# update-many: every range in one write
out, _ = run(["gsheets", "update-many", "ID",
              '[{"range":"A!A1","values":[["x"]]},{"range":"B!B2","values":[["y"]]}]'])
kw = calls[-1][1]
check("update-many sends one values.batchUpdate", calls[-1][0] == "values.batchUpdate")
check("update-many keeps USER_ENTERED", kw["body"]["valueInputOption"] == "USER_ENTERED")
check("update-many carries every range", len(kw["body"]["data"]) == 2)
check("update-many says it was one request", "in 1 request" in out, out.strip())

run(["gsheets", "update-many", "ID", '{"A!A1": [["x"]], "B!B2": [["y"]]}'])
check("update-many accepts the object form",
      {d["range"] for d in calls[-1][1]["body"]["data"]} == {"A!A1", "B!B2"})

_, err = run(["gsheets", "update-many", "ID", '[{"range":"A!A1","values":[["0.125"]]}]'])
check("update-many is locale-checked too", "WARNING" in err and "0.125" in err)

# cells across several ranges must stay attributable
out, _ = run(["gsheets", "cells", "ID", "A!A1:B2", "B!C1:D2"])
data = json.loads(out)
check("cells tags each block with tab and start",
      isinstance(data[0], dict) and data[0]["tab"] == "Tab1" and data[1]["startRow"] == 10,
      str(data)[:70])
check("cells asks for identifying fields when multi-range",
      "sheets.properties.title" in calls[-1][1]["fields"])

out, _ = run(["gsheets", "cells", "--fields=sheets.data.rowData.values(note)", "ID", "A!A1"])
check("--fields overrides the mask",
      calls[-1][1]["fields"] == "sheets.data.rowData.values(note)", calls[-1][1]["fields"])
check("--fields is not mistaken for a range",
      calls[-1][1]["ranges"] == ["A!A1"], str(calls[-1][1]["ranges"]))

# `objects` reaches what lives beside the cells, and must make stacking visible
out, _ = run(["gsheets", "objects", "ID"])
check("objects lists every tab", "'Tab1'" in out and "'Leer'" in out, repr(out[:40]))
check("objects renders merge ranges as A1", "A1:B1" in out, out[:40])
check("objects counts identical rules as duplicates", "2 duplicate(s)" in out, out[:40])
# Row 3 is merely resized, row 5 is hidden by a collapse. Reported as one list of
# indices the line answered neither "what is odd" nor "what is not visible".
resized_line = next(l for l in out.splitlines() if "rows resized" in l)
hidden_line = next(l for l in out.splitlines() if "rows hidden" in l)
check("objects reports only deviating rows",
      resized_line.endswith("default 21px: 3"), resized_line)
check("objects keeps hidden rows apart from merely resized ones",
      hidden_line.split()[-1] == "5" and "3" not in hidden_line.split()[-1], hidden_line)
check("objects says when a tab holds nothing", "nothing beside the cells" in out, out[:40])
check("objects never asks for cell contents",
      "rowData" not in calls[-1][1]["fields"], calls[-1][1]["fields"][:40])

out, _ = run(["gsheets", "objects", "--json", "ID"])
data = json.loads(out)
check("objects --json keeps the structures",
      data["Tab1"]["protectedRanges"][0]["protectedRangeId"] == 7)
check("objects --json summarises dimensions",
      data["Tab1"]["rows"]["defaultPixelSize"] == 21
      and [e["index"] for e in data["Tab1"]["rows"]["exceptions"]] == [3, 5],
      str(data["Tab1"].get("rows")))
check("objects omits empty collections", "charts" not in data["Tab1"], str(data["Tab1"].keys()))
check("objects keeps an empty tab in the output", data["Leer"] == {"sheetId": 1})

out, _ = run(["gsheets", "objects", "ID", "Tab1"])
check("objects filters by tab", "'Tab1'" in out and "'Leer'" not in out, repr(out[:40]))

run(["gsheets", "objects", "ID", "Nichtda"])
check("objects rejects an unknown tab",
      isinstance(LAST_EXIT, str) and "no tab named Nichtda" in LAST_EXIT, str(LAST_EXIT)[:50])

# --duplicates: the diagnosis turned into the fix, and the order it needs
out, err = run(["gsheets", "objects", "--duplicates", "ID"])
requests = json.loads(out)
rules = [r["deleteConditionalFormatRule"] for r in requests if "deleteConditionalFormatRule" in r]
check("--duplicates emits a delete per redundant copy", len(requests) == 3, str(requests))
# Front to back these would delete a B and then C: the wrong rules, silently
check("conditional formats are deleted highest index first",
      [r["index"] for r in rules] == [3, 1], str(rules))
check("each names the sheet its indices belong to", all(r["sheetId"] == 0 for r in rules))
check("an id-based duplicate is deleted by id, not position",
      {"deleteProtectedRange": {"protectedRangeId": 8}} in requests, str(requests))
check("the first copy is the one kept",
      not any(r.get("deleteProtectedRange", {}).get("protectedRangeId") == 7
              for r in requests))
check("a lone object is left alone",
      not any("deleteBanding" in r for r in requests), str(requests))
# The summary mask would call two charts sharing a title the same chart
check("--duplicates compares whole objects, not the summary's mask",
      "spec.title" not in calls[-1][1]["fields"] and "sheets.charts," in calls[-1][1]["fields"],
      calls[-1][1]["fields"][:60])
check("comparing whole objects still costs one call",
      len([c for c in calls if c[0] == "get"]) == 1 and not calls[-1][1]["includeGridData"],
      str(len(calls)))
check("--duplicates sends nothing", not any(sent[-1:]), str(sent[-1:]))
check("the note says how to apply it, on stderr",
      "gsheets batch ID" in err and "Nothing was sent" in err, err.strip()[:60])
check("a duplicate no delete expresses is reported, not silently dropped",
      "of 4 found" in err, err.strip()[:80])

out, _ = run(["gsheets", "objects", "--duplicates", "ID", "Leer"])
check("a clean tab emits an empty batch", json.loads(out) == [], out.strip())

run(["gsheets", "objects", "--duplicates", "--json", "ID"])
check("--duplicates and --json are refused together",
      isinstance(LAST_EXIT, str) and "pick one" in LAST_EXIT, str(LAST_EXIT)[:50])

run(["gsheets", "get", "--duplicates", "ID"])
check("--duplicates is refused off `objects`",
      isinstance(LAST_EXIT, str) and "applies to `objects`" in LAST_EXIT, str(LAST_EXIT)[:50])

check("a1 renders a bounded range",
      gsheets.a1({"startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 2}) == "A1:B1")
check("a1 names a range with no bounds", gsheets.a1({}) == "whole sheet")
check("column letters go past Z", gsheets.col_name(26) == "AA", gsheets.col_name(26))

# a hand-typed argument is normal input, so a bad one must not raise
run(["gsheets", "batch", "ID", "{oops"])
check("malformed JSON is one line",
      isinstance(LAST_EXIT, str) and "invalid JSON" in LAST_EXIT, str(LAST_EXIT)[:60])
run(["gsheets", "delete-rows", "ID", "Tab1", "x", "4"])
check("a non-numeric row is one line",
      isinstance(LAST_EXIT, str) and "whole number" in LAST_EXIT, str(LAST_EXIT)[:60])

# a swallowed flag is the worst failure here: the write goes through silently
run(["gsheets", "update", "--dryrun", "ID", "A1", '[["x"]]'])
check("a mistyped --dry-run is refused",
      isinstance(LAST_EXIT, str) and "unknown option: --dryrun" in LAST_EXIT,
      str(LAST_EXIT)[:50])
check("and nothing was written", not any(sent), str(sent))

run(["gsheets", "cells", "--fields=", "ID", "A1"])
check("an empty --fields= is refused rather than defaulted",
      isinstance(LAST_EXIT, str) and "is empty" in LAST_EXIT, str(LAST_EXIT)[:40])

run(["gsheets", "json", "--json", "ID", "A1"])
check("--json is refused where it would do nothing",
      isinstance(LAST_EXIT, str) and "applies to `objects`" in LAST_EXIT, str(LAST_EXIT)[:50])

for payload in ('[["Tab!A1", [[1]]]]', '"text"', '[{"values":[[1]]}]'):
    run(["gsheets", "update-many", "ID", payload])
    check(f"update-many refuses {payload[:18]}",
          isinstance(LAST_EXIT, str) and "expected" in LAST_EXIT, str(LAST_EXIT)[:40])
    check("update-many sent nothing when refused", not any(sent), str(sent))

out, _ = run(["gsheets", "update-many", "--dry-run", "ID",
              '[{"range":"A!A1","values":[["x"]]}]'])
check("update-many honours --dry-run",
      json.loads(out)["body"]["data"][0]["range"] == "A!A1" and not any(sent), out[:60])

# --dry-run must show the real request and send nothing
out, _ = run(["gsheets", "update", "--dry-run", "ID", "A1", '[["x"]]'])
shown = json.loads(out)
check("dry-run prints the request it would send",
      shown["dry-run"] is True and shown["body"] == {"values": [["x"]]}, out[:60])
check("dry-run sends no write", not any(sent), str(sent))

# reads still happen, which is what makes the preview trustworthy
_, err = run(["gsheets", "update", "--dry-run", "ID", "A1", '[["0.125"]]'])
check("dry-run still reads, so the locale check fires", "WARNING" in err, err[:40])
check("dry-run wrote nothing even after reading", not any(sent), str(sent))

out, _ = run(["gsheets", "create", "--dry-run", "Neu"])
check("dry-run create leaves no spreadsheet behind",
      json.loads(out)["body"] == {"properties": {"title": "Neu"}} and not any(sent), out[:60])
check("dry-run create prints no URL", "FAKEID" not in out, out[:40])

out, _ = run(["gsheets", "delete-rows", "--dry-run", "ID", "Tab1", "3", "4"])
check("dry-run covers structural writes too",
      json.loads(out)["body"]["requests"][0]["deleteDimension"]["range"]["startIndex"] == 2
      and not any(sent), out[:60])

out, _ = run(["gsheets", "update", "ID", "A1", '[["x"]]'])
check("without --dry-run the write really goes", any(sent), str(sent))
check("and the normal success line comes back", "update: 2 cells" in out, out.strip())

# `link` must write text + run and leave userEnteredFormat alone
run(["gsheets", "link", "ID", "'Tab1'!B2",
     '[["Label","https://example.com/x"],["Zwei","https://example.com/y"]]'])
req = calls[-1][1]["body"]["requests"][0]["updateCells"]
check("link starts at the parsed cell",
      req["start"] == {"sheetId": 7, "rowIndex": 1, "columnIndex": 1}, str(req["start"]))
check("link writes one cell per pair", len(req["rows"]) == 2)
check("link sets a whole-cell run",
      req["rows"][0]["values"][0]["textFormatRuns"][0]["format"]["link"]["uri"]
      == "https://example.com/x")
check("link touches only value and runs", req["fields"] == "userEnteredValue,textFormatRuns")

# an empty URL clears the run rather than writing a bogus link
run(["gsheets", "link", "ID", "A1", '[["Nur Text",""]]'])
req = calls[-1][1]["body"]["requests"][0]["updateCells"]
check("link with no URL clears the run", req["rows"][0]["values"][0]["textFormatRuns"] == [])

# two short dot-separated groups are a date to Sheets ("1.2.3" -> 01.02.2003),
# so warning about them would be noise just like a full date
_, err = run(["gsheets", "update", "ID", "A1", '[["1.2.3"],["9.9.9"],["31.12.99"]]'])
check("short dates do not warn", "WARNING" not in err, err.strip()[:60])

# ...while the shapes that really do mangle are still caught
_, err = run(["gsheets", "update", "ID", "A1", '[["1.234.567"],["0.125"]]'])
check("three-digit groups still warn", "WARNING" in err and "2 value(s)" in err)

# missing arguments must explain themselves, not raise IndexError
for cmd, need in (("cells", 1), ("link", 2), ("update", 2), ("clear", 1),
                  ("delete-rows", 3), ("batch", 1), ("update-many", 1), ("reset", 1),
                  ("insert-rows", 2), ("group-rows", 3), ("ungroup-rows", 3)):
    run(["gsheets", cmd, "ID"])
    check(f"{cmd} reports missing arguments",
          isinstance(LAST_EXIT, str) and f"expected {need} argument" in LAST_EXIT,
          str(LAST_EXIT)[:50])

# an API error must stay one readable line: Google answers some requests with a
# full HTML page, and a raw traceback would dump kilobytes into the caller's output
class Resp:
    status = 403
    reason = "Forbidden"


class Failing(Sheets):
    def __init__(self, err):
        super().__init__()
        self.err = err

    def get(self, **kw):
        raise self.err


def run_failing(argv, err):
    global LAST_EXIT
    LAST_EXIT = None
    gsheets.api = lambda: Failing(err)
    out, errout = io.StringIO(), io.StringIO()
    sys.argv = argv
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errout):
        try:
            gsheets.main()
        except SystemExit as e:
            LAST_EXIT = e.code
    return out.getvalue(), errout.getvalue()


run_failing(["gsheets", "info", "ID"],
            HttpError(Resp(), b'{"error": {"message": "Sheets API has not been used"}}'))
check("an API error exits with one line",
      isinstance(LAST_EXIT, str) and LAST_EXIT == "HTTP 403: Sheets API has not been used",
      str(LAST_EXIT)[:70])

run_failing(["gsheets", "info", "ID"], HttpError(Resp(), b"<!DOCTYPE html><html>" + b"x" * 5000))
check("an HTML error page is summarised, not printed",
      isinstance(LAST_EXIT, str) and len(LAST_EXIT) < 200 and "HTML error page" in LAST_EXIT,
      f"{len(str(LAST_EXIT))} chars")

# a transient 503 must not become a write that silently never happened
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
_real_build, _real_creds = gsheets.build, gsheets.credentials
gsheets.build = lambda *a, **kw: (_built.update(kw)
                                 or type("S", (), {"spreadsheets": lambda s: "svc",
                                                   "documents": lambda s: "svc"})())
gsheets.credentials = lambda: "creds"
REAL_API()
gsheets.build, gsheets.credentials = _real_build, _real_creds
check("api() hands out a retrying service",
      _built.get("requestBuilder") is gauth.RetryingRequest, str(_built.get("requestBuilder")))

run(["gsheets", "update-many", "ID", '[{"range":"A!A1","values":[["x"]]}]'])
check("writes ask the library to retry",
      retries and all(r == gauth.RETRIES for r in retries), str(retries))
check("the retry count is more than zero", gauth.RETRIES > 0, str(gauth.RETRIES))

# an unattested address must say so, so `create` cannot give false reassurance
use_token(email="tester@example.com", source="inferred")
out, _ = run(["gsheets", "whoami"])
check("inferred account is labelled", "inferred" in out, out.splitlines()[0])

os.unlink(token_path)
if os.path.exists(gauth.TOKEN):
    os.unlink(gauth.TOKEN)

# The library is the point of the class: a guard that exits takes the caller's
# run down with it, and driving main() would never catch that.
def direct(fn):
    try:
        return "ok", fn()
    except gauth.ToolError as e:
        return "ToolError", str(e)
    except SystemExit as e:
        return "SystemExit", str(e.code)


print("\n--- Client, called directly ---")
use_token()
client = gsheets.Client("ID", service=Sheets())

for name, call in (
        ("update_many with a bad shape", lambda: client.update_many([["A1", [[1]]]])),
        ("cells with no range at all", lambda: client.cells()),
        ("link with an unparseable range", lambda: client.link("not-a-range", [["a", "b"]])),
        ("delete_rows on an unknown tab", lambda: client.delete_rows("Nichtda", 1, 2)),
        ("objects on an unknown tab", lambda: client.objects("Nichtda"))):
    kind, msg = direct(call)
    check(f"{name} raises instead of exiting", kind == "ToolError", f"{kind}: {str(msg)[:40]}")

check("Client(service=…) uses the service it was given", client.service is not None)
check("update_many returns a plain cell count",
      isinstance(client.update_many({"A!A1": [[1]]}), int))
check("read always returns range-tagged blocks",
      all("range" in b for b in client.read("A!A1", "B!B1")))
check("rows returns just the grid", client.rows("A!A1") == [["x"], [], ["y"]])
check("objects returns a dict keyed by tab", set(client.objects()) == {"Tab1", "Leer"})

# --- row numbers, dimension groups and rooted field masks ---------------------
# All three came from an agent driving the tool from another machine. Two of them
# the fixture above actively hid.

check("start_row reads an offset range", gsheets.start_row("Tab!A5:C9") == 5)
check("start_row handles a bare tab's resolved echo",
      gsheets.start_row("Tab!A1:Z997") == 1)
check("start_row survives a tab name holding a bang",
      gsheets.start_row("'Te!st'!A12:B20") == 12)
check("start_row falls back to 1 with no row in the range",
      gsheets.start_row("Tab!A:C") == 1)

check("dim_a1 spans rows 1-based inclusive",
      gsheets.dim_a1({"dimension": "ROWS", "startIndex": 4, "endIndex": 8}) == "5:8")
check("dim_a1 spans columns as letters",
      gsheets.dim_a1({"dimension": "COLUMNS", "startIndex": 4, "endIndex": 8}) == "E:H")
check("dim_a1 with no bounds is honestly the whole sheet",
      gsheets.dim_a1({"dimension": "ROWS"}) == "whole sheet")
check("ranges_of gives a group its real span, not 'whole sheet'",
      gsheets.ranges_of({"range": {"dimension": "ROWS", "startIndex": 20,
                                   "endIndex": 23}, "depth": 1}) == ["21:23"])
check("ranges_of surfaces the collapse state",
      gsheets.ranges_of({"range": {"dimension": "ROWS", "startIndex": 20,
                                   "endIndex": 23}, "depth": 1,
                         "collapsed": True}) == ["21:23 collapsed"])
check("a GridRange is still read as one",
      gsheets.ranges_of({"range": PROTECT["range"]}) == ["C1:D5"])

out, _ = run(["gsheets", "objects", "SHEET", "Tab1"])
check("the objects summary names the group's rows",
      "1:4" in out and "whole sheet" not in out, out)

check("a bare CellData path is rooted for spreadsheets.get",
      gsheets.root_cell_mask("hyperlink") ==
      "sheets.data.rowData.values(hyperlink),sheets.properties.title,"
      "sheets.data(startRow,startColumn)")
check("an already-rooted mask is passed through untouched",
      gsheets.root_cell_mask("sheets.data.rowData.values.hyperlink") ==
      "sheets.data.rowData.values.hyperlink")
run(["gsheets", "cells", "SHEET", "Tab1!C5", "--fields=hyperlink"])
mask = next(kw["fields"] for name, kw in calls if name == "get")
check("cells --fields=hyperlink reaches the API rooted", 
      mask.startswith("sheets.data.rowData.values(hyperlink)"), mask)
check("the rooted mask keeps the multi-range block tags",
      "sheets.properties.title" in mask and "startRow" in mask, mask)

out, _ = run(["gsheets", "get", "SHEET", "Tab1!A5:C9", "--row-numbers"])
check("get --row-numbers anchors on the API's range, not on line 1",
      out.splitlines() == ["5\tx", "6\t", "7\ty"], str(out.splitlines()))
out, _ = run(["gsheets", "get", "SHEET", "Tab1!A5:C9"])
check("get without the flag is byte-for-byte as before",
      out.splitlines() == ["x", "", "y"], str(out.splitlines()))
out, _ = run(["gsheets", "json", "SHEET", "Tab1!A5:C9", "--row-numbers"])
check("json --row-numbers tags each row",
      json.loads(out)[0] == {"row": 5, "values": ["x"]}, out[:70])
check("json --row-numbers keeps a blank row addressable",
      json.loads(out)[1] == {"row": 6, "values": []}, out[:70])

# --- insert-rows, group-rows, ungroup-rows ------------------------------------
# 1-based inclusive at this surface, like delete-rows, against the API's 0-based
# end-exclusive insertDimension. The descending order is the reason these exist.

out, _ = run(["gsheets", "insert-rows", "ID", "Tab1", "5", "12", "20"])
spans = [r["insertDimension"]["range"] for r in calls[-1][1]["body"]["requests"]]
check("insert-rows sends one request per position, in ONE call",
      len(spans) == 3 and sum(1 for c in calls if c[0] == "batchUpdate") == 1,
      str(len(spans)))
# Front to back, the 12 and the 20 would both land a row too low
check("insert-rows orders the positions highest first",
      [s["startIndex"] for s in spans] == [20, 12, 5],
      str([s["startIndex"] for s in spans]))
check("'after row N' is index N, one row by default",
      spans[-1] == {"sheetId": 7, "dimension": "ROWS", "startIndex": 5, "endIndex": 6},
      str(spans[-1]))
check("insert-rows says what it did against the ORIGINAL grid",
      "after row 5, 12, 20" in out, out.strip())

run(["gsheets", "insert-rows", "ID", "Tab1", "5", "--count=3"])
span = calls[-1][1]["body"]["requests"][0]["insertDimension"]["range"]
check("--count sets the height of each insert",
      (span["startIndex"], span["endIndex"]) == (5, 8), str(span))

run(["gsheets", "insert-rows", "ID", "Tab1", "5", "5", "9"])
check("two inserts after the same row collapse into one",
      len(calls[-1][1]["body"]["requests"]) == 2,
      str(len(calls[-1][1]["body"]["requests"])))

sent.clear()
out, _ = run(["gsheets", "insert-rows", "--dry-run", "ID", "Tab1", "5"])
check("insert-rows honours --dry-run", not any(sent), str(sent))

run(["gsheets", "group-rows", "ID", "Tab1", "5", "8"])
rng = calls[-1][1]["body"]["requests"][0]["addDimensionGroup"]["range"]
check("group-rows takes a 1-based inclusive span",
      (rng["startIndex"], rng["endIndex"]) == (4, 8), str(rng))
run(["gsheets", "ungroup-rows", "ID", "Tab1", "5", "8"])
rng = calls[-1][1]["body"]["requests"][0]["deleteDimensionGroup"]["range"]
check("ungroup-rows takes the same span",
      (rng["startIndex"], rng["endIndex"]) == (4, 8), str(rng))
check("group-rows reports the rows it grouped",
      "grouped rows 5-8" in run(["gsheets", "group-rows", "ID", "Tab1", "5", "8"])[0])

for name, call in (
        ("insert-rows with no position", lambda: client.insert_rows("Tab1")),
        ("insert-rows with a zero count",
         lambda: client.insert_rows("Tab1", 5, count=0)),
        ("insert-rows given a 0-based row", lambda: client.insert_rows("Tab1", 0)),
        ("group-rows given a backwards span",
         lambda: client.group_rows("Tab1", 8, 5)),
        ("group-rows given row 0", lambda: client.group_rows("Tab1", 0, 5))):
    kind, msg = direct(call)
    check(f"{name} raises instead of exiting", kind == "ToolError",
          f"{kind}: {str(msg)[:52]}")

# A flag that applies nowhere is refused, not swallowed – the rule the older options
# already follow. Ignoring --row-numbers on a write tells the caller an option took
# effect when it did not, which is the same failure --dry-run is protected from.
for argv, why in (
        (["gsheets", "update", "ID", "A1", '[["x"]]', "--row-numbers"], "--row-numbers"),
        (["gsheets", "get", "ID", "A!A1", "--count=9"], "--count="),
        (["gsheets", "get", "ID", "A!A1", "--fields=hyperlink"], "--fields=")):
    run(argv)
    check(f"{why} is refused where it does nothing",
          isinstance(LAST_EXIT, str) and why in LAST_EXIT, str(LAST_EXIT)[:60])

run(["gsheets", "json", "ID", "A!A1", "--row-numbers"])
check("--row-numbers still works where it belongs", LAST_EXIT is None, str(LAST_EXIT)[:60])
run(["gsheets", "cells", "ID", "A!A1", "--fields=hyperlink"])
check("--fields still works on cells", LAST_EXIT is None, str(LAST_EXIT)[:60])
run(["gsheets", "insert-rows", "ID", "Tab1", "5", "--count=2"])
check("--count still works on insert-rows", LAST_EXIT is None, str(LAST_EXIT)[:60])

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
