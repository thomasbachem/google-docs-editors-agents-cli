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

`GAPI_STATS=1` makes a run say what it spent, on stderr as it exits:

```
gapi: 55 API call(s) – sheets read 30 (peak 24/60s), sheets write 25 (peak 21/60s); 512 KB sent, largest 414 KB
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
window, and it is their sum Google meters. Set `GAPI_STATS` to a **path**
rather than `1`, and every process appends there while all of them read the
whole file:

```sh
GAPI_STATS=/tmp/build-calls.jsonl ./bauen.py
```

```
gapi: 55 API call(s) in 11 processes – sheets read 30 (peak 24/60s), …
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

Printing it yourself in a process that also has `GAPI_STATS` set gets you the line twice — the
import registered an exit hook. Drop that one:

```python
atexit.unregister(gauth.print_stats)
```

## Unstacking a sheet

`gsheets objects <sheet>` counts duplicates; `--duplicates` prints the
`batchUpdate` requests that remove them, and sends nothing:

```sh
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

## Comments: which cell, and when that can be trusted

Comment threads – the side panel, not cell notes – are Drive objects, and Drive does not say where
they are. Every anchor on a spreadsheet reads `{"type":"workbook-range","uid":0,"range":"<n>"}`:
`uid` was 0 on all of them, and none of the numbers appears anywhere in the spreadsheet's Sheets
metadata. Measured on 2026-09-14, on a real sheet with 11 threads and on the scratch sheet.

`gsheets comments` therefore reads the cell off an **XLSX export**, whose
`xl/threadedComments/*.xml` carries each thread's `ref`, its creation time and a `done` flag. The
export's thread ids are unrelated to Drive's and regenerated on every export, so the join is on
creation time: Drive's `2026-09-07T14:21:18.389Z` is the export's `2026-09-07T14:21:18.00`, UTC and
truncated. Text breaks a tie within one second. Across both sheets every thread matched exactly one
entry.

What held, each checked by moving the sheet under the comments and exporting again:

| Change | XLSX export | ODS export |
|---|---|---|
| rows inserted above | follows (B5 -> B8) | follows |
| rows sorted | follows the row | – |
| row moved with `moveDimension` | follows (B4 -> B7) | follows |
| cell moved with `cutPaste` | follows (B2 -> D2) | follows |
| thread resolved | `done="1"`, always equal to Drive's `resolved` | **omitted** |
| comment made on a range C8:D9 | one cell, D9 | – |
| **the comment's row deleted** | **A1** | where the row collapsed, on the row below (B5, then B7 after 2 rows inserted above) |
| **the comment's column deleted** | **A1** | where the column collapsed, on the column to its right (H5) |

Both moves showed in the Sheets interface too: the marker sat on the new cell, and the Comments pane
headed each thread with it (`Tabellenblatt1 · D2 · ① Cut and pasted`, the last part the quoted
snapshot). A cut is therefore how a comment moves at all – its cell's content moves with it.

The last two rows are the trap. Drive keeps the orphaned thread open and marks nothing, and A1 looks
like a real cell – the Sheets interface shows the thread on no cell at all, and its Comments pane
leaves the cell out of the heading (`Tabellenblatt1 · · ③ Orphan by column`). The ODS export is
what catches it: it agreed with the XLSX export on every genuine open comment compared – 11
comments, 21 comparisons across moves – and disagreed on every orphan in every export taken after
its cell went, 8 readings of 5 orphans. Four lost their row and one its column; two carried a reply
(one typed, one through the API) and one an @mention. Still a pattern rather than a law. `comments`
exports both, and reports a thread whose exports disagree that way as deleted.

`comments` finds a thread in the ODS export by its first line, which replies and mentions leave
alone: a thread is one annotation there – its text, `-Thomas Bachem`, then each reply followed by
its author – and a mention reads `@mail@thomasbachem.com` as in Drive and the XLSX export.

Two limits on that check:

- **Resolved threads are absent from the ODS export**, so a resolved thread on A1 cannot be told
  from a resolved orphan. It is reported as unconfirmed rather than guessed.
- **The ODS export holds one comment per cell**, the newest. The Sheets interface showed one thread
  per cell, and the only second thread on a cell seen was one made through the API.

`quotedFileContent` – the text of the cell a comment was made on – would be the obvious other clue,
and is not one: it is a snapshot, unchanged after the cell was rewritten. It is printed for a thread
whose cell is in doubt, as what the cell *used to* say. It arrives HTML-escaped (`&#216;`). Drive's
`resolved` field can be absent rather than `false` on a thread never resolved – it was on the
scratch sheet's, not on the real sheet's – so it is read with a default.

**Tab names cannot be taken from either export.** The XLSX one strips `: \ / ? * [ ]` and cuts a
name to 31 characters – `Plan: Q1/Q2 [Entwurf]? *mit sehr langem Tabnamen*` became `Plan Q1Q2
Entwurf mit sehr lang` – and when that makes two names alike it renames the second
`Tabellenblatt2`. The ODS one strips the same characters and keeps the length. Both list every
tab, hidden ones included, in the spreadsheet's own order, so `comments` takes the names from one
Sheets read and lines the exports up by position.

A listing is three Drive calls and that one Sheets read, all reads. Not yet measured: how the
exports fare on a large workbook, where Google documents a size limit. A failed export is reported
rather than failing the listing: without the XLSX one the threads have no cell, without the ODS
one they keep theirs and no A1 can be confirmed or found to be a deleted cell.

Reading the exports alone needs no Drive scope at all:
`docs.google.com/spreadsheets/d/<id>/export?format=xlsx` and `format=ods` both answered a token
holding only `spreadsheets`. But that is an undocumented web endpoint, and without Drive there are
no thread ids to reply to.

**Writing** is a reply: `replies.create` with `content`, or with `action: "resolve"`/`"reopen"`,
where a resolve needs no text. All three show in the Sheets interface at once. Google also takes a
resolve on a thread already resolved, and a reopen on one already open, adding another status line
each time – so `resolve` and `reopen` read the thread's state first and send nothing when it is
already there, which keeps a script run twice from littering every thread it touched. After a
reopen, the XLSX export lists the resolve and reopen as replies in the account's language ("Als
geklärt gekennzeichnet", "Erneut geöffnet"), which is why replies are read from Drive, where they
carry `action`, and never from the export.

**Creating** a comment on a cell does not work, which is why there is no command for it. A new
comment given an existing thread's anchor lands on that cell in both exports and is not shown on it
in the Sheets interface; one given no anchor lands on A1 in both and shows no marker. It does appear
in the Comments pane – headed "Originalinhalt gelöscht", *original content deleted*, which tells a
reader something was removed rather than that the comment was never on a cell – and is not even
how a real orphan is headed, which keeps its tab and quoted text. Google's Drive
guide says as much – the editors "don't render comments created with the Drive API anchored to
content; they treat these comments as unanchored comments" – and calls anchors immutable, so
`comments.update` cannot move one either.

## Smart chips

Settled on 2026-09-14 on the scratch sheet: `chipRuns` **read** with a token holding no Drive
scope, while **writing** a file chip without one is refused – `HTTP 403: The request scopes are not
sufficient for reading from Drive` – and accepted with it, the placeholder `@` then replaced by the
file's title.

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

```sh
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
import os, shutil, sys
# Derive the checkout from the installed command rather than writing its path down:
# `install` symlinks into the checkout, so this keeps resolving when it moves.
CHECKOUT = os.path.dirname(os.path.realpath(shutil.which("gsheets")))
sys.path.insert(0, CHECKOUT)
from gsheets import Client
from gdocs import Document

sheet = Client(url_or_id)
sheet.update_many({"Tab!A1": [[1]]})       # several ranges, one request
sheet.objects()                            # what lives beside the cells
sheet.duplicates()                         # the deletes that would unstack them
sheet.reset("Tab", "Andere")               # formatting a value overwrite leaves behind
sheet.clear("Tab!A1:Z100", "Andere!A1:D9") # values only, one call for both
sheet.cells("Tab!A1:C9", fields="sheets.data.rowData.values(effectiveValue)")
sheet.comments()                           # every thread, with its tab and cell

doc = Document(url_or_id, tab="t.0")
doc.append("Text")
```

Importing is not enough on its own: the interpreter running your script has to be
one the dependencies are installed for. `os.path.join(CHECKOUT, ".venv", "bin",
"python")` is that interpreter, which is what a runner spawning child steps should
hand them. A plain `python3` resolves the modules and then dies on
`ModuleNotFoundError: No module named 'googleapiclient'`.

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

`api()` returns the raw service for anything the classes do not cover, and each
instance hands its own back the same way – `sheet.api()`, `doc.api()`. It now
carries the retry too, which the client library does not do by default.
`gauth.http_error_message(err)` formats an `HttpError` the way the CLIs print
it, for callers that want the same one-liner.

## Running it from another machine

The directory is often reached over a mount – another host, a VM, a cloud agent with the folder
shared in. The code runs fine there; the install does not travel with it.

`.venv` holds binaries for the OS it was built on, so `./.venv/bin/python` is simply missing on
anything else – and a second one inside the mount would collide with the first. Build it outside the
mount and name it:

```sh
python3 -m venv ~/.venvs/gsheets
~/.venvs/gsheets/bin/python -m pip install google-api-python-client google-auth-oauthlib
export GAPI_PYTHON=~/.venvs/gsheets/bin/python
```

`GAPI_PYTHON` is used exactly as given, so a wrong one fails loudly rather than falling back
behind your back. Without it the wrappers try `python3` on `PATH`, which needs those same two
packages and nothing more – but that install fails wherever PEP 668 marks the system Python
externally managed: Debian 12 and up, Ubuntu 23.04 and up, Fedora, Homebrew. Older images still
accept `--user`, so let the refusal tell you which you are on rather than assuming.

The wrappers are `/bin/sh` and resolve their own symlink chain, so a mount, a rename and a symlink
on `PATH` all work. Why that takes more than swapping the shebang is commented in the wrappers
themselves.

On Python older than the version `google-api-core` wants, every invocation opens with a
`FutureWarning`. It goes to **stderr**, so only a merged stream is polluted – `PYTHONWARNINGS=ignore`
silences it either way.

## Environment

`GAPI_PYTHON` picks the interpreter the `gsheets`/`gdocs` wrappers exec, overriding both the
bundled `.venv` and the `python3` fallback – the section above is what it is for.

`GAPI_RETRIES` sets how many times a transient failure (429, 5xx) is retried: default 3, `0`
disables, capped at 10. `GAPI_QUOTA_WAIT` sets the fixed pauses used to sit out a per-minute
quota: default `20,40`, `0` disables. An unusable value warns and falls back rather than failing
the command. `GAPI_STATS` prints the call summary described under **Quota, measured**: off unless
set, `1`/`on`/`true`/`yes` counts within the one process, and any other value is taken as a path
every process appends to.

Each of the four was spelled `GTOOLS_…` until the project outgrew the directory it was named
after. That spelling is still read, so nothing set before the rename stops working; where both
are set the `GAPI_` one wins, and a complaint names whichever one carries the bad value.

The backoff sleeps `rand() * 2**n` per attempt, so a run lands anywhere below its ceiling – and
the ceiling badly overstates it. Simulated over 200,000 runs: at 3 the total averages ~7s against
a 14s ceiling; at 5 it **never** reaches 60s despite a 62s ceiling; at 6 it clears 60s barely half
the time (55%); only at 7 is it dependable (94%), and by then every transient blip can cost up to
254s.

So these retries are for a transient 429 or 5xx, and no setting also makes them cover the quota's
sliding 60-second window. The jitter is there to spread contending clients apart, and a limit of
60 calls per minute per *user* has no contention to spread: it is a clock, not a crowd.

`GAPI_QUOTA_WAIT` is the second policy, for that clock. A 429 whose body names a per-minute
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
