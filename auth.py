#!/usr/bin/env python3
"""One-time (re-)authorization for gsheets: opens a browser consent for the
"Sheets CLI" OAuth client and stores the refresh token in token.json.

Scopes: spreadsheets and documents (read/write the contents of sheets and
docs) plus openid + userinfo.email, which are identity-only and exist so the
tools can report which Google account they act as. No Drive scope, so files
still cannot be deleted, moved, renamed or shared.
"""

import base64
import json
import os
import shutil
import sys

# Google returns a scope string that differs from the one requested once openid
# is involved; without this oauthlib aborts the flow with "Scope has changed".
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

import gauth  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.path.join(HERE, "token.json")
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


def main():
    secrets = os.path.join(HERE, "client_secret.json")
    if not os.path.exists(secrets):
        sys.exit(
            f"missing {secrets}\n"
            "Create an OAuth client (type: Desktop app) in your Google Cloud project and\n"
            "save the downloaded JSON there – see README.md, step 3."
        )

    previous = ""
    if os.path.exists(TOKEN):
        try:
            previous = json.load(open(TOKEN)).get("email", "")
        except Exception:
            pass
        shutil.copy2(TOKEN, TOKEN + ".bak")

    print("Opening browser consent…")
    if previous:
        print(f"Sign in as {previous} – the account the existing sheets belong to.")

    flow = InstalledAppFlow.from_client_secrets_file(secrets, SCOPES)
    creds = flow.run_local_server(port=0)
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


if __name__ == "__main__":
    main()
