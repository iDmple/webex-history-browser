#!/usr/bin/env python3
"""
Build a static, offline browser for the extracted Webex history.

Scans <base>/spaces/ and <base>/one-to-one/ for room folders (each containing
messages.jsonl + room.json) and produces:

    <base>/webex_data.js   -- window.WEBEX_DATA = {...} (loaded by index.html)
    <base>/index.html      -- self-contained browser with global text search

Open index.html directly in a browser (double-click); no server needed.

Usage:
    python3 build_browser.py [--base DIR]
Also called automatically at the end of extract_webex.py.
"""

import os
import sys
import re
import json
import argparse
import datetime


IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg")
AUDIO_EXTS = (".m4a", ".mp3", ".wav", ".ogg", ".oga", ".opus", ".aac", ".weba")


def name_from_email(email):
    """Turn 'jane-mary.doe@example.com' into 'Jane-mary Doe'."""
    if not email or "@" not in email:
        return ""
    local = email.split("@", 1)[0]
    parts = [p for p in re.split(r"[._]+", local) if p]
    if not parts:
        return ""
    return " ".join(w[:1].upper() + w[1:] for w in parts)


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def read_jsonl(path):
    out = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return out


def _room_folders(base):
    """Yield (folder, kind) for every room folder, across the layout variants.

    Handles both the flat `spaces/<room>/` layout and the grouped
    `teams/<team>/<room>/` layout, plus `one-to-one/<room>/`.
    """
    for name in sorted(_listdir(os.path.join(base, "one-to-one"))):
        p = os.path.join(base, "one-to-one", name)
        if os.path.isdir(p):
            yield p, "direct"
    for name in sorted(_listdir(os.path.join(base, "spaces"))):
        p = os.path.join(base, "spaces", name)
        if os.path.isdir(p):
            yield p, "space"
    teams_root = os.path.join(base, "teams")
    for team in sorted(_listdir(teams_root)):
        troot = os.path.join(teams_root, team)
        if not os.path.isdir(troot):
            continue
        for name in sorted(_listdir(troot)):
            p = os.path.join(troot, name)
            if os.path.isdir(p):
                yield p, "space"


