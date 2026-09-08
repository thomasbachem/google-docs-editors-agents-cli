---
name: google-sheets-docs
description: "Use whenever a task involves creating, reading or editing a Google Sheet or Google Doc - a new sheet or document, cells, ranges, tabs, formulas, formatting, row groups. Two commands, gsheets and gdocs, drive the official Sheets and Docs APIs directly: faster than the browser, and able to edit contents the Drive connector cannot. Use them too when an edit by another route has stalled."
---

# Google Sheets and Docs through their APIs

Two commands, `gsheets` and `gdocs`, talk to the official APIs. They read several ranges
in one call and edit cells, tabs, formatting and row groups directly. Neither holds a
Drive scope: they can change the contents of a file you name and nothing else - no
deleting, moving, renaming or sharing, and no reaching a file you were not given.

## First, is it here?

```sh
gsheets whoami
```

That prints the Google account and project when it works. Two failures mean different
things:

- **Command not found** does not mean absent. The project installs to `~/.local/bin`,
  and some environments do not carry that directory on `PATH` - a plain `bash -c` reads
  neither `.profile` nor `.bashrc`. Try `~/.local/bin/gsheets whoami`, and if that
  answers, keep calling it from there. Do not retry the bare name and quietly give up.
- **`no token.json`** means installed but not authorized. Only the person whose Google
  account it is can fix that, by running `python3 auth.py` in the checkout and completing
  a browser consent. Do not go looking for credentials.

## If it really is not installed

Worth doing whenever the project's folder is reachable - connected to this session, or
requestable below. Find the directory holding `gsheets.py` and `install`, then:

```sh
python3 -m pip install --user google-api-python-client google-auth-oauthlib
./install
```

`install` reports where it put the commands and whether a bare `gsheets` will find them.
In a sandbox whose home starts empty this belongs to each session, not once for all.

If the folder is not reachable, ask for it rather than telling the person to reconnect
and start over. Request the directory holding `gsheets.py` through this surface's
folder-access tool - `mcp__cowork__request_cowork_directory` in Cowork,
`mcp__ccd_directory__request_directory` in the Claude Code desktop app - passing the
path, so what they see is that exact folder and one approval.

Propose a path you have some reason to believe: one you were told, or one this file
names where it does. Not one read off a folder's name - an ungranted folder lists its
subdirectories and not its files, so an empty one looks exactly like one holding
`gsheets.py`. Ask when you have nothing better. Where a session grants without
prompting, the grant confirms nothing about whether you asked for the right place, so
check `gsheets.py` is there. Grants are per session: this recurs, and is not a sign
something is broken.

Only once they decline, or an install fails, fall back - and know what the fallback
reaches: Claude's Drive connector reads a file and renames it, but writes no cell, so an
edit needs the Sheets interface. Say which route you took: a detour nobody was told
about is how this stays broken for weeks.

## Using it

Run `gsheets` or `gdocs` bare first. That usage text is the command reference and it
names the traps each API hides; a guessed call costs a silent bad write rather than an
error. `create` makes a new file and prints its URL - everything else wants the URL or
id of one that exists.

Either command:

- **Look before you write.** `gsheets info` gives exact tab names, sheet ids and grid
  sizes; `gdocs info` gives tabs and the heading outline. Where the write depends on what
  is already there, read that first - in Sheets with `cells` rather than `get`, because
  `get` shows what the sheet displays and `cells` shows what it holds, formulas,
  validation, links and real errors included.
- **`--dry-run` prints the request and sends nothing.** Reads still happen, so what it
  prints is what would really go over the wire. Worth a call before anything large.
- **Spend calls, not requests.** Both quotas count calls. `get`, `json` and `cells` take
  several ranges each, `update-many` writes several in one, `clear` and `reset` take
  several tabs, and a `batch` you are already sending can carry the rest along.
- **Check your own work** by reading the changed part back, rather than by trusting that
  a call which returned did what you meant.
- **Leave the rest alone.** Preserve values, formulas, validation, styling and structure
  outside what was asked for, and say what you changed.

In Sheets:

- **Three conventions collide in one call** on a non-English sheet: values are written in
  the sheet's locale, formula arguments separate with `;`, and number-format patterns
  stay US whatever the locale. Pass decimals as JSON numbers, never as strings - on a
  German sheet "0.125" parses as 125, in silence. `update` and `append` warn on two of
  the three.
- **`reset` is the destructive one.** It strips formatting, notes, validations, rich text
  and merges across a tab's whole grid, keeping only the values. `clear` empties cells
  and keeps formatting. Nothing here undoes either.
- **`objects` sees what `get` and `cells` cannot** - conditional formats, row groups,
  charts, merges - and counts duplicates, which is how a job run twice stacks rules
  invisibly until the sheet crawls.

In Docs:

- **Every edit shifts every index after it,** and a stale index raises nothing: the text
  just lands in the wrong place. Re-read `index` after each edit, and order the
  index-bearing requests within one `batch` highest first.
- **`replace` needs no indices,** which makes it the safer way to change text already
  there - but it spans every tab unless `--tab` narrows it, so on a multi-tab document it
  rewrites tabs you never opened.
- **A new paragraph inherits the previous one's style.** Text appended under a TITLE is
  TITLE too, silently. Write the text first and style afterwards: styles shift no
  indices, so one `index` read still covers every range.

## Going further

`REFERENCE.md` in the checkout holds the measurements behind those claims: the quota that
counts calls, the three locale conventions, reading formatting, resetting a tab, and the
environment variables.

Both are also Python modules, which beats paying a process start each time for a script
making dozens of calls. `sheet.api()` and `doc.api()` hand back the raw service, already
retrying, for anything the methods do not cover; `REFERENCE.md` has that API.
