#!/usr/bin/env python3
"""
Webex chat history extractor.

Extracts all messages from Webex spaces (group) and one-to-one (direct) rooms,
organizing them into:

    <base>/spaces/<title>__<shortid>/
    <base>/one-to-one/<title>__<shortid>/

Each room folder contains:
    messages.jsonl   -- source of truth; one raw message per line, chronological.
    transcript.md    -- human-readable transcript, regenerated from messages.jsonl.
    room.json        -- room metadata as returned by the API.

Incremental: state is kept in <base>/_state/extraction_state.json. On re-runs
only messages newer than the last extraction are fetched and APPENDED to
messages.jsonl (transcript.md is regenerated). Each run is logged with its
UTC timestamp.

Usage:
    export WEBEX_TOKEN='<personal access token from developer.webex.com>'
    python3 extract_webex.py            # extract into the script's directory
    python3 extract_webex.py --base /path/to/output

Note: developer.webex.com personal access tokens expire ~12h after issue, so
re-runs need a fresh token pasted into WEBEX_TOKEN.
"""

import os
import sys
import json
import time
import base64
import re
import argparse
import datetime
import urllib.request
import urllib.error
import urllib.parse

API = "https://webexapis.com/v1"


def now_utc_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def short_id(room_id):
    """Derive a stable, short, unique-ish slug from a Webex room id."""
    try:
        raw = base64.b64decode(room_id + "===").decode("utf-8", "ignore")
        uuid = raw.rstrip("/").split("/")[-1]
        if uuid:
            return uuid[:8]
    except Exception:
        pass
    return room_id[-8:]


def sanitize(name, fallback="untitled"):
    if not name:
        name = fallback
    name = name.strip()
    # Replace path separators and characters awkward on filesystems.
    name = re.sub(r'[/\\:*?"<>|\n\r\t]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.strip(". ")  # no leading/trailing dots or spaces
    if not name:
        name = fallback
    return name[:120]


class Webex:
    def __init__(self, token):
        self.token = token

    def _request(self, url):
        """GET a URL, returning (json_body, next_url). Handles 429/5xx retries."""
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url)
            req.add_header("Authorization", "Bearer " + self.token)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    # strict=False tolerates raw control chars some clients
                    # leave unescaped inside Webex message text.
                    body = json.loads(resp.read().decode("utf-8"), strict=False)
                    link = resp.headers.get("Link", "")
                    return body, self._next_link(link)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry = int(e.headers.get("Retry-After", "5") or "5")
                    sys.stderr.write(
                        "  rate limited (429), sleeping %ds...\n" % retry
                    )
                    time.sleep(retry + 1)
                    continue
                if e.code in (500, 502, 503, 504) and attempt <= 5:
                    wait = min(2 ** attempt, 30)
                    sys.stderr.write(
                        "  server error %d, retry in %ds...\n" % (e.code, wait)
                    )
                    time.sleep(wait)
                    continue
                # 401/403/other: surface the body for debugging.
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "ignore")[:400]
                except Exception:
                    pass
                raise RuntimeError("HTTP %d for %s :: %s" % (e.code, url, detail))
            except urllib.error.URLError:
                if attempt <= 5:
                    wait = min(2 ** attempt, 30)
                    sys.stderr.write("  network error, retry in %ds...\n" % wait)
                    time.sleep(wait)
                    continue
                raise

    @staticmethod
    def _next_link(link_header):
        for part in link_header.split(","):
            m = re.search(r'<([^>]+)>\s*;\s*rel="next"', part)
            if m:
                return m.group(1)
        return None

    def list_rooms(self, room_type):
        url = "%s/rooms?type=%s&max=1000&sortBy=created" % (API, room_type)
        rooms = []
        while url:
            body, url = self._request(url)
            rooms.extend(body.get("items", []))
        return rooms

    def me(self):
        body, _ = self._request("%s/people/me" % API)
        return body

    def get_room(self, room_id):
        body, _ = self._request("%s/rooms/%s" % (API, urllib.parse.quote(room_id)))
        return body

    def list_teams(self):
        url = "%s/teams?max=100" % API
        items = []
        while url:
            body, url = self._request(url)
            items.extend(body.get("items", []))
        return items

    def list_memberships(self, room_id):
        url = "%s/memberships?roomId=%s&max=1000" % (
            API, urllib.parse.quote(room_id))
        items = []
        while url:
            body, url = self._request(url)
            items.extend(body.get("items", []))
        return items

    def iter_messages(self, room_id, stop_created=None, stop_id=None):
        """Yield messages newest-first; stop once we reach already-seen ones."""
        url = "%s/messages?roomId=%s&max=100" % (API, room_id)
        while url:
            body, url = self._request(url)
            for msg in body.get("items", []):
                if stop_id and msg.get("id") == stop_id:
                    return
                if stop_created and msg.get("created", "") <= stop_created:
                    return
                yield msg

    def download_file(self, url, dest_dir, prefix):
        """Download one attachment. Returns {url, filename, path} or None."""
        os.makedirs(dest_dir, exist_ok=True)
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url)
            req.add_header("Authorization", "Bearer " + self.token)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    filename = filename_from_headers(resp.headers, url)
                    safe = "%s_%s" % (sanitize_filename(prefix),
                                      sanitize_filename(filename))
                    path = os.path.join(dest_dir, safe)
                    if os.path.exists(path) and os.path.getsize(path) > 0:
                        return {"url": url, "filename": filename,
                                "path": path}
                    tmp = path + ".part"
                    with open(tmp, "wb") as out:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            out.write(chunk)
                    os.replace(tmp, path)
                    return {"url": url, "filename": filename, "path": path}
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry = int(e.headers.get("Retry-After", "5") or "5")
                    time.sleep(retry + 1)
                    continue
                if e.code in (500, 502, 503, 504) and attempt <= 4:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                sys.stderr.write("    attachment %s failed: HTTP %d\n"
                                 % (url, e.code))
                return None
            except urllib.error.URLError:
                if attempt <= 4:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                sys.stderr.write("    attachment %s failed: network\n" % url)
                return None


