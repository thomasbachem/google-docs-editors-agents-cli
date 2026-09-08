# Google Sheets+Docs from the Command Line, for Agents

An agent told to edit a spreadsheet usually ends up driving the browser: pasting TSV through the
clipboard, aiming at cells through the Name Box, expanding a collapsed group first so the write does
not land in the wrong row. Those failures belong to the interface, not the task, and none exists
through the API.

- `gsheets` – create, read and edit spreadsheets
- `gdocs` – create, read and edit documents

Two of the Google Docs editors, over their official APIs, sharing one OAuth client and one token –
several ranges per call against a quota that counts calls, and every trap in the docs measured
against the live API, not reasoned about, because a wrong claim costs an agent a silent bad
write rather than a raised eyebrow. Slides and Forms are the same family and not built yet: the name
is the family, not a claim about today.

Neither holds a **Drive scope**, deliberately. They can change the contents of a file you name and
nothing else – no deleting, moving, renaming or sharing, and no reaching a file you did not name.
It is a property of the token, not a setting in the code, so it holds however wrong a
caller goes, which is what makes these safe to point at a spreadsheet someone depends on.

Run either bare for its usage text – the command reference, naming the traps each API hides:
locale parsing, link normalization, index shift. Both are also Python modules, the cheaper path for
a script making dozens of calls: the commands are those methods with argument parsing around them.
`REFERENCE.md` holds that API, the measurements these claims stand on, the environment variables and
running this over a mount; `AGENTS.md` is for working on the tool rather than with it.

## Setup

Steps 1 and 2 are browser work in your own Google account, once per account – a second machine
reuses the same project and client. Steps 3 and 4 are shell, once per machine. Step 5 is once per
checkout, where `token.json` lands.

**1. A Google Cloud project with the two APIs enabled**

Create a project at https://console.cloud.google.com/projectcreate. On a Workspace account leave
*Location* set to the organization rather than "No organization" – step 2 needs a project the domain
owns. Then enable both APIs:

- https://console.cloud.google.com/apis/library/sheets.googleapis.com
- https://console.cloud.google.com/apis/library/docs.googleapis.com

Leave Drive off – **Credentials** says why that absence is the point.

**2. An OAuth client of type Desktop app**

Under *Google Auth Platform*, in that same project:

- *Branding* – an app name and your own address as the support email
- *Audience* – *Internal* on a Workspace domain you administer: no test users, no publishing, no
  verification, no warning screen. A **personal account** has no *Internal* – see below before
  choosing *External*.
- *Clients* → *Create client* → application type *Desktop app*. Download the JSON – step 5 takes
  it out of `~/Downloads` on its own, or name it: `python3 auth.py <path>`.

*Desktop app* is not cosmetic: `auth.py` completes the flow against a local server on a random port,
which only that type permits. It refuses any other up front, because a Web client reaches the
consent screen and then fails on a redirect mismatch that names nothing.

**External costs a seven-day token expiry** while the app is in *Testing*: the refresh token dies and
the tools stop with `invalid_grant` until `auth.py` runs again. A personal account has no other
option, so add your address under *Test users* and then *publish the app*, which ends the expiry
at the cost of one "Google hasn't verified this app" screen wanting *Advanced → Go to …*.

**If Internal is greyed out**, the project sits outside the organization – move or recreate it there
rather than taking *External* as the way around. On a Workspace account you do not administer, both
creating a project in the org and moving one into it are rights an admin can withhold, so that is a
question for them. If consent is refused by policy rather than test-user membership, the setting is
in the Admin console under *Security* → *Access and data control* → *API controls*.

**3. Get the code**

```sh
git clone https://github.com/thomasbachem/google-docs-editors-agents-cli
cd google-docs-editors-agents-cli
```

Somewhere long-lived, not inside whatever project you happen to be in: `install` symlinks
rather than copies, so wherever this lands stays the tool's home.

