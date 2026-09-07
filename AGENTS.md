# Working on this repo

Two CLIs over the Google Sheets and Docs APIs. `README.md` covers setup and credentials,
`REFERENCE.md` the measured behaviour of both APIs. Adding Slides or Forms is a scope in `auth.py`,
a module beside these two, and one browser consent for everybody already installed – `require_scope`
prints that instead of Google's 403.
Run `gsheets` or `gdocs` bare for the usage text – the reference for what these APIs get wrong,
and worth reading before you add a flag.

## Measure, do not reason

Every claim here was checked against the live API. The ones that were reasoned about instead were
wrong often enough to make that the rule: a dimension group's range is not a `GridRange`,
ungrouping does not clear the collapse flag, `${0:A:h}` is zsh and expands to nothing under `sh`.
All three shipped as confident prose first.

The trap underneath is that a field mask omitting a field **does not error** – it returns the
default, so a check written from the docs can pass against data that was never fetched. If you
cannot verify a claim on a real sheet, do not write it down as a fact.

## Leave the scratch surfaces empty

With no Drive scope, nothing these tools create can be deleted afterwards: every throwaway
spreadsheet or document is permanent litter, cleanable only by hand. Two surfaces exist for live
checks – leave them empty when done.

| Surface | Why it looks like this |
|---|---|
| `gsheet Scratch` | Locale `de_DE`, tab `Tabellenblatt1`, 997x26 |
| `gdocs Scratch` | Two tabs: `t.0` and `Zweiter` |

Neither shape is accidental. The sheet's `de_DE` locale is what makes the dotted-decimal warning
testable at all – an `en_US` sheet exercises nothing. The document keeps a second tab so `--tab` and
the all-tabs reach of `replace` can be checked without adding one. Their ids are personal, so they
live in `scratch.local` – one `name = id` per line, untracked.

Putting them back:

- Values: `gsheets clear <sheet> 'Tab!A1:Z100'` – several ranges at once, in one call
- Formatting, notes, validations, rich text and merges: `gsheets reset <sheet> <tab>`
- Sheet objects: `gsheets objects <sheet>` first to see what is there, then delete by id via
  `gsheets batch` – conditional formats, banded and protected ranges survive both a `clear` and a
  `reset`
- Hidden rows outlive the collapsed group that hid them, so `reset --sizes` after ungrouping
- A document tab: `gdocs delete <doc> 1 <end index - 1>`, per tab, reading the end index from
  `gdocs index` each time

`gsheets delete-rows` is structural: it shrinks the grid rather than blanking cells, so restore the
count with `appendDimension` through `gsheets batch`, or with `insert-rows` at a given position.

## Do not add a Drive scope

It looks like a missing feature and it is the point: without it these tools cannot delete, move,
rename or share a file, which is why someone lets an agent near a spreadsheet they depend on.
Drive-backed features failing with HTTP 403 is the design working, not a bug to route around. A
read-only sibling credential holding `drive.readonly`, purely to look file ids up by name, has been
proposed and **declined** – settled, not open.

## Run the suite

`./dev/run-tests.sh` is hermetic: no network, no Google account, a throwaway token, so it runs
anywhere and `create` cannot leave a file behind. It ends on `all tests passed` or `TESTS FAILED`.
New behaviour needs a check; the existing ones read as sentences about what must stay true, so write
yours the same way.

Live behaviour the fakes cannot prove – whether Google accepts a request shape – is checked by hand
against the scratch surfaces above.