def filename_from_headers(headers, url):
    cd = headers.get("Content-Disposition", "") or ""
    # RFC 5987 filename*=UTF-8''... takes precedence.
    m = re.search(r"filename\*=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.I)
    if not m:
        m = re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        name = m.group(1).strip()
        try:
            name = urllib.parse.unquote(name)
        except Exception:
            pass
        if name:
            return name
    # Fallback: last path segment, else a generic name + extension guess.
    tail = url.rstrip("/").split("/")[-1] or "attachment"
    ctype = (headers.get("Content-Type", "") or "").split(";")[0].strip()
    ext = {
        "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
        "application/pdf": ".pdf", "text/plain": ".txt",
    }.get(ctype, "")
    if ext and not tail.lower().endswith(ext):
        tail += ext
    return tail


def sanitize_filename(name):
    name = re.sub(r'[/\\:*?"<>|\n\r\t]+', "_", name or "")
    name = name.strip(". ") or "file"
    return name[:150]


def load_state(state_path):
    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"runs": [], "rooms": {}}


def save_state(state_path, state):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, state_path)


def read_jsonl(path):
    out = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def render_transcript(room, messages, me_id, name_map=None, attach=None, title=None):
    name_map = name_map or {}
    attach = attach or {}
    title = title or room.get("title") or "(untitled)"
    lines = []
    lines.append("# %s" % title)
    lines.append("")
    lines.append("- Room type: %s" % room.get("type", "?"))
    lines.append("- Room id: `%s`" % room.get("id", ""))
    if room.get("created"):
        lines.append("- Room created: %s" % room["created"])
    lines.append("- Messages: %d" % len(messages))
    lines.append("")
    lines.append("---")
    lines.append("")
    for m in messages:
        ts = m.get("created", "")
        pid = m.get("personId")
        who = name_map.get(pid) or m.get("personEmail") or pid or "unknown"
        if pid and me_id and pid == me_id:
            who = "%s (me)" % who
        text = m.get("text")
        if not text and m.get("html"):
            text = re.sub(r"<[^>]+>", "", m["html"])
        text = (text or "").rstrip()
        edited = " (edited)" if m.get("updated") else ""
        prefix = "**[%s] %s%s:**" % (ts, who, edited)
        if m.get("parentId"):
            prefix = "> " + prefix  # threaded reply
        block = "%s %s" % (prefix, text) if text else prefix
        lines.append(block)
        for a in attach.get(m.get("id"), []):
            rel = os.path.join("files", os.path.basename(a.get("path", "")))
            lines.append("    - [attachment: %s](%s)" % (a.get("filename", rel), rel))
        # Attachment URLs present but not (yet) downloaded.
        if m.get("files") and not attach.get(m.get("id")):
            for f in m.get("files", []):
                lines.append("    - [attachment url] %s" % f)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def name_from_email(email):
    """Turn 'jane-mary.doe@example.com' into 'Jane-mary Doe'."""
    if not email or "@" not in email:
        return ""
    local = email.split("@", 1)[0]
    parts = [p for p in re.split(r"[._]+", local) if p]
    return " ".join(w[:1].upper() + w[1:] for w in parts) if parts else ""


