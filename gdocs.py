#!/usr/bin/env python3
"""Minimal Google Docs CLI, authorized via auth.py (token.json beside this script).

Usage:
  gdocs whoami                                # which Google account this token belongs to
  gdocs create  <title>                       # create a new document, prints its URL
  gdocs info    <doc>                         # title, tabs, end index, heading outline
  gdocs get     <doc>                         # print the text
  gdocs index   <doc>                         # print the text with the indices edits need
  gdocs json    <doc>                         # raw document JSON (escape hatch)
  gdocs append  <doc> <text>                  # add text at the end, prints the range written
  gdocs insert  <doc> <index> <text>          # insert text at an index
  gdocs replace <doc> <find> <new>            # replace every occurrence, prints the count
  gdocs delete  <doc> <start> <end>           # delete a range (end-exclusive)
  gdocs style   <doc> <start> <end> <style>   # paragraph style: TITLE, HEADING_1 … NORMAL_TEXT
  gdocs format  <doc> <start> <end> <json>    # text style, e.g. '{"bold":true}' or
                                              #   '{"link":{"url":"https://…"}}'
  gdocs batch   <doc> <json>                  # raw batchUpdate requests (escape hatch)

Without a subprocess – every command is a `Document` method:

    from gdocs import Document
    doc = Document(url_or_id)          # tab="…" scopes it, as --tab does
    doc.append("Text")
    doc.replace("alt", "neu")

`Document` returns data rather than printing and raises ToolError instead of
exiting, so a guard cannot take its caller down. `create()` is a module
function, since it has no document yet. `api()` hands out the raw service,
already retrying.

<doc> may be a full docs.google.com URL or a bare document id. Pass --tab=<id>
(ids come from `info`) to work on a tab other than the first – without it every
command reads and writes the first tab, which is the only one most docs have.
The exception is `replace`, which spans EVERY tab unless --tab narrows it, so on
a multi-tab document it changes text in tabs you never looked at.
A bare -- ends the flags, for text that itself starts with --tab=.
--dry-run prints the request a writing command would send, and sends nothing.
Reads still happen, so `append --dry-run` shows the index it really computed.

Every position is an index into the document, and EVERY EDIT SHIFTS THE ONES
AFTER IT. So:
  • Re-read `index` after each edit. An index measured before an insert points
    somewhere else afterwards – nothing errors, the text just lands wrong.
  • Within one `batch`, requests apply in order, each seeing what the previous
    one left behind. Order index-bearing requests back to front, highest index
    first; `batch` warns when it spots rising ones.
  • `replace` needs no indices at all, so it is the safer way to change text
    that is already there. It covers every tab unless --tab narrows it.
Indices count UTF-16 units: an emoji or other astral character advances the
index by 2 where Python's len() counts 1. `append`/`insert` report the range
they wrote, already counted that way.

`index` shortens each preview to 70 characters and marks the cut with …, so
match on a line's beginning, never on the whole – `get` prints the full text.

The body always ends in a newline that cannot be deleted or written past, so
the highest usable insert index is one below the document's end index –
`append` works that out for you. Text arrives exactly as given, so start it
with a newline when it should begin its own paragraph.

A paragraph started that way inherits the previous one's named style – append
under a TITLE and the new text is TITLE too, silently. Write all the text
first and style afterwards: styles shift no indices, so one `index` read still
covers every range.

Transient failures (429, 5xx) are retried with backoff on every call;
GAPI_RETRIES sets how many attempts (default 3, 0 disables, capped at 10).
A 429 naming a per-minute quota is instead sat out with fixed announced pauses
(GAPI_QUOTA_WAIT, default "20,40"; 0 turns it off).
GAPI_STATS=1 prints what a run spent against those quotas as it exits, per
API and split into reads and writes, with the count in its busiest 60 seconds –
the only figure a per-minute limit compares against – plus the payload sent.
Set it to a PATH and every process appends there instead, which is what makes
the peak true for a build running several at once; remove the file between runs.

There is no Markdown or HTML import: text goes in plain and structure is
applied afterwards with `style`/`format`. A converting Drive upload could do it
in one step, but needs a Drive scope this tool deliberately does not hold –
which is also why it cannot delete, move, rename or share a file.
"""

