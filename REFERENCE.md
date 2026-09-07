# Reference

The behaviour behind the shorter claims in the usage text, and the two ways to configure it: the
in-process API and the environment variables. Everything here was measured against the live API –
`AGENTS.md` says why that is the rule rather than a boast.

## Quota, measured

The per-minute limit counts **calls**, not requests, over a sliding 60-second
window. That one sentence decides how a generator is written: without it people
send 54 individual updates where one would do.

Two measurements from a real build on 2026-09-04, not from these scratch
surfaces:

- A full build makes 30 reads and 25 writes in 39 s. Since the window is 60 s,
  no more than about one and a half builds ever overlap in it, so the worst
  window sits at **46 of 60** – whether two runs follow each other or three.
- 1543 formatting requests weighed **414 KB in a single `batchUpdate`**: far
  under any payload limit, and one call against the quota. Splitting them into
  bundles buys nothing.

`GTOOLS_STATS=1` makes a run say what it spent, on stderr as it exits:

```
gtools: 55 API call(s) – sheets read 30 (peak 24/60s), sheets write 25 (peak 21/60s); 512 KB sent, largest 414 KB
```

The **peak** is the one to read. It counts the busiest 60 seconds, which is what
the limit compares against – a run making 90 calls over five minutes is nowhere
near it, whatever the total says. Reads and writes are
metered separately by Google and counted separately here, told apart by HTTP
method. The **largest single payload** is likewise the figure that decides
whether a bundle needs splitting, against a cap around 2 MB; the total sent does
not.

One count is one call issued. A request the client library retried inside its
own backoff spends the quota again without passing through the counter, so
after a run that hit 429s the real usage is a little above what it reports.

### Across processes

`Calls` lives in one process, so a build that fans out defeats a per-process
peak: eleven generators finishing inside 45 seconds each see a fraction of one
window, and it is their sum Google meters. Set `GTOOLS_STATS` to a **path**
rather than `1`, and every process appends there while all of them read the
whole file:

```
GTOOLS_STATS=/tmp/build-calls.jsonl ./bauen.py
```

```
gtools: 55 API call(s) in 11 processes – sheets read 30 (peak 24/60s), …
```

That also survives a runner which swallows stderr, since the file outlives the
processes that wrote it. Three properties worth knowing:

- **Append-only, one short line per call.** Concurrent writers therefore keep
  every entry – a read-modify-write (even a `.tmp` + `os.replace`) would keep
  whichever process finished last and silently drop its siblings.
- **Wall clock, not monotonic.** Python only defines a monotonic reading against
  others from the same process; it happens to be boot-relative on macOS and
  Linux, but a shared file should not rest on that.
- **The caller owns the file.** It is never truncated here, because a process
  that truncated it would take its siblings' entries with it – so remove it
  between runs, or the totals keep counting the run before.

**Warnings do not travel this way, deliberately.** `warn_locale` and the quota notices go to stderr,
which presupposes a reader — and a pipeline has none. Collecting them belongs to whoever *starts*
the processes and already holds the streams, not to the tool that only emits them: a runner that
captures stderr per generator can name the lines it saw beside the usage line, rather than piping
eleven generators' output into its report. Putting the collection point in the tool would mean
broadcasting warnings whose false-positive profile is still being found — a wrong warning in a build
report is worse than an unseen one on a stream.

An unusable path warns once and falls back to counting that process alone. In
process, the same numbers sit on `gauth.calls` without the env var:

```python
gauth.calls.counts()               # {("sheets", "read"): 30, …}
gauth.calls.peak("sheets", "write")
gauth.calls.records()              # every call of the run, the file's when shared
```

A runner that swallows stderr can print the collected summary itself once the build is done —
`Calls` reads any such file, not only one it wrote:

```python
print(gauth.Calls("/tmp/build-calls.jsonl").report())
```

Printing it yourself in a process that also has `GTOOLS_STATS` set gets you the line twice — the
import registered an exit hook. Drop that one:

```python
atexit.unregister(gauth.print_stats)
```

## Unstacking a sheet

`gsheets objects <sheet>` counts duplicates; `--duplicates` prints the
`batchUpdate` requests that remove them, and sends nothing:

```
gsheets batch <sheet> "$(gsheets objects <sheet> --duplicates)"
```

It earns its keep only where a generator does **not** delete its own objects before re-adding them —
one that cleans up after itself never stacks in the first place. On the one workbook this has been
pointed at, across roughly ten builds, it has reported `no duplicates` every time.

