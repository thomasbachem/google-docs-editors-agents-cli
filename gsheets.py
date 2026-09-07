#!/usr/bin/env python3
"""Minimal Google Sheets CLI, authorized via auth.py (token.json beside this script).

Usage:
  gsheets whoami                              # which Google account this token belongs to
  gsheets create <title>                      # create new spreadsheet, prints its URL
  gsheets info   <sheet>                      # spreadsheet title, tabs, sheet ids, grid sizes
  gsheets get    <sheet> [A1range ...]        # print values as TSV (default: first tab)
                                              #   --row-numbers prefixes the sheet row
  gsheets json   <sheet> [A1range ...]        # print values as JSON (--row-numbers too)
  gsheets update <sheet> <A1range> <json>     # write cells, e.g. '[["a","b"],["c","d"]]'
  gsheets update-many <sheet> <json>          # write SEVERAL ranges in ONE call – below
  gsheets append <sheet> <A1range> <json>     # append rows after a table
  gsheets clear  <sheet> <A1range ...>        # clear values (keeps formatting)
  gsheets cells  <sheet> <A1range ...>        # read ANY CellData as JSON: links,
                                              #   formats, notes, errors, rich text;
                                              #   --fields=<mask> picks the fields,
                                              #   e.g. --fields=hyperlink
  gsheets objects <sheet> [tab ...]           # sheet-level state: merges, protected
                                              #   and banded ranges, conditional
                                              #   formats, charts, filters, groups,
                                              #   odd row/col sizes (--json for all,
                                              #   --duplicates to unstack them)
  gsheets link   <sheet> <A1range> <json>     # text + hyperlink per cell, downward:
                                              #   '[["Label","https://…"], …]'
  gsheets reset  <sheet> <tab ...>            # clear formatting, notes, validations,
                                              #   rich text and merges over the WHOLE
                                              #   current grid, keeping values
                                              #   (--sizes: row/col sizes + hidden too)
  gsheets delete-rows <sheet> <tab> <from> <to>   # remove rows structurally (1-based, inclusive)
  gsheets insert-rows <sheet> <tab> <after-row ...>  # insert below each row, ONE call
                                              #   (--count=N rows per position)
  gsheets group-rows <sheet> <tab> <from> <to>     # outline rows into a group
  gsheets ungroup-rows <sheet> <tab> <from> <to>   # remove one outline level
  gsheets batch  <sheet> <json>               # raw batchUpdate requests (escape hatch)

<sheet> may be a full docs.google.com URL or a bare spreadsheet id.
--dry-run prints the request a writing command would send, and sends nothing.
Reads still happen, so what it prints is what would really go over the wire.
Its sibling `gdocs` does the same for Google Docs, off the same token.

Without a subprocess – every command is a `Client` method, and a process start
costs ~0.3s, which dominates once a script makes dozens of calls:

    from gsheets import Client
    sheet = Client(url_or_id)
    sheet.update_many({"Tab!A1": [[1]]})
    sheet.objects()

`Client` carries what the commands carry – the locale check below, retries, the
write behaviour described further down – and returns data rather than printing.
It raises ToolError on a refusal instead of exiting, so a guard cannot take its
caller down. `create()` is a module function, since it has no sheet yet.
`api()` still hands out the raw service, now already retrying.

Values are written with USER_ENTERED semantics, i.e. parsed the way typed input
would be IN THE SHEET'S OWN LOCALE. On a de_DE sheet that means:
  • Pass decimals as JSON numbers (0.125), not strings. The string "0.125"
    parses as 125 – the dot is read as a thousands separator – and "0.5"
    stays text, since that grouping is invalid. Both silent. As a string it
    must be written German-style: "0,125".
  • Formulas separate arguments with ';' – "=SUM(B1;C1)". A comma is refused:
    the cell keeps the formula and evaluates to #ERROR! "Formula parse error",
    so nothing looks wrong until something reads the value. Function names
    stay English.
  • NUMBER FORMAT PATTERNS go the other way – they stay US convention whatever
    the locale. On a de_DE sheet the pattern "#,##0.0" displays 1234.5 as
    "1.234,5": comma means thousands and dot means decimal IN THE PATTERN, and
    the sheet renders them the German way round. So values are German, formula
    separators are German, patterns are English – three conventions in one
    call. Watch "0.#" especially: on de_DE it appends a bare separator, so 7
    shows as "7," – and "#,##0.#" is no way around it.
`update`/`append` warn on both checkable halves – a dotted-decimal string, and
a formula separating arguments with ',' – whenever the sheet's locale is not
English. The formula check skips a DECIMAL comma ("=0,035*B91"), since that is
the same German convention the value check demands; a comma after a reference
("=MAX(A1,2)") cannot be a decimal and is still flagged. Both are safety nets,
not guarantees. Dates ("26.05.2023") are exempt: on a de_DE sheet they are
correct, and warning per row would drown the one case that matters. Patterns are
not checked at all, since a pattern is right exactly when it looks wrong.

Links, and how to verify them: `link` writes display text plus a hyperlink.
Reading one back is a trap, because a run covering the whole cell is normalized
on save – `textFormatRuns` comes back EMPTY and the link moves to
`userEnteredFormat.textFormat.link` plus the read-only `hyperlink` field. A run
covering only part of the text stays in `textFormatRuns`, where `hyperlink` is
then null. `cells` reads all three, so it shows the link either way.

Drive smart chips (`chipRuns`/`richLinkProperties`, the hover-preview file chips)
need a Drive scope this tool deliberately does not hold, and fail with
HTTP 403 "The request scopes are not sufficient for reading from Drive".
Use `link` for a plain hyperlink instead.

Several ranges in one call – the difference between one request and N against
a quota that counts calls, not requests:
  • `get`, `json` and `cells` take any number of ranges. With a single range
    they print exactly what they always did. With several, `json` prints the
    API's valueRanges array and `cells` prints one block per range, each tagged
    with its tab and start row/column.
  • `update-many` writes every range in ONE request, taking either
    '[{"range":"Tab!A1","values":[[1]]}, …]' or '{"Tab!A1": [[1]], …}'.
    The locale check covers all of it.
  • `clear` and `reset` take any number of ranges and tabs, likewise one call
    each however many. `reset` resolves every sheetId from one metadata read,
    so a nine-tab workbook costs 2 calls rather than 18.

But the cheapest bundled command is still beaten by no command at all. If you
are already sending a `batchUpdate` – a generator writing its own formatting –
carry the work along as REQUESTS in that batch and spend nothing:
  • Resetting: `reset_requests(*sheet.tab_ids("A", "B"))` returns exactly what
    `reset` would have sent.
  • Clearing values: `{"updateCells": {"range": {"sheetId": id},
    "fields": "userEnteredValue"}}`. Note that `clear` CANNOT be folded in –
    `values.batchClear` is a separate endpoint from `spreadsheets.batchUpdate`,
    which is why the request form is the one to reach for here.
Ready-made commands invite one call each, and against a quota counting calls
that is the expensive habit. The batching above is for callers who have no
batch of their own; a caller who has one should be adding to it.

`objects` covers what lives BESIDE the cells, which `get`/`cells` cannot see at
all. Those objects accumulate: a build that adds a conditional format each run
stacks identical rules invisibly until the sheet crawls, so the summary counts
duplicates – entries identical once their ids are ignored. Row and column sizes
are reported as the modal size plus the exceptions, since a 997-row dump answers
no question anyone has.

It takes no field mask of yours, and that is the safer half: a mask which omits
a field does not error, it silently yields the default. Asking for
`rowGroups(range,depth)` reports every group as un-collapsed whatever the sheet
says – measured, 2 of 40 groups were actually closed. That is precisely why the
mask here asks for whole objects: a dimension group's span AND its `collapsed`
both survive, so the summary answers "what does this outline look like right
now" and `--json` makes saving and restoring collapse state scriptable. Its own
mask does trim some collections to a summary, which is why `--duplicates`
re-reads them whole.

`--duplicates` turns that count into the batchUpdate requests that remove the
stack – printed for `gsheets batch`, never sent, since nothing here can undo a
delete. The first copy of each is kept, so a conditional format's precedence is
unchanged. Their deletes are also why this is not a one-liner: a rule is removed
BY INDEX and every later rule then shifts down, so the requests come highest
index first. Front to back, [A, A, B, B, C] would delete a B and then C, leaving
a duplicate standing and losing the unique rule.

It compares WHOLE objects, on a wider mask than the summary above, because the
summary's mask would invent duplicates: two charts both titled "Umsatz" are
identical under `charts(chartId,spec.title)` and are not the same chart.

Which kinds can stack at all was settled against the live API, not reasoned
about. Conditional formats, protected ranges, charts and developer metadata do,
silently. Merges, banded ranges and filter views cannot – Google refuses the
second one outright, the last with "Dieser Ansichtsname ist bereits vorhanden",
and a filter view only compares equal when its title does. Dimension groups are
excluded for a different reason: deleting one by its range lowers the depth
rather than removing a copy.

`cells` is the general read escape hatch, not a cosmetic one: [fields] takes any
field mask over the CellData resource, so it reaches what `get`/`json` cannot –
including `effectiveValue.errorValue`, the only way to tell a real #N/A from a
cell whose text merely reads "#N/A". The mask travels to `spreadsheets.get`,
which is rooted at the Spreadsheet, so `--fields=hyperlink` is rooted here into
`sheets.data.rowData.values(hyperlink)` rather than refused – a mask that
already starts at a spreadsheet field is passed through untouched.

`insert-rows` takes SEVERAL positions and sorts them descending itself, which is
the one thing worth knowing about it: an insert shifts every row below it, so
positions applied front to back are all wrong after the first. State them against
the grid as it stands now and let the call sort itself out – `insert-rows S Tab 5
12 20` is one request per position in one call, not three calls with arithmetic
between them. Rows are 1-based and inclusive here, matching `delete-rows` rather
than the API's own 0-based `insertDimension`; new rows inherit from the row BELOW.

`group-rows`/`ungroup-rows` are the same 1-based span. Grouping inside a group
nests one level deeper, and ungrouping LOWERS the depth rather than deleting, so
the two are inverses only at the top level.

Neither touches the collapse state, and one measured surprise follows from that:
UNGROUPING DOES NOT CLEAR THE COLLAPSE FLAG. The sheet keeps it against the
range, so re-grouping the same span brings the group back collapsed – with a
control, a never-grouped range comes back expanded, and expanding BEFORE
ungrouping makes the later re-group expanded too. So expand first if the group
may come back, or the flag outlives the group that carried it.

With the two apart, the outline cross-checks itself: where nothing else hides
rows, the collapsed spans should account for exactly the rows on the `hidden`
line – 23 spans summing to 36 rows, on the sheet that prompted the split. Not an
invariant, since a row can be hidden by hand, but a cheap check on your own work
that a single conflated line could not support at all.

Read the collapse trap narrowly: it is the only known way to be surprised here. Opening or
shutting a group – through the outline toggles or `updateDimensionGroup` –
persists exactly as it should, observed holding for half a day on a live sheet
until a person deliberately changed it. Outline state is not generally unreliable,
and treating it as such buys a save/restore dance in code that never needed one.

Setting it is a `batch` job rather than a flag here, because `updateDimensionGroup`
identifies a group by range AND depth, and depth is only knowable from a read –
a guessed depth is refused outright ("There is no group at dimensionGroup.depth 1
that spans exactly …"), which is at least loud. `objects --json` already prints
each group as the exact `dimensionGroup` payload that request wants, so saving
and restoring the whole outline is one read and one write, however many groups:

    saved = sheet.objects("Tab")["Tab"]["rowGroups"]      # 1 call
    sheet.batch([{"updateDimensionGroup": {"dimensionGroup": g,
                                           "fields": "collapsed"}} for g in saved])
Per-group commands would cost a call each against a quota that counts calls,
which is why this stays a batch.

`get`/`json` take `--row-numbers`, because every structural edit
(`insertDimension`, `delete-rows`, the A1 range of the write itself) is stated in
row numbers that a bare TSV dump does not carry. The numbers come from the range
the API echoes back, not from counting output lines, so an offset range numbers
correctly and a bare tab name resolves first. Measured: a blank row inside the
range comes back as an empty row and holds its position, while blank rows at the
END are dropped, so the numbering never silently slides – it just stops.

Three writes that fail without looking like failures:
  • A write to a cell COVERED BY A MERGE is discarded silently. The call still
    prints "1 cells" and the API still returns updatedCells: 1, yet nothing
    lands – only a merge's top-left cell is writable. Measured, not assumed.
  • A write past the grid is refused ("Range … exceeds grid limits. Max rows:
    N"), never auto-expanded. Add rows with `batch` first.
  • The per-minute quota counts CALLS, not requests, over a sliding 60-second
    window: one `batchUpdate` carrying 1543 requests is a single write. A rate
    estimated from request counts is wrong by orders of magnitude.

A 429 or a transient 5xx needs no retry code of your own: every call here asks
the client library to back off and retry. Only `gsheets.api()` callers have to
request it themselves, with execute(num_retries=3) – its default is no retry,
which is how a one-off 503 becomes a write that simply did not happen.
GTOOLS_RETRIES sets the number of attempts (default 3, 0 disables, capped at
10). The backoff is random and doubles each time, so 3 costs at most ~14s.
A 429 that names a PER-MINUTE quota is a different failure and gets a different
answer: fixed, announced pauses that outlast the sliding 60-second window
(GTOOLS_QUOTA_WAIT, default "20,40"; 0 turns the waiting off). No jittered
backoff setting covers both – see README.md.

GTOOLS_STATS=1 prints what a run spent, per quota, on stderr as it exits:

    gtools: 55 API call(s) – sheets read 30 (peak 24/60s), sheets write 25
    (peak 21/60s); 512 KB sent, largest 414 KB

The peak is the count in the busiest 60 seconds, which is the only figure the
limit compares against – a run making 90 calls over five minutes is nowhere
near it. The largest single payload is the one that decides whether a bundle
needs splitting, against a cap around 2 MB.

Set GTOOLS_STATS to a PATH instead and every process appends there, with all of
them reading the whole file. That is the only form that means anything for a
build which fans out: eleven generators finishing inside 45 seconds each see a
fraction of one window, and it is their sum that trips the limit. The file is
appended to, never truncated – remove it between runs, or the totals keep
counting the run before. In-process the same numbers are on `gauth.calls`
(`counts()`, `peak()`, `records()`) without the env var.
"""