import json
import re
import sys

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gauth import (DOCS_SCOPE, DryRun, RetryingRequest, ToolError, account,
                   as_int, credentials, http_error_message, load_json, project,
                   require_scope, send, split_flags)

DOC_URL = "https://docs.google.com/document/d/{}/edit"


def doc_id(ref):
    m = re.search(r"/document/d/([A-Za-z0-9_-]+)", ref)
    return m.group(1) if m else ref


def api():
    require_scope(DOCS_SCOPE, "Google Docs")
    return build("docs", "v1", credentials=credentials(),
                 requestBuilder=RetryingRequest).documents()


def utf16_len(text):
    """Length in UTF-16 units – what the Docs API counts, unlike len()."""
    return len(text.encode("utf-16-le")) // 2


def tabs_of(doc):
    """Flatten the tab tree to [(tabId, title, content)].

    A document from before tabs existed reports no tabs at all, so it is served
    from the legacy top-level body instead and callers never special-case it.
    """
    found = []

    def walk(tabs):
        for tab in tabs or []:
            props = tab.get("tabProperties", {})
            body = tab.get("documentTab", {}).get("body", {})
            found.append((props.get("tabId", ""), props.get("title", ""),
                          body.get("content", [])))
            walk(tab.get("childTabs"))

    walk(doc.get("tabs"))
    if not found:
        found.append(("", "", doc.get("body", {}).get("content", [])))
    return found


def pick_tab(doc, want):
    tabs = tabs_of(doc)
    if not want:
        return tabs[0]
    for tab in tabs:
        if tab[0] == want:
            return tab
    raise ToolError(f"no tab {want!r} in this document – have "
                    f"{', '.join(repr(t[0]) for t in tabs)} (see `gdocs info`)")


def text_of(content):
    """Flatten structural elements to plain text, following tables and TOCs."""
    out = []
    for el in content or []:
        if "paragraph" in el:
            for e in el["paragraph"].get("elements", []):
                if "textRun" in e:
                    out.append(e["textRun"].get("content", ""))
                elif "person" in e:
                    out.append(e["person"].get("personProperties", {}).get("name", ""))
                elif "richLink" in e:
                    out.append(e["richLink"].get("richLinkProperties", {}).get("title", ""))
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                for cell in row.get("tableCells", []):
                    out.append(text_of(cell.get("content")))
        elif "tableOfContents" in el:
            out.append(text_of(el["tableOfContents"].get("content")))
    return "".join(out)


def named_style(el):
    return el.get("paragraph", {}).get("paragraphStyle", {}).get("namedStyleType", "")


def end_index(content):
    """Index just past the body's final newline, which cannot be written at."""
    return max((el.get("endIndex", 1) for el in content or []), default=1)


def location(index, tab):
    loc = {"index": index}
    if tab:
        loc["tabId"] = tab
    return loc


def span(start, end, tab):
    rng = {"startIndex": start, "endIndex": end}
    if tab:
        rng["tabId"] = tab
    return rng


def first_index(node, key=""):
    """The document index a request targets, or None when it has none.

    Only an index inside a Location or a Range is a document offset. Other
    fields are called "index" too – TabProperties.index is a tab's position
    among its siblings – and reading one of those as a position would warn
    about an ordering that does not exist.
    """
    if isinstance(node, dict):
        if "ocation" in key and isinstance(node.get("index"), int):
            return node["index"]
        if "ange" in key and isinstance(node.get("startIndex"), int):
            return node["startIndex"]
        for name, value in node.items():
            found = first_index(value, name)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = first_index(value, key)
            if found is not None:
                return found
    return None


def warn_index_order(requests):
    """Flag a batch whose later requests sit at higher indices than earlier ones.

    Requests apply in order, so an earlier edit moves the text a later one aimed
    at. Back-to-front order is immune by construction; any other order had to do
    the arithmetic itself, which is worth a second look.
    """
    idx = [i for i in (first_index(r) for r in requests) if i is not None]
    late = sum(1 for n, i in enumerate(idx) if any(j > i for j in idx[n + 1:]))
    if not late:
        return
    print(
        f"WARNING: {late} request(s) are followed by one at a higher index "
        f"(indices in order: {idx}). Requests apply in sequence and each edit shifts "
        f"everything after it, so the later ones land off by whatever the earlier ones "
        f"added or removed. Order them back to front – highest index first – unless "
        f"these already allow for the shift.",
        file=sys.stderr,
    )