Two things make this worth a flag rather than a hand-written batch.

**Conditional formats are deleted by index**, and each delete shifts every later
rule down one. On rules `[A, A, B, B, C]` the duplicates sit at 1 and 3; removing
them front to back deletes a `B` and then `C`, leaving a duplicate standing and
losing the unique rule. The requests therefore come highest index first, which
is correct because one `batchUpdate` applies its requests in order. Verified
live: five stacked rules with thresholds `1,1,2,2,3` came back as `1,2,3`.

**It compares whole objects**, on a wider field mask than the summary uses. Under
the summary's `charts(chartId,spec.title)` two charts both titled "Umsatz" are
identical, and they are not the same chart – the mask trap again, this time able
to destroy work. It costs no extra call: the wider mask carries no row or column
sizes, so it drops `includeGridData`.

Which kinds can stack at all was settled against the live API rather than reasoned about, by trying
to create each duplicate:

| Stacks silently | Cannot be duplicated |
|---|---|
| conditional formats, protected ranges, charts, developer metadata | merges, banded ranges, filter views |

Google refuses the second one in the right-hand column outright — a banding with *"Einem benannten
Bereich … können keine weiteren abwechselnden Hintergrundfarben hinzugefügt werden"*, a filter view
with *"Dieser Ansichtsname ist bereits vorhanden"*, and since a filter view compares equal only when
its title matches, an identical pair is unreachable. Those kinds are therefore never emitted, and
neither are dimension groups — for a different reason: deleting one by its range lowers the depth
rather than removing a copy. Every delete shape that *is* emitted was applied to a real duplicate
and checked.

## Three locale conventions in one call

A `de_DE` workbook runs three different conventions at once, and two of them point opposite ways.
All measured on the scratch sheet:

| What | Convention | Example |
|---|---|---|
| Values | German | `"0,125"` — `"0.125"` becomes 125, `"0.5"` stays text |
| Formula separators | German | `=IF(1<=12;0;5)` — with commas: `#ERROR!` |
| **Number format patterns** | **US** | pattern `#,##0.0` displays 1234.5 as `1.234,5` |

The pattern row is the one that catches people twice. Comma means thousands and dot means decimal
*inside the pattern* whatever the locale, and the sheet then renders them the German way round — so
a pattern is right exactly when it looks wrong.

A comma-separated formula is not rejected outright either: the cell **keeps the formula** and
evaluates to `#ERROR! "Formula parse error"`, so nothing looks broken until something reads the
value.

One pattern deserves its own warning: **`0.#` appends a bare separator on `de_DE`**, so 7 displays
as `7,`. `#,##0.#` is no way around it — use an integer pattern.

`update`/`append` warn on the two halves that can be checked — a dotted-decimal string, and a
formula separating arguments with `,`. Both share the one locale read. Patterns are not checked,
because a correct one is indistinguishable from a mistake without knowing what it is meant to
display.

The formula check has to be careful, because a `de_DE` formula legitimately contains commas —
`=0,035*B91` is a decimal, and a decimal comma is the very convention the *value* check demands. A
first version missed that and produced **65 false alarms across 7,923 real formulas** in one
workbook. So a comma counts as an argument separator unless it is a decimal point, which needs
digits on both sides *and* the left-hand ones forming a number rather than the tail of a reference:

| formula | verdict | why |
|---|---|---|
| `=0,035*B91` | silent | `0,035` is a number |
| `=RUNDEN(1,5;0)` | silent | decimal inside a call |
| `=IF(A1<=12,0,B1)` | flagged | `,B1` separates arguments |
| `=MAX(A1,2)` | flagged | `A1` is a reference, and no number starts with a letter |
| `=SUM($A$1,2)` | flagged | same, absolute |
| `=CONCATENATE("a,b";C1)` | silent | inside a string literal |
| `='Test, Komma'!A1` | silent | inside a single-quoted sheet name |

Measured against a real corpus of **7,995 formulas** — including a block written by someone else,
so not merely echoing the conventions of the code that generates them: **0 false alarms**, and all
9 deliberately mis-comma'd formulas caught. Both halves matter; a check that never fires would also
score zero false alarms.

