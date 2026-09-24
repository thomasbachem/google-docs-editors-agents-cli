"""Comment threads, the part gsheets and gdocs share.

A comment on a spreadsheet or a document lives in the Drive API, not in the
Sheets or Docs one, and Drive does not say where on the file it sits: its anchor
is an opaque id neither of the other APIs mentions. Each CLI works the place out
from an export in a format of its own. What is the same for both lives here –
listing the threads, their fields, answering, resolving and reopening, and how
a thread prints.
"""

import html
import json
import sys
import zipfile
from collections import Counter
from xml.etree import ElementTree

# gauth before anything of Google's – it hides the warnings they print on import
from gauth import (DRIVE_SCOPE, RetryingRequest, ToolError, credentials,
                   http_error_message, require_scope, send)
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# What a listing asks Drive for. `resolved` can be absent rather than false on
# a thread nobody ever resolved – measured on some sheets, not others – so it is
# read with a default.
COMMENT_FIELDS = ("nextPageToken,comments(id,anchor,content,createdTime,modifiedTime,"
                  "resolved,quotedFileContent(value),author(displayName),"
                  "replies(id,action,content,createdTime,author(displayName),deleted))")


def drive_api():
    """The Drive service, which is where comments live and all this reaches Drive for."""
    require_scope(DRIVE_SCOPE, "Google Drive")
    return build("drive", "v3", credentials=credentials(), requestBuilder=RetryingRequest)


def match_threads(comments, roots):
    """The export entry each Drive comment is, matched on its creation second.

    The XLSX and DOCX exports both write Drive's `2026-09-07T14:21:18.389Z`
    truncated to the second, and their own ids are no help. Text decides only
    where two threads share a second, in Drive or in the export; it cannot
    lead, since what Drive and an export each make of a comment's text is
    theirs to differ on. A tie in Drive matters as much as one in the export: a
    DOCX export leaves out a thread whose text was deleted, so it and an open
    thread made in the same second meet a single entry, which is the open one's.
    An entry two threads would both take goes to neither. Returns a list
    parallel to `comments`, None where nothing matched.
    """
    per_second = Counter((c.get("createdTime") or "")[:19] for c in comments if c.get("anchor"))
    picks = []
    for c in comments:
        second = (c.get("createdTime") or "")[:19]
        same = [t for t in roots if t["created"] == second] if c.get("anchor") else []
        if len(same) > 1 or per_second[second] > 1:
            text = (c.get("content") or "").strip()
            same = [t for t in same if t["text"].strip() == text]
        picks.append(same[0] if len(same) == 1 else None)
    taken = Counter(id(t) for t in picks if t is not None)
    return [t if t is not None and taken[id(t)] == 1 else None for t in picks]


def record(c):
    """A Drive thread's own fields, as both listings return them after where it is."""
    return {
        "resolved": bool(c.get("resolved")),
        "author": (c.get("author") or {}).get("displayName", ""),
        "created": c.get("createdTime", ""), "modified": c.get("modifiedTime", ""),
        "text": c.get("content") or "",
        # A snapshot from when the comment was made, HTML-escaped by Drive – measured
        # unchanged after the text under it was rewritten, so it says what was there
        "quoted": html.unescape((c.get("quotedFileContent") or {}).get("value", "")),
        "replies": [{"id": r.get("id"), "author": (r.get("author") or {}).get("displayName", ""),
                     "created": r.get("createdTime", ""), "text": r.get("content", ""),
                     "action": r.get("action")}
                    for r in c.get("replies") or [] if not r.get("deleted")],
    }


def thread_lines(where, c, remark=""):
    """One thread as `comments` prints it: where, state, id and author, then the text."""
    stamp = c["created"][:16].replace("T", " ")
    lines = [f"{where}  {'resolved' if c['resolved'] else 'open'}  id={c['id']}  "
             f"{c['author']}, {stamp} UTC"]
    lines += [f"  {remark}"] if remark else []
    lines += [f"  {line}" for line in (c["text"] or "").splitlines() or [""]]
    for r in c["replies"]:
        action = {"resolve": " (marked resolved)", "reopen": " (reopened)"}.get(r["action"], "")
        body = (r["text"] or "").splitlines() or [""]
        lines.append(f"  > {r['author']}, {r['created'][:16].replace('T', ' ')} UTC{action}"
                     f"{': ' + body[0] if body[0] else ''}")
        lines += [f"    {line}" for line in body[1:]]
    return lines