import json
import re
import sys

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gauth import (DryRun, RetryingRequest, ToolError, account, as_int,
                   credentials, http_error_message, load_json, send, split_flags)

# "0.125", "1.234.567" – strings a non-English locale mis-parses (grouped
# integer) or refuses to parse at all (left as text). Never the intended number.
DOTTED_DECIMAL = re.compile(r"^-?\d+(\.\d+)+$")

# "26.05.2023" – a correct de_DE date, not a broken decimal. Warning about these
# would drown the real hazard: a column of dates fires one alarm per row, which
# teaches the reader to ignore all of them. Two small dot-separated groups are
# always a date to Sheets ("1.2.3" -> 01.02.2003), never a number; the mangled
# cases have a single dot or three-digit groups, so they still reach the warning.
DATE_LIKE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,4}$")

# What `cells` reads by default. A link may live in any of three places, so all
# three are included – see the note in the module docstring.
CELL_FIELDS = (
    "sheets.data.rowData.values(formattedValue,userEnteredValue,effectiveValue,"
    "note,hyperlink,textFormatRuns,userEnteredFormat(numberFormat,textFormat,"
    "horizontalAlignment,backgroundColorStyle))"
)

# With several ranges each block must carry its own identity, or two blocks on
# one tab are indistinguishable once flattened into rows.
MULTI_CELL_FIELDS = (CELL_FIELDS.rstrip() +
                     ",sheets.properties.title,sheets.data(startRow,startColumn)")

