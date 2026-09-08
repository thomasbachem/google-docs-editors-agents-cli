---
name: google-sheets-docs
description: "Use whenever a task involves creating, reading or editing a Google Sheet or Google Doc - a new sheet or document, cells, ranges, tabs, formatting, row groups. Works through the official Sheets and Docs APIs with a command line tool rather than the browser or the Drive connector, which is faster and cannot reach a file you were not given. Use it too when an edit through the Drive connector has failed or turned awkward."
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

If the folder is not reachable, ask for it rather than telling the person to reconnect and
start over. Request the directory holding `gsheets.py` through this surface's folder-access
tool - `mcp__cowork__request_cowork_directory` in Cowork, `mcp__ccd_directory__request_directory`
in the Claude Code desktop app - passing the path, so what they see is that exact folder and
one approval. Ask for that path; do not infer it from folder names. An ungranted folder lists
its subdirectories and not its files, so an empty one looks exactly like one holding
`gsheets.py` - and in a session that grants without prompting, a guess costs nobody a click
and is wrong just as often. Grants are per session, so this recurs and is not a sign something
is broken. Only once they decline, or an install fails, use the Drive connector or the Sheets
interface - and say which route you took: a slow path nobody was told about is how this stays
broken for weeks.

## Using it

Run `gsheets` or `gdocs` with no arguments for the full command reference. Read that
rather than guessing calls - it names the traps each API hides, among them locale
parsing, link normalization and index shift. `REFERENCE.md` in the checkout holds the
measurements behind those claims: the quota that counts calls, the three locale
conventions, reading formatting, resetting a tab, and the environment variables.

Both are also Python modules. For a script making dozens of calls that beats paying a
process start each time; `REFERENCE.md` has that API.
