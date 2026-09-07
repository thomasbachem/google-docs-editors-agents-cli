# Google Sheets+Docs from the Command Line, for Agents

An agent told to edit a spreadsheet usually ends up driving the browser: pasting TSV through the
clipboard, aiming at cells through the Name Box, expanding a collapsed group first so the write does
not land in the wrong row. Those failure modes belong to the interface, not to the task, and none of
them exists through the API.

- `gsheets` – create, read and edit spreadsheets
- `gdocs` – create, read and edit documents

Two of the Google Docs editors, over their official APIs, sharing one OAuth client and one token –
several ranges per call against a quota that counts calls, and every trap in the docs measured
against the live API rather than reasoned about, because a wrong claim costs an agent a silent bad
write rather than a raised eyebrow. Slides and Forms are the same family and not built yet: the name
is the family, not a claim about today.

Neither holds a **Drive scope**, deliberately. They can change the contents of a file you name and
nothing else – no deleting, moving, renaming or sharing, and no reaching a file you did not name.
That is a property of the token rather than a setting in the code, so it holds however wrong a
caller goes, which is what makes these safe to point at a spreadsheet someone depends on.

Run either bare for its usage text. That is the command reference, and it names the traps each API
hides – locale parsing, link normalization, index shift. This file is setup, credentials and how the
pieces fit, readable by whoever supervises the agent. `REFERENCE.md` holds the measurements those
claims stand on; `AGENTS.md` is for working on the tool rather than with it.

## Setup on a new machine

Steps 1–3 are once per machine, step 4 once per Google account.

**1. Python and the two dependencies**

```
python3 -m venv .venv
./.venv/bin/python -m pip install google-api-python-client google-auth-oauthlib
```

`python -m pip` rather than `./.venv/bin/pip`: a venv's console scripts carry an absolute shebang
and break the moment the directory moves, while the interpreter does not care. Python 3.9 and up;
everything else arrives as a dependency of those two.

**2. A Google Cloud project with the two APIs enabled**

Create a project at `https://console.cloud.google.com/projectcreate`. On a Workspace account leave
*Location* set to the organization rather than "No organization" – step 3 needs a project the domain
owns. Then enable both APIs inside it:

- `https://console.cloud.google.com/apis/library/sheets.googleapis.com`
- `https://console.cloud.google.com/apis/library/docs.googleapis.com`

Leave the Drive API off: requesting no Drive scope is what keeps these tools unable to delete,
move, rename or share anything – see **Credentials**. Put the project id in `.project`; no code
reads it, but the URLs above need it the day a third API is wanted.

**3. An OAuth client of type Desktop app**

Under **Google Auth Platform**, in that same project:

- *Branding* – an app name and your own address as the support email
- *Audience* – **Internal**, on a Workspace domain you administer: no test users, no publishing,
  no verification, no warning screen.
- *Clients* → *Create client* → application type **Desktop app**. Download the JSON and save it in
  this directory as `client_secret.json`.

Desktop app is not cosmetic: `auth.py` completes the flow against a local server on a random port,
which only that client type permits.

**If Internal is greyed out**, the project was created outside the organization – move or recreate
it there rather than taking External as the way around, which costs a seven-day token expiry that
Internal does not have.

A personal account has no Internal: choose **External**, add your own address under **Test users**,
and then **publish the app** – External while still in Testing expires the refresh token after
seven days, and the tools stop with `invalid_grant` until `auth.py` runs again. Publishing leaves it
unverified, so consent shows "Google hasn't verified this app" once and wants *Advanced → Go to …*.

If consent is refused by admin policy rather than test-user membership, the setting is in the Admin
console under *Security* → *Access and data control* → *API controls*.

**4. Authorize, then check**

```
./.venv/bin/python auth.py
./gsheets whoami        # the account the token now acts as
./dev/run-tests.sh      # the offline suite: no network, no account
```

`auth.py` opens a browser consent and writes `token.json`. That file and `client_secret.json` are
personal from here on – **Credentials** says what that means.

## Putting it on PATH

```
./install                 # symlinks gsheets and gdocs into ~/.local/bin
./install /usr/local/bin  # or wherever you keep commands
```

Symlinks rather than copies, so the installed commands keep following this checkout: edit it or
`git pull` it and there is nothing to reinstall.

It matters most for an agent: anything that has to *find* the tool guesses a path, and a guessed
path rots the first time the directory moves. `command -v gsheets` does not.

`install` refuses a name belonging to somebody else's command and repoints one of ours left dangling
by a move – that second case is the one you will actually hit, and the one that leaves "command not
found" behind. `--force` overrides the refusal. It also says when the target is not on `PATH`,
when a different `gsheets` comes first on it, and when the tools resolve but crash for want
of the dependencies – and it will not say the bare name works when it does not.

