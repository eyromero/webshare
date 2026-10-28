#!/usr/bin/env python3
"""Post Webshare status updates (status.webshare.io) to a Discord channel.

Environment variables:
  DISCORD_WEBHOOK_URL  required  Discord channel webhook URL
  FEED_URL             optional  RSS/Atom feed URL (default below)
  DISCORD_MENTION      optional  who to ping on non-resolved updates:
                                 "everyone", "here", or a role ID
  STATE_FILE           optional  where seen items are stored (default seen.json)

Usage:
  python webshare_status.py          check the feed, post anything new
  python webshare_status.py --test   send a test message to Discord
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

FEED_URL = os.environ.get("FEED_URL") or "https://status.webshare.io/feed.rss"
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL") or ""
MENTION = (os.environ.get("DISCORD_MENTION") or "").strip().lstrip("@").lower()
STATE_FILE = os.environ.get("STATE_FILE") or "seen.json"
UA = "webshare-status-discord/1.0"  # Discord rejects the default Python user agent
ATOM = "{http://www.w3.org/2005/Atom}"
MAX_STATE = 300

GREEN, BLUE, YELLOW, RED = 0x2ECC71, 0x3498DB, 0xF1C40F, 0xE74C3C


def http(url, payload=None):
    headers = {"User-Agent": UA}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def clean(text, limit=1500):
    """Turn feed HTML into plain text that fits in a Discord embed."""
    text = re.sub(r"<li[^>]*>", "• ", text or "", flags=re.I)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", text, flags=re.I)
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.I | re.S)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def parse_feed(raw):
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):  # RSS
        items.append({
            "id": it.findtext("guid") or it.findtext("link") or it.findtext("title") or "",
            "title": it.findtext("title") or "Status update",
            "link": it.findtext("link") or "",
            "body": it.findtext("description") or "",
            "date": it.findtext("pubDate") or "",
        })
    for e in root.iter(ATOM + "entry"):  # Atom
        link = e.find(ATOM + "link")
        items.append({
            "id": e.findtext(ATOM + "id") or "",
            "title": e.findtext(ATOM + "title") or "Status update",
            "link": link.get("href", "") if link is not None else "",
            "body": e.findtext(ATOM + "content") or e.findtext(ATOM + "summary") or "",
            "date": e.findtext(ATOM + "updated") or "",
        })
    return items


def fingerprint(item):
    # Includes date and body so a new update on an existing incident is posted too.
    key = "|".join([item["id"], item["date"], item["body"]])
    return hashlib.sha1(key.encode()).hexdigest()


def colour(item):
    # incident.io items start with "Status: <state>"; fall back to keywords.
    m = re.search(r"Status:\s*([A-Za-z ]+)", item["body"])
    head = m.group(1).lower() if m else (item["title"] + " " + clean(item["body"], 200)).lower()
    if "resolved" in head or "completed" in head:
        return GREEN
    if "maintenance" in head or "scheduled" in head:
        return BLUE
    if "monitoring" in head:
        return YELLOW
    return RED


def post(item, mention=True):
    col = colour(item)
    embed = {
        "title": item["title"][:256],
        "description": clean(item["body"]) or "No details yet.",
        "color": col,
        "footer": {"text": item["date"] or "status.webshare.io"},
    }
    if item["link"]:
        embed["url"] = item["link"]
    ping = bool(mention and MENTION and col != GREEN)
    content, allowed = "", {"parse": []}
    if ping and MENTION in ("everyone", "here"):
        content, allowed = f"@{MENTION}", {"parse": ["everyone"]}
    elif ping:
        content, allowed = f"<@&{MENTION}>", {"parse": [], "roles": [MENTION]}
    payload = {
        "username": "Webshare Status",
        "content": content,
        "allowed_mentions": allowed,
        "embeds": [embed],
    }
    http(WEBHOOK, payload)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def save_state(keys):
    with open(STATE_FILE, "w") as f:
        json.dump(keys[:MAX_STATE], f, indent=0)


def main():
    if not WEBHOOK:
        sys.exit("DISCORD_WEBHOOK_URL is not set.")

    if "--test" in sys.argv:
        post({
            "title": "Test: Webshare status bot is connected",
            "link": "https://status.webshare.io",
            "body": "If you can see this, the webhook works.",
            "date": "",
        }, mention=False)
        print("Test message sent.")
        return

    try:
        items = parse_feed(http(FEED_URL))
    except (urllib.error.URLError, ET.ParseError) as err:
        sys.exit(f"Could not read the feed at {FEED_URL}: {err}\n"
                 "Copy the RSS/Atom link from 'Subscribe to updates' on "
                 "status.webshare.io and set it as FEED_URL.")

    current = [fingerprint(i) for i in items]
    seen = load_state()

    if seen is None:
        print(f"First run: recorded {len(items)} existing item(s) without posting.")
    else:
        seen_set = set(seen)
        new = [i for i, k in zip(items, current) if k not in seen_set]
        for item in reversed(new):  # feeds list newest first; post oldest first
            post(item)
            time.sleep(1)
        print(f"Posted {len(new)} new update(s).")

    previous = [k for k in (seen or []) if k not in set(current)]
    save_state(current + previous)


if __name__ == "__main__":
    main()