class Document:
    """The commands as methods, so they can be used without a subprocess.

    Every guard the CLI applies applies here – the retry, the tab resolution,
    the index arithmetic described above – because the commands are these
    methods with argument parsing and printing wrapped around them.

        from gdocs import Document
        doc = Document("https://docs.google.com/document/d/…/edit")
        doc.append("Text")
        doc.replace("alt", "neu")

    Methods return data and raise ToolError on a refusal; nothing here prints
    or exits. `tab` scopes every call to one tab, as --tab does.
    """

    def __init__(self, doc, service=None, tab="", dry=False):
        self.id = doc_id(doc)
        if not self.id:
            raise ToolError("no document id given – pass a docs.google.com URL or a bare id")
        self.service = service or api()
        self.tab = tab
        self.dry = dry

    def raw(self):
        """The whole document, tab content included."""
        return self.service.get(documentId=self.id, includeTabsContent=True).execute()

    def tabs(self):
        """[(tabId, title, content)] over the whole tab tree."""
        return tabs_of(self.raw())

    def content(self):
        """The structural elements of the selected tab."""
        return pick_tab(self.raw(), self.tab)[2]

    def text(self):
        """The selected tab's text, tables and TOCs followed."""
        return text_of(self.content())

    def end(self):
        """The index just past the final newline of the selected tab."""
        return end_index(self.content())

    def append(self, text):
        """Add text at the end of the selected tab, returning [start, end)."""
        return self.insert(max(self.end() - 1, 1), text)

    def insert(self, at, text):
        send(self.service.batchUpdate(documentId=self.id, body={"requests": [
            {"insertText": {"location": location(at, self.tab), "text": text}}
        ]}), self.dry)
        return at, at + utf16_len(text)

    def replace(self, find, new):
        """Replace every occurrence – across ALL tabs unless one is selected."""
        req = {"replaceAllText": {
            "containsText": {"text": find, "matchCase": True},
            "replaceText": new,
        }}
        if self.tab:
            req["replaceAllText"]["tabsCriteria"] = {"tabIds": [self.tab]}
        result = send(self.service.batchUpdate(
            documentId=self.id, body={"requests": [req]}), self.dry)
        return (result.get("replies") or [{}])[0].get(
            "replaceAllText", {}).get("occurrencesChanged", 0)

    def delete(self, start, end):
        send(self.service.batchUpdate(documentId=self.id, body={"requests": [
            {"deleteContentRange": {"range": span(start, end, self.tab)}}
        ]}), self.dry)

    def style(self, start, end, named):
        """Set a paragraph's named style – TITLE, HEADING_1 … NORMAL_TEXT."""
        send(self.service.batchUpdate(documentId=self.id, body={"requests": [
            {"updateParagraphStyle": {
                "range": span(start, end, self.tab),
                "paragraphStyle": {"namedStyleType": named},
                "fields": "namedStyleType",
            }}
        ]}), self.dry)

    def format(self, start, end, style):
        """Set text style – only the keys given, everything else untouched."""
        if not isinstance(style, dict) or not style:
            raise ToolError("format: expected a non-empty style object, e.g. "
                            "'{\"bold\":true}' – an empty one would have to reset "
                            "every style to be meaningful")
        send(self.service.batchUpdate(documentId=self.id, body={"requests": [
            {"updateTextStyle": {
                "range": span(start, end, self.tab),
                "textStyle": style,
                # Named keys only: anything absent keeps whatever it had.
                "fields": ",".join(style),
            }}
        ]}), self.dry)
        return list(style)

    def batch(self, requests):
        """Raw documents.batchUpdate – warns on rising indices, as the CLI does."""
        warn_index_order(requests)
        result = send(self.service.batchUpdate(
            documentId=self.id, body={"requests": requests}), self.dry)
        return result.get("replies", [])


def create(title, service=None, dry=False):
    """Create a document in the token account's Drive, returning its URL."""
    doc = send((service or api()).create(body={"title": title}), dry)
    return DOC_URL.format(doc["documentId"])