# A --fields mask reaches spreadsheets.get, which is rooted at the Spreadsheet,
# while the command is called `cells` – so `--fields=hyperlink` is the obvious
# reading and used to fail with a bare "HTTP 400: invalid argument". Anything not
# already rooted is taken as the CellData path it looks like. The block tags come
# along, or a custom mask would silently cost multi-range output its identity.
SPREADSHEET_FIELDS = ("sheets", "properties", "spreadsheetId", "spreadsheetUrl",
                      "namedRanges", "developerMetadata", "dataSources")


def root_cell_mask(fields):
    """A caller's --fields, rooted at the spreadsheet unless it already is."""
    mask = fields.strip()
    if mask.startswith(SPREADSHEET_FIELDS):
        return mask
    return (f"sheets.data.rowData.values({mask}),"
            "sheets.properties.title,sheets.data(startRow,startColumn)")

# Everything that is not a cell. rowMetadata/columnMetadata live inside GridData,
# so this needs includeGridData – the mask is what keeps rowData out of it.
OBJECT_FIELDS = (
    "sheets.properties(sheetId,title),"
    "sheets.merges,"
    "sheets.protectedRanges(protectedRangeId,description,range,warningOnly),"
    "sheets.conditionalFormats,"
    "sheets.bandedRanges(bandedRangeId,range),"
    "sheets.charts(chartId,spec.title),"
    "sheets.filterViews(filterViewId,title,range),"
    "sheets.basicFilter,"
    "sheets.rowGroups,sheets.columnGroups,"
    "sheets.developerMetadata,"
    "sheets.data(rowMetadata(pixelSize,hiddenByUser),"
    "columnMetadata(pixelSize,hiddenByUser))"
)

# Collections listed by `objects`, with the id fields that make two otherwise
# identical entries look different.
OBJECT_KINDS = (
    ("merges", ()),
    ("protectedRanges", ("protectedRangeId",)),
    ("conditionalFormats", ()),
    ("bandedRanges", ("bandedRangeId",)),
    ("charts", ("chartId",)),
    ("filterViews", ("filterViewId",)),
    ("rowGroups", ()),
    ("columnGroups", ()),
    ("developerMetadata", ("metadataId",)),
)

# What `reset` clears: everything on a cell except its value. textFormatRuns is
# not part of userEnteredFormat, so it needs naming separately – though it is
# the one entry here that a value overwrite would have taken anyway.
RESET_FIELDS = "userEnteredFormat,dataValidation,note,textFormatRuns"

# Sheets' own defaults, which is what a reset restores – deliberately not the
# sheet's current modal size, which a generator that set every row to 30px
# would already have moved.
ROW_HEIGHT, COLUMN_WIDTH = 21, 100

# What `--duplicates` compares against. The summary mask above cannot be used
# for it: two charts both titled "Umsatz" are identical under
# charts(chartId,spec.title) and are not the same chart, and deleting one on
# that evidence destroys work. Whole objects only, and no dimension fields,
# which is also what lets this drop includeGridData.
FULL_OBJECT_FIELDS = (
    "sheets.properties(sheetId,title),"
    "sheets.merges,"
    "sheets.protectedRanges,"
    "sheets.conditionalFormats,"
    "sheets.bandedRanges,"
    "sheets.charts,"
    "sheets.filterViews,"
    "sheets.rowGroups,sheets.columnGroups,"
    "sheets.developerMetadata"
)

# How `--duplicates` removes a redundant copy. Every one of these names an id,
# so the request stands alone whatever else rides in the batch – unlike a
# conditional format, which is deleted by index and handled separately. All
# three request shapes were confirmed against the live API, on duplicates made
# for the purpose.
#
# What is absent is absent because Google will not let the duplicate exist:
# a second identical merge cannot coexist, a second banding over a range is
# refused ("no further alternating colors"), and a second filter view of the
# same name is refused too ("Dieser Ansichtsname ist bereits vorhanden") – and
# since a filter view compares equal only when its title matches, an identical
# pair is unreachable. Dimension groups are out for a different reason:
# deleting one by its range lowers the depth rather than removing a copy.
DELETE_REQUEST = {
    "protectedRanges": ("protectedRangeId",
                        lambda v: {"deleteProtectedRange": {"protectedRangeId": v}}),
    "charts": ("chartId",
               lambda v: {"deleteEmbeddedObject": {"objectId": v}}),
    "developerMetadata": ("metadataId",
                          lambda v: {"deleteDeveloperMetadata": {
                              "dataFilter": {"developerMetadataLookup": {"metadataId": v}}}}),
}


def sheet_id(ref):
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", ref)
    return m.group(1) if m else ref


def api():
    return build("sheets", "v4", credentials=credentials(),
                 requestBuilder=RetryingRequest).spreadsheets()


