#!/usr/bin/env python3
"""Builds videos.json for the Videos app from the channels listed in channels.txt.

Runs inside GitHub Actions (see .github/workflows/update-videos.yml). It reads each
channel's public video feed, keeps the newest videos, and drops any channel that was
removed from channels.txt. Only the Python standard library is used.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
CHANNELS_FILE = os.path.join(HERE, "channels.txt")
OUT_FILE = os.path.join(HERE, "videos.json")

KEEP_PER_CHANNEL = 60   # the library grows over time up to this many videos per channel
SKIP_SHORTS = True      # leave out YouTube Shorts

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "CONSENT=YES+cb; SOCS=CAI",
}
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
ID_RE = re.compile(r"^UC[\w-]{22}$")


def warn(msg):
    print(f"::warning::{msg}")


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # network hiccups, 5xx, etc.
            last = e
            time.sleep(2 * (i + 1))
    raise last


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_no_redirect = urllib.request.build_opener(_NoRedirect)


def is_short(video_id):
    """youtube.com/shorts/<id> answers 200 for a Short and redirects for a normal video."""
    try:
        req = urllib.request.Request(f"https://www.youtube.com/shorts/{video_id}", headers=HEADERS, method="HEAD")
        with _no_redirect.open(req, timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def parse_channels():
    entries = []
    with open(CHANNELS_FILE, encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split(None, 1)
            key = parts[0].strip().rstrip("/")
            name = parts[1].strip() if len(parts) > 1 else ""
            entries.append((key, name))
    return entries


def resolve(key, known):
    """Turn a handle, URL or ID into a UC... channel ID."""
    if key in known:
        return known[key]
    m = re.search(r"(UC[\w-]{22})", key)
    if m and ("/channel/" in key or ID_RE.match(key)):
        return m.group(1)
    if key.startswith("http"):
        url = key
    else:
        url = "https://www.youtube.com/" + (key if key.startswith("@") else "@" + key)
    html = fetch(url)
    for pat in (r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
                r'"externalId":"(UC[\w-]{22})"',
                r'"browseId":"(UC[\w-]{22})"'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    raise ValueError("could not find a channel ID on " + url)


def read_feed(channel_id):
    xml = fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    root = ET.fromstring(xml)
    name = (root.findtext("a:author/a:name", default="", namespaces=NS)
            or root.findtext("a:title", default="", namespaces=NS))
    vids = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        if not vid:
            continue
        vids.append({
            "id": vid,
            "title": e.findtext("a:title", default="", namespaces=NS),
            "published": e.findtext("a:published", default="", namespaces=NS),
        })
    return name, vids


def main():
    old = {}
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = {}
    known_ids = old.get("resolved", {})
    old_videos = {}
    for v in old.get("videos", []):
        old_videos.setdefault(v["channel"], []).append(v)
    old_names = {c["id"]: c.get("name", "") for c in old.get("channels", [])}
    known_shorts = set(old.get("shorts", []))

    channels, videos, resolved = [], [], {}
    for key, custom_name in parse_channels():
        try:
            cid = resolve(key, known_ids)
        except Exception as e:
            warn(f"Skipping '{key}': {e}")
            continue
        resolved[key] = cid
        if any(c["id"] == cid for c in channels):
            continue
        try:
            feed_name, fresh = read_feed(cid)
        except Exception as e:
            warn(f"Couldn't read the video feed for '{key}' this time ({e}). Keeping its saved videos.")
            feed_name, fresh = old_names.get(cid, key), []

        seen, merged = set(), []
        for v in fresh + old_videos.get(cid, []):
            if v["id"] in seen:
                continue
            seen.add(v["id"])
            merged.append({"id": v["id"], "title": v["title"], "published": v["published"], "channel": cid})
        if SKIP_SHORTS:
            already = {v["id"] for v in old_videos.get(cid, [])}
            keep = []
            for v in merged:
                if v["id"] in known_shorts:
                    continue
                if v["id"] not in already and is_short(v["id"]):
                    known_shorts.add(v["id"])
                    continue
                keep.append(v)
            merged = keep
        merged.sort(key=lambda v: v["published"], reverse=True)
        merged = merged[:KEEP_PER_CHANNEL]

        channels.append({"id": cid, "name": custom_name or feed_name or key, "key": key})
        videos.extend(merged)
        print(f"{key}: {len(merged)} videos ({len(fresh)} in the latest feed)")

    videos.sort(key=lambda v: v["published"], reverse=True)
    data = {
        "channels": channels,
        "videos": videos,
        "resolved": resolved,
        "shorts": sorted(known_shorts)[-2000:],
    }
    same = {k: old.get(k) for k in data} == data
    data["updated"] = old.get("updated") if same else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"Done: {len(channels)} channels, {len(videos)} videos{' (no changes)' if same else ''}.")
    if not channels:
        warn("No channels could be loaded. Check channels.txt.")


if __name__ == "__main__":
    sys.exit(main())
