#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ห้องเฝ้าทอง · Gold News Bot
ดึงข่าวที่ส่งผลต่อราคาทองคำจาก Google News RSS แล้วส่งเข้า Telegram
รันอัตโนมัติทุกชั่วโมงผ่าน GitHub Actions — ส่งเฉพาะข่าวใหม่ที่ยังไม่เคยส่ง

ตั้งค่าได้ผ่าน environment variables:
  TELEGRAM_TOKEN    (จำเป็นตอนใช้งานจริง — ถ้าไม่ใส่จะเป็นโหมดทดสอบ พิมพ์ออกจอแทน)
  TELEGRAM_CHAT_ID  (จำเป็นตอนใช้งานจริง)
  MAX_PER_RUN       (ไม่บังคับ, ค่าเริ่มต้น 8 — กันสแปมต่อรอบ)
  LANG_HL           (ไม่บังคับ, ค่าเริ่มต้น en-US — เปลี่ยนเป็น th ได้ถ้าอยากได้ข่าวไทย)
"""

import os
import re
import json
import time
import html
import pathlib
from datetime import datetime, timezone, timedelta

import requests
import feedparser

# ---------- ตั้งค่า ----------
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "8"))
HL = os.environ.get("LANG_HL", "en-US")
GL = "US" if HL.startswith("en") else HL.split("-")[-1].upper()

DRY_RUN = not (TG_TOKEN and TG_CHAT)          # ไม่มี token = โหมดทดสอบ
BANGKOK = timezone(timedelta(hours=7))         # เวลาไทย
STATE_FILE = pathlib.Path("state/seen.json")
SEEN_LIMIT = 600                               # เก็บ id ล่าสุดเท่านี้ กันไฟล์โต

# คำค้นที่โฟกัสเฉพาะปัจจัยที่ขยับราคาทอง
QUERIES = [
    'gold price XAU',
    'gold Federal Reserve interest rate',
    'SPDR gold OR central bank gold buying',
    'gold safe haven war OR geopolitics',
]

# ===== สำนักข่าวใหญ่ต่างประเทศที่ออกข่าวไว (allowlist) =====
# ถ้า STRICT_SOURCES=1 (ค่าเริ่มต้น) จะส่งเฉพาะข่าวจากสำนักในลิสต์นี้เท่านั้น
# เทียบแบบไม่สนตัวพิมพ์ใหญ่เล็ก ขอแค่ชื่อแหล่งมีคำใดคำหนึ่งนี้อยู่
ALLOWED_SOURCES = [
    "reuters", "bloomberg", "cnbc", "financial times", "ft.com",
    "wall street journal", "wsj", "marketwatch", "barron",
    "fxstreet", "investing.com", "kitco", "dailyfx",
    "associated press", "ap news", "agence france", "afp",
    "yahoo finance", "forexlive", "mining.com",
]
STRICT_SOURCES = os.environ.get("STRICT_SOURCES", "1").strip() not in ("0", "false", "no", "")


def source_allowed(source: str) -> bool:
    if not STRICT_SOURCES:
        return True
    low = (source or "").lower()
    return any(name in low for name in ALLOWED_SOURCES)

# จับหมวดจากคีย์เวิร์ดในพาดหัว (ไม่มีการชี้ขึ้น/ลง เพื่อไม่ให้เป็นสัญญาณเทรด)
CATEGORIES = [
    ("🌍 สงคราม/ภูมิรัฐศาสตร์",
     r"\bwars?\b|conflict|iran|israel|hormuz|russia|ukraine|middle east|missile|geopolit|air ?strike|ceasefire|peace deal|sanction|tariff"),
    ("🏛️ Fed/ดอกเบี้ย/เงินเฟ้อ",
     r"\bfed\b|federal reserve|interest rate|rate cut|rate hike|fomc|powell|warsh|yield|treasur|cpi|inflation|jobs|payroll"),
    ("📊 กองทุน/ธนาคารกลาง",
     r"spdr|gld|etf|holdings|central bank|reserve|tonnes|bullion demand|inflow|outflow"),
    ("💵 ดอลลาร์",
     r"\bdollar\b|\busd\b|dxy|greenback"),
]
DEFAULT_CAT = "📰 ข่าวทองทั่วไป"


def categorize(text: str) -> str:
    low = text.lower()
    for label, pat in CATEGORIES:
        if re.search(pat, low):
            return label
    return DEFAULT_CAT


def load_seen() -> set:
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(seen: set):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # เก็บแค่ล่าสุด SEEN_LIMIT รายการ
    trimmed = list(seen)[-SEEN_LIMIT:]
    STATE_FILE.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")


def fetch_entries():
    items = {}
    for q in QUERIES:
        url = ("https://news.google.com/rss/search?q="
               + requests.utils.quote(q + " when:1d")
               + f"&hl={HL}&gl={GL}&ceid={GL}:{HL.split('-')[0]}")
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[warn] ดึงฟีดไม่สำเร็จ ({q}): {e}")
            continue
        for e in feed.entries:
            uid = e.get("id") or e.get("link")
            if not uid:
                continue
            title = e.get("title", "").strip()
            # Google News ใส่ชื่อแหล่งไว้ท้ายพาดหัวแบบ " - Source"
            source = ""
            src = e.get("source")
            if src and getattr(src, "title", None):
                source = src.title
            if not source and " - " in title:
                title, source = title.rsplit(" - ", 1)
            # กรองให้เหลือเฉพาะสำนักข่าวใหญ่ที่กำหนด
            if not source_allowed(source):
                continue
            # เวลา
            published = None
            if e.get("published_parsed"):
                published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            items[uid] = {
                "id": uid,
                "title": title.strip(),
                "source": source.strip(),
                "link": e.get("link", ""),
                "published": published,
            }
    # เรียงใหม่สุดก่อน
    out = list(items.values())
    out.sort(key=lambda x: x["published"] or datetime(1970, 1, 1, tzinfo=timezone.utc),
             reverse=True)
    return out


def fmt_time(dt):
    if not dt:
        return ""
    return dt.astimezone(BANGKOK).strftime("%d %b %H:%M น.")


def build_message(it):
    cat = categorize(it["title"] + " " + it["source"])
    title = html.escape(it["title"])
    src = html.escape(it["source"]) if it["source"] else "ข่าว"
    t = fmt_time(it["published"])
    meta = " · ".join(x for x in [src, t] if x)
    return (f"{cat}\n"
            f"<b>{title}</b>\n"
            f"{meta}\n"
            f'<a href="{html.escape(it["link"])}">เปิดอ่าน ↗</a>')


def send_telegram(text):
    if DRY_RUN:
        print("\n----- [โหมดทดสอบ จะส่งข้อความนี้เข้า Telegram] -----")
        print(text)
        return True
    api = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(api, data={
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }, timeout=30)
    if not r.ok:
        print(f"[error] Telegram ส่งไม่สำเร็จ {r.status_code}: {r.text[:200]}")
        return False
    return True


def main():
    print(f"== Gold News Bot == {datetime.now(BANGKOK):%Y-%m-%d %H:%M} "
          f"({'โหมดทดสอบ' if DRY_RUN else 'ส่งจริง'})")
    seen = load_seen()
    first_run = len(seen) == 0
    entries = fetch_entries()
    print(f"ดึงข่าวมาได้ {len(entries)} ชิ้น, เคยส่งแล้ว {len(seen)} ชิ้น")

    fresh = [e for e in entries if e["id"] not in seen]

    if first_run:
        # รอบแรก: กันสแปม — ส่งแค่ 3 ข่าวล่าสุด ที่เหลือมาร์คว่าเคยเห็นแล้ว
        to_send = fresh[:3]
        if not DRY_RUN:
            send_telegram("✅ <b>ห้องเฝ้าทองออนไลน์แล้ว</b>\nจะส่งข่าวทองที่สำคัญให้ทุกชั่วโมงโดยอัตโนมัติ")
        for e in entries:
            seen.add(e["id"])
    else:
        to_send = fresh[:MAX_PER_RUN]

    sent = 0
    for e in to_send:
        if send_telegram(build_message(e)):
            seen.add(e["id"])
            sent += 1
            time.sleep(1)   # เว้นจังหวะกัน rate limit
    # มาร์คข่าวใหม่ที่เกิน MAX_PER_RUN ว่าเคยเห็นแล้ว (จะไม่ส่งซ้ำรอบหน้า)
    for e in fresh:
        seen.add(e["id"])

    save_seen(seen)
    print(f"ส่งข่าวใหม่ {sent} ชิ้น (พบใหม่ทั้งหมด {len(fresh)} ชิ้น)")


if __name__ == "__main__":
    main()