def _listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def collect(base, me_id, me_email):
    # teamId -> team name, so spaces can be grouped under their team.
    team_names = {}
    for t in _load_json(os.path.join(base, "teams.json"), []) or []:
        if t.get("id"):
            team_names[t["id"]] = t.get("name") or "(team)"

    # Global person registry (name + downloaded avatar path), shared by all rooms.
    ppl_meta = _load_json(os.path.join(base, "people.json"), {})  # pid -> {name,avatar}
    people = []          # [{n:name, a:avatar}]
    people_idx = {}      # pid -> index

    def person_index(pid, fallback_name):
        key = pid or ("?" + (fallback_name or ""))
        if key not in people_idx:
            info = ppl_meta.get(pid, {}) if pid else {}
            rec = {"n": info.get("name") or fallback_name or "unknown"}
            if info.get("avatar"):
                rec["a"] = info["avatar"]
            people_idx[key] = len(people)
            people.append(rec)
        return people_idx[key]

    rooms = []
    for folder, kind in _room_folders(base):
        room_meta = _load_json(os.path.join(folder, "room.json"), {})
        attach = _load_json(os.path.join(folder, "attachments.json"), {})
        members = _load_json(os.path.join(folder, "members.json"), [])
        name_map = {}
        for mb in members if isinstance(members, list) else []:
            if mb.get("personId") and mb.get("personDisplayName"):
                name_map[mb["personId"]] = mb["personDisplayName"]
        rel_folder = os.path.relpath(folder, base).replace("\\", "/")
        msgs = read_jsonl(os.path.join(folder, "messages.jsonl"))
        msgs.sort(key=lambda m: (m.get("created", ""), m.get("id", "")))
        slim = []
        for m in msgs:
            pid = m.get("personId")
            who = (name_map.get(pid) or name_from_email(m.get("personEmail"))
                   or m.get("personEmail") or pid or "unknown")
            is_me = bool(me_id and pid == me_id) or (
                me_email and m.get("personEmail") == me_email)
            atts = []
            for a in attach.get(m.get("id"), []) or []:
                p = a.get("path", "")
                if p:
                    nm = a.get("filename", p)
                    entry = {"n": nm, "p": (rel_folder + "/" + p).replace("\\", "/")}
                    ext = os.path.splitext(nm)[1].lower()
                    if ext in IMG_EXTS:
                        entry["img"] = 1
                    elif ext in AUDIO_EXTS:
                        entry["aud"] = 1
                    atts.append(entry)
            rec = {
                "t": m.get("created", ""),
                "w": who,
                "m": 1 if is_me else 0,
                "p": person_index(pid, who),
                "x": m.get("text") or "",
                "r": 1 if m.get("parentId") else 0,
            }
            if atts:
                rec["a"] = atts
            elif m.get("files"):
                rec["a"] = [{"n": "[%d attachment(s), not downloaded]"
                             % len(m["files"]), "p": ""}]
            slim.append(rec)
        # Determine the counterpart (for 1:1) once, to reuse for title + avatar.
        other = None
        other_name = ""
        if kind == "direct":
            for mb in members if isinstance(members, list) else []:
                if mb.get("personId") and mb.get("personId") != me_id:
                    other = mb["personId"]
                    other_name = (mb.get("personDisplayName")
                                  or name_from_email(mb.get("personEmail"))
                                  or mb.get("personEmail") or "")
                    break
        # Resolve a real title. Webex uses "" or the literal "Empty Title" as a
        # placeholder for 1:1 rooms, and sometimes the raw email.
        raw = (room_meta.get("title") or "").strip()
        resolved = ("" if raw.lower() in ("", "empty title") else raw) or other_name
        if not resolved:  # last resort: a non-me message sender's name
            for mm in slim:
                if not mm.get("m") and mm.get("w") and mm["w"] != "unknown":
                    resolved = mm["w"]
                    break
        # Hide placeholder-title rooms that have no messages.
        if not resolved and not slim:
            continue
        title = resolved or os.path.basename(folder)
        if kind == "direct":
            title = name_from_email(title) or title  # prettify an email title
        team_id = room_meta.get("teamId") or ""
        room = {
            "title": title,
            "kind": kind,
            "folder": rel_folder,
            "created": room_meta.get("created", ""),
            "count": len(slim),
            "teamId": team_id,
            "team": team_names.get(team_id, ""),
            "messages": slim,
        }
        if kind == "direct":
            room["p"] = person_index(other, title) if other else -1
        rooms.append(room)

    # Spaces first, then direct; within each, most recently active first.
    def last_ts(r):
        return r["messages"][-1]["t"] if r["messages"] else r.get("created", "")
    spaces = sorted([r for r in rooms if r["kind"] == "space"],
                    key=last_ts, reverse=True)
    direct = sorted([r for r in rooms if r["kind"] == "direct"],
                    key=last_ts, reverse=True)
    return spaces + direct, people


