# Working on this repo

Two CLIs over the Google Sheets and Docs APIs, plus Drive for spreadsheet comments. `README.md`
covers setup and credentials, `REFERENCE.md` the measured behaviour of the APIs. Adding Slides or
Forms is a scope in `auth.py`, a module beside these two, and one browser consent for everybody
already installed – `require_scope` prints that instead of Google's 403.
Run `gsheets` or `gdocs` bare for the usage text – the reference for what these APIs get wrong,
and worth reading before you add a flag.

## The layout

| Path | Role |
|---|---|
| `gsheets`, `gdocs` | `/bin/sh` wrappers – self-locating through symlinks, so the directory can be renamed and they can go on `PATH` |
| `gsheets.py`, `gdocs.py` | the two CLIs |
| `gauth.py` | shared token handling, flag parsing, retries, error formatting |
| `gcomments.py` | the Drive comment threads – listing, answering, resolving – apart from how `gsheets` places them |
| `auth.py` | one-time browser consent |
| `token.json`, `client_secret.json` | credentials, mode 600 – never tracked, never shared |
| `scratch.local` | your own scratch surface ids, untracked – see **Leave the scratch surfaces empty** |
| `install` | symlink `gsheets` and `gdocs` onto `PATH` |
| `REFERENCE.md` | measured API behaviour, the in-process API, the environment variables |
| `AGENTS.md`, `CLAUDE.md` | what an agent working ON this repo needs; the second is a symlink to the first |
| `skill/` | the Cowork skill, built into an upload by `install --skill` – see `README.md` |
| `dev/` | the offline test suite |
| `LICENSE` | MIT |

## Measure, do not reason

Every claim here was checked against the live API. The ones that were reasoned about instead were
wrong often enough to make that the rule: a dimension group's range is not a `GridRange`,
ungrouping does not clear the collapse flag, `${0:A:h}` is zsh and expands to nothing under `sh`.
All three shipped as confident prose first.

The trap underneath is that a field mask omitting a field **does not error** – it returns the
default, so a check written from the docs can pass against data that was never fetched. If you
cannot verify a claim on a real sheet, do not write it down as a fact.

## Leave the scratch surfaces empty

No command deletes a file, so every throwaway spreadsheet or document these tools create is litter
someone has to clear by hand. Two surfaces exist for live checks – leave them empty when done.

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
- Comments: `gsheets comments --all <sheet>`, then
  `sheet.drive().comments().delete(fileId=sheet.id, commentId=…).execute()` per id. A comment on a
  cell can only be made by hand in the Sheets interface, so whatever a check needed, someone made –
  delete it. Deleted ones linger as empty entries under `includeDeleted`, which nothing removes

`gsheets delete-rows` is structural: it shrinks the grid rather than blanking cells, so restore the
count with `appendDimension` through `gsheets batch`, or with `insert-rows` at a given position.

## The Drive scope is for comments

The token holds the full `drive` scope because a spreadsheet's comments exist only in the Drive
API – and `drive.file`, by Google's own description, reaches only files the app created or was
handed, which a sheet made in the Sheets interface is not (documented, not measured). Decided
2026-09-14, after weighing a narrower split – reading comments with their cells needs no Drive
scope at all, through the web export URL, measured, and only replying does – and choosing one
consent for everybody over it.

What stays true is narrower than it was: no command deletes, moves, renames or shares a file.
That is now a promise of the code, not of the token, so keep it one – a command that does any of
those is a decision for the maintainer, not a feature to slip in.

## Run the suite

`./dev/run-tests.sh` is hermetic: no network, no Google account, a throwaway token, so it runs
anywhere and `create` cannot leave a file behind. It ends on `all tests passed` or `TESTS FAILED`.
New behaviour needs a check; the existing ones read as sentences about what must stay true, so write
yours the same way.

Live behaviour the fakes cannot prove – whether Google accepts a request shape – is checked by hand
against the scratch surfaces above.