Read that number as a **regression guard, not a completeness proof**. Both quote characters have to
be honoured — a tab may be called `Test, Komma`, and `='Test, Komma'!A1` returns its value quite
happily, while an earlier version knew only double quotes and flagged it as an error. The corpus
could not have caught that: none of its nine tab names contains a comma, so the case simply is not
in there. A corpus shows the absence only of the mistakes that occur in it, never of the ones that
could. What proves the quote fix is the live probe on the scratch sheet, not the 7,995.

One shape stays undecidable — `=MAX(12,5)`, a number on both sides, which Sheets cannot resolve
either. It is read as a decimal and passes. That is the deliberate direction to be wrong in: a
missed one surfaces as `#ERROR!` in the cell, while a false alarm fires on every build of a
*correct* workbook and teaches people to ignore warnings — hitting exactly those who followed the
decimal convention.

## Reading formatting: which field answers which question

`userEnteredFormat` is what is **stored**; `effectiveFormat` is what the sheet **shows**. They differ
in a way that ambushes any drift check: an explicitly stored value equal to the default is invisible
on screen and still appears in every diff. A cell carrying `wrapStrategy: OVERFLOW_CELL` looks
identical to one carrying nothing, because `OVERFLOW_CELL` *is* the default — but only one of them
shows the field.

- *Does this look different?* → compare `effectiveFormat`
- *Is something different stored?* → compare `userEnteredFormat`

Reading `effectiveFormat` also answers questions `userEnteredFormat` cannot: after a `reset`,
`effectiveFormat.verticalAlignment` reports `BOTTOM` while `userEnteredFormat` is simply absent.

## Resetting a tab

Rewriting a cell's value leaves most of the rest of it standing: borders, backgrounds, number
formats, wrap strategy, notes and data validations, plus merges and row heights. A generator's
second run therefore inherits the first one's decoration, and stale formatting is invisible in a
way stale values are not.

**Rich text is the exception, and it cuts the other way.** `textFormatRuns` belong to the *text*, so
rewriting a value already takes them — and so does the link a whole-cell run normalizes into.
Measured: after a plain `update`, `userEnteredFormat.textFormat.link` and `hyperlink` were both
gone, while the background on that same cell survived. So clearing rich text only reaches a sheet
whose values stay put; where a generator rewrites every value, it is never the reason to reset.

```
gsheets reset <sheet> <tab ...>          # formatting, notes, validations, rich text, merges
gsheets reset <sheet> <tab ...> --sizes  # row/column sizes and hidden flags too
```

Any number of tabs, always **one metadata read and one write**. Resetting one tab at a time costs
2 calls each, so a nine-tab workbook is 18 against a budget of 60 — where naming all nine at once
is 2. `clear` batches the same way (`values.batchClear`), so nine tabs blanked in one call
rather than nine.

### Cheaper still: no call at all

A perfectly bundled command costs one call; a *request* costs none. Anything already sending a
`batchUpdate` — a generator writing its own formatting — should carry the work along in that batch
instead of calling `reset` beside it:

```python
requests = gsheets.reset_requests(*sheet.tab_ids("Plan", "Ist"))
sheet.batch(requests + my_own_formatting)     # one call for all of it
```

Clearing **values** that way needs a different request, and this is the part worth knowing:
`values.batchClear` is a separate endpoint from `spreadsheets.batchUpdate`, so `clear` can never be
folded into a batch. Use the request form instead:

```json
{"updateCells": {"range": {"sheetId": 0}, "fields": "userEnteredValue"}}
```

Values are kept — `clear` is for those. Merges are the ones worth removing even when a sheet looks
fine: **a write to a cell a stale merge covers is discarded silently**, still reporting
`updatedCells: 1`.

The requests name only the sheet, never computed bounds, and a `GridRange` with no bounds means the
whole sheet *at its current size*. That is deliberate: bounds worked out from a size read before the
generators ran leave every row below them untouched, which is the way this goes wrong. Running it
twice is harmless — `unmergeCells` over a merge-free sheet is not an error.

`--sizes` is separate because a deliberate layout also lives in row heights, and it restores Sheets'
own defaults (21 px rows, 100 px columns) rather than whatever the sheet has drifted to.

**Data validations are the one thing a reset may destroy irrecoverably.** Sheets renders some
dropdowns as chips, and that rendering is not exposed through the API at all — so nothing here can
detect it, and a rule put back through v4 can return as the plain arrow. Look at
`gsheets cells <sheet> <range> --fields='sheets.data.rowData.values(dataValidation)'` before
resetting a sheet whose dropdowns matter, and build a narrower `updateCells` mask by hand if you
need to keep them.

