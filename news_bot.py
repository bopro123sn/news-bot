"""Daily news digest (English): Al Jazeera + Yahoo Finance -> topic filter + top movers -> Gemini -> Telegram."""
import os
import re
import sys
import time
import calendar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests
import trafilatura

GEO_KEYWORDS = [
    "iran", "tehran", "hormuz", "trump", "vance", "white house", "pentagon",
    "united states", "u.s.", " us ", "washington", "ceasefire", "blockade",
    "israel", "houthi", "war", "tariff", "sanction",
]
MARKET_KEYWORDS = [
    "stock", "stocks", "market", "shares", "s&p", "nasdaq", "dow", "fed ",
    "federal reserve", "rate", "inflation", "earnings", "oil", "treasury",
    "dollar", "rally", "selloff", "surge", "plunge", "jump", "tumble",
]
# (feed url, keywords an item must match)
# Both feeds are read in full for the last 24h; the keywords only mark priority items.
FEEDS = [
    ("https://www.aljazeera.com/xml/rss/all.xml", None),
    ("https://finance.yahoo.com/news/rssindex", None),
]
PRIORITY_KEYWORDS = GEO_KEYWORDS + MARKET_KEYWORDS
MAX_FULL_BODIES = 10
LOCAL_TZ = ZoneInfo("America/Toronto")
SEND_HOUR = 8
LOOKBACK_HOURS = 24
MAX_ITEMS = 40
MAX_CHARS_PER_ITEM = 3000
MOVERS_PER_SIDE = 5
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
UA = {"User-Agent": "Mozilla/5.0 (personal news digest bot)"}


def matches(text: str, keywords) -> bool:
    t = f" {text.lower()} "
    return any(k in t for k in keywords)


def fetch_items():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    items, seen = [], set()
    for url, keywords in FEEDS:
        try:
            feed = feedparser.parse(url, request_headers=UA)
        except Exception as ex:
            print(f"feed failed {url}: {ex}", file=sys.stderr)
            continue
        for e in feed.entries:
            link = e.get("link", "")
            if not link or link in seen:
                continue
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if ts:
                published = datetime.fromtimestamp(calendar.timegm(ts), timezone.utc)
                if published < cutoff:
                    continue
            title = e.get("title", "")
            summary = re.sub(r"<[^>]+>", "", e.get("summary", ""))
            if keywords and not matches(f"{title} {summary}", keywords):
                continue
            seen.add(link)
            source = "Al Jazeera" if "aljazeera" in url else "Yahoo Finance"
            priority = matches(f"{title} {summary}", PRIORITY_KEYWORDS)
            items.append({"title": title, "link": link, "summary": summary,
                          "source": source, "priority": priority})
    # Balanced mix from both sources, priority-topic items first within each.
    half = MAX_ITEMS // 2
    out = []
    for src in ("Al Jazeera", "Yahoo Finance"):
        mine = [i for i in items if i["source"] == src]
        mine.sort(key=lambda i: not i["priority"])  # stable: keeps feed order
        out += mine[:half]
    return out


def fetch_body(item) -> str:
    try:
        r = requests.get(item["link"], headers=UA, timeout=20)
        r.raise_for_status()
        text = trafilatura.extract(r.text) or ""
    except Exception as ex:  # fall back to the RSS summary
        print(f"body fetch failed for {item['link']}: {ex}", file=sys.stderr)
        text = ""
    return (text or item["summary"])[:MAX_CHARS_PER_ITEM]


def ticker_headlines(symbol: str, limit: int = 4):
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
    try:
        feed = feedparser.parse(url, request_headers=UA)
        return [
            f"- {e.get('title', '')}: {re.sub(r'<[^>]+>', '', e.get('summary', ''))[:300]} ({e.get('link', '')})"
            for e in feed.entries[:limit]
        ]
    except Exception as ex:
        print(f"ticker feed failed {symbol}: {ex}", file=sys.stderr)
        return []


