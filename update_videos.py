#!/usr/bin/env python3
"""Builds videos.json for the Videos app from the channels listed in channels.txt.

Runs inside GitHub Actions (see .github/workflows/update-videos.yml). For each channel
it reads the channel's "Videos" tab (which never includes Shorts), going back as far as
the count you set, and uses the channel's feed for exact upload dates. If the Videos tab
can't be read, it falls back to the feed (newest 15 uploads, Shorts removed).
Channels removed from channels.txt are dropped. Only the Python standard library is used.
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

DEFAULT_COUNT = 20      # used when channels.txt doesn't say "videos per channel: N"
MAX_COUNT = 40          # upper limit for any one channel (higher numbers are cut to this)

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
NOW = time.time()


def warn(msg):
    print(f"::warning::{msg}")


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts))


def fetch(url, data=None, tries=3):
    last = None
    for i in range(tries):
        try:
            headers = dict(HEADERS)
            body = None
            if data is not None:
                body = json.dumps(data).encode()
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=body, headers=headers)
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


# ---------- channels.txt ----------
def parse_channels():
    """Returns (default_count, premium, [(key, count_or_None, name)])."""
    default = DEFAULT_COUNT
    premium = False
    entries = []
    with open(CHANNELS_FILE, encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            m = re.match(r"(?i)^videos\s+per\s+channel\s*[:=]?\s*(\d+)\s*$", line)
            if m:
                default = int(m.group(1))
                continue
            m = re.match(r"(?i)^youtube\s+premium\s*[:=]?\s*(\w+)\s*$", line)
            if m:
                premium = m.group(1).lower() in ("yes", "on", "true", "y")
                continue
            parts = line.split()
            key = parts[0].rstrip("/")
            count = None
            rest = parts[1:]
            if rest and rest[0].isdigit():
                count = int(rest[0])
                rest = rest[1:]
            entries.append((key, count, " ".join(rest)))
    return max(1, min(default, MAX_COUNT)), premium, entries


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


# ---------- the channel feed (exact dates, newest 15) ----------
def read_feed(channel_id):
    xml = fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    root = ET.fromstring(xml)
    name = (root.findtext("a:author/a:name", default="", namespaces=NS)
            or root.findtext("a:title", default="", namespaces=NS))
    vids = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        if vid:
            vids.append({"id": vid,
                         "title": e.findtext("a:title", default="", namespaces=NS),
                         "published": e.findtext("a:published", default="", namespaces=NS)})
    return name, vids


# ---------- the channel's Videos tab (no Shorts, goes back further) ----------
UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2629800, "year": 31557600}


def ago_to_date(text):
    m = re.search(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", text or "")
    if not m:
        return iso(NOW)
    return iso(NOW - int(m.group(1)) * UNITS[m.group(2)])


def _text(t):
    if isinstance(t, dict):
        if "simpleText" in t:
            return t["simpleText"]
        if "runs" in t:
            return "".join(r.get("text", "") for r in t["runs"])
        if "content" in t:
            return t["content"]
    return t if isinstance(t, str) else ""


def _walk(node, found, conts):
    if isinstance(node, dict):
        if "videoRenderer" in node and isinstance(node["videoRenderer"], dict):
            vr = node["videoRenderer"]
            blob = json.dumps(vr)
            if vr.get("videoId") and "upcomingEventData" not in vr and '"style": "LIVE"' not in blob \
                    and "MEMBERS_ONLY" not in blob:
                found.append({"id": vr["videoId"], "title": _text(vr.get("title")),
                              "ago": _text(vr.get("publishedTimeText"))})
            return
        if "lockupViewModel" in node and isinstance(node["lockupViewModel"], dict):
            lv = node["lockupViewModel"]
            blob = json.dumps(lv)
            if lv.get("contentId") and "VIDEO" in str(lv.get("contentType", "VIDEO")) and "MEMBERS_ONLY" not in blob:
                title = _text(((lv.get("metadata") or {}).get("lockupMetadataViewModel") or {}).get("title"))
                ago = next(iter(re.findall(r'"content": "([^"]*\bago)"', blob)), "")
                found.append({"id": lv["contentId"], "title": title, "ago": ago})
            return
        if "continuationItemRenderer" in node:
            tok = re.search(r'"token": "([^"]+)"', json.dumps(node["continuationItemRenderer"]))
            if tok:
                conts.append(tok.group(1))
            return
        for v in node.values():
            _walk(v, found, conts)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found, conts)


def read_videos_tab(channel_id, want):
    html = fetch(f"https://www.youtube.com/channel/{channel_id}/videos")
    i = html.find("ytInitialData")
    if i < 0:
        raise ValueError("no video data on the channel page")
    i = html.find("{", i)
    data, _ = json.JSONDecoder().raw_decode(html, i)
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    ver = ver.group(1) if ver else "2.20240101.00.00"
    found, conts = [], []
    _walk(data, found, conts)
    pages = 0
    while len(found) < want and conts and pages < 10:
        pages += 1
        more = json.loads(fetch("https://www.youtube.com/youtubei/v1/browse?prettyPrint=false",
                                data={"context": {"client": {"clientName": "WEB", "clientVersion": ver, "hl": "en"}},
                                      "continuation": conts[-1]}))
        conts = []
        _walk(more, found, conts)
    seen, out = set(), []
    for v in found:
        if v["id"] not in seen:
            seen.add(v["id"])
            out.append(v)
    if not out:
        raise ValueError("the Videos tab was empty")
    return out[:want]


# ---------- main ----------
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

    default_count, premium, entries = parse_channels()
    print(f"Videos per channel: {default_count} (unless a channel line sets its own number)")
    print(f"YouTube Premium mode: {'on' if premium else 'off'}")
    channels, videos, resolved = [], [], {}
    for key, count, custom_name in entries:
        want = max(1, min(count or default_count, MAX_COUNT))
        try:
            cid = resolve(key, known_ids)
        except Exception as e:
            warn(f"Skipping '{key}': {e}")
            continue
        resolved[key] = cid
        if any(c["id"] == cid for c in channels):
            continue

        prev = {v["id"]: v for v in old_videos.get(cid, [])}
        try:
            feed_name, feed = read_feed(cid)
        except Exception as e:
            feed_name, feed = old_names.get(cid, ""), []
        exact = {v["id"]: v["published"] for v in feed}

        merged, source = [], "videos tab"
        try:
            for v in read_videos_tab(cid, want):
                date = exact.get(v["id"]) or (prev.get(v["id"]) or {}).get("published") or ago_to_date(v["ago"])
                merged.append({"id": v["id"], "title": v["title"], "published": date, "channel": cid})
        except Exception as e:
            source = "feed"
            if feed:
                warn(f"Couldn't read the Videos tab for '{key}' ({e}). Using its newest uploads instead.")
            pool = feed + [v for v in old_videos.get(cid, []) if v["id"] not in exact]
            for v in pool:
                if v["id"] in known_shorts:
                    continue
                if v["id"] not in prev and is_short(v["id"]):
                    known_shorts.add(v["id"])
                    continue
                merged.append({"id": v["id"], "title": v["title"], "published": v["published"], "channel": cid})
            if not merged and not feed:
                warn(f"Couldn't read '{key}' this time. Keeping its saved videos.")
                merged = list(old_videos.get(cid, []))

        merged.sort(key=lambda v: v["published"], reverse=True)
        merged = merged[:want]
        channels.append({"id": cid, "name": custom_name or feed_name or old_names.get(cid) or key, "key": key})
        videos.extend(merged)
        print(f"{key}: {len(merged)} of {want} videos (from the {source})")

    videos.sort(key=lambda v: v["published"], reverse=True)
    data = {
        "channels": channels,
        "videos": videos,
        "resolved": resolved,
        "shorts": sorted(known_shorts)[-2000:],
        "premium": premium,
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