def effective_title(room, members, me_id):
    """Room title, falling back for empty 1:1 titles to the other person's
    display name, or a name derived from their email. An email used as the
    title is prettified into a name too."""
    t = (room.get("title") or "").strip()
    if t:
        return (name_from_email(t) or t) if room.get("type") == "direct" else t
    if room.get("type") == "direct":
        for mb in members or []:
            if mb.get("personId") and mb.get("personId") != me_id:
                nm = (mb.get("personDisplayName")
                      or name_from_email(mb.get("personEmail"))
                      or mb.get("personEmail"))
                if nm:
                    return nm
    return t


def room_relpath(room, team_names, title=None):
    """On-disk location for a room:
       one-to-one/<title>__<id>, teams/<team>/<title>__<id>, or spaces/<title>__<id>.
    """
    rid = room["id"]
    title = sanitize(title if title is not None else room.get("title"),
                     fallback=room.get("type", "room"))
    leaf = "%s__%s" % (title, short_id(rid))
    tid = room.get("teamId") or ""
    if room.get("type") == "direct":
        return os.path.join("one-to-one", leaf)
    if tid and team_names.get(tid):
        return os.path.join("teams", sanitize(team_names[tid], "team"), leaf)
    return os.path.join("spaces", leaf)


def process_room(wx, room, base, team_names, state, me_id, run_ts,
                 download_attachments=True):
    rid = room["id"]

    # Membership first: needed to name empty-title 1:1 rooms before we pick a folder.
    members = []
    name_map = {}
    try:
        members = wx.list_memberships(rid)
        for mb in members:
            if mb.get("personId") and mb.get("personDisplayName"):
                name_map[mb["personId"]] = mb["personDisplayName"]
    except Exception as e:
        sys.stderr.write("    (memberships unavailable: %s)\n" % e)

    eff_title = effective_title(room, members, me_id)
    # Reuse this room's existing folder if we've seen it before; otherwise place
    # it by type: one-to-one/, teams/<team>/, or spaces/.
    prev = state["rooms"].get(rid, {})
    prev_rel = prev.get("folder")
    if prev_rel and os.path.isdir(os.path.join(base, prev_rel)):
        folder = os.path.join(base, prev_rel)
    else:
        folder = os.path.join(base, room_relpath(room, team_names, eff_title))
    os.makedirs(folder, exist_ok=True)
    jsonl_path = os.path.join(folder, "messages.jsonl")

    with open(os.path.join(folder, "room.json"), "w", encoding="utf-8") as f:
        json.dump(room, f, indent=2, ensure_ascii=False)
    with open(os.path.join(folder, "members.json"), "w", encoding="utf-8") as f:
        json.dump(members, f, indent=2, ensure_ascii=False)

    stop_created = prev.get("newest_created")
    stop_id = prev.get("newest_id")

    new_msgs = list(wx.iter_messages(rid, stop_created=stop_created, stop_id=stop_id))
    new_msgs.sort(key=lambda m: (m.get("created", ""), m.get("id", "")))

    # Recovery guard: if there is no saved state for this room but a jsonl
    # already exists, a previous run was interrupted mid-room. Drop any
    # message ids already on disk so we never append duplicates.
    if not prev and os.path.exists(jsonl_path):
        existing_ids = {m.get("id") for m in read_jsonl(jsonl_path)}
        if existing_ids:
            before = len(new_msgs)
            new_msgs = [m for m in new_msgs if m.get("id") not in existing_ids]
            if before != len(new_msgs):
                sys.stderr.write("    (recovery: skipped %d already-saved msg(s))\n"
                                 % (before - len(new_msgs)))

    if new_msgs:
        with open(jsonl_path, "a", encoding="utf-8") as f:
            for m in new_msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

    # Download attachments for new messages; track in attachments.json sidecar.
    attach_path = os.path.join(folder, "attachments.json")
    attach = {}
    if os.path.exists(attach_path):
        try:
            with open(attach_path, encoding="utf-8") as f:
                attach = json.load(f)
        except Exception:
            attach = {}
    n_files = 0
    if download_attachments:
        files_dir = os.path.join(folder, "files")
        for m in new_msgs:
            urls = m.get("files") or []
            if not urls:
                continue
            entries = []
            for idx, furl in enumerate(urls):
                info = wx.download_file(furl, files_dir, "%s_%d" % (m["id"], idx))
                if info:
                    entries.append({"url": info["url"],
                                    "filename": info["filename"],
                                    "path": os.path.relpath(info["path"], folder)})
                    n_files += 1
            if entries:
                attach[m["id"]] = entries
        if n_files:
            with open(attach_path, "w", encoding="utf-8") as f:
                json.dump(attach, f, indent=2, ensure_ascii=False)

    # Regenerate readable transcript from the full jsonl.
    all_msgs = read_jsonl(jsonl_path)
    all_msgs.sort(key=lambda m: (m.get("created", ""), m.get("id", "")))
    with open(os.path.join(folder, "transcript.md"), "w", encoding="utf-8") as f:
        f.write(render_transcript(room, all_msgs, me_id, name_map, attach,
                                  title=eff_title))

    newest = all_msgs[-1] if all_msgs else {}
    prev_files = state["rooms"].get(rid, {}).get("total_attachments", 0)
    state["rooms"][rid] = {
        "title": room.get("title"),
        "type": room.get("type"),
        "folder": os.path.relpath(folder, base),
        "newest_created": newest.get("created"),
        "newest_id": newest.get("id"),
        "total_messages": len(all_msgs),
        "total_attachments": prev_files + n_files,
        "last_extracted": run_ts,
    }
    return len(new_msgs), len(all_msgs), n_files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "webex-history"),
                    help="data directory (default: ./webex-history)")
    ap.add_argument("--only", choices=["spaces", "one-to-one"], help="limit scope")
    ap.add_argument("--limit", type=int, default=0,
                    help="process at most N rooms per scope (0 = all)")
    ap.add_argument("--rooms", default="",
                    help="comma-separated room IDs; overrides scope (for testing)")
    ap.add_argument("--no-attachments", action="store_true",
                    help="skip downloading file attachments")
    args = ap.parse_args()

    token = os.environ.get("WEBEX_TOKEN", "").strip()
    if not token:
        sys.stderr.write("ERROR: set WEBEX_TOKEN environment variable.\n")
        sys.exit(2)

    base = args.base
    os.makedirs(base, exist_ok=True)
    state_path = os.path.join(base, "_state", "extraction_state.json")
    state = load_state(state_path)

    wx = Webex(token)
    me = wx.me()
    me_id = me.get("id")
    run_ts = now_utc_iso()
    sys.stderr.write("Authenticated as %s <%s>\n" % (
        me.get("displayName"), (me.get("emails") or [""])[0]))
    sys.stderr.write("Extraction run timestamp (UTC): %s\n\n" % run_ts)

    # Fetch teams so spaces can be filed under teams/<team>/ and grouped in the UI.
    team_names = {}
    try:
        teams = wx.list_teams()
        with open(os.path.join(base, "teams.json"), "w", encoding="utf-8") as f:
            json.dump(teams, f, indent=2, ensure_ascii=False)
        team_names = {t["id"]: t.get("name") for t in teams if t.get("id")}
    except Exception as e:
        sys.stderr.write("WARN: could not fetch teams: %s\n" % e)

    # Build the work list: (subdir, [rooms]).
    scopes = [("group", "spaces"), ("direct", "one-to-one")]
    if args.only:
        scopes = [s for s in scopes if s[1] == args.only]

    if args.rooms:
        # Targeted test mode: fetch each room by id, route by its type.
        worklist = {"spaces": [], "one-to-one": []}
        for rid in [r.strip() for r in args.rooms.split(",") if r.strip()]:
            room = wx.get_room(rid)
            subdir = "spaces" if room.get("type") == "group" else "one-to-one"
            worklist[subdir].append(room)
        work = [(sd, rms) for sd, rms in worklist.items() if rms]
    else:
        work = []
        for room_type, subdir in scopes:
            rooms = wx.list_rooms(room_type)
            if args.limit:
                rooms = rooms[:args.limit]
            work.append((subdir, rooms))

    grand_new = 0
    grand_files = 0
    processed = 0
    run_summary = {"timestamp": run_ts, "rooms": 0, "new_messages": 0,
                   "attachments": 0}
    me_email = (me.get("emails") or [""])[0]

    # index.html goes to the data dir's parent (so the top level stays clean).
    html_dir = os.path.dirname(os.path.abspath(base))

    def refresh_browser():
        try:
            import build_browser
            build_browser.build(base, me_id=me_id, me_email=me_email,
                                 generated_at=run_ts, html_dir=html_dir)
        except Exception as e:
            sys.stderr.write("WARN: could not build browser: %s\n" % e)

    for subdir, rooms in work:
        sys.stderr.write("== %s: %d room(s) ==\n" % (subdir, len(rooms)))
        for i, room in enumerate(rooms, 1):
            title = room.get("title") or "(untitled)"
            try:
                n_new, n_total, n_files = process_room(
                    wx, room, base, team_names, state, me_id, run_ts,
                    download_attachments=not args.no_attachments)
            except Exception as e:
                sys.stderr.write("  [%d/%d] ERROR %s: %s\n" % (
                    i, len(rooms), title, e))
                continue
            grand_new += n_new
            grand_files += n_files
            processed += 1
            run_summary["rooms"] += 1
            flag = "  +%d new" % n_new if n_new else "  (no new)"
            if n_files:
                flag += ", +%d file(s)" % n_files
            sys.stderr.write("  [%d/%d] %-45.45s total=%d%s\n" % (
                i, len(rooms), title, n_total, flag))
            # Persist state incrementally so an interruption is recoverable.
            save_state(state_path, state)
            # Periodically refresh the browser so a mid-run refresh shows progress.
            if processed % 10 == 0:
                refresh_browser()

    run_summary["new_messages"] = grand_new
    run_summary["attachments"] = grand_files
    state["runs"].append(run_summary)
    save_state(state_path, state)

    # Download person avatars (skipped with --no-attachments).
    if not args.no_attachments:
        try:
            import fetch_avatars
            stats = fetch_avatars.fetch_avatars(
                base, token, progress=lambda s: sys.stderr.write(s + "\n"))
            sys.stderr.write("Avatars: %s\n" % stats)
        except Exception as e:
            sys.stderr.write("WARN: avatar fetch failed: %s\n" % e)

    # Refresh the offline browser (index.html + webex_data.js) with everything.
    refresh_browser()
    sys.stderr.write("Browser refreshed: %s\n" % os.path.join(html_dir, "index.html"))

    sys.stderr.write("\nDone. %d new message(s), %d attachment(s) this run.\n"
                     % (grand_new, grand_files))
    sys.stderr.write("State: %s\n" % state_path)
    print(run_ts)  # stdout: the extraction timestamp


if __name__ == "__main__":
    main()