def build(base, me_id=None, me_email=None, generated_at=None, html_dir=None):
    """Build the offline browser.

    Data (webex_data.js + all room/avatar/attachment files) lives under `base`.
    index.html is written to `html_dir` (defaults to `base`). When html_dir is a
    parent of base, every resource URL is prefixed with the relative path from
    html_dir to base so the page resolves them correctly.
    """
    generated_at = generated_at or datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    html_dir = html_dir or base
    prefix = os.path.relpath(base, html_dir).replace("\\", "/")
    prefix = "" if prefix == "." else prefix + "/"

    rooms, people = collect(base, me_id, me_email)
    total_msgs = sum(r["count"] for r in rooms)
    data = {
        "generatedAt": generated_at,
        "prefix": prefix,  # prepended to every resource URL by the page
        "totals": {
            "spaces": sum(1 for r in rooms if r["kind"] == "space"),
            "direct": sum(1 for r in rooms if r["kind"] == "direct"),
            "messages": total_msgs,
        },
        "people": people,
        "rooms": rooms,
    }
    js_path = os.path.join(base, "webex_data.js")
    with open(js_path, "w", encoding="utf-8") as f:
        f.write("window.WEBEX_DATA = ")
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    html_path = os.path.join(html_dir, "index.html")
    html = INDEX_HTML.replace('src="webex_data.js"',
                              'src="%swebex_data.js"' % prefix)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    return js_path, html_path, data["totals"]


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Webex History</title>
<style>
  :root {
    --bg: #f5f6f8; --panel: #ffffff; --panel-2: #f0f2f5; --border: #e2e5ea;
    --text: #1f2329; --muted: #6b7280; --accent: #0b8f7a; --accent-soft: #d9f2ec;
    --me: #e8f0fe; --hit: #fff3bf; --shadow: 0 1px 3px rgba(0,0,0,.08);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #16181d; --panel: #1e2127; --panel-2: #23272e; --border: #2c3038;
      --text: #e6e8eb; --muted: #9aa0aa; --accent: #2fd6b8; --accent-soft: #143b34;
      --me: #1c2a3a; --hit: #4d4320; --shadow: 0 1px 3px rgba(0,0,0,.4);
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text); display: flex; flex-direction: column;
  }
  header {
    padding: 10px 14px; background: var(--panel); border-bottom: 1px solid var(--border);
    display: flex; gap: 12px; align-items: center; flex-wrap: wrap; box-shadow: var(--shadow);
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; white-space: nowrap; }
  header .meta { color: var(--muted); font-size: 12px; }
  .searchwrap {
    flex: 1; min-width: 220px; display: flex; align-items: center; gap: 6px;
    padding: 4px 6px 4px 8px; border: 1px solid var(--border); border-radius: 8px;
    background: var(--panel-2);
  }
  .searchwrap:focus-within { outline: 2px solid var(--accent); border-color: var(--accent); }
  #search {
    flex: 1; min-width: 80px; padding: 4px 2px; border: 0; background: transparent;
    color: var(--text); font-size: 14px;
  }
  #search:focus { outline: none; }
  .scope-chip {
    display: none; align-items: center; gap: 5px; flex-shrink: 0;
    padding: 3px 6px 3px 4px; border-radius: 6px; background: var(--accent-soft);
    color: var(--text); font-size: 12px; max-width: 220px;
  }
  .scope-chip.on { display: inline-flex; }
  .scope-chip .cname {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 150px;
  }
  .scope-chip .x {
    cursor: pointer; color: var(--muted); font-weight: 700; line-height: 1;
    padding: 0 2px; border-radius: 4px;
  }
  .scope-chip .x:hover { color: var(--text); background: rgba(128,128,128,.2); }
  .scope-chip .avatar { width: 16px; height: 16px; font-size: 9px; flex-shrink: 0; }
  .layout { flex: 1; display: flex; min-height: 0; }
  aside {
    width: 300px; flex-shrink: 0; background: var(--panel);
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column; min-height: 0;
  }
  .splitter {
    width: 6px; flex-shrink: 0; cursor: col-resize; background: transparent;
    border-right: 1px solid var(--border); margin-left: -1px;
  }
  .splitter:hover, .splitter.dragging { background: var(--accent); }
  #roomfilter {
    margin: 10px; padding: 7px 10px; border: 1px solid var(--border); border-radius: 8px;
    background: var(--panel-2); color: var(--text); font-size: 13px;
  }
  .roomlist { overflow-y: auto; flex: 1; padding-bottom: 20px; }
  .group-label {
    padding: 9px 14px 5px; font-size: 11px; text-transform: uppercase;
    letter-spacing: .06em; color: var(--muted); font-weight: 600; position: sticky;
    top: 0; background: var(--panel); z-index: 1;
  }
  .group-label.section {
    cursor: pointer; display: flex; align-items: center; gap: 6px; user-select: none;
  }
  .group-label.section:hover { color: var(--text); }
  .group-label .caret {
    display: inline-block; width: 9px; flex-shrink: 0; transition: transform .12s ease;
  }
  .group-label.section.open .caret { transform: rotate(90deg); }
  .group-label .stitle { flex: 1; }
  .group-label .scount { font-weight: 400; }
  .room {
    padding: 7px 14px; cursor: pointer; border-left: 3px solid transparent;
    display: flex; gap: 8px; align-items: center;
  }
  .room:hover { background: var(--panel-2); }
  .room.active { background: var(--accent-soft); border-left-color: var(--accent); }
  .room .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .room .count { color: var(--muted); font-size: 11px; flex-shrink: 0; }
  .room.child { padding-left: 26px; }
  /* Avatar: colored circle with the initial, real photo layered on top
     (photo hides itself on error, revealing the initial). */
  .avatar, .mav {
    position: relative; overflow: hidden; border-radius: 50%; flex-shrink: 0;
    display: inline-flex; align-items: center; justify-content: center;
    color: #fff; font-weight: 700;
  }
  .avatar { width: 20px; height: 20px; font-size: 10px; }
  .mav { width: 28px; height: 28px; font-size: 12px; margin-top: 1px; }
  .avatar img, .mav img {
    position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover;
  }
  .room.direct .name { font-weight: 500; }
  /* Teams: bold, collapsible, with a people icon + subtle tinted band. */
  .team {
    padding: 8px 14px; cursor: pointer; display: flex; align-items: center;
    gap: 6px; font-weight: 700; user-select: none;
    background: linear-gradient(var(--panel-2), var(--panel-2)) no-repeat;
  }
  .team:hover { filter: brightness(0.97); }
  .team .caret {
    display: inline-block; width: 10px; color: var(--muted); flex-shrink: 0;
    transition: transform .12s ease; font-size: 11px;
  }
  .team.open .caret { transform: rotate(90deg); }
  .team .tico { width: 16px; height: 16px; flex-shrink: 0; color: var(--accent); }
  .team .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .team .count { color: var(--muted); font-size: 11px; font-weight: 400; flex-shrink: 0; }
  main { flex: 1; overflow-y: auto; padding: 18px 22px; min-width: 0; }
  .room-title { font-size: 18px; font-weight: 600; margin: 0 0 2px; }
  .room-sub { color: var(--muted); font-size: 12px; margin-bottom: 16px; }
  /* All messages left-aligned; my own get a subtle background tint. */
  .msg {
    display: flex; gap: 9px; align-items: flex-start; margin: 3px 0; max-width: 900px;
    padding: 6px 10px; border-radius: 8px;
  }
  .msg.me { background: var(--me); }
  .msg.reply { border-left: 3px solid var(--border); margin-left: 18px; }
  .msg .mbody { flex: 1; min-width: 0; }
  .msg .head { font-size: 12px; color: var(--muted); margin-bottom: 1px; }
  .msg .who { color: var(--accent); font-weight: 600; }
  .msg .body { white-space: pre-wrap; word-wrap: break-word; }
  .msg .att { margin-top: 4px; font-size: 12px; }
  .msg .att a { color: var(--accent); text-decoration: none; }
  .msg .att a:hover { text-decoration: underline; }
  .msg .att img {
    display: block; max-width: 260px; max-height: 260px; width: auto; height: auto;
    border-radius: 8px; border: 1px solid var(--border);
  }
  .msg .att-aud audio { height: 34px; max-width: 260px; vertical-align: middle; }
  .att-meta { display: flex; align-items: center; gap: 8px; margin-top: 2px; }
  .att-meta .fn {
    color: var(--muted); overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; max-width: 180px;
  }
  .att .dl { color: var(--accent); white-space: nowrap; }
  .empty { color: var(--muted); padding: 40px; text-align: center; }
  mark { background: var(--hit); color: inherit; border-radius: 3px; padding: 0 1px; }
  .result-room { font-weight: 600; margin: 16px 0 4px; color: var(--accent); cursor: pointer; }
  .result {
    padding: 6px 10px; border-radius: 8px; margin: 2px 0; cursor: pointer;
    border: 1px solid transparent;
  }
  .result:hover { background: var(--panel-2); border-color: var(--border); }
  .result.me { background: var(--me); }
  .result.me:hover { background: var(--me); border-color: var(--border); }
  .result .rmeta { font-size: 11px; color: var(--muted); }
  .flash { animation: flash 1.6s ease; }
  @keyframes flash { 0%,30% { background: var(--hit); } 100% { background: transparent; } }
  @media (max-width: 720px) { aside { width: 190px; } }
