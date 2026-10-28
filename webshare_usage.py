#!/usr/bin/env python3
"""Webshare usage alerts for Discord.

Checks the bandwidth of every active Webshare plan and the subscription's
renewal date, and posts to Discord when something needs attention. Each alert
is sent once per billing period.

Environment variables:
  WEBSHARE_API_KEY     required  Webshare API key
  DISCORD_WEBHOOK_URL  required  Discord channel webhook URL
  DISCORD_MENTION      optional  "everyone", "here" or a role ID, pinged on critical alerts
  BANDWIDTH_ALERTS     optional  bandwidth percentages to alert at (default "80,90,100")
  CRITICAL_PERCENT     optional  bandwidth percentage that pings DISCORD_MENTION (default 90)
  RENEWAL_ALERT_DAYS   optional  days before the period ends to alert (default "3,1")
  DISPLAY_TZ           optional  time zone for dates in messages (default Asia/Manila)
  STATE_FILE           optional  where sent alerts are remembered (default usage_state.json)

Usage:
  python webshare_usage.py            check and alert
  python webshare_usage.py --summary  also post a usage summary of every plan
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

API = "https://proxy.webshare.io/api/v2"
API_KEY = os.environ.get("WEBSHARE_API_KEY") or ""
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL") or ""
MENTION = (os.environ.get("DISCORD_MENTION") or "").strip().lstrip("@").lower()
BW_ALERTS = sorted(int(x) for x in (os.environ.get("BANDWIDTH_ALERTS") or "80,90,100").split(",") if x.strip())
CRITICAL_PCT = int(os.environ.get("CRITICAL_PERCENT") or 90)
RENEW_DAYS = sorted(float(x) for x in (os.environ.get("RENEWAL_ALERT_DAYS") or "3,1").split(",") if x.strip())
STATE_FILE = os.environ.get("STATE_FILE") or "usage_state.json"
UA = "webshare-usage-discord/1.0"  # Discord rejects the default Python user agent
GB = 1_000_000_000  # decimal GB; if Webshare counts GiB this alerts slightly early, never late
DASHBOARD = "https://dashboard.webshare.io/"

GREEN, BLUE, YELLOW, ORANGE, RED = 0x2ECC71, 0x3498DB, 0xF1C40F, 0xE67E22, 0xE74C3C

try:
    TZ = ZoneInfo(os.environ.get("DISPLAY_TZ") or "Asia/Manila") if ZoneInfo else timezone.utc
except Exception:
    TZ = timezone.utc


# ---------- HTTP ----------

def http(url, payload=None, headers=None):
    h = {"User-Agent": UA, **(headers or {})}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    return json.loads(body) if body else None


def get(url):
    return http(url, headers={"Authorization": f"Token {API_KEY}"})


def api(path, **params):
    url = f"{API}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    return get(url)


# ---------- Webshare data ----------

def active_plans(sub):
    plans, url = [], f"{API}/subscription/plan/"
    while url:
        page = get(url)
        plans += [p for p in page.get("results", []) if p.get("status", "active") == "active"]
        url = page.get("next")
    if not plans and sub.get("plan"):
        plans = [api(f"/subscription/plan/{sub['plan']}/")]
    return plans


def bandwidth_used(plan, start, now):
    stats = api("/stats/aggregate/", plan_id=plan["id"],
                timestamp__gte=start.isoformat(), timestamp__lte=now.isoformat())
    return (stats or {}).get("bandwidth_total") or 0


def plan_name(p):
    parts = [(p.get("proxy_type") or ""), (p.get("proxy_subtype") or "")]
    name = " ".join(w.replace("_", " ") for w in parts if w and w != "default").title() or "Plan"
    name = name.replace(" And ", " & ").replace("Isp", "ISP")
    count = p.get("proxy_count")
    return f"{name} · {count} proxies (#{p['id']})" if count else f"{name} (#{p['id']})"


# ---------- formatting ----------

def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_dt(dt):
    return dt.astimezone(TZ).strftime("%b %d, %Y %I:%M %p")


def fmt_gb(b):
    return f"{b / GB:,.2f} GB"


def fmt_left(days):
    if days < 1:
        return f"{max(days * 24, 0):.0f} hours"
    return f"{days:.0f} day" + ("" if round(days) == 1 else "s")


# ---------- Discord ----------

def send(title, lines, color, ping=False):
    content, allowed = "", {"parse": []}
    if ping and MENTION in ("everyone", "here"):
        content, allowed = f"@{MENTION}", {"parse": ["everyone"]}
    elif ping and MENTION:
        content, allowed = f"<@&{MENTION}>", {"parse": [], "roles": [MENTION]}
    http(WEBHOOK, {
        "username": "Webshare Usage",
        "content": content,
        "allowed_mentions": allowed,
        "embeds": [{
            "title": title[:256],
            "url": DASHBOARD,
            "description": "\n".join(lines)[:4000],
            "color": color,
        }],
    })
    time.sleep(1)


# ---------- state ----------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)


# ---------- account data ----------

def load_account():
    """Fetch the subscription and every active plan's usage for this period."""
    now = datetime.now(timezone.utc)
    sub = api("/subscription/")
    plans = active_plans(sub)
    start, end = parse_dt(sub["start_date"]), parse_dt(sub["end_date"])
    acct = {
        "now": now, "sub": sub, "start": start, "end": end,
        "days_left": (end - now).total_seconds() / 86400,
        "elapsed": max((now - start).total_seconds(), 3600),
        "period_secs": (end - start).total_seconds(),
        "auto_renew": bool(sub.get("renewals_enabled")),
        "plans": [],
    }
    for p in plans:
        used = bandwidth_used(p, start, now)
        limit_gb = p.get("bandwidth_limit") or 0
        row = {"plan": p, "name": plan_name(p), "used": used, "limit_gb": limit_gb}
        if limit_gb:
            row["limit"] = limit_gb * GB
            row["pct"] = used / row["limit"] * 100
            row["rate"] = used / acct["elapsed"]  # bytes per second so far this period
            row["projected"] = row["rate"] * acct["period_secs"]
        acct["plans"].append(row)
    return acct


