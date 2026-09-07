#!/usr/bin/env python3
"""One-time (re-)authorization for gsheets: opens a browser consent for the
"Sheets CLI" OAuth client and stores the refresh token in token.json.

    python3 auth.py [path-to-the-downloaded-client-json]

Scopes: spreadsheets and documents (read/write the contents of sheets and
docs) plus openid + userinfo.email, which are identity-only and exist so the
tools can report which Google account they act as. No Drive scope, so files
still cannot be deleted, moved, renamed or shared.
"""

import base64
import glob
import json
import os
import shutil
import sys

# Google returns a scope string that differs from the one requested once openid
# is involved; without this oauthlib aborts the flow with "Scope has changed".
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402

import gauth  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.path.join(HERE, "token.json")
DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads")
# Google's own name for a downloaded OAuth client, so this matches little else.
CLIENT_GLOB = "client_secret_*.apps.googleusercontent.com.json"
# Well formed and cannot exist – a probe, not a lookup.
NO_SUCH_ID = "1" + "A" * 43
# Long enough for a real person to switch accounts and read a consent screen, short
# enough that an unattended run ends rather than waiting for a browser nobody opened.
CONSENT_TIMEOUT = 600
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def email_from_id_token(raw):
    """Read the email claim out of the id_token JWT.

    Signature verification is skipped deliberately: this token came straight
    from Google's token endpoint over TLS in the flow we just ran, so there is
    no untrusted party in between.
    """
    if not raw:
        return ""
    try:
        payload = raw.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email", "")
    except Exception:
        return ""


def adopt_client(dest, given=""):
    """Put the downloaded client JSON in place, and say which file was taken.

    Google names it after the client id and drops it in Downloads, which makes
    moving it the fiddliest step of the setup and the only one that is pure file
    shuffling. False means there was nothing to take, so the caller can explain.
    """
    if given:
        if not os.path.isfile(given):
            sys.exit(f"no such file: {given}")
        source = given
    else:
        # Newest first: re-downloading after a botched client is the common repair,
        # and the freshest file is the one that repair produced.
        found = sorted(glob.glob(os.path.join(DOWNLOADS, CLIENT_GLOB)),
                       key=os.path.getmtime, reverse=True)
        if not found:
            return False
        if len(found) > 1:
            sys.exit(f"several downloaded OAuth clients in {DOWNLOADS} – name one:\n"
                     + "\n".join(f"  python3 auth.py {p}" for p in found))
        source = found[0]
    if os.path.exists(dest):
        # Replacing a working client is how a botched one gets repaired; losing the
        # old one in the process would cost a trip back to the Console.
        shutil.copyfile(dest, dest + ".bak")
        os.chmod(dest + ".bak", 0o600)
        print(f"kept the previous client as {dest}.bak")
    shutil.copyfile(source, dest)
    os.chmod(dest, 0o600)   # it came from Downloads, where it is world-readable
    print(f"took {source}\n  -> {dest} (mode 600)")
    return True


def warn_disabled_apis(creds):
    """Say which API is off while the caller is still doing setup.

    Enabling one and forgetting the other is easy, and consent succeeds either
    way – scopes are granted independently of what the project has turned on – so
    the 403 otherwise lands at first real use, inside whatever the caller was
    actually doing. Asking for an id that cannot exist settles it in one call each.

    Both branches were measured against the live API, by turning the Docs API off
    in the project and back on. Enabled answers "HTTP 404: Requested entity was not
    found."; disabled answers 403 "Google Docs API has not been used in project N
    before or it is disabled", carrying the URL that switches it on. Either state
    took effect within seconds, so a probe run right after setup is not too early.

    Matching on that text rather than on the 403 is deliberate: a token missing the
    scope answers 403 too, with "Request had insufficient authentication scopes",
    and calling that a disabled API would send the caller to the wrong Console page.
    If Google ever rewords this, the check goes quiet rather than wrong – the 403 at
    first use is then what it always was.
    """
    for name, version, ask in (
        ("Sheets", ("sheets", "v4"), lambda s: s.spreadsheets().get(spreadsheetId=NO_SUCH_ID)),
        ("Docs", ("docs", "v1"), lambda s: s.documents().get(documentId=NO_SUCH_ID)),
    ):
        try:
            ask(build(*version, credentials=creds)).execute()
        except HttpError as err:
            text = str(err)
            if "has not been used in project" in text or "SERVICE_DISABLED" in text:
                print(f"\nWARNING: the {name} API is not enabled in this project, so the\n"
                      f"tools will fail on first use. Google says:\n"
                      f"  {gauth.http_error_message(err)}", file=sys.stderr)
        except Exception:
            # A probe is not worth failing an authorization that already worked.
            pass


