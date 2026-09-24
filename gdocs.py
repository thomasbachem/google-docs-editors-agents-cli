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
  gdocs comments <doc>                        # open comment threads, each with its text
                                              #   and index range (--all adds resolved, --json)
  gdocs reply   <doc> <comment-id> <text>     # answer a thread
  gdocs resolve <doc> <comment-id> [text]     # mark it resolved, optionally saying why
  gdocs reopen  <doc> <comment-id> [text]     # and back

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

Comments – the threads in the side panel – live in the Drive API, which does
not say where in the document one sits: its anchor is an id ("kix.yezgapnvz1yj")
the Docs API never mentions. So `comments` reads a DOCX export, which marks
where each open thread starts and ends, joins it to Drive's thread by creation
second, and lines the export's paragraphs up with the document's to turn that
into an index range and a tab – ready for `delete`, `format` or `--tab`. It
prints the text the thread is on now, and what it was made on where that
differs: Drive's quote is a snapshot. An image, footnote mark or page break in
that text reads as U+FFFC, one character for its one index. Measured: the range
follows a paragraph inserted above it, keeps out text inserted right before it,
stays on the new text when `replace` rewrites the whole passage, and ignores a
reply. Two things the export cannot say:
  • A thread whose text was DELETED is still open in Drive, and absent from the
    export – the Docs pane heads it "original content deleted". `comments`
    says its text was deleted.
  • A RESOLVED thread is absent from the export too, so where it was is
    unknown; only what it was made on is printed.
Three calls per listing, one for a document with no open thread. No command
CREATES a comment: Google documents that the editors treat one made through
the API as unanchored, and measured on a spreadsheet, the Comments pane then
heads it "original content deleted".