def plan_line(acct, r):
    if not r["limit_gb"]:
        return f"{fmt_gb(r['used'])} used · unlimited bandwidth"
    return (f"{fmt_gb(r['used'])} of {r['limit_gb']:g} GB (**{r['pct']:.0f}%**) · "
            f"on pace for {fmt_gb(r['projected'])} by {acct['end'].astimezone(TZ):%b %d}")


def summary(acct=None):
    """Return (title, lines, color) for the usage summary. Used by the Discord bot too."""
    acct = acct or load_account()
    lines = [f"**{r['name']}**\n{plan_line(acct, r)}\n" for r in acct["plans"]] or ["No active plans found."]
    lines.append(f"Period ends {fmt_dt(acct['end'])} ({fmt_left(acct['days_left'])} left) · "
                 f"auto-renew {'on' if acct['auto_renew'] else '**off**'}")
    if acct["sub"].get("throttled"):
        lines.append("⚠️ Subscription is throttled")
    return "📊 Webshare usage summary", lines, GREEN


# ---------- main ----------

def main():
    missing = [n for n, v in (("WEBSHARE_API_KEY", API_KEY), ("DISCORD_WEBHOOK_URL", WEBHOOK)) if not v]
    if missing:
        sys.exit("Missing: " + ", ".join(missing))

    try:
        acct = load_account()
    except urllib.error.HTTPError as err:
        sys.exit(f"Webshare API error {err.code}. Check WEBSHARE_API_KEY. {err.read()[:300]!r}")

    sub, now, end = acct["sub"], acct["now"], acct["end"]
    days_left, auto_renew = acct["days_left"], acct["auto_renew"]

    # Alerts reset at the start of each billing period.
    state = load_state()
    if state.get("period") != sub["start_date"]:
        state = {"period": sub["start_date"], "fired": []}
    fired = set(state["fired"])

    # Bandwidth per plan
    for r in acct["plans"]:
        if not r["limit_gb"]:
            continue
        pid, name, used, limit, pct, rate = (r["plan"]["id"], r["name"], r["used"],
                                             r["limit"], r["pct"], r["rate"])
        crossed = [t for t in BW_ALERTS if pct >= t]
        if not crossed or f"bw:{pid}:{crossed[-1]}" in fired:
            continue
        top = crossed[-1]
        fired.update(f"bw:{pid}:{t}" for t in crossed)

        critical = top >= CRITICAL_PCT
        lines = [
            f"**Used:** {fmt_gb(used)} of {r['limit_gb']:g} GB ({pct:.1f}%)",
            f"**Remaining:** {fmt_gb(max(limit - used, 0))}",
            f"**Period ends:** {fmt_dt(end)} ({fmt_left(days_left)} left)",
        ]
        if rate > 0 and used < limit:
            runs_out = now + timedelta(seconds=(limit - used) / rate)
            if runs_out < end:
                lines.append(f"**At the current pace it runs out around {fmt_dt(runs_out)}**")
        if top >= 100:
            title, color = f"🛑 {name}: bandwidth limit reached", RED
        else:
            title, color = f"{'🚨' if critical else '⚠️'} {name}: {pct:.0f}% of bandwidth used", \
                ORANGE if critical else YELLOW
        send(title, lines, color, ping=critical)

    # Renewal / end of period
    due = [d for d in RENEW_DAYS if 0 < days_left <= d]
    if due and f"renew:{due[0]:g}" not in fired:
        fired.update(f"renew:{d:g}" for d in due)
        if auto_renew:
            send(f"🔁 Webshare renews in {fmt_left(days_left)}",
                 [f"**Renews:** {fmt_dt(end)}", "Auto-renew is on. Make sure the payment method on file is valid."],
                 BLUE)
        else:
            send(f"⏳ Webshare plan ends in {fmt_left(days_left)}: auto-renew is OFF",
                 [f"**Ends:** {fmt_dt(end)}",
                  "Proxies stop working at the end of the period unless the subscription is renewed."],
                 RED, ping=True)

    # Account problems (alert once while the problem lasts)
    failed = sub.get("failed_payment_times") or 0
    flags = [
        ("throttled", sub.get("throttled"), "🐢 Webshare subscription is throttled",
         "Webshare is slowing the proxies, usually because of high bandwidth use on a small number of proxies."),
        ("paused", sub.get("paused"), "⏸️ Webshare subscription is paused",
         "Proxies are paused" + (f" until {fmt_dt(parse_dt(sub['reactivation_date']))}."
                                 if sub.get("reactivation_date") else ".")),
        ("payment", failed > 0, "💳 Webshare renewal payment failed",
         f"The automatic renewal payment failed {failed} time(s). Check the payment method in the dashboard."),
    ]
    for key, active, title, text in flags:
        k = f"flag:{key}"
        if active and k not in fired:
            fired.add(k)
            send(title, [text], RED, ping=True)
        elif not active:
            fired.discard(k)

    if "--summary" in sys.argv:
        send(*summary(acct))

    state["fired"] = sorted(fired)
    save_state(state)
    print(f"Checked {len(acct['plans'])} plan(s); {days_left:.1f} days left in period.")


if __name__ == "__main__":
    main()
