#!/usr/bin/env python3
"""
Download Webex person avatars for the extracted history.

Collects every personId seen in the extraction (room memberships + message
senders), resolves them via the Webex People API (in bulk), downloads each
avatar image into <base>/avatars/, and writes <base>/people.json:

    { "<personId>": { "name": "...", "avatar": "avatars/<uuid>.jpg" }, ... }

build_browser.py reads people.json to show real photos (with an initial as a
fallback when a person has no avatar). Note: Webex exposes avatars only for
*people* — teams and spaces have no downloadable picture in the public API.

Usage:
    export WEBEX_TOKEN='<token>'
    python3 fetch_avatars.py [--base DIR]
Also called automatically at the end of extract_webex.py.
"""

import os
import sys
import json
import time
import base64
import argparse
import urllib.request
import urllib.error
import urllib.parse

API = "https://webexapis.com/v1"


def _uuid_from_id(pid):
    try:
        raw = base64.b64decode(pid + "===").decode("utf-8", "ignore")
        tail = raw.rstrip("/").split("/")[-1]
        if tail:
            return tail
    except Exception:
        pass
    return pid[-16:]


def _get_json(url, token):
    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"), strict=False)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After", "5") or "5") + 1)
                continue
            if e.code in (500, 502, 503, 504) and attempt <= 4:
                time.sleep(min(2 ** attempt, 20))
                continue
            raise
        except urllib.error.URLError:
            if attempt <= 4:
                time.sleep(min(2 ** attempt, 20))
                continue
            raise


def _download(url, dest, token):
    """Download an avatar image. Avatar URLs are usually public; fall back to
    an authenticated request if needed. Returns the extension used or None."""
    for use_auth in (False, True):
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(url)
            if use_auth:
                req.add_header("Authorization", "Bearer " + token)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    ctype = (resp.headers.get("Content-Type", "") or "").lower()
                    ext = {"image/jpeg": ".jpg", "image/png": ".png",
                           "image/gif": ".gif", "image/webp": ".webp"}.get(
                        ctype.split(";")[0].strip(), ".jpg")
                    data = resp.read()
                if not data:
                    return None
                with open(dest + ext, "wb") as f:
                    f.write(data)
                return ext
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(int(e.headers.get("Retry-After", "5") or "5") + 1)
                    continue
                if e.code in (401, 403) and not use_auth:
                    break  # retry outer loop with auth
                if e.code in (500, 502, 503, 504) and attempt <= 3:
                    time.sleep(min(2 ** attempt, 15))
                    continue
                return None
            except urllib.error.URLError:
                if attempt <= 3:
                    time.sleep(min(2 ** attempt, 15))
                    continue
                return None
    return None


def _iter_room_folders(base):
    for sub in ("one-to-one", "spaces"):
        root = os.path.join(base, sub)
        if os.path.isdir(root):
            for n in os.listdir(root):
                p = os.path.join(root, n)
                if os.path.isdir(p):
                    yield p
    troot = os.path.join(base, "teams")
    if os.path.isdir(troot):
        for team in os.listdir(troot):
            tp = os.path.join(troot, team)
            if os.path.isdir(tp):
                for n in os.listdir(tp):
                    p = os.path.join(tp, n)
                    if os.path.isdir(p):
                        yield p


def collect_person_ids(base):
    ids = {}
    for folder in _iter_room_folders(base):
        mj = os.path.join(folder, "members.json")
        if os.path.exists(mj):
            try:
                for mb in json.load(open(mj, encoding="utf-8")):
                    if mb.get("personId"):
                        ids.setdefault(mb["personId"],
                                       mb.get("personDisplayName") or "")
            except Exception:
                pass
        jl = os.path.join(folder, "messages.jsonl")
        if os.path.exists(jl):
            with open(jl, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        m = json.loads(line, strict=False)
                    except Exception:
                        continue
                    if m.get("personId"):
                        ids.setdefault(m["personId"], "")
    return ids


def fetch_avatars(base, token, progress=None):
    ids = collect_person_ids(base)
    if not ids:
        return {"people": 0, "avatars": 0}
    avatars_dir = os.path.join(base, "avatars")
    os.makedirs(avatars_dir, exist_ok=True)
    people = {}
    people_json = os.path.join(base, "people.json")
    if os.path.exists(people_json):
        try:
            people = json.load(open(people_json, encoding="utf-8"))
        except Exception:
            people = {}

    id_list = list(ids.keys())
    downloaded = 0
    resolved = 0
    for i in range(0, len(id_list), 80):
        chunk = id_list[i:i + 80]
        q = "&".join("id=" + urllib.parse.quote(x) for x in chunk)
        try:
            body = _get_json("%s/people?%s&max=80" % (API, q), token)
        except Exception as e:
            if progress:
                progress("  people lookup failed for a chunk: %s" % e)
            continue
        for person in body.get("items", []):
            pid = person.get("id")
            if not pid:
                continue
            entry = people.get(pid, {})
            entry["name"] = person.get("displayName") or entry.get("name") or ""
            resolved += 1
            avatar_url = person.get("avatar")
            if avatar_url and not entry.get("avatar"):
                dest = os.path.join(avatars_dir, _uuid_from_id(pid))
                ext = _download(avatar_url, dest, token)
                if ext:
                    entry["avatar"] = "avatars/" + _uuid_from_id(pid) + ext
                    downloaded += 1
            people[pid] = entry
        if progress:
            progress("  resolved %d/%d people, %d avatars downloaded"
                     % (min(i + 80, len(id_list)), len(id_list), downloaded))

    # Fill names for anyone the API didn't return, using membership display names.
    for pid, name in ids.items():
        if pid not in people and name:
            people[pid] = {"name": name}

    with open(people_json, "w", encoding="utf-8") as f:
        json.dump(people, f, indent=2, ensure_ascii=False)
    return {"people": len(people), "avatars": downloaded, "resolved": resolved}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "webex-history"),
                    help="data directory (default: ./webex-history)")
    args = ap.parse_args()
    token = os.environ.get("WEBEX_TOKEN", "").strip()
    if not token:
        sys.stderr.write("ERROR: set WEBEX_TOKEN.\n")
        sys.exit(2)
    stats = fetch_avatars(args.base, token, progress=lambda s: sys.stderr.write(s + "\n"))
    sys.stderr.write("Done: %s\n" % stats)


if __name__ == "__main__":
    main()