def dispatch():
    values, flags, argv = split_flags(sys.argv, ("tab",), ("--dry-run",))
    if values["tab"] == "":
        # An unset shell variable expands to nothing, and silently writing to
        # the first tab is the wrong answer to --tab=$TAB
        raise ToolError("--tab= is empty – drop it for the first tab, or pass an id from "
                        "`gdocs info`")
    tab, dry = values["tab"] or "", "--dry-run" in flags

    if len(argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)

    cmd = argv[1]
    if tab and cmd in ("whoami", "create"):
        # Swallowing it would suggest the tab was honoured. `create` in particular
        # looks like it might place content, and it cannot.
        raise ToolError(f"--tab= does not apply to `{cmd}` – it names a tab inside a "
                        f"document that already exists")

    if cmd == "whoami":
        print(account() or "unknown – re-run auth.py to record the account")
        proj = project()
        if proj:
            print(f"project: {proj}")
        return

    if len(argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)

    args = argv[3:]
    needed = {"append": 1, "insert": 2, "replace": 2, "delete": 2,
              "style": 3, "format": 3, "batch": 1}.get(cmd, 0)
    if len(args) < needed:
        raise ToolError(f"{cmd}: expected {needed} argument(s) after <doc>, got "
                        f"{len(args)} – run `gdocs` for usage")

    if cmd == "create":
        print(create(argv[2], dry=dry))
        # stderr, so `gdocs create … | read url` cannot pick up this line
        print(f"created in the Drive of {account() or 'unknown account – see gdocs whoami'}",
              file=sys.stderr)
        return

    doc = Document(argv[2], tab=tab, dry=dry)

    if cmd == "info":
        raw = doc.raw()
        print(raw.get("title", "?"))
        for tab_id, title, content in tabs_of(raw):
            label = f"{title!r}  tabId={tab_id}" if tab_id else "(no tabs)"
            print(f"  {label}  end index={end_index(content)}")
            for el in content:
                style = named_style(el)
                if style == "TITLE" or style.startswith("HEADING"):
                    print(f"    {el.get('startIndex', 0):>6}  {style:<11} "
                          f"{text_of([el]).strip()[:60]}")
        print(f"  read as {account() or 'unknown account'}")

    elif cmd == "get":
        sys.stdout.write(doc.text())

    elif cmd == "index":
        content = doc.content()
        for el in content:
            kind = next((k for k in ("paragraph", "table", "tableOfContents", "sectionBreak")
                         if k in el), "?")
            style = named_style(el)
            tag = style if style and style != "NORMAL_TEXT" else kind
            body = text_of([el])
            # the … matters: a silent cut invites comparing against a full text
            shown = body[:70] + "…" if len(body) > 70 else body
            print(f"{el.get('startIndex', 0):>6} {el.get('endIndex', 0):>6}  {tag:<14} "
                  f"{shown!r}")
        print(f"# end index {end_index(content)}, so insert at "
              f"{max(end_index(content) - 1, 1)} at the latest", file=sys.stderr)

    elif cmd == "json":
        print(json.dumps(doc.raw(), ensure_ascii=False))

    elif cmd in ("append", "insert"):
        if cmd == "append":
            start, end = doc.append(args[0])
        else:
            start, end = doc.insert(as_int(args[0], "insert"), args[1])
        print(f"{cmd}: {end - start} chars at [{start}, {end})")

    elif cmd == "replace":
        print(f"replace: {doc.replace(args[0], args[1])} occurrence(s)")

    elif cmd == "delete":
        start, end = as_int(args[0], "delete"), as_int(args[1], "delete")
        doc.delete(start, end)
        print(f"deleted [{start}, {end})")

    elif cmd == "style":
        start, end = as_int(args[0], "style"), as_int(args[1], "style")
        doc.style(start, end, args[2])
        print(f"styled [{start}, {end}) as {args[2]}")

    elif cmd == "format":
        start, end = as_int(args[0], "format"), as_int(args[1], "format")
        keys = doc.format(start, end, load_json(args[2], "format"))
        print(f"formatted [{start}, {end}): {', '.join(keys)}")

    elif cmd == "batch":
        print(json.dumps(doc.batch(load_json(args[0], "batch")), ensure_ascii=False))

    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


def main():
    try:
        dispatch()
    except DryRun:
        pass          # send() already printed the request it did not make
    except ToolError as err:
        sys.exit(str(err))
    except HttpError as err:
        sys.exit(http_error_message(err))


if __name__ == "__main__":
    main()