One default surprises people, so check before resetting a formatted sheet: **vertical alignment
goes back to `BOTTOM`, not `TOP`**. Measured on `effectiveFormat` — a wrapped cell in an 80 px row
read `TOP`/`WRAP` before the reset and `BOTTOM`/`OVERFLOW_CELL` after. Any row taller than its text
then has that text at the foot of it, which reads as broken rather than as reset. Where a sheet
wraps or has tall rows, follow the reset with a `repeatCell` setting `verticalAlignment` — that is a
house style to re-apply, not something a reset can guess.

## Using it in-process

Both tools are importable, and that is the cheaper path for anything that makes
more than a handful of calls: a process start costs roughly 0.3s, which
dominates a script issuing dozens.

```python
import sys; sys.path.insert(0, "/path/to/google-docs-editors-agents-cli")
from gsheets import Client
from gdocs import Document

sheet = Client(url_or_id)
sheet.update_many({"Tab!A1": [[1]]})       # several ranges, one request
sheet.objects()                            # what lives beside the cells
sheet.duplicates()                         # the deletes that would unstack them
sheet.reset("Tab", "Andere")               # formatting a value overwrite leaves behind
sheet.clear("Tab!A1:Z100", "Andere!A1:D9") # values only, one call for both
sheet.cells("Tab!A1:C9", fields="sheets.data.rowData.values(effectiveValue)")

doc = Document(url_or_id, tab="t.0")
doc.append("Text")
```

The commands **are** these methods with argument parsing and printing wrapped
around them, so nothing is reimplemented on either side and the guards apply
equally: the locale check, the retries, the merge and grid-limit behaviour, the
index arithmetic. Methods return data and raise `gauth.ToolError` on a refusal
rather than exiting – a library that calls `sys.exit()` takes its caller's run
down with it.

Each `Client` and `Document` builds its own service, costing a `build()` call
and a token read apiece. Driving several sheets or documents at once, hand them
one service to share:

```python
svc = gsheets.api()
Client(first, service=svc)
Client(second, service=svc)
```

`api()` returns the raw service for anything the classes do not cover. It now
carries the retry too, which the client library does not do by default.
`gauth.http_error_message(err)` formats an `HttpError` the way the CLIs print
it, for callers that want the same one-liner.

## Environment

`GTOOLS_PYTHON` picks the interpreter the `gsheets`/`gdocs` wrappers exec, overriding both the
bundled `.venv` and the `python3` fallback – see **Running it from another machine** in `README.md`.

`GTOOLS_RETRIES` sets how many times a transient failure (429, 5xx) is retried: default 3, `0`
disables, capped at 10. `GTOOLS_QUOTA_WAIT` sets the fixed pauses used to sit out a per-minute
quota: default `20,40`, `0` disables. An unusable value warns and falls back rather than failing
the command. `GTOOLS_STATS` prints the call summary described under **Quota, measured**: off unless
set, `1`/`on`/`true`/`yes` counts within the one process, and any other value is taken as a path
every process appends to.

The backoff sleeps `rand() * 2**n` per attempt, so a run lands anywhere below its ceiling – and
the ceiling badly overstates it. Simulated over 200,000 runs: at 3 the total averages ~7s against
a 14s ceiling; at 5 it **never** reaches 60s despite a 62s ceiling; at 6 it clears 60s barely half
the time (55%); only at 7 is it dependable (94%), and by then every transient blip can cost up to
254s.

So these retries are for a transient 429 or 5xx, and no setting also makes them cover the quota's
sliding 60-second window. The jitter is there to spread contending clients apart, and a limit of
60 calls per minute per *user* has no contention to spread: it is a clock, not a crowd.

`GTOOLS_QUOTA_WAIT` is the second policy, for that clock. A 429 whose body names a per-minute
quota is sat out with **fixed** pauses that sum past the window – default `20,40`, so two waits
rather than the seven retries it would otherwise take. Each one is announced on stderr, because a
silent minute reads as a hang:

```
Read requests per minute per user reached – waiting 20s, then retrying
```

Set it to `0` to turn the waiting off, which is usually what a person at a terminal wants and
rarely what a build script does. A 429 that names no per-minute quota is left to the short
jittered backoff above, so a passing blip never costs a minute. That distinction rests on 19 real
quota errors: every one was HTTP 429 with reason `RATE_LIMIT_EXCEEDED` and a body naming
`Read`/`Write requests per minute per user`.
