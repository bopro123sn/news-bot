"""Daily news digest: Al Jazeera RSS -> filter by topic -> Gemini summary -> Telegram."""
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

FEEDS = [
    "https://www.aljazeera.com/xml/rss/all.xml",
]
KEYWORDS = [
    "iran", "tehran", "hormuz", "trump", "vance", "white house", "pentagon",
    "united states", "u.s.", " us ", "washington", "ceasefire", "blockade",
    "israel", "houthi", "war",
]
LOCAL_TZ = ZoneInfo("America/Toronto")
SEND_HOUR = 8
LOOKBACK_HOURS = 24
MAX_ITEMS = 15
MAX_CHARS_PER_ITEM = 3500
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
UA = {"User-Agent": "Mozilla/5.0 (personal news digest bot)"}


def matches(text: str) -> bool:
    t = f" {text.lower()} "
    return any(k in t for k in KEYWORDS)


def fetch_items():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    items, seen = [], set()
    for url in FEEDS:
        feed = feedparser.parse(url, request_headers=UA)
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
            if not matches(f"{title} {summary}"):
                continue
            seen.add(link)
            items.append({"title": title, "link": link, "summary": summary})
    return items[:MAX_ITEMS]


def fetch_body(item) -> str:
    try:
        r = requests.get(item["link"], headers=UA, timeout=20)
        r.raise_for_status()
        text = trafilatura.extract(r.text) or ""
    except Exception as ex:  # fall back to the RSS summary
        print(f"body fetch failed for {item['link']}: {ex}", file=sys.stderr)
        text = ""
    return (text or item["summary"])[:MAX_CHARS_PER_ITEM]


def summarize(items) -> str:
    blocks = []
    for i, it in enumerate(items, 1):
        blocks.append(f"[{i}] {it['title']}\n{it['link']}\n{fetch_body(it)}")
    prompt = (
        "Bạn là biên tập viên tin tức. Dưới đây là các bài từ Al Jazeera trong 24 giờ qua. "
        "Hãy viết bản tin buổi sáng bằng tiếng Việt, tập trung vào: Mỹ, Donald Trump, "
        "quan hệ Mỹ-Iran và tình hình chiến sự. Cấu trúc: (1) 3-5 gạch đầu dòng điểm tin "
        "quan trọng nhất, (2) các diễn biến chính theo từng chủ đề, mỗi ý 1-2 câu và kèm link "
        "bài gốc. Chỉ dùng thông tin trong các bài, không bịa, nêu rõ khi nguồn chỉ là "
        "tuyên bố của một bên. Tối đa khoảng 3000 ký tự, không dùng markdown.\n\n"
        + "\n\n---\n\n".join(blocks)
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
    if not items:
        send_telegram("Hôm nay không có bài mới nào khớp chủ đề trên Al Jazeera.")
        return
    send_telegram(f"Bản tin sáng {datetime.now(LOCAL_TZ):%d/%m/%Y}\n\n{summarize(items)}")


if __name__ == "__main__":
    main()
