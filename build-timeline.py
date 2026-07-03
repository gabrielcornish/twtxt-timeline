#!/usr/bin/env python3
"""Convert each source in feeds.txt into a shareable twtxt feed under feeds/.

Reads feeds.txt (one source per line: "nick url" or just "url"; blank lines and
lines starting with # are ignored), fetches each source (twtxt OR RSS/Atom), and
writes feeds/<slug>.txt — a valid twtxt feed hosted on GitHub, so it has CORS and
anyone can follow it. RSS/Atom items become twtxt posts (timestamp + title link).
Also writes feeds/index.txt listing the follow lines for convenience.

Standard library only, so the GitHub Action needs no installs."""

import os, re, glob
import urllib.request, urllib.error
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

# Base URL where the generated feeds are served (each feed's own "# url =").
RAW_BASE = "https://raw.githubusercontent.com/gabrielcornish/twtxt-timeline/main/feeds/"
OUT_DIR = "feeds"
MAX_POSTS = 50   # newest posts kept per generated feed
TIMEOUT = 20
UA = "twtxt-timeline/1.0 (+https://github.com/gabrielcornish/twtxt-timeline)"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def host_of(url):
    return re.sub(r"^https?://", "", url).split("/")[0]


def base_of(url):
    return url.rsplit("/", 1)[0] + "/"


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "feed"


def oneline(s):
    # a twtxt post/field must stay on one physical line (U+2028 line breaks are OK)
    return re.sub(r"[\t\r\n]+", " ", (s or "")).strip()


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
    return nick, posts


def parse_feed_xml(text, url, nick_hint):
    root = ET.fromstring(text.lstrip("\ufeff \t\r\n"))
    raw, feed_title = [], None
    if strip_ns(root.tag).lower() == "rss":
        chan = root.find("channel")
        if chan is not None:
            feed_title = chan.findtext("title")
            for it in chan.findall("item"):
                d = parse_rfc822(it.findtext("pubDate") or "")
                if d:
                    raw.append((d, (it.findtext("title") or "").strip(),
                                (it.findtext("link") or "").strip()))
    else:  # Atom
        for ch in list(root):
            if strip_ns(ch.tag).lower() == "title" and feed_title is None:
                feed_title = ch.text
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
                raw.append((d, title, link))
    nick = nick_hint or feed_title or host_of(url)
    posts = []
    for d, title, link in raw:
        text_val = (title + " " + link).strip() if title else link
        posts.append((d, text_val))
    return nick, posts


def describe(e):
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP %s" % e.code
    if isinstance(e, urllib.error.URLError):
        return "unreachable (%s)" % getattr(e, "reason", "network error")
    return str(e)[:80]


def looks_like_xml(body):
    head = body.lstrip("\ufeff \t\r\n")[:200].lower()
    return head.startswith("<?xml") or "<rss" in head or "<feed" in head


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


def write_feed(slug, nick, source_url, posts):
    posts = sorted(posts, key=lambda p: p[0])[-MAX_POSTS:]  # oldest..newest, newest kept
    out = [
        "# nick = %s" % oneline(nick),
        "# url = %s%s.txt" % (RAW_BASE, slug),
        "# source = %s" % source_url,
        "# description = twtxt conversion of %s" % source_url,
        "",
    ]
    for d, text in posts:
        out.append("%s\t%s" % (d.astimezone(timezone.utc).isoformat(), oneline(text)))
    path = os.path.join(OUT_DIR, slug + ".txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return len(posts)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for f in glob.glob(os.path.join(OUT_DIR, "*.txt")):
        os.remove(f)                       # drop feeds for sources you removed
    if os.path.exists("timeline.json"):
        os.remove("timeline.json")         # clean up the old JSON approach

    used, index, report = set(), [], []
    for nick_hint, url in read_feeds("feeds.txt"):
        try:
            body = fetch(url)
            nick, posts = (parse_feed_xml if looks_like_xml(body) else parse_twtxt)(body, url, nick_hint)
        except Exception as e:
            report.append("SKIP  %s  (%s)" % (url, describe(e)))
            continue
        slug, base, i = slugify(nick_hint or nick), slugify(nick_hint or nick), 2
        while slug in used:
            slug = "%s-%d" % (base, i); i += 1
        used.add(slug)
        n = write_feed(slug, nick, url, posts)
        index.append("%s %s%s.txt" % (slug, RAW_BASE, slug))
        report.append("OK    %s  ->  feeds/%s.txt  (%d posts)" % (url, slug, n))

    with open(os.path.join(OUT_DIR, "index.txt"), "w", encoding="utf-8") as f:
        f.write("# Converted twtxt feeds. Add any of these to your twtxt.txt as:\n")
        f.write("#   # follow = <nick> <url>\n\n")
        f.write("\n".join(index) + ("\n" if index else ""))

    print("\n".join(report) if report else "no sources in feeds.txt")


if __name__ == "__main__":
    main()

2. Update the workflow's commit step

Open .github/workflows/build.yml → pencil ✏️ → replace all with this (only change from before: it commits the whole feeds/ folder instead of one JSON file):

name: Build timeline

on:
  schedule:
    - cron: "17 * * * *"
  workflow_dispatch: {}
  push:
    paths:
      - feeds.txt
      - build-timeline.py

permissions:
  contents: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build feeds
        run: python3 build-timeline.py
      - name: Commit changes
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add -A
          if git diff --staged --quiet; then
            echo "No changes."
          else
            git commit -m "Update converted twtxt feeds"
            git push
          fi