def grid_start(s, ref, a1):
    """Resolve the first cell of an A1 range to (sheetId, rowIndex, columnIndex).

    updateCells takes a start coordinate rather than a range, so only the first
    cell has to be parsed.
    """
    tab, sep, cells = a1.rpartition("!")
    tab = tab.strip().strip("'") if sep else ""
    m = re.match(r"^([A-Za-z]+)(\d+)$", cells.split(":")[0].strip())
    if not m:
        raise ToolError(f"cannot parse a start cell from {a1!r} – "
                        f"expected e.g. \"'Tab'!B2\"")
    col = 0
    for ch in m.group(1).upper():
        col = col * 26 + (ord(ch) - 64)
    tabs = [p["properties"] for p in
            s.get(spreadsheetId=ref, fields="sheets.properties(sheetId,title)").execute()["sheets"]]
    if tab:
        found = next((p for p in tabs if p["title"] == tab), None)
        if found is None:
            raise ToolError(f"no tab named {tab!r} – "
                            f"{', '.join(repr(p['title']) for p in tabs)}")
    else:
        found = tabs[0]
    return found["sheetId"], int(m.group(2)) - 1, col - 1


def col_name(n):
    """0-based column index to letters: 0 -> A, 26 -> AA."""
    name = ""
    while True:
        name = chr(ord("A") + n % 26) + name
        n = n // 26 - 1
        if n < 0:
            return name


def a1(rng):
    """Compact A1 for a GridRange. An absent bound means the whole row or column."""
    if not isinstance(rng, dict):
        return "?"
    r0, r1 = rng.get("startRowIndex"), rng.get("endRowIndex")
    c0, c1 = rng.get("startColumnIndex"), rng.get("endColumnIndex")
    start = (col_name(c0) if c0 is not None else "") + (str(r0 + 1) if r0 is not None else "")
    end = (col_name(c1 - 1) if c1 is not None else "") + (str(r1) if r1 is not None else "")
    if not start and not end:
        return "whole sheet"
    return start if end == start else f"{start}:{end}"


def start_row(a1_range):
    """1-based first row of an A1 range – what row numbering anchors on.

    Read off what the API returned rather than what was asked for, so a bare tab
    name (resolved to "Tab!A1:Z997") and an offset range both number correctly.
    """
    tail = (a1_range or "").rsplit("!", 1)[-1]
    found = re.match(r"\$?[A-Za-z]*\$?(\d+)", tail.split(":")[0])
    return int(found.group(1)) if found else 1


def row_span(start, end, what):
    """A 1-based inclusive row span as the API's 0-based, end-exclusive pair."""
    if start < 1:
        raise ToolError(f"{what}: rows are 1-based, got {start}")
    if end < start:
        raise ToolError(f"{what}: end row {end} is before start row {start}")
    return start - 1, end


def dim_a1(rng):
    """Compact A1 for a DimensionRange – rows as "5:8", columns as "E:H".

    A dimension group carries one of these, NOT a GridRange: its bounds are
    startIndex/endIndex, so a1() finds no bounds at all and reports every group
    in the sheet as "whole sheet". Measured on a real outline of 23 groups.
    """
    lo, hi = rng.get("startIndex"), rng.get("endIndex")
    if lo is None and hi is None:
        return "whole sheet"
    if rng.get("dimension") == "COLUMNS":
        start = col_name(lo) if lo is not None else ""
        end = col_name(hi - 1) if hi is not None else ""
    else:
        start = str(lo + 1) if lo is not None else ""
        end = str(hi) if hi is not None else ""
    return start if end == start else f"{start}:{end}"


def ranges_of(item):
    """The A1 ranges an object covers, however that object spells them."""
    if not isinstance(item, dict):
        return []
    if "ranges" in item:                       # conditionalFormats
        return [a1(r) for r in item["ranges"]]
    if "range" in item:                        # protected, banded, filterView, groups
        rng = item["range"]
        if isinstance(rng, dict) and ("startIndex" in rng or "endIndex" in rng
                                      or "dimension" in rng):
            # On an outlined sheet the collapse state IS the structure, so it
            # belongs in the summary rather than only in --json.
            return [dim_a1(rng) + (" collapsed" if item.get("collapsed") else "")]
        return [a1(rng)]
    if "startRowIndex" in item or "startColumnIndex" in item:   # merges
        return [a1(item)]
    return []


def repeats(items, id_keys):
    """Positions of the entries that repeat an earlier one, ids ignored.

    These are what matters: a generator re-run lays down another identical
    rule, and nothing in the UI makes the stack visible. The FIRST of each body
    is kept, which is what makes removal safe for conditional formats – their
    order is precedence, so the rule that was already winning still wins.
    """
    seen, found = set(), []
    for i, item in enumerate(items):
        body = json.dumps({k: v for k, v in item.items() if k not in id_keys},
                          sort_keys=True, ensure_ascii=False)
        if body in seen:
            found.append(i)
        seen.add(body)
    return found


def reset_requests(*sheet_ids, sizes=False):
    """The requests `reset` sends, for folding into a batch you already have.

    A perfectly bundled command still costs one call; a request costs none.
    Anything already sending a batchUpdate – a generator writing its formatting
    – should carry these along rather than call `reset` beside it.

    Clearing VALUES the same way needs a different request, not this one:
    `values.batchClear` is a separate endpoint and cannot ride in a
    batchUpdate at all. Use {"updateCells": {"range": {"sheetId": id},
    "fields": "userEnteredValue"}} there.
    """
    requests = []
    for gid in sheet_ids:
        requests += [
            {"updateCells": {"range": {"sheetId": gid}, "fields": RESET_FIELDS}},
            {"unmergeCells": {"range": {"sheetId": gid}}},
        ]
        if sizes:
            for dimension, default in (("ROWS", ROW_HEIGHT), ("COLUMNS", COLUMN_WIDTH)):
                requests.append({"updateDimensionProperties": {
                    "range": {"sheetId": gid, "dimension": dimension},
                    "properties": {"pixelSize": default, "hiddenByUser": False},
                    "fields": "pixelSize,hiddenByUser"}})
    return requests


def duplicate_requests(found):
    """batchUpdate requests removing every redundant copy `objects` reported.

    Ordering is the whole difficulty. A conditional format is deleted BY INDEX
    into its sheet's rule list, and each delete shifts every later rule down
    one, so duplicates removed front to back take the wrong rules: on
    [A, A, B, B, C] the ascending pair 1 and 3 deletes a B and then C, leaving
    a duplicate standing and losing the unique rule. Highest index first is
    correct, because one batchUpdate applies its requests in order. Everything
    in DELETE_REQUEST names an id instead and does not care.
    """
    requests = []
    for entry in found.values():
        rules = entry.get("conditionalFormats") or []
        for index in reversed(repeats(rules, ())):
            requests.append({"deleteConditionalFormatRule": {
                "sheetId": entry["sheetId"], "index": index}})
        for kind, (id_key, build_request) in DELETE_REQUEST.items():
            items = entry.get(kind) or []
            for index in repeats(items, (id_key,)):
                # An entry Google returned without its id cannot be addressed by
                # any delete – the CLI's count says how many were left standing
                if items[index].get(id_key) is not None:
                    requests.append(build_request(items[index][id_key]))
    return requests


