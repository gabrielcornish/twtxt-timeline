#!/usr/bin/env python3
"""Build timeline.json for the gabesarcade.com ?timeline reader.

Reads feeds.txt (one feed per line: "nick url" or just "url"; blank lines and
lines starting with # are ignored), fetches each feed, understands both twtxt
and RSS/Atom, merges the newest posts into one JSON file, and records a
per-feed status so the page can show what loaded and what didn't.

Standard library only, so the GitHub Action needs no installs."""

import json, re
import urllib.request, urllib.error
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

PER_FEED = 20    # newest posts kept from each feed
TOTAL = 100      # newest posts kept overall
TIMEOUT = 20     # seconds per fetch
UA = "twtxt-timeline/1.0 (+https://github.com/gabrielcornish/twtxt-timeline)"
LINEBREAK = "\u2028"  # twtxt line separator; the reader renders it as a line break


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def host_of(url):
    return re.sub(r"^https?://", "", url).split("/")[0]


def base_of(url):
    return url.rsplit("/", 1)[0] + "/"


def parse_dt(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_rfc822(s):
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if not d:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def parse_twtxt(text, url, nick_hint):
    meta, posts = {}, []
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        if line.startswith("#"):
            m = re.match(r"^#\s*([A-Za-z0-9_-]+)\s*=\s*(.*)$", line)
            if m:
                meta[m.group(1).lower()] = m.group(2).strip()
            continue
        if "\t" not in line:
            continue
        ts, txt = line.split("\t", 1)
        d = parse_dt(ts)
        if d:
            posts.append((d, txt))
    nick = nick_hint or meta.get("nick") or host_of(url)
    return nick, base_of(url), posts


def parse_feed_xml(text, url, nick_hint):
    root = ET.fromstring(text.lstrip("\ufeff \t\r\n"))
    posts, feed_title, feed_link = [], None, None
    if strip_ns(root.tag).lower() == "rss":
        chan = root.find("channel")
        if chan is not None:
            feed_title = chan.findtext("title")
            feed_link = chan.findtext("link")
            for it in chan.findall("item"):
                d = parse_rfc822(it.findtext("pubDate") or "")
                if d:
                    posts.append((d, (it.findtext("title") or "").strip(),
                                  (it.findtext("link") or "").strip()))
    else:  # Atom
        for ch in list(root):
            n = strip_ns(ch.tag).lower()
            if n == "title" and feed_title is None:
                feed_title = ch.text
            elif n == "link" and ch.get("rel") in (None, "alternate") and not feed_link:
                feed_link = ch.get("href")
        for it in root:
            if strip_ns(it.tag).lower() != "entry":
                continue
            title, link, date = "", "", ""
            for ch in it:
                n = strip_ns(ch.tag).lower()
                if n == "title":
                    title = (ch.text or "").strip()
                elif n == "link" and ch.get("rel") in (None, "alternate"):
                    link = ch.get("href") or link
                elif n in ("updated", "published") and not date:
                    date = ch.text or ""
            d = parse_dt(date)
            if d:
                posts.append((d, title, link))
    nick = nick_hint or feed_title or host_of(url)
    site = feed_link or base_of(url)
    # RSS/Atom -> twtxt-ish text: title, then the link on its own line (U+2028)
    out = []
    for d, title, link in posts:
        if title and link:
            txt = title + LINEBREAK + link
        elif link:
            txt = link
        else:
            txt = title
        out.append((d, txt))
    return nick, site, out


def describe(e):
    if isinstance(e, urllib.error.HTTPError):
        return "the feed's site returned an error (HTTP %s)" % e.code
    if isinstance(e, urllib.error.URLError):
        return "couldn't reach the feed (%s)" % getattr(e, "reason", "network error")
    return "couldn't load the feed (%s)" % (str(e)[:80])


def looks_like_xml(body):
    head = body.lstrip("\ufeff \t\r\n")[:200].lower()
    return head.startswith("<?xml") or "<rss" in head or "<feed" in head


def load_feed(nick_hint, url):
    try:
        body = fetch(url)
    except Exception as e:
        return {"ok": False, "nick": nick_hint or host_of(url), "url": url,
                "error": describe(e)}, []
    try:
        if looks_like_xml(body):
            nick, site, posts = parse_feed_xml(body, url, nick_hint)
        else:
            nick, site, posts = parse_twtxt(body, url, nick_hint)
    except Exception as e:
        return {"ok": False, "nick": nick_hint or host_of(url), "url": url,
                "error": "loaded, but couldn't be parsed (%s)" % (str(e)[:80])}, []
    posts.sort(key=lambda p: p[0], reverse=True)
    posts = posts[:PER_FEED]
    items = [{"nick": nick, "site": site,
              "timestamp": d.astimezone(timezone.utc).isoformat(), "text": txt}
             for d, txt in posts]
    return {"ok": True, "nick": nick, "url": url, "count": len(items)}, items


def read_feeds(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and re.match(r"https?://", parts[1]):
                out.append((parts[0], parts[1]))
            elif re.match(r"https?://", parts[0]):
                out.append((None, parts[0]))
    return out


def main():
    feeds = read_feeds("feeds.txt")
    items, sources = [], []
    for nick_hint, url in feeds:
        status, feed_items = load_feed(nick_hint, url)
        sources.append(status)
        items.extend(feed_items)
    items.sort(key=lambda x: x["timestamp"], reverse=True)
    items = items[:TOTAL]
    data = {"generated": datetime.now(timezone.utc).isoformat(),
            "items": items, "sources": sources}
    with open("timeline.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=True, indent=1)
    print("wrote timeline.json: %d items from %d feeds" % (len(items), len(feeds)))


if __name__ == "__main__":
    main()