There is no Markdown or HTML import: text goes in plain and structure is
applied afterwards with `style`/`format`. A converting Drive upload could do it
in one step and is not built. The shared token holds a Drive scope, for
comments, yet no command here deletes, moves, renames or shares a file.
"""

import bisect
import difflib
import io
import json
import re
import sys
import zipfile
from collections import Counter
from xml.etree import ElementTree

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import gcomments
from gauth import (DOCS_SCOPE, DryRun, RetryingRequest, ToolError, Unreachable, account,
                   as_int, credentials, http_error_message, load_json, project,
                   require_scope, send, split_flags)

DOC_URL = "https://docs.google.com/document/d/{}/edit"

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"
W15 = "{http://schemas.microsoft.com/office/word/2012/wordml}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
# Formatting and table properties, whose children reuse names like w:tab (a tab stop)
DOCX_PROPERTIES = {W + name for name in ("pPr", "rPr", "sectPr", "tblPr", "trPr", "tcPr",
                                         "tblGrid")}
# An element holding one index and no text – an inline image, a footnote mark, a page
# break – as a character on both sides, so a position just before or after one maps
# exactly. The export writes them where they stand: w:drawing around wp:inline,
# w:footnoteReference and w:br type="page" (measured). A soft line break is
# w:br type="textWrapping", and a person chip a link reading the person's name, which is
# what paragraphs_of() takes from the Docs API too (both measured).
OBJECT = "\ufffc"
API_OBJECTS = ("inlineObjectElement", "footnoteReference", "pageBreak", "columnBreak")


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


def docx_comments(data):
    """The paragraphs of a DOCX export, and each comment with where it starts and ends.

    A position is (paragraph number, characters into its text). Paragraphs are
    counted in document order, table cells included; the export adds one per
    tab, styled Title and holding the tab's name, which the document itself does
    not have – the numbers of the Title-styled paragraphs come back too, so the
    tabs can be told apart. Of the formats Google exports, this one marks both
    ends of a comment and dates it to the second; it leaves resolved threads
    out, as the ODT and plain-text exports do (measured). A reply is an entry of
    its own, marked as one. Returns (paragraphs, entries, titled).
    """
    zipped = zipfile.ZipFile(io.BytesIO(data))
    names = set(zipped.namelist())
    paragraphs, starts, ends, pending, titled = [], {}, {}, [], set()

    def walk(node, inside):
        for el in node:
            if el.tag in DOCX_PROPERTIES:
                continue
            if el.tag == W + "p":
                paragraphs.append("")
                style = el.find(f"{W}pPr/{W}pStyle")
                if style is not None and style.get(W + "val") == "Title":
                    titled.add(len(paragraphs) - 1)
                # a range opened between paragraphs starts at the next one
                starts.update((cid, (len(paragraphs) - 1, 0)) for cid in pending)
                pending.clear()
                walk(el, True)
            elif el.tag in (W + "t", W + "delText") and inside:
                paragraphs[-1] += el.text or ""
            elif el.tag == W + "tab" and inside:
                paragraphs[-1] += "\t"
            elif el.tag == W + "br" and inside:
                # a line break is \u000b in the Docs API; a page break is an element of its own
                paragraphs[-1] += OBJECT if el.get(W + "type") in ("page", "column") else "\u000b"
            elif el.tag == W + "drawing" and inside:
                # a floating image holds no index in the Docs API; only an inline one does
                paragraphs[-1] += OBJECT if el.find(WP + "inline") is not None else ""
            elif el.tag == W + "footnoteReference" and inside:
                paragraphs[-1] += OBJECT
            elif el.tag == W + "commentRangeStart":
                if inside:
                    starts[el.get(W + "id")] = (len(paragraphs) - 1, len(paragraphs[-1]))
                else:
                    pending.append(el.get(W + "id"))
            elif el.tag == W + "commentRangeEnd":
                ends[el.get(W + "id")] = ((len(paragraphs) - 1, len(paragraphs[-1]))
                                          if paragraphs else (0, 0))
            else:
                walk(el, inside)

    walk(ElementTree.fromstring(zipped.read("word/document.xml")), False)
    parents = {}
    if "word/commentsExtended.xml" in names:
        for ex in ElementTree.fromstring(zipped.read("word/commentsExtended.xml")).iter(
                W15 + "commentEx"):
            parents[ex.get(W15 + "paraId")] = ex.get(W15 + "paraIdParent")
    entries = []
    if "word/comments.xml" in names:
        for item in ElementTree.fromstring(zipped.read("word/comments.xml")).iter(W + "comment"):
            cid, paras = item.get(W + "id"), item.findall(W + "p")
            entries.append({
                # "2026-09-14T17:51:07Z" – Drive's createdTime, truncated to the second
                "created": (item.get(W + "date") or "")[:19],
                "text": "\n".join("".join(t.text or "" for t in p.iter(W + "t")) for p in paras),
                "reply": any(parents.get(p.get(W14 + "paraId")) for p in paras),
                "start": starts.get(cid), "end": ends.get(cid)})
    return paragraphs, entries, titled


def paragraphs_of(doc):
    """Every paragraph in document order, tab after tab, table cells included.

    Each as (tabId, tab title, startIndex, endIndex, pieces), a piece being one
    element's (text as an export shows it, startIndex, whether it is a text run):
    what turns characters into an export's paragraph back into an index.
    """
    found = []

    def walk(content, tab, title):
        for el in content or []:
            if "paragraph" in el:
                pieces = []
                for e in el["paragraph"].get("elements", []):
                    at = e.get("startIndex", 0)
                    if "textRun" in e:
                        pieces.append((e["textRun"].get("content", ""), at, True))
                    elif "person" in e:
                        pieces.append((e["person"].get("personProperties", {}).get("name", ""),
                                       at, False))
                    elif "richLink" in e:
                        pieces.append((e["richLink"].get("richLinkProperties", {}).get("title", ""),
                                       at, False))
                    elif any(kind in e for kind in API_OBJECTS):
                        pieces.append((OBJECT, at, False))
                    else:
                        pieces.append(("", at, False))
                # the paragraph's own newline is no text an export shows
                if pieces and pieces[-1][2] and pieces[-1][0].endswith("\n"):
                    pieces[-1] = (pieces[-1][0][:-1], pieces[-1][1], True)
                found.append((tab, title, el.get("startIndex", 0), el.get("endIndex", 0), pieces))
            elif "table" in el:
                for row in el["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content"), tab, title)
            elif "tableOfContents" in el:
                walk(el["tableOfContents"].get("content"), tab, title)

    for tab, title, content in tabs_of(doc):
        walk(content, tab, title)
    return found


def index_at(pieces, offset):
    """The index `offset` characters into a paragraph as an export shows it."""
    for text, at, is_text in pieces:
        if is_text and offset <= len(text):
            return at + utf16_len(text[:offset])
        if not is_text and offset < len(text):
            return at
        offset -= len(text)
    return None


def pair_paragraphs(theirs, ours):
    """Which of the document's paragraphs each exported one is: (exact, near) index maps.

    `exact` pairs paragraphs that read the same; `near` pairs, one to one and in
    place, the paragraphs of a stretch that reads differently on both sides. A
    paragraph occurring once on each side pins the two lists together first –
    in order, the longest run of them that agrees – and only the gaps between
    those need more: difflib over a whole document of many empty paragraphs
    slows down quadratically, 25s for 12000 paragraphs, measured.

    A gap of the same length on both sides is paired by position – what the
    export spells differently, a link's title say, is then not taken for a
    neighbour that happens to read like it. A gap of different lengths goes
    through difflib, keeping only pairs whose text occurs once in that gap on
    each side: with a text there twice, which copy is which is a guess.
    """
    exact, near = {}, {}
    once_theirs, once_ours = Counter(theirs), Counter(ours)
    at_ours = {text: j for j, text in enumerate(ours) if once_ours[text] == 1}
    candidates = [(i, at_ours[text]) for i, text in enumerate(theirs)
                  if once_theirs[text] == 1 and text in at_ours]
    # the longest run of candidates rising on both sides, by patience sorting
    tops, links, back = [], [], {}
    for n, (_, j) in enumerate(candidates):
        k = bisect.bisect_left(tops, j)
        if k == len(tops):
            tops.append(j)
            links.append(n)
        else:
            tops[k], links[k] = j, n
        back[n] = links[k - 1] if k else None
    pins, n = [], links[-1] if links else None
    while n is not None:
        pins.append(candidates[n])
        n = back[n]
    i0 = j0 = 0
    for i1, j1 in [*reversed(pins), (len(theirs), len(ours))]:
        if i1 - i0 == j1 - j0:
            for i, j in zip(range(i0, i1), range(j0, j1)):
                (exact if theirs[i] == ours[j] else near)[i] = j
        else:
            gap_theirs, gap_ours = Counter(theirs[i0:i1]), Counter(ours[j0:j1])
            matcher = difflib.SequenceMatcher(None, theirs[i0:i1], ours[j0:j1], autojunk=False)
            for tag, a0, a1, b0, b1 in matcher.get_opcodes():
                if tag == "equal":
                    exact.update((i0 + a, j0 + b) for a, b in zip(range(a0, a1), range(b0, b1))
                                 if gap_theirs[theirs[i0 + a]] == gap_ours[ours[j0 + b]] == 1)
                elif tag == "replace" and a1 - a0 == b1 - b0:
                    near.update(zip(range(i0 + a0, i0 + a1), range(j0 + b0, j0 + b1)))
        if i1 < len(theirs):
            exact[i1] = j1
        i0, j0 = i1 + 1, j1 + 1
    return exact, near


def tab_stretches(theirs, titled, ours):
    """The export's paragraphs split by tab: [(their numbers, our numbers)] per tab.

    Each tab's part of the export opens with a Title paragraph holding its name,
    so pairing tab by tab keeps a run of empty paragraphs at the end of one tab
    from being paired with those at the start of the next. A tab's title is
    looked for where the paragraph counts put it; failing that, the Title
    paragraph of that name nearest there is taken, so one of the document's own
    that happens to carry the next tab's name earlier on does not cut in. Two
    equally near, or none, and the whole document is one stretch. So is a
    document of one tab – how it exports is not measured – less the title
    paragraph it may open with.
    """
    tabs = []
    for j, (tab, title, *_) in enumerate(ours):
        if not tabs or tabs[-1][0] != tab:
            tabs.append((tab, title, []))
        tabs[-1][2].append(j)
    whole = [(list(range(len(theirs))), list(range(len(ours))))]
    if len(tabs) < 2:
        if tabs and tabs[0][1] and 0 in titled and theirs and theirs[0] == tabs[0][1]:
            return [(list(range(1, len(theirs))), list(range(len(ours))))]
        return whole
    opens, at = [], 0
    for k, (_, title, mine) in enumerate(tabs):
        guess = opens[-1] + 1 + len(tabs[k - 1][2]) if opens else 0
        found = sorted((abs(i - guess), i) for i in range(at, len(theirs))
                       if i in titled and theirs[i] == title)
        if not found or (len(found) > 1 and found[0][0] == found[1][0]):
            return whole
        opens.append(found[0][1])
        at = found[0][1] + 1
    return [(list(range(opens[k] + 1, opens[k + 1] if k + 1 < len(opens) else len(theirs))), mine)
            for k, (_, _, mine) in enumerate(tabs)]


def place_doc_comments(comments, exported, doc):
    """Drive's threads, each with the tab, index range and text the export puts it on.

    `exported` is docx_comments()' result, or None when no export was read;
    `doc` the document with its tabs' content. `placement` says what is known:
      text       found in the export, its range turned into indices exactly
      paragraph  found, but its paragraphs read differently in the export than in
                 the document – an element not measured, a rich link say – so the
                 range is theirs, whole; the text it is on has to be in them, or it
                 is unknown
      deleted    open, yet absent from the export: its text was deleted, which is
                 how that has shown (measured) and what the Docs pane says
      resolved   a resolved thread, which no export carries – place unknown
      none       no anchor – a comment on the file, not on its text
      unknown    no export was read, or it could not be matched
    """
    paragraphs, entries, titled = exported or ([], [], set())
    roots = [e for e in entries if not e["reply"]]
    ours = paragraphs_of(doc) if doc else []
    exact, near = {}, {}
    for mine_theirs, mine_ours in tab_stretches(paragraphs, titled, ours):
        pairs = pair_paragraphs([paragraphs[i] for i in mine_theirs],
                                ["".join(p[0] for p in ours[j][4]) for j in mine_ours])
        for found, into in zip(pairs, (exact, near)):
            into.update((mine_theirs[i], mine_ours[j]) for i, j in found.items())

    # a resolved thread is never in the export (measured), so it competes for no entry
    unresolved = [c for c in comments if not c.get("resolved")]
    found = dict(zip(map(id, unresolved), gcomments.match_threads(unresolved, roots)))
    matches = [found.get(id(c)) for c in comments]
    claimed = {id(m) for m in matches if m is not None}
    placed = []
    for c, match in zip(comments, matches):
        tab_id = tab = start = end = passage = None
        if not c.get("anchor"):
            placement = "none"
        elif c.get("resolved"):
            placement = "resolved"
        elif match is None:
            second = (c.get("createdTime") or "")[:19]
            # an entry of its second that no thread claimed may be this one, told apart by
            # nothing but a text the export spells its own way – not an absence
            placement = ("deleted" if exported is not None and not any(
                e["created"] == second and id(e) not in claimed for e in roots) else "unknown")
        elif match["start"] is None or match["end"] is None:
            placement = "unknown"
        else:
            (p0, o0), (p1, o1) = match["start"], match["end"]
            passage = (paragraphs[p0][o0:o1] if p0 == p1 else "\n".join(
                [paragraphs[p0][o0:], *paragraphs[p0 + 1:p1], paragraphs[p1][:o1]]))
            j0, j1 = exact.get(p0), exact.get(p1)
            if j0 is not None and j1 is not None and ours[j0][0] == ours[j1][0] and None not in (
                    first := index_at(ours[j0][4], o0), last := index_at(ours[j1][4], o1)):
                placement, (tab_id, tab), start, end = "text", ours[j0][:2], first, last
            else:
                j0, j1 = exact.get(p0, near.get(p0)), exact.get(p1, near.get(p1))
                lines = passage.split("\n")
                # a pairing by position alone proves nothing, so the text has to be there
                if (passage and j0 is not None and j1 is not None and ours[j0][0] == ours[j1][0]
                        and lines[0] in "".join(p[0] for p in ours[j0][4])
                        and lines[-1] in "".join(p[0] for p in ours[j1][4])):
                    placement, (tab_id, tab) = "paragraph", ours[j0][:2]
                    start, end = ours[j0][2], ours[j1][3]
                else:
                    placement = "unknown"
        placed.append({"id": c.get("id"), "tab": tab, "tab_id": tab_id, "start": start,
                       "end": end, "placement": placement, "passage": passage,
                       **gcomments.record(c)})
    return placed


def comment_lines(c):
    """One thread as the `comments` command prints it."""
    at = f"{c['tab']!r} (--tab={c['tab_id']}) " if c["tab_id"] else ""
    where = {"text": f"{at}[{c['start']}, {c['end']})",
             "paragraph": f"{at}[{c['start']}, {c['end']}) (its paragraphs – the exact range "
                          f"could not be confirmed)",
             "deleted": "(its text was deleted)",
             "resolved": "(place unknown – a resolved thread leaves the export)",
             "none": "(no text – a comment on the file)",
             "unknown": "(place unknown)"}[c["placement"]]
    if c["passage"] is not None:
        on = f"on {c['passage']!r}"
        remark = on + (f", made on {c['quoted']!r}" if c["quoted"] and c["quoted"] != c["passage"]
                       else "")
    else:
        remark = f"made on {c['quoted']!r}" if c["quoted"] else ""
    return gcomments.thread_lines(where, c, remark)


class Document(gcomments.Threads):
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

    def __init__(self, doc, service=None, tab="", dry=False, drive=None):
        self.id = doc_id(doc)
        if not self.id:
            raise ToolError("no document id given – pass a docs.google.com URL or a bare id")
        self.service = service or api()
        self.tab = tab
        self.dry = dry
        self._drive = drive

    def api(self):
        """The service this drives, for anything these methods do not cover.

        Same object as the module-level api(), reached from an instance –
        `doc.api()` is the obvious guess, and guessing wrong costs a call.
        """
        return self.service

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

    def comments(self):
        """Every comment thread, open and resolved, with its tab, index range and text.

        Three calls for up to 100 threads: the threads, a DOCX export to place
        them and one read of the document to turn the export's positions into
        indices. The export is skipped when no thread is open and anchored – it
        leaves resolved ones out anyway – and the read when the export holds
        none. Every tab at once, whatever `tab` this Document was given. See
        place_doc_comments() for what each placement means.
        """
        found = self.threads()
        exported = doc = None
        if any(c.get("anchor") and not c.get("resolved") for c in found):
            exported = self._exported("DOCX", DOCX, docx_comments,
                                      "the threads are listed without a place")
            if exported and exported[1]:
                doc = self.raw()
        return place_doc_comments(found, exported, doc)


def create(title, service=None, dry=False):
    """Create a document in the token account's Drive, returning its URL."""
    doc = send((service or api()).create(body={"title": title}), dry)
    return DOC_URL.format(doc["documentId"])


def dispatch():
    values, flags, argv = split_flags(sys.argv, ("tab",), ("--dry-run", "--json", "--all"))
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
    if tab and cmd in gcomments.COMMANDS:
        raise ToolError(f"--tab= does not apply to `{cmd}` – a thread belongs to the whole "
                        f"document, and `comments` names the tab each one is in")
    if "--json" in flags and cmd != "comments":
        raise ToolError("--json applies to `comments` – for the document, `json` is its own "
                        "command")
    if "--all" in flags and cmd != "comments":
        raise ToolError("--all applies to `comments`")

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
              "style": 3, "format": 3, "batch": 1, **gcomments.NEEDED}.get(cmd, 0)
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

    elif cmd in gcomments.COMMANDS:
        gcomments.run_command(cmd, doc, args, flags, comment_lines)

    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


def main():
    try:
        dispatch()
    except DryRun:
        pass          # send() already printed the request it did not make
    except (ToolError, Unreachable) as err:
        sys.exit(str(err))
    except HttpError as err:
        sys.exit(http_error_message(err))


if __name__ == "__main__":
    main()