def dimension_summary(entries):
    """Modal pixel size plus only the rows/columns that deviate or are hidden."""
    sizes = [e.get("pixelSize") for e in entries if e.get("pixelSize") is not None]
    common = max(set(sizes), key=sizes.count) if sizes else None
    odd = []
    for i, e in enumerate(entries):
        note = {}
        if e.get("pixelSize") is not None and e["pixelSize"] != common:
            note["pixelSize"] = e["pixelSize"]
        if e.get("hiddenByUser"):
            note["hiddenByUser"] = True
        if note:
            odd.append({"index": i + 1, **note})
    return common, odd


def decimal_comma(text, i):
    """Whether the comma at text[i] is a decimal point, not an argument separator.

    Needs digits on both sides, and the left-hand ones must be a NUMBER rather
    than the tail of a reference: "0,035" is a decimal, "A1,2" cannot be, since
    no number begins with a letter. That second half is what makes =MAX(A1,2)
    decidable rather than a coin toss.
    """
    if i + 1 >= len(text) or not text[i + 1].isdigit():
        return False
    start = i
    while start > 0 and text[start - 1].isdigit():
        start -= 1
    if start == i:
        return False
    before = text[start - 1] if start else ""
    return not (before.isalpha() or before in "$_")


def comma_separated_formula(text):
    """Whether a formula separates its arguments with ',' rather than ';'.

    Narrower than it first looks, because a de_DE formula legitimately contains
    commas: 0,035 is a decimal, and a decimal comma is the very convention the
    value check above demands of callers. Flagging those had the two halves of
    this warning contradict each other – measured against 7923 real formulas in
    one workbook, 65 false alarms and no true ones.

    So a comma is an argument separator unless it is a decimal point:

        =0,035*B91        0,035 is a number         -> silent
        =IF(A1<=12,0,B1)  ",B1" separates           -> flagged
        =MAX(A1,2)        "A1" is a reference       -> flagged

    One shape stays undecidable, with a number on both sides: =MAX(12,5) could
    be one argument or two, and Sheets cannot tell either. It is read as a
    decimal and passes silently – a missed one shows up as #ERROR! in the cell,
    where a false alarm fires on every build of a CORRECT workbook and teaches
    people to ignore warnings.

    Commas inside quotes are never counted, and BOTH kinds of quote matter: a
    string literal takes "double" ones, a sheet name single ones, and a tab may
    legitimately be called 'Test, Komma' – measured, ='Test, Komma'!A1 returns
    its value quite happily. A doubled quote toggles off and straight back on,
    leaving the scan in the right state either way.
    """
    if not isinstance(text, str) or not text.startswith("="):
        return False
    literal = sheet_name = False
    for i, ch in enumerate(text):
        if ch == '"' and not sheet_name:
            literal = not literal
        elif ch == "'" and not literal:
            sheet_name = not sheet_name
        elif ch == "," and not (literal or sheet_name) and not decimal_comma(text, i):
            return True
    return False


def sample_of(hits):
    """Up to three offenders, with a count for the rest."""
    shown = ", ".join(repr(h) for h in hits[:3])
    return shown + (f" and {len(hits) - 3} more" if len(hits) > 3 else "")


def warn_locale(s, ref, values):
    """Flag what a non-English sheet locale will not read the way it was meant.

    Two hazards, one metadata read: values are parsed in the sheet's locale,
    and so are formulas – but in opposite directions, which is what makes this
    worth warning about rather than merely documenting.
    """
    flat = [v for row in values if isinstance(row, list) for v in row if isinstance(v, str)]
    decimals = [v for v in flat if DOTTED_DECIMAL.match(v) and not DATE_LIKE.match(v)]
    formulas = [v for v in flat if comma_separated_formula(v)]
    if not decimals and not formulas:
        return
    locale = (
        s.get(spreadsheetId=ref, fields="properties.locale")
        .execute().get("properties", {}).get("locale", "")
    )
    if locale.startswith("en"):
        return
    if decimals:
        print(
            f"WARNING: sheet locale is {locale}, and {len(decimals)} value(s) are "
            f"dotted-decimal strings ({sample_of(decimals)}). Neither lands as the number "
            f"meant: the dot reads as a thousands separator (\"0.125\" -> 125) or, when the "
            f"grouping is invalid, the cell stays text (\"0.5\"). Pass JSON numbers, or "
            f"German strings (\"0,125\").",
            file=sys.stderr,
        )
    if formulas:
        print(
            f"WARNING: sheet locale is {locale}, and {len(formulas)} formula(s) separate "
            f"arguments with ',' ({sample_of(formulas)}). Sheets refuses those – measured, "
            f"the cell keeps the formula and evaluates to #ERROR! \"Formula parse error\", "
            f"so nothing looks wrong until something reads the value. Use ';' – "
            f"\"=IF(A1<=12;0;B1)\". Function names stay English.",
            file=sys.stderr,
        )