class Threads:
    """The comment methods, for a class carrying `id`, `dry` and `_drive`."""

    def drive(self):
        """The Drive service behind the comment methods, built on first use.

        Lazily, so a file that never touches a comment costs no second build –
        and a token from before the Drive scope keeps working for everything else.
        """
        if self._drive is None:
            self._drive = drive_api()
        return self._drive

    def threads(self):
        """Every thread Drive has on the file, open and resolved, page by page."""
        found, page = [], None
        while True:
            got = self.drive().comments().list(fileId=self.id, fields=COMMENT_FIELDS,
                                               pageSize=100, pageToken=page).execute()
            found += got.get("comments", [])
            page = got.get("nextPageToken")
            if not page:
                return found

    def _exported(self, label, mime, parse, lost):
        """An export, parsed – or None when it cannot be had, reported with what that `lost`."""
        try:
            return parse(self.drive().files().export(fileId=self.id, mimeType=mime).execute())
        except (HttpError, zipfile.BadZipFile, ElementTree.ParseError, KeyError, ValueError) as err:
            detail = http_error_message(err) if isinstance(err, HttpError) else repr(err)
            print(f"WARNING: the {label} export could not be read, so {lost} – {detail}",
                  file=sys.stderr)
            return None

    def reply(self, comment, text):
        """Answer a thread by its id, as `comments` lists it."""
        if not (text or "").strip():
            raise ToolError("reply: the text is empty")
        return self._reply(comment, {"content": text})

    def resolve(self, comment, text=""):
        """Mark a thread resolved – a reply carrying the action, text optional.

        Returns None and sends nothing when the thread already is. Google takes a
        second resolve without complaint and adds another status line to the
        thread – measured, likewise a second reopen – which is how a script run
        twice would litter every thread it touched. The check costs one read.
        """
        return self._set_state(comment, "resolve", text)

    def reopen(self, comment, text=""):
        """Reopen a resolved thread, the same way – None when it is already open."""
        return self._set_state(comment, "reopen", text)

    def _set_state(self, comment, action, text):
        now = self.drive().comments().get(fileId=self.id, commentId=comment,
                                          fields="resolved").execute()
        # absent, not false, on a thread never resolved – see COMMENT_FIELDS
        if bool(now.get("resolved")) == (action == "resolve"):
            return None
        return self._reply(comment, {"action": action, **({"content": text} if text else {})})

    def _reply(self, comment, body):
        return send(self.drive().replies().create(fileId=self.id, commentId=comment, body=body,
                                                  fields="id,action,content"), self.dry)


COMMANDS = ("comments", "reply", "resolve", "reopen")
NEEDED = {"reply": 2, "resolve": 1, "reopen": 1}


def run_command(cmd, target, args, flags, lines):
    """The comment commands' output, the same in both CLIs – `lines` renders a thread."""
    if cmd == "comments":
        found = target.comments()
        shown = found if "--all" in flags else [c for c in found if not c["resolved"]]
        if "--json" in flags:
            print(json.dumps(shown, ensure_ascii=False))
        elif not shown:
            print("no comments" if not found else "no open comments")
        else:
            print("\n\n".join("\n".join(lines(c)) for c in shown))
        if len(found) > len(shown):
            # stderr, so --json output stays parseable
            print(f"{len(found) - len(shown)} resolved thread(s) not shown – --all includes them",
                  file=sys.stderr)

    elif cmd == "reply":
        target.reply(args[0], " ".join(args[1:]))
        print(f"replied to comment {args[0]}")

    else:
        text = " ".join(args[1:])
        if getattr(target, cmd)(args[0], text) is None:
            state = "resolved" if cmd == "resolve" else "open"
            print(f"comment {args[0]} is already {state} – nothing sent"
                  + (", the text included: `reply` posts it on its own" if text else ""))
        else:
            print(f"{cmd}{'d' if cmd == 'resolve' else 'ed'} comment {args[0]}")
