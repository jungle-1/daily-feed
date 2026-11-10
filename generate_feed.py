#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日自动抓取中文新闻源，重新生成 feed.json 的新闻部分。

- 新闻：从多个 RSS 源抓取近 14 天内容，国际 6 条 + 国内 4 条，按时间倒序。
- 短剧 / 游戏：无可靠公开接口，沿用仓库中旧 feed.json 的对应数据；
  需要更新时直接在 GitHub 网页编辑 feed.json 的 dramas / games 数组即可。
- 任一源抓取失败不影响整体；当天抓取不足时用旧数据补齐，保证栏目不为空。
"""
import calendar
import html
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
FEED_PATH = ROOT / "feed.json"

CN_TZ = timezone(timedelta(hours=8))
NOW = datetime.now(CN_TZ)
CUTOFF = NOW - timedelta(days=14)

TIMEOUT = 20
MAX_SUMMARY = 140
NEED_INTL = 6
NEED_CN = 4

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) feed-bot/1.0"

# (RSS 地址, 归类地区, 来源名)。国内新闻用中新网；国际用多家海外中文媒体，
# 均对海外（GitHub Actions 运行环境）访问友好；个别源失效不影响整体。
# 注：联合早报的 /rss/realtime/* 已失效（返回 HTML 页面、无 RSS 条目），已移除。
SOURCES = [
    ("https://feeds.bbci.co.uk/zhongwen/simp/rss.xml", "国际", "BBC中文"),
    ("https://www.rfi.fr/cn/rss", "国际", "RFI"),
    ("https://www.ftchinese.com/rss/news", "国际", "FT中文网"),
    ("https://rss.dw.com/rdf/rss-chi-all", "国际", "DW中文"),
    ("https://cn.nytimes.com/rss/", "国际", "纽约时报中文"),
    ("http://www.chinanews.com.cn/rss/scroll-news.xml", "国内", "中新网"),
]

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def clean(text, limit):
    text = html.unescape(TAG_RE.sub("", text or ""))
    text = SPACE_RE.sub(" ", text).strip()
    return text[:limit]


def entry_time(entry):
    # feedparser 的 published_parsed 是 UTC struct_time，用 calendar.timegm 正确转换
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return NOW
    try:
        return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc).astimezone(CN_TZ)
    except Exception:
        return NOW


def fetch(url):
    for attempt in range(2):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            if r.status_code == 200 and r.content:
                return r.content
        except Exception:
            pass
    return None


def collect(region):
    """抓取某地区的所有条目：{title, summary, date, ts}，按时间倒序、按标题去重。"""
    seen = set()
    items = []
    for url, reg, source in SOURCES:
        if reg != region:
            continue
        raw = fetch(url)
        if not raw:
            print(f"[warn] 抓取失败：{source} {url}", flush=True)
            continue
        feed = feedparser.parse(raw)
        for e in feed.entries:
            title = clean(e.get("title", ""), 80)
            if not title or title in seen:
                continue
            ts = entry_time(e)
            if ts < CUTOFF:
                continue
            summary = clean(e.get("summary", "") or e.get("description", ""), MAX_SUMMARY)
            if not summary:
                summary = title
            seen.add(title)
            items.append({
                "title": title,
                "summary": summary,
                "ts": ts,
            })
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items


def load_old():
    if not FEED_PATH.exists():
        return {}
    try:
        return json.loads(FEED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def pick(items, region, need, old_news):
    """取前 need 条；不足时用旧 feed.json 中同地区新闻补齐。"""
    out = []
    for it in items[:need]:
        out.append({
            "title": it["title"],
            "summary": it["summary"],
            "region": region,
            "date": it["ts"].strftime("%Y-%m-%d"),
        })
    if len(out) < need:
        have = {x["title"] for x in out}
        for o in old_news:
            if o.get("region") != region or o.get("title") in have:
                continue
            out.append({
                "title": o.get("title", ""),
                "summary": o.get("summary", ""),
                "region": region,
                "date": o.get("date", ""),
            })
            have.add(o.get("title", ""))
            if len(out) >= need:
                break
    return out


def main():
    old = load_old()
    old_news = old.get("news", []) if isinstance(old.get("news"), list) else []

    intl = pick(collect("国际"), "国际", NEED_INTL, old_news)
    cn = pick(collect("国内"), "国内", NEED_CN, old_news)

    news = intl + cn
    day = NOW.strftime("%Y%m%d")
    for i, n in enumerate(news, 1):
        n["id"] = f"n{day}{i:02d}"
        # 字段顺序与 App 端约定一致：id 在前便于阅读
        n_ordered = {"id": n["id"], "title": n["title"],
                     "summary": n["summary"], "region": n["region"], "date": n["date"]}
        news[i - 1] = n_ordered

    feed = {
        "updatedAt": NOW.strftime("%Y-%m-%d"),
        "news": news,
        # 短剧 / 游戏沿用旧数据；需要更新时手动编辑本文件
        "dramas": old.get("dramas", []),
        "games": old.get("games", []),
    }

    FEED_PATH.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    print(f"OK: 国际 {len(intl)} 条 / 国内 {len(cn)} 条，已写入 {FEED_PATH.name}")
    if not feed["dramas"]:
        print("[warn] dramas 为空：首次运行请把 feed_sample.json 的内容拷入 feed.json")
    if not feed["games"]:
        print("[warn] games 为空：首次运行请把 feed_sample.json 的内容拷入 feed.json")
    return 0 if news else 1


if __name__ == "__main__":
    sys.exit(main())