def main():
    secrets = os.path.join(HERE, "client_secret.json")
    given = sys.argv[1] if len(sys.argv) > 1 else ""
    if (given or not os.path.exists(secrets)) and not adopt_client(secrets, given):
        sys.exit(
            f"missing {secrets}\n"
            "Create an OAuth client (type: Desktop app) in your Google Cloud project\n"
            f"and download the JSON – see README.md, step 2. It is taken from\n"
            f"{DOWNLOADS} on its own, or name it: python3 auth.py <path>."
        )

    # The one setup mistake with no readable failure: a Web client passes this far
    # and dies mid-consent on a redirect-URI mismatch, which names nothing. Desktop
    # clients key the JSON under "installed", Web ones under "web".
    try:
        kind = next(iter(json.load(open(secrets))), "")
    except (ValueError, OSError) as err:
        sys.exit(f"cannot read {secrets}: {err}\n"
                 "Re-download the client JSON – see README.md, step 2.")
    if kind != "installed":
        sys.exit(
            f"{secrets} is a {kind!r} client, not a Desktop app.\n"
            "run_local_server() below needs the Desktop type: a Web client has no\n"
            "redirect URI for localhost and fails mid-consent with a mismatch that\n"
            "names neither cause nor fix. Create a client of type Desktop app and\n"
            "replace this file – see README.md, step 2."
        )

    previous = ""
    if os.path.exists(TOKEN):
        try:
            previous = json.load(open(TOKEN)).get("email", "")
        except Exception:
            pass
        shutil.copy2(TOKEN, TOKEN + ".bak")

    # An agent driving this sees the command stop producing output and has to decide
    # whether it hung. Say that the wait is the design, and how long it lasts.
    print(f"Opening browser consent. This waits for you to finish in the browser – up to\n"
          f"{CONSENT_TIMEOUT // 60} minutes. It is not stuck, and nothing is written until you are done.")
    if previous:
        print(f"Sign in as {previous} – the account the existing sheets belong to.")

    flow = InstalledAppFlow.from_client_secrets_file(secrets, SCOPES)
    try:
        creds = flow.run_local_server(port=0, timeout_seconds=CONSENT_TIMEOUT)
    except WSGITimeoutError:
        sys.exit(f"\nno consent within {CONSENT_TIMEOUT // 60} minutes, so nothing was changed –\n"
                 f"{TOKEN} is exactly as it was. Re-run when you can finish the browser step.")
    email = email_from_id_token(getattr(creds, "id_token", None))
    from_claim = bool(email)

    if previous and email and email != previous:
        # Writing this token would silently point the tool at a different Drive,
        # leaving every existing spreadsheet unreachable.
        sys.exit(
            f"\nREFUSED: authorized as {email}, but the previous token was {previous}.\n"
            f"token.json is unchanged. Re-run and pick {previous}, or delete\n"
            f"{TOKEN} first if the switch is intentional."
        )

    if not email and previous:
        # No id_token came back; keep what we knew rather than blanking it out.
        print(
            f"note: Google returned no email claim – keeping {previous} on record.",
            file=sys.stderr,
        )
        email = previous

    data = json.loads(creds.to_json())
    data["email"] = email  # to_json() drops unknown keys, so it is re-added on refresh too
    data["email_source"] = "id_token" if from_claim else "inferred"
    # Shared with the CLIs so the atomic write exists once – this copy used to
    # leak its temp file when the dump failed
    gauth.write_token(data, TOKEN)

    print(f"token saved to {TOKEN}")
    print(f"account: {email or 'unknown (no email claim returned)'}")
    warn_disabled_apis(creds)


if __name__ == "__main__":
    main()