class Client:
    """The commands as methods, so they can be used without a subprocess.

    Every guard the CLI applies applies here – the locale check, the retry, the
    merge and grid-limit behaviour described above – because the commands are
    these methods with argument parsing and printing wrapped around them.

        from gsheets import Client
        sheet = Client("https://docs.google.com/spreadsheets/d/…/edit")
        sheet.update_many({"Tab!A1": [[1]]})
        sheet.objects()

    Methods return data and raise ToolError on a refusal; nothing here prints
    or exits. Pass dry=True to preview writes instead of sending them, which
    prints the request and raises DryRun, exactly as --dry-run does.
    """

    def __init__(self, sheet, service=None, dry=False):
        self.id = sheet_id(sheet)
        self.service = service or api()
        self.dry = dry

    def info(self):
        """Spreadsheet metadata: title, locale, and every tab's properties."""
        return self.service.get(spreadsheetId=self.id).execute()

    def first_tab(self):
        return self.info()["sheets"][0]["properties"]["title"]

    def read(self, *ranges):
        """Value blocks for any number of ranges, in a single request.

        Always a list of {range, values}, whatever the count – the CLI's
        single-range shortcut is a presentation choice, not this one.
        """
        wanted = list(ranges) or [f"'{self.first_tab()}'"]
        if len(wanted) == 1:
            got = self.service.values().get(spreadsheetId=self.id, range=wanted[0]).execute()
            # The API's own range, not the request's: a bare tab name comes back
            # resolved ("Tab" -> "Tab!A1:Z997"), which is what row numbers anchor on.
            return [{"range": got.get("range", wanted[0]), "values": got.get("values", [])}]
        return (self.service.values().batchGet(spreadsheetId=self.id, ranges=wanted)
                .execute().get("valueRanges", []))

    def rows(self, rng=None):
        """The value grid of one range – the common read."""
        return self.read(*([rng] if rng else []))[0].get("values", [])

    def cells(self, *ranges, fields=None):
        """CellData for any number of ranges – links, formats, notes, errors.

        One range gives the rows flat; several give a block per range, tagged
        with its tab and start, since two blocks on one tab are otherwise
        indistinguishable.
        """
        if not ranges:
            raise ToolError("cells: needs at least one range – without one the whole "
                            "grid comes back")
        mask = (root_cell_mask(fields) if fields else
                (CELL_FIELDS if len(ranges) == 1 else MULTI_CELL_FIELDS))
        meta = self.service.get(spreadsheetId=self.id, ranges=list(ranges),
                                includeGridData=True, fields=mask).execute()
        out = []
        for sheet in meta.get("sheets", []):
            title = sheet.get("properties", {}).get("title", "")
            for block in sheet.get("data", []):
                grid = [row.get("values", []) for row in block.get("rowData", [])]
                if len(ranges) == 1:
                    out.extend(grid)
                else:
                    out.append({"tab": title, "startRow": block.get("startRow", 0),
                                "startColumn": block.get("startColumn", 0), "rows": grid})
        return out

    def objects(self, *tabs, whole=False):
        """What lives beside the cells, per tab.

        No caller-supplied field mask – an omitted field is returned as its
        default rather than refused. See the module docstring.

        `whole` fetches each object complete instead of the summary's shape,
        which is what duplicates() needs to compare them honestly. It carries
        no row or column sizes, so it costs one call either way.
        """
        meta = self.service.get(spreadsheetId=self.id, includeGridData=not whole,
                                fields=FULL_OBJECT_FIELDS if whole else OBJECT_FIELDS).execute()
        wanted = set(tabs)
        found = {}
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            title = props.get("title", "")
            if wanted and title not in wanted:
                continue
            entry = {"sheetId": props.get("sheetId")}
            for kind, _ in OBJECT_KINDS:
                if sheet.get(kind):
                    entry[kind] = sheet[kind]
            if sheet.get("basicFilter"):
                entry["basicFilter"] = sheet["basicFilter"]
            for name, field in (("rows", "rowMetadata"), ("columns", "columnMetadata")):
                entries = []
                for block in sheet.get("data", []):
                    entries.extend(block.get(field, []))
                common, odd = dimension_summary(entries)
                if odd:
                    entry[name] = {"defaultPixelSize": common, "exceptions": odd}
            found[title] = entry
        if wanted - set(found):
            raise ToolError(f"no tab named {', '.join(sorted(wanted - set(found)))} – "
                            f"run `gsheets info` for the tab names")
        return found

    def duplicates(self, *tabs):
        """The batchUpdate requests that would remove every redundant copy.

        Returned, never sent. Deleting a sheet object cannot be undone through
        this tool – there is no Drive scope and no version history here – so
        the decision to apply them stays with the caller: `batch(…)`.
        """
        return duplicate_requests(self.objects(*tabs, whole=True))

    def reset(self, *tabs, sizes=False):
        """Clear the formatting a content overwrite leaves standing.

        Borders, backgrounds, number formats, wrap strategy, notes and data
        validations all survive having their values rewritten, so a generator's
        second run inherits the first one's decoration. Merges go too, and
        those are the dangerous ones: a write to a cell a stale merge covers is
        discarded silently.

        Rich text is the exception, and it cuts the other way. textFormatRuns
        belong to the TEXT, so rewriting a value already takes them – and so
        does the link a whole-cell run normalizes into, measured: after a plain
        `update`, userEnteredFormat.textFormat.link was gone while the
        background on that same cell survived. Clearing rich text here only
        reaches a sheet whose values stay put; where a generator rewrites every
        value it is never the reason to call this.

        One thing it destroys may not be rebuildable: a data validation. Sheets
        renders some dropdowns as chips, and that rendering is not visible
        through the API at all – so nothing here can check it, and a rule put
        back through v4 can come back as the plain arrow. Look before resetting
        a sheet whose dropdowns matter.

        The ranges name only the sheet, which means the WHOLE sheet at its
        CURRENT size. That is the point – computing bounds from a size read
        before the generators ran leaves everything below them untouched.

        Values are kept; `clear` is for those. `sizes` additionally puts row
        heights, column widths and hidden flags back to Sheets' defaults, which
        is off by default because a deliberate layout lives in those too.

        One default is worth knowing before resetting a formatted sheet:
        vertical alignment goes back to BOTTOM, not TOP – measured on
        effectiveFormat, not assumed. Any row taller than its text then has
        that text sitting at the foot of it, which reads as broken rather than
        as reset. Follow with a repeatCell setting verticalAlignment where a
        sheet wraps or has tall rows.

        Any number of tabs, in one read and one write however many – a nine-tab
        workbook reset one tab at a time costs eighteen calls against a budget
        of sixty.

        Already sending a batchUpdate? Then this is a call too many – put
        reset_requests() into that batch instead and spend none.
        """
        if not tabs:
            raise ToolError("reset: needs at least one tab – run `gsheets info` for the names")
        requests = reset_requests(*self.tab_ids(*tabs), sizes=sizes)
        send(self.service.batchUpdate(spreadsheetId=self.id,
                                      body={"requests": requests}), self.dry)
        return len(requests)

    def tab_ids(self, *tabs):
        """sheetIds behind tab titles, from ONE metadata read.

        Resolving them one at a time costs a call apiece against a quota that
        counts calls, which is the whole reason `reset` takes several tabs.
        Unknown names are reported together rather than one run at a time.
        """
        known = {t["properties"]["title"]: t["properties"]["sheetId"]
                 for t in self.info()["sheets"]}
        missing = [t for t in tabs if t not in known]
        if missing:
            raise ToolError(f"no tab named {', '.join(repr(t) for t in missing)} – "
                            f"run `gsheets info` for the tab names")
        return [known[t] for t in tabs]

    def update(self, rng, values):
        return self._write_values("update", rng, values)

    def append(self, rng, values):
        return self._write_values("append", rng, values)

    def _write_values(self, kind, rng, values):
        warn_locale(self.service, self.id, values)
        call = self.service.values().update if kind == "update" else self.service.values().append
        result = send(call(spreadsheetId=self.id, range=rng,
                           valueInputOption="USER_ENTERED", body={"values": values}), self.dry)
        return result.get("updatedCells") or result.get("updates", {}).get("updatedCells", 0)

    def update_many(self, data):
        """Write several ranges in ONE request.

        Takes [{"range":…,"values":…}, …] or {"Tab!A1": [[…]], …}; returns the
        cell count. One request whatever the size – the quota counts calls.
        """
        if isinstance(data, dict):
            data = [{"range": k, "values": v} for k, v in data.items()]
        if not isinstance(data, list) or not all(
                isinstance(d, dict) and "range" in d for d in data):
            raise ToolError('update-many: expected \'[{"range":…,"values":…}, …]\' or '
                            '\'{"Tab!A1": [[…]], …}\' – see `gsheets` for both forms')
        warn_locale(self.service, self.id, [row for d in data for row in d.get("values", [])])
        result = send(self.service.values().batchUpdate(spreadsheetId=self.id, body={
            "valueInputOption": "USER_ENTERED", "data": data,
        }), self.dry)
        return result.get("totalUpdatedCells", 0)

    def clear(self, *ranges):
        """Blank the values in any number of ranges, in ONE request.

        Always batchClear, even for one range: the single-range call buys
        nothing and the quota counts calls, so nine tabs cleared one at a time
        would be nine of them. Formatting is untouched – `reset` is for that.
        """
        if not ranges:
            raise ToolError("clear: needs at least one range")
        result = send(self.service.values().batchClear(
            spreadsheetId=self.id, body={"ranges": list(ranges)}), self.dry)
        return result.get("clearedRanges", [])

    def link(self, rng, pairs):
        """Display text plus a hyperlink per cell, downward from rng."""
        gid, row0, col0 = grid_start(self.service, self.id, rng)
        rows = []
        for pair in pairs:
            text = str(pair[0]) if pair else ""
            uri = pair[1] if len(pair) > 1 else ""
            rows.append({"values": [{
                "userEnteredValue": {"stringValue": text},
                # A run covering the whole cell is what Sheets normalizes into
                # userEnteredFormat.textFormat.link on save – see `cells`.
                "textFormatRuns": [{"startIndex": 0, "format": {"link": {"uri": uri}}}]
                if uri else [],
            }]})
        send(self.service.batchUpdate(spreadsheetId=self.id, body={"requests": [{"updateCells": {
            "start": {"sheetId": gid, "rowIndex": row0, "columnIndex": col0},
            "rows": rows,
            "fields": "userEnteredValue,textFormatRuns",
        }}]}), self.dry)
        return len(rows)

    def delete_rows(self, tab, start, end):
        """Remove rows structurally, 1-based and inclusive – this shrinks the grid."""
        lo, hi = row_span(start, end, "delete-rows")
        send(self.service.batchUpdate(spreadsheetId=self.id, body={
            "requests": [{"deleteDimension": {"range": {
                "sheetId": self.tab_ids(tab)[0], "dimension": "ROWS",
                "startIndex": lo, "endIndex": hi,
            }}}]}), self.dry)

    def insert_rows(self, tab, *after, count=1):
        """Insert `count` rows below each 1-based row, in ONE call.

        Every position is stated against the grid as it is NOW, and the requests
        go out highest row first. That ordering is the whole point: an insert
        shifts everything below it, so positions applied front to back are all
        wrong after the first – the same shape of trap as deleting conditional
        formats by index. Duplicates collapse, since two inserts after the same
        row are indistinguishable from one insert of twice the height.

        New rows inherit from the row BELOW, which is the API's default and the
        right one here: a detail row added under a category header should look
        like its neighbouring detail rows, not like the header. `batch` with
        `inheritFromBefore` for the other choice.
        """
        if not after:
            raise ToolError("insert-rows: needs at least one row to insert after")
        if count < 1:
            raise ToolError(f"insert-rows: count must be at least 1, got {count}")
        for row in after:
            if row < 1:
                raise ToolError(f"insert-rows: rows are 1-based, got {row}")
        gid = self.tab_ids(tab)[0]
        return self.batch([{"insertDimension": {"range": {
            "sheetId": gid, "dimension": "ROWS",
            "startIndex": row, "endIndex": row + count,  # "after row N" IS index N
        }}} for row in sorted(set(after), reverse=True)])

    def group_rows(self, tab, start, end):
        """Outline these rows into a group, 1-based and inclusive.

        Grouping a span already inside a group nests it one level deeper rather
        than failing, which is how an outline is built up.
        """
        lo, hi = row_span(start, end, "group-rows")
        return self.batch([{"addDimensionGroup": {"range": {
            "sheetId": self.tab_ids(tab)[0], "dimension": "ROWS",
            "startIndex": lo, "endIndex": hi}}}])

    def ungroup_rows(self, tab, start, end):
        """Remove one outline level over these rows, 1-based and inclusive.

        Deleting by range LOWERS the depth rather than removing a copy, so a
        doubly-nested span needs this twice. It is also why `--duplicates`
        leaves dimension groups alone.
        """
        lo, hi = row_span(start, end, "ungroup-rows")
        return self.batch([{"deleteDimensionGroup": {"range": {
            "sheetId": self.tab_ids(tab)[0], "dimension": "ROWS",
            "startIndex": lo, "endIndex": hi}}}])

    def batch(self, requests):
        """Raw spreadsheets.batchUpdate – the escape hatch, retries included."""
        result = send(self.service.batchUpdate(
            spreadsheetId=self.id, body={"requests": requests}), self.dry)
        return result.get("replies", [])