On a Mac that has never had developer tools, this first shell command opens a dialog offering to
install them – accept it. `/usr/bin/git` and `/usr/bin/python3` are the same stub until it
finishes, so step 4 needs that install as much as this one does.

**4. Python and the two dependencies**

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install google-api-python-client google-auth-oauthlib
```

`python -m pip` rather than `./.venv/bin/pip`: a venv's console scripts carry an absolute shebang
and break the moment the directory moves; the interpreter does not. Python 3.9 and up,
everything else arrives as a dependency of those two.

**5. Authorize, then check**

```sh
./.venv/bin/python auth.py
./gsheets whoami        # the account the token now acts as
./dev/run-tests.sh      # the offline suite: no network, no account
```

`auth.py` opens a browser consent and writes `token.json`. That file and `client_secret.json` are
personal from here on – **Credentials** says what that means.

## Putting it on PATH

```sh
./install                 # symlinks gsheets and gdocs into ~/.local/bin
./install /usr/local/bin  # or wherever you keep commands
```

Symlinks, so the installed commands keep following this checkout: edit it or `git pull` it and there
is nothing to reinstall. It matters most for an agent: anything that has to *find* the tool guesses
a path, and a guessed path rots the first time the directory moves. `command -v gsheets` does not.

`install` refuses a name belonging to somebody else's command and repoints one of ours left dangling
by a move – the second is the one you will actually hit, and the one that leaves "command not
found" behind. `--force` overrides the refusal. It also reports when the target is not on `PATH`,
when a different `gsheets` comes first on it, and when the tools resolve but crash for want of the
dependencies – and it will not say the bare name works when it does not.

That last one matters here more than most places. Adding the directory to your shell profile fixes
your own shell and nothing an agent runs: a plain `bash -c` is neither a login nor an interactive
shell, so it reads neither `.profile` nor `.bashrc` – and that is the shape agents, cron jobs and
CI steps use. For those, install into a directory already on `PATH`, or call the full path.

`--claude-md` adds a short section to `~/.claude/CLAUDE.md` (or `$CLAUDE_CONFIG_DIR`) so sessions in
other projects reach for these commands instead of a browser or connector. A plain run offers the
flag rather than taking it: it appends between markers, never rewrites, and stands down when the
file already covers the subject. Removing it is deleting the marked block.

## Driving it from Claude

Three things decide whether a surface can run these: **a shell**, **network to `googleapis.com`**,
and **the token**.

- **Claude Code** – and anything else with a shell, a CI job or a cron entry – has all three the
  moment `install` has run. Probe with `command -v gsheets` rather than hunting for a checkout.
- **Cowork** has a shell and a network, so the token is the only open question. Mount or upload it
  and it works.
- **The claude.ai app** cannot run a command at all, and wants the Google Drive connector instead.
  A skill should try the command, fall back to the connector, and say which it used.

## Credentials

`token.json` and `client_secret.json` are personal. The **code** is shareable, the credentials are
not – a copy for **another person** needs its own Google Cloud project and its own consent.

Personal means per-person, not per-machine. Copying your own token into a cloud sandbox running your
own agent is not sharing it, and is what lets Cowork run these at all. The scope list bounds that:
`spreadsheets`, `documents`, `openid`, `userinfo.email` – a token that escapes can change the
contents of files someone names and nothing else, so Drive-backed features fail by design: smart
chips in Sheets return HTTP 403. Handing the pair to another person is the line, not moving them off
this disk.

Another Google API is enabled in the project `gsheets whoami` names, at
`https://console.cloud.google.com/apis/library?project=<id>`. A missing one surfaces as
`HTTP 403: … has not been used in project … before or it is disabled`, with the activation URL in
the message.

## Re-authorizing

```sh
./.venv/bin/python auth.py
```

Opens a browser consent and rewrites `token.json`, keeping a `.bak`. It refuses to overwrite when
the consent lands on a different Google account than the token records, so an accidental sign-in
as the wrong user cannot orphan the existing spreadsheets. Needed only when scopes change or the
refresh token is revoked; expiry alone is handled automatically.