def fetch_movers() -> str:
    """Top gainers/losers from Yahoo's screener, with recent headlines per ticker."""
    try:
        import yfinance as yf
    except Exception as ex:
        print(f"yfinance unavailable: {ex}", file=sys.stderr)
        return ""
    sections = []
    for label, screen in (("TOP GAINERS", "day_gainers"), ("TOP LOSERS", "day_losers")):
        try:
            quotes = yf.screen(screen, count=MOVERS_PER_SIDE).get("quotes", [])
        except Exception as ex:
            print(f"screener {screen} failed: {ex}", file=sys.stderr)
            continue
        lines = []
        for q in quotes[:MOVERS_PER_SIDE]:
            sym = q.get("symbol", "")
            pct = q.get("regularMarketChangePercent")
            price = q.get("regularMarketPrice")
            pct_s = f"{pct:+.1f}%" if isinstance(pct, (int, float)) else "n/a"
            lines.append(f"{sym} ({q.get('shortName', '')}) {pct_s} at {price}")
            lines.extend(ticker_headlines(sym))
        if lines:
            sections.append(f"{label}:\n" + "\n".join(lines))
    return "\n\n".join(sections)


def summarize(items, movers: str) -> str:
    blocks, full = [], 0
    for i, it in enumerate(items, 1):
        if it["priority"] and full < MAX_FULL_BODIES:
            body, full = fetch_body(it), full + 1
        else:
            body = it["summary"][:600]
        blocks.append(f"[{i}] ({it['source']}) {it['title']}\n{it['link']}\n{body}")
    prompt = (
        "You are a news editor writing a morning briefing in clear, concise English for one reader. "
        "The articles below are everything Al Jazeera and Yahoo Finance published in the last 24 hours. "
        "Pick the most important content and summarize it, giving priority to these topics: the United "
        "States (including Donald Trump), wars and conflicts around the world, and stock-market news. "
        "Use ONLY the material below; do not add outside facts. Attribute claims made by one side "
        "(e.g. 'Tehran says...'). Plain text, no markdown, about 3,500 characters maximum.\n\n"
        "Structure:\n"
        "1) TOP HEADLINES: the 3-5 most important stories of the last 24 hours.\n"
        "2) UNITED STATES: the most important news about the US, including Donald Trump and the "
        "administration (politics, policy, economy, US-Iran relations), 1-2 sentences each, with the "
        "source link.\n"
        "   WORLD CONFLICTS: the state of wars and armed conflicts around the world (Iran/Middle East, "
        "Ukraine-Russia, Gaza, and any others in the news), 1-2 sentences each, with the source link.\n"
        "3) MARKETS: main market and economy news, then TOP MOVERS: for the biggest gainers and "
        "losers give ticker, % move, and the likely reason based ONLY on the headlines supplied for "
        "that ticker. If they give no clear reason, say 'no clear catalyst in the news provided'.\n"
        "4) ALSO WORTH KNOWING: up to 3 other major stories outside these topics, one line each.\n\n"
        "=== ARTICLES ===\n" + "\n\n---\n\n".join(blocks)
        + "\n\n=== TOP MOVERS DATA ===\n" + (movers or "Not available today.")
    )
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def send_telegram(text: str):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    for i in range(0, len(text), 4000):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text[i:i + 4000],
                  "disable_web_page_preview": True},
            timeout=30,
        )
        r.raise_for_status()
        time.sleep(1)


def main():
    # Workflow fires at two UTC times to cover daylight saving; only send at 8am Toronto.
    if os.environ.get("FORCE") != "1" and datetime.now(LOCAL_TZ).hour != SEND_HOUR:
        print("Not 8am in Toronto, skipping.")
        return
    items = fetch_items()
    movers = fetch_movers()
    if not items and not movers:
        send_telegram("No new articles matching your topics in the last 24 hours.")
        return
    send_telegram(f"Morning briefing - {datetime.now(LOCAL_TZ):%b %d, %Y}\n\n{summarize(items, movers)}")


if __name__ == "__main__":
    main()