</style>
</head>
<body>
<header>
  <h1>Webex History</h1>
  <span class="meta" id="hmeta"></span>
  <div class="searchwrap">
    <span class="scope-chip" id="scopeChip"></span>
    <input id="search" type="search" placeholder="Search all messages…" autocomplete="off">
  </div>
</header>
<div class="layout">
  <aside>
    <input id="roomfilter" type="search" placeholder="Filter rooms by name…" autocomplete="off">
    <div class="roomlist" id="roomlist"></div>
  </aside>
  <div class="splitter" id="splitter" title="Drag to resize"></div>
  <main id="main"><div class="empty">Select a room, or search above.</div></main>
</div>
<script src="webex_data.js"></script>
<script>
(function () {
  var D = window.WEBEX_DATA || { rooms: [], totals: {}, generatedAt: "" };
  var rooms = D.rooms || [];
  rooms.forEach(function (r, i) { r._i = i; });
  var main = document.getElementById("main");
  var roomlist = document.getElementById("roomlist");
  var searchBox = document.getElementById("search");
  var roomFilter = document.getElementById("roomfilter");
  var scopeChip = document.getElementById("scopeChip");
  var active = -1;
  var scopeRoom = -1;          // room index the search is scoped to (-1 = all)
  var expanded = {};           // teamId -> true when its rooms are shown
  var sectionsCollapsed = {};  // 'teams'|'spaces'|'direct' -> true when hidden
  var PEOPLE = D.people || [];  // [{n:name, a:avatar-path}]
  var PREFIX = D.prefix || "";  // path from index.html to the data folder
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var TEAM_SVG = '<svg class="tico" viewBox="0 0 24 24" fill="currentColor">' +
    '<path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5s-3 1.34-3 3 1.34 3 3 3zm' +
    '-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 ' +
    '1.17-7 3.5V19h14v-2.5C15 14.17 10.33 13 8 13zm8 0c-.29 0-.62.02-.97.05C15.64 ' +
    '13.36 17 14.28 17 16.5V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>';

  function avatarColor(s) {
    var h = 0;
    for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return "hsl(" + (h % 360) + ",42%,45%)";
  }
  function avatarHtml(name, idx, cls) {
    var p = (idx != null && idx >= 0) ? PEOPLE[idx] : null;
    var init = ((name || "?").trim()[0] || "?").toUpperCase();
    var s = '<span class="' + cls + '" style="background:' + avatarColor(name || "") +
      '">' + esc(init);
    if (p && p.a) {
      s += '<img src="' + esc(PREFIX + p.a) + '" alt="" loading="lazy" ' +
        "onerror=\"this.style.display='none'\">";
    }
    return s + "</span>";
  }

  document.getElementById("hmeta").textContent =
    (D.totals.spaces || 0) + " spaces · " + (D.totals.direct || 0) +
    " one-to-one · " + (D.totals.messages || 0) + " messages · extracted " +
    (D.generatedAt ? fmtTime(D.generatedAt) : "");

  function esc(s) {
    return (s || "").replace(/[&<>]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
    });
  }
  function fmtTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return esc(iso);
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getDate() + " " + MONTHS[d.getMonth()] + " " + d.getFullYear() +
      ", " + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function hl(s, q) {
    s = esc(s);
    if (!q) return s;
    var idx = s.toLowerCase().indexOf(q.toLowerCase());
    if (idx < 0) return s;
    return s.slice(0, idx) + "<mark>" + s.slice(idx, idx + q.length) +
      "</mark>" + s.slice(idx + q.length);
  }

  function matches(r, f) { return !f || r.title.toLowerCase().indexOf(f) >= 0; }
  function roomHtml(r, child) {
    // Spaces (standalone and inside teams) get a colored first-letter circle,
    // just like a one-to-one with no photo.
    var icon = r.kind === "direct"
      ? avatarHtml(r.title, r.p, "avatar")
      : avatarHtml(r.title, -1, "avatar");
    return '<div class="room ' + (r.kind === "direct" ? "direct" : "space") +
      (child ? " child" : "") + (r._i === active ? " active" : "") +
      '" data-i="' + r._i + '">' + icon + '<span class="name">' + esc(r.title) +
      '</span><span class="count">' + r.count + "</span></div>";
  }
  function sectionHeader(key, title, count, open) {
    return '<div class="group-label section' + (open ? " open" : "") +
      '" data-section="' + key + '"><span class="caret">&#9656;</span>' +
      '<span class="stitle">' + title + '</span><span class="scount">' +
      count + "</span></div>";
  }
  function teamHeaderHtml(tid, t, open) {
    return '<div class="team' + (open ? " open" : "") + '" data-team="' + esc(tid) +
      '"><span class="caret">&#9656;</span>' + TEAM_SVG + '<span class="name">' +
      esc(t.name) + '</span><span class="count">' + t.rooms.length + "</span></div>";
  }

  function renderRoomList() {
    var f = roomFilter.value.trim().toLowerCase();
    var filtering = !!f;
    var html = "";
    var spaces = rooms.filter(function (r) { return r.kind === "space"; });

    // Group team-affiliated spaces by their team.
    var byTeam = {}, teamOrder = [];
    spaces.forEach(function (r) {
      if (!r.teamId || !r.team) return;  // unresolved team -> treat as standalone
      if (!byTeam[r.teamId]) {
        byTeam[r.teamId] = { name: r.team, rooms: [] };
        teamOrder.push(r.teamId);
      }
      byTeam[r.teamId].rooms.push(r);
    });
    teamOrder.sort(function (a, b) {
      return byTeam[a].name.toLowerCase().localeCompare(byTeam[b].name.toLowerCase());
    });

    // --- Teams section (collapsible header; each team also collapsible) ---
    var visTeams = teamOrder.filter(function (tid) {
      if (!filtering) return true;
      var t = byTeam[tid];
      return t.name.toLowerCase().indexOf(f) >= 0 ||
        t.rooms.some(function (r) { return matches(r, f); });
    });
    if (visTeams.length) {
      var openTeams = filtering || !sectionsCollapsed.teams;
      html += sectionHeader("teams", "Teams", visTeams.length, openTeams);
      if (openTeams) {
        visTeams.forEach(function (tid) {
          var t = byTeam[tid];
          var nameHit = t.name.toLowerCase().indexOf(f) >= 0;
          var kids = filtering ? (nameHit ? t.rooms : t.rooms.filter(function (r) {
            return matches(r, f); })) : t.rooms;
          var open = expanded[tid] || filtering;
          html += teamHeaderHtml(tid, t, open);
          if (open) kids.forEach(function (r) { html += roomHtml(r, true); });
        });
      }
    }

    // --- Spaces section (standalone, no team) ---
    var loose = spaces.filter(function (r) {
      return (!r.teamId || !r.team) && matches(r, f); });
    if (loose.length) {
      var openSpaces = filtering || !sectionsCollapsed.spaces;
      html += sectionHeader("spaces", "Spaces", loose.length, openSpaces);
      if (openSpaces) loose.forEach(function (r) { html += roomHtml(r, false); });
    }

    // --- One-to-one section ---
    var direct = rooms.filter(function (r) {
      return r.kind === "direct" && matches(r, f); });
    if (direct.length) {
      var openDirect = filtering || !sectionsCollapsed.direct;
      html += sectionHeader("direct", "One-to-one", direct.length, openDirect);
      if (openDirect) direct.forEach(function (r) { html += roomHtml(r, false); });
    }

    roomlist.innerHTML = html || '<div class="empty">No rooms match.</div>';
  }

  function attHtml(a) {
    if (!a.p) return '<div class="att">📎 ' + esc(a.n) + "</div>";
    var url = esc(PREFIX + a.p);
    if (a.img) {
      return '<div class="att att-img"><a href="' + url +
        '" target="_blank"><img src="' + url + '" alt="" loading="lazy"></a>' +
        '<div class="att-meta"><span class="fn">' + esc(a.n) + '</span>' +
        '<a class="dl" href="' + url + '" download>⬇ Download</a></div></div>';
    }
    if (a.aud) {
      return '<div class="att att-aud"><audio controls preload="none" src="' + url +
        '"></audio><div class="att-meta"><span class="fn">' + esc(a.n) + '</span>' +
        '<a class="dl" href="' + url + '" download>⬇ Download</a></div></div>';
    }
    return '<div class="att">📎 <a href="' + url + '" target="_blank">' +
      esc(a.n) + '</a> <a class="dl" href="' + url + '" download>⬇</a></div>';
  }

  function updateChip() {
    if (scopeRoom >= 0 && rooms[scopeRoom]) {
      var r = rooms[scopeRoom];
      var icon = r.kind === "direct"
        ? avatarHtml(r.title, r.p, "avatar") : avatarHtml(r.title, -1, "avatar");
      scopeChip.innerHTML = icon + '<span class="cname">' + esc(r.title) +
        '</span><span class="x" title="Search everywhere">✕</span>';
      scopeChip.classList.add("on");
      searchBox.placeholder = "Search in this chat…";
    } else {
      scopeChip.classList.remove("on");
      scopeChip.innerHTML = "";
      searchBox.placeholder = "Search all messages…";
    }
  }

  function openRoom(i, scrollToTs) {
    active = i;
    scopeRoom = i;             // selecting a chat scopes search to it
    updateChip();
    var r = rooms[i];
    // Remember the selection across refreshes. The URL hash works on file://
    // (where localStorage is often blocked); localStorage is a fallback.
    try { history.replaceState(null, "", "#" + encodeURIComponent(r.folder)); } catch (e) {}
    try { localStorage.setItem("webexSelectedFolder", r.folder); } catch (e) {}
    // Make sure the room is visible in the sidebar (expand its section/team).
    if (r.kind === "direct") sectionsCollapsed.direct = false;
    else if (r.teamId && r.team) {
      sectionsCollapsed.teams = false; expanded[r.teamId] = true;
    } else sectionsCollapsed.spaces = false;
    renderRoomList();
    renderChat(i, scrollToTs);
    saveSearchState();
  }

  function renderChat(i, scrollToTs) {
    var r = rooms[i];
    var sub = r.kind === "space"
      ? (r.team ? "Team: " + esc(r.team) : "Space") : "One-to-one";
    var html = '<div class="room-title">' + esc(r.title) + "</div>";
    html += '<div class="room-sub">' + sub + " · " + r.count + " messages · " +
      esc(r.folder) + "</div>";
    r.messages.forEach(function (m, mi) {
      var att = "";
      (m.a || []).forEach(function (a) { att += attHtml(a); });
      var av = avatarHtml(m.w, m.p, "mav");
      html += '<div class="msg' + (m.m ? " me" : "") + (m.r ? " reply" : "") +
        '" id="m' + i + "_" + mi + '">' + av + '<div class="mbody">' +
        '<div class="head"><span class="who">' + esc(m.w) + '</span> · ' +
        fmtTime(m.t) + '</div><div class="body">' + esc(m.x) + "</div>" + att +
        "</div></div>";
    });
    if (!r.messages.length) html += '<div class="empty">No messages.</div>';
    main.innerHTML = html;
    if (scrollToTs != null) {
      var el = document.getElementById("m" + i + "_" + scrollToTs);
      if (el) { el.scrollIntoView({ block: "center" }); el.classList.add("flash"); }
    } else {
      main.scrollTop = main.scrollHeight;
    }
  }

  function runSearch(q) {
    q = q.trim();
    if (q.length < 2) {
      if (active >= 0) renderChat(active);
      else main.innerHTML = '<div class="empty">Select a room, or search above.</div>';
      return;
    }
    var scoped = scopeRoom >= 0;
    var ql = q.toLowerCase(), out = "", hits = 0, MAX = 500;
    var count = scoped ? 1 : rooms.length;
    for (var n = 0; n < count && hits < MAX; n++) {
      var i = scoped ? scopeRoom : n;
      var r = rooms[i], rHits = [];
      for (var mi = 0; mi < r.messages.length; mi++) {
        if (r.messages[mi].x.toLowerCase().indexOf(ql) >= 0) rHits.push(mi);
      }
      if (!rHits.length) continue;
      if (!scoped) {
        out += '<div class="result-room" data-open="' + i + '">' + esc(r.title) +
          " (" + rHits.length + ")</div>";
      }
      for (var k = 0; k < rHits.length && hits < MAX; k++, hits++) {
        var m = r.messages[rHits[k]];
        out += '<div class="result' + (m.m ? " me" : "") + '" data-open="' + i +
          '" data-msg="' + rHits[k] + '"><div class="rmeta">' + esc(m.w) + " · " +
          fmtTime(m.t) + '</div><div>' + hl(m.x, q) + "</div></div>";
      }
    }
    var where = scoped ? " in " + esc(rooms[scopeRoom].title) : "";
    main.innerHTML = out
      ? '<div class="room-sub">' + (hits >= MAX ? MAX + "+ " : hits) +
        ' matches' + where + ' for "' + esc(q) + '"</div>' + out
      : '<div class="empty">No messages match "' + esc(q) + '"' + where + ".</div>";
  }

  roomlist.addEventListener("click", function (e) {
    var sec = e.target.closest(".group-label.section");
    if (sec) {
      var k = sec.dataset.section;
      sectionsCollapsed[k] = !sectionsCollapsed[k];
      renderRoomList();
      return;
    }
    var team = e.target.closest(".team");
    if (team) {
      var tid = team.dataset.team;
      expanded[tid] = !expanded[tid];
      renderRoomList();
      return;
    }
    var el = e.target.closest(".room");
    if (el) { searchBox.value = ""; openRoom(+el.dataset.i); }
  });
  main.addEventListener("click", function (e) {
    var el = e.target.closest("[data-open]");
    if (!el) return;
    var i = +el.dataset.open;
    var msg = el.dataset.msg != null ? +el.dataset.msg : null;
    searchBox.value = "";
    openRoom(i, msg);
  });
  function saveSearchState() {
    try {
      localStorage.setItem("webexSearch", searchBox.value);
      localStorage.setItem("webexScopeOff", scopeRoom < 0 ? "1" : "0");
    } catch (e) {}
  }
  var t;
  searchBox.addEventListener("input", function () {
    saveSearchState();
    clearTimeout(t); t = setTimeout(function () { runSearch(searchBox.value); }, 160);
  });
  searchBox.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { searchBox.value = ""; runSearch(""); saveSearchState(); }
  });
  // Remove the chat chip -> search everywhere.
  scopeChip.addEventListener("click", function (e) {
    if (!e.target.closest(".x")) return;
    scopeRoom = -1;
    updateChip();
    if (searchBox.value.trim().length >= 2) runSearch(searchBox.value);
    saveSearchState();
    searchBox.focus();
  });
  roomFilter.addEventListener("input", renderRoomList);

  // Draggable divider between the sidebar and the reading pane (width remembered).
  (function () {
    var aside = document.querySelector("aside");
    var splitter = document.getElementById("splitter");
    var layout = document.querySelector(".layout");
    try {
      var w0 = parseInt(localStorage.getItem("webexAsideW"), 10);
      if (w0) aside.style.width = w0 + "px";
    } catch (e) {}
    var dragging = false;
    splitter.addEventListener("mousedown", function (e) {
      dragging = true; splitter.classList.add("dragging");
      document.body.style.userSelect = "none"; e.preventDefault();
    });
    window.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      var rect = layout.getBoundingClientRect();
      var w = Math.max(160, Math.min(e.clientX - rect.left, rect.width - 240));
      aside.style.width = w + "px";
    });
    window.addEventListener("mouseup", function () {
      if (!dragging) return;
      dragging = false; splitter.classList.remove("dragging");
      document.body.style.userSelect = "";
      try { localStorage.setItem("webexAsideW", parseInt(aside.style.width, 10)); } catch (e) {}
    });
  })();

  renderRoomList();
  // Restore selected chat + search term + scope across refreshes (hash first,
  // then localStorage as a fallback).
  (function restore() {
    var sel = "", savedQuery = "", scopeOff = false;
    try {
      sel = decodeURIComponent((location.hash || "").slice(1)) ||
        localStorage.getItem("webexSelectedFolder") || "";
      savedQuery = localStorage.getItem("webexSearch") || "";
      scopeOff = localStorage.getItem("webexScopeOff") === "1";
    } catch (e) {}
    if (sel) {
      for (var si = 0; si < rooms.length; si++) {
        if (rooms[si].folder === sel) { openRoom(si); break; }
      }
    }
    if (scopeOff) { scopeRoom = -1; updateChip(); }
    if (savedQuery) { searchBox.value = savedQuery; runSearch(savedQuery); }
    saveSearchState();
  })();
})();
</script>
</body>
</html>
"""


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.path.join(here, "webex-history"),
                    help="data directory (default: ./webex-history)")
    ap.add_argument("--html-dir", default="",
                    help="where to write index.html (default: the data dir's parent)")
    ap.add_argument("--me-id", default=os.environ.get("WEBEX_ME_ID", ""))
    ap.add_argument("--me-email", default=os.environ.get("WEBEX_ME_EMAIL", ""))
    args = ap.parse_args()
    html_dir = args.html_dir or os.path.dirname(os.path.abspath(args.base))
    js_path, html_path, totals = build(
        args.base, args.me_id or None, args.me_email or None, html_dir=html_dir)
    sys.stderr.write("Wrote %s and %s (%s)\n" % (js_path, html_path, totals))


if __name__ == "__main__":
    main()