That last one matters here more than most places. Adding the directory to your shell profile fixes
your own shell and nothing an agent runs: a plain `bash -c` is neither a login nor an interactive
shell, so it reads neither `.profile` nor `.bashrc` – and that is the shape agents, cron jobs and
CI steps use. For those, install into a directory already on `PATH`, or call the full path.

`--claude-md` adds a short section to `~/.claude/CLAUDE.md` (or `$CLAUDE_CONFIG_DIR`), so
sessions in other projects reach for these commands instead of a browser or connector – without
it an agent that has never seen this repo has no reason to try. A plain run offers the flag
rather than taking it: this is the only file we write into that you authored, so it appends
between markers, never rewrites, and stands down when the file already covers the subject.
Removing it is deleting the marked block.

## Driving it from Claude

Three things decide whether a surface can run these, and it either has them or does not:
**a shell**, **network to `googleapis.com`**, and **the token**.

- **Claude Code** runs on your machine and has all three the moment `install` has run. A skill or
  prompt can then probe with `command -v gsheets` instead of hunting for a checkout.
- **Cowork** has a shell and a network, so the token is the only open question. Mount or upload it
  and it works: this tool was first driven from a Linux VM with the owner's folders mounted under
  `$HOME/mnt`, which is exactly that arrangement.
- **The claude.ai app** cannot run a command at all, and wants the Google Drive connector instead.
  A skill should try the command, fall back to the connector, and say which it used.
- **Anything else with a shell** – a CI job, a cron entry – needs nothing special.

## Running it from another machine

The directory is often reached over a mount – another host, a VM, a cloud agent with the folder
shared in. The code runs fine there; two things about the install do not.

`.venv` holds binaries for the OS it was built on, so `./.venv/bin/python` is simply missing on
anything else. The wrappers fall back to `python3` on `PATH` when it will not execute, which needs
the same two packages as step 1 (`pip install --user google-api-python-client
google-auth-oauthlib`) and nothing more – both CLIs are plain scripts. `GTOOLS_PYTHON=<path>`
forces a specific interpreter, and is used exactly as given, so a wrong one fails loudly rather
than falling back behind your back.

The wrappers are `/bin/sh` and resolve their own symlink chain, so a mount, a rename and a symlink
on `PATH` all work. Why that takes more than swapping the shebang is commented in the wrappers
themselves.

On Python older than the version `google-api-core` wants, every invocation opens with a
`FutureWarning`. It goes to **stderr**, so only a merged stream is polluted – `PYTHONWARNINGS=ignore`
silences it either way.

## Credentials

`token.json` and `client_secret.json` are personal. The **code** is shareable, the credentials are
not – a copy for **another person** needs its own Google Cloud project and its own consent.

Personal means per-person, not per-machine. Copying your own token into a cloud sandbox running
your own agent is not sharing it, and is what makes the cloud sessions under **Driving it** work at
all. The scopes below are what bound that decision: a token that escapes can change the contents of
files someone names, and cannot delete, move, share or even enumerate anything. Handing the pair to
another person is the line, not moving them off this disk.

Scopes: `spreadsheets`, `documents`, `openid`, `userinfo.email`. Drive-backed features therefore
fail by design – smart chips in Sheets return HTTP 403.

Enabling another Google API for these tools is done in the project named in `.project`, at
`https://console.cloud.google.com/apis/library?project=<id>`. A missing one surfaces as
`HTTP 403: … has not been used in project … before or it is disabled`, with the activation URL in
the message.

## Re-authorizing

```
./.venv/bin/python auth.py
```

Opens a browser consent and rewrites `token.json`, keeping a `.bak`. It refuses to overwrite when
the consent lands on a different Google account than the token records, so an accidental sign-in
as the wrong user cannot orphan the existing spreadsheets. Needed only when scopes change or the
refresh token is revoked – expiry alone is handled automatically.

## Files

| Path | Role |
|---|---|
| `gsheets`, `gdocs` | `/bin/sh` wrappers – self-locating through symlinks, so the directory can be renamed and they can go on `PATH` |
| `gsheets.py`, `gdocs.py` | the two CLIs |
| `gauth.py` | shared token handling, flag parsing, retries, error formatting |
| `auth.py` | one-time browser consent |
| `token.json`, `client_secret.json` | credentials, mode 600 – never tracked, never shared |
| `scratch.local` | your own scratch surface ids, untracked – see `AGENTS.md` |
| `.project` | the Google Cloud project id backing the OAuth client |
| `install` | symlink `gsheets` and `gdocs` onto `PATH` |
| `REFERENCE.md` | measured API behaviour, the in-process API, the environment variables |
| `AGENTS.md`, `CLAUDE.md` | what an agent working ON this repo needs; the second is a symlink to the first |
| `dev/` | the offline test suite |
| `LICENSE` | MIT |