def create(title, service=None, dry=False):
    """Create a spreadsheet in the token account's Drive, returning its URL."""
    result = send((service or api()).create(body={"properties": {"title": title}}), dry)
    return result["spreadsheetUrl"]


def dispatch():
    values, flags, argv = split_flags(sys.argv, ("fields", "count"),
                                      ("--dry-run", "--json", "--duplicates", "--sizes",
                                       "--row-numbers"))
    if values["fields"] == "":
        raise ToolError("--fields= is empty – drop the option to use the default mask")
    fields, dry = values["fields"] or "", "--dry-run" in flags
    count = as_int(values["count"], "--count") if values["count"] is not None else 1
    if len(argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)

    cmd = argv[1]
    if "--json" in flags and cmd != "objects":
        raise ToolError("--json applies to `objects` – for values, `json` is its own command")
    if "--sizes" in flags and cmd != "reset":
        raise ToolError("--sizes applies to `reset`")
    if "--row-numbers" in flags and cmd not in ("get", "json"):
        raise ToolError("--row-numbers applies to `get` and `json`")
    if values["count"] is not None and cmd != "insert-rows":
        raise ToolError("--count= applies to `insert-rows`")
    if values["fields"] is not None and cmd != "cells":
        raise ToolError("--fields= applies to `cells`")
    if "--duplicates" in flags:
        if cmd != "objects":
            raise ToolError("--duplicates applies to `objects`, which is what finds them")
        if "--json" in flags:
            raise ToolError("--duplicates prints delete requests and --json the objects "
                            "themselves – pick one")

    if cmd == "whoami":
        print(account() or "unknown – re-run auth.py to record the account")
        return

    if len(argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)

    args = argv[3:]
    needed = {"cells": 1, "link": 2, "update": 2, "append": 2, "clear": 1,
              "delete-rows": 3, "batch": 1, "update-many": 1, "reset": 1,
              "insert-rows": 2, "group-rows": 3, "ungroup-rows": 3}.get(cmd, 0)
    if len(args) < needed:
        raise ToolError(f"{cmd}: expected {needed} argument(s) after <sheet>, got "
                        f"{len(args)} – run `gsheets` for usage")

    if cmd == "create":
        print(create(argv[2], dry=dry))
        # stderr, so `gsheets create … | read url` cannot pick up this line
        print(f"created in the Drive of {account() or 'unknown account – see gsheets whoami'}",
              file=sys.stderr)
        return

    sheet = Client(argv[2], dry=dry)

    if cmd == "info":
        meta = sheet.info()
        props = meta["properties"]
        print(props["title"])
        for tab in meta["sheets"]:
            p = tab["properties"]
            g = p["gridProperties"]
            print(f"  {p['title']!r}  sheetId={p['sheetId']}  {g['rowCount']}x{g['columnCount']}")
        print(f"  locale={props.get('locale', '?')}  read as {account() or 'unknown account'}")

    elif cmd in ("get", "json"):
        blocks = sheet.read(*args)
        single = (len(args) <= 1)
        numbered = "--row-numbers" in flags
        if cmd == "json":
            if numbered:
                blocks = [dict(b, values=[{"row": start_row(b.get("range", "")) + i,
                                           "values": row}
                                          for i, row in enumerate(b.get("values", []))])
                          for b in blocks]
            print(json.dumps(blocks[0].get("values", []) if single else blocks,
                             ensure_ascii=False))
        else:
            for block in blocks:
                if not single:
                    print(f"=== {block.get('range', '?')} ===")
                first = start_row(block.get("range", ""))
                for i, row in enumerate(block.get("values", [])):
                    line = "\t".join(str(c) for c in row)
                    print(f"{first + i}\t{line}" if numbered else line)

    elif cmd == "update-many":
        data = load_json(args[0], "update-many")
        cells = sheet.update_many(data)
        # len() is the range count for both accepted forms
        count = len(data) if isinstance(data, (list, dict)) else 0
        print(f"update-many: {cells} cells across {count} range(s), in 1 request")

    elif cmd in ("update", "append"):
        cells = getattr(sheet, cmd)(args[0], load_json(args[1], cmd))
        print(f"{cmd}: {cells} cells")

    elif cmd == "cells":
        print(json.dumps(sheet.cells(*args, fields=fields or None), ensure_ascii=False))

    elif cmd == "objects":
        found = sheet.objects(*args, whole="--duplicates" in flags)
        if "--duplicates" in flags:
            requests = duplicate_requests(found)
            # stdout stays pure JSON, so the note cannot end up inside `batch`
            print(json.dumps(requests, ensure_ascii=False))
            total = sum(len(repeats(entry.get(kind) or [], id_keys))
                        for entry in found.values() for kind, id_keys in OBJECT_KINDS)
            if not total:
                print("no duplicates", file=sys.stderr)
            else:
                left = (f", of {total} found – the rest are duplicates no delete request "
                        f"can express (merges, bandings, dimension groups)"
                        if total > len(requests) else "")
                print(f"{len(requests)} duplicate(s) removable{left}. Nothing was sent – "
                      f"apply with:\n"
                      f"  gsheets batch {argv[2]} \"$(gsheets objects {argv[2]} --duplicates)\"",
                      file=sys.stderr)
        elif "--json" in flags:
            print(json.dumps(found, ensure_ascii=False))
        elif not found:
            print("no tabs matched")
        else:
            for title, entry in found.items():
                print(f"{title!r}  sheetId={entry['sheetId']}")
                if not {k: v for k, v in entry.items() if k != "sheetId"}:
                    print("  (nothing beside the cells)")
                for kind, id_keys in OBJECT_KINDS:
                    items = entry.get(kind)
                    if not items:
                        continue
                    spots = [r for i in items for r in ranges_of(i)]
                    shown = ", ".join(spots[:3]) + (" …" if len(spots) > 3 else "")
                    dupes = len(repeats(items, id_keys))
                    tail = f"  ({dupes} duplicate(s))" if dupes else ""
                    print(f"  {kind:<19}{len(items):>3}  {shown}{tail}")
                if entry.get("basicFilter"):
                    print(f"  {'basicFilter':<19}{1:>3}  "
                          f"{a1(entry['basicFilter'].get('range', {}))}")
                for name in ("rows", "columns"):
                    dim = entry.get(name)
                    if not dim:
                        continue
                    # A collapsed group HIDES; wrapped text RESIZES. Merged into one
                    # list of indices the line answers neither question, and looks
                    # most like the one it answers least – "what is not visible".
                    # Worse, its label asserted a cause: on a real 65-row sheet all 36
                    # entries were hidden and NOT ONE was resized, yet the line read
                    # "off default 21px: 3, 4, 5 …" and was believed. It misled in both
                    # directions, inventing resizes as readily as it buried hides.
                    # The --json shape already keeps them apart, so only this splits.
                    odd = dim["exceptions"]
                    for label, group in (
                            ("resized", [e for e in odd if "pixelSize" in e]),
                            ("hidden", [e for e in odd if e.get("hiddenByUser")])):
                        if not group:
                            continue
                        idx = ", ".join(str(e["index"]) for e in group[:8])
                        more = " …" if len(group) > 8 else ""
                        size = (f"default {dim['defaultPixelSize']}px: "
                                if label == "resized" else "")
                        print(f"  {name + ' ' + label:<19}{len(group):>3}  {size}{idx}{more}")

    elif cmd == "link":
        written = sheet.link(args[0], load_json(args[1], "link"))
        print(f"link: {written} cell(s) from {args[0]}")

    elif cmd == "clear":
        cleared = sheet.clear(*args)
        batched = ", in 1 call" if len(args) > 1 else ""
        print(f"cleared {', '.join(cleared or args)}{batched}")

    elif cmd == "reset":
        sizes = "--sizes" in flags
        sheet.reset(*args, sizes=sizes)
        extra = ", sizes and hidden flags" if sizes else ""
        batched = ", in 1 call" if len(args) > 1 else ""
        print(f"reset {', '.join(repr(t) for t in args)}: formatting, notes, validations, "
              f"rich text, merges{extra} – values kept{batched}")

    elif cmd == "delete-rows":
        start, end = as_int(args[1], "delete-rows"), as_int(args[2], "delete-rows")
        sheet.delete_rows(args[0], start, end)
        print(f"deleted rows {start}-{end} of {args[0]!r}")

    elif cmd == "insert-rows":
        after = [as_int(a, "insert-rows") for a in args[1:]]
        sheet.insert_rows(args[0], *after, count=count)
        where = ", ".join(str(r) for r in sorted(set(after)))
        print(f"inserted {count} row(s) after row {where} of {args[0]!r}, in 1 request")

    elif cmd in ("group-rows", "ungroup-rows"):
        start, end = as_int(args[1], cmd), as_int(args[2], cmd)
        getattr(sheet, cmd.replace("-", "_"))(args[0], start, end)
        print(f"{'grouped' if cmd == 'group-rows' else 'ungrouped'} rows "
              f"{start}-{end} of {args[0]!r}")

    elif cmd == "batch":
        print(json.dumps(sheet.batch(load_json(args[0], "batch")), ensure_ascii=False))

    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


def main():
    try:
        dispatch()
    except DryRun:
        pass          # send() already printed the request it did not make
    except ToolError as err:
        sys.exit(str(err))
    except HttpError as err:
        sys.exit(http_error_message(err))


if __name__ == "__main__":
    main()
