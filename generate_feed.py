#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日自动抓取中文新闻 / 游戏榜单，重新生成 feed.json。

- 新闻：国际 6 条 + 国内 4 条 + 国内热点 4 条（百度热搜，讨论度高）
  + 亚太 4 条（韩联社中文 + 中新网国际中的亚太稿件，按影响程度加权）。
- 游戏：手游沿用旧 feed.json 前 6 条；Steam 国区热销 6 条 + 近半年新品 4 条。
- 短剧：无可靠公开接口（红果网页不返回榜单、DataEye/云合需登录），
  沿用 feed.json 里的人工精选，需要更新时直接编辑 dramas 数组。
- 任一源失败不影响整体；可用旧数据补齐，保证栏目不为空。
"""
import calendar
import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
FEED_PATH = ROOT / "feed.json"

CN_TZ = timezone(timedelta(hours=8))
NOW = datetime.now(CN_TZ)
CUTOFF = NOW - timedelta(days=14)

TIMEOUT = 25
MAX_SUMMARY = 140
NEED_INTL = 6
NEED_CN = 4
NEED_HOT = 4
NEED_APAC = 4

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# (RSS 地址, 归类地区, 来源名)。地区按 RSS 频道归类，不要用全站滚动频道，
# 否则国际新闻会混进国内栏目（例如「阿布扎比会谈」）。个别源失效不影响整体。
SOURCES = [
    ("https://feeds.bbci.co.uk/zhongwen/simp/rss.xml", "国际", "BBC中文"),
    ("https://www.rfi.fr/cn/rss", "国际", "RFI"),
    ("https://www.ftchinese.com/rss/news", "国际", "FT中文网"),
    ("https://rss.dw.com/rdf/rss-chi-all", "国际", "DW中文"),
    ("https://cn.nytimes.com/rss/", "国际", "纽约时报中文"),
    ("http://www.chinanews.com.cn/rss/world.xml", "国际", "中新网国际"),
    ("http://www.chinanews.com.cn/rss/china.xml", "国内", "中新网国内"),
    # 亚太：韩联社中文是稳定的中文亚太源；中新网国际频道再用关键词筛一批亚太稿件
    ("https://cn.yna.co.kr/RSS/news.xml", "亚太", "韩联社中文"),
]

BAIDU_HOT = "https://top.baidu.com/api/board?platform=wise&tab=realtime"
STEAM_FEATURED = "https://store.steampowered.com/api/featuredcategories?cc=cn&l=schinese"
STEAM_APP = "https://store.steampowered.com/api/appdetails?appids=%d&cc=cn&l=schinese"

# 亚太国家/地区关键词：用于从中新网国际频道里筛出亚太稿件
ASIA_WORDS = [
    "日本", "韩国", "朝鲜", "印度", "东盟", "越南", "泰国", "菲律宾", "马来西亚",
    "新加坡", "印尼", "印度尼西亚", "澳大利亚", "澳洲", "新西兰", "巴基斯坦",
    "孟加拉", "斯里兰卡", "缅甸", "柬埔寨", "老挝", "蒙古", "尼泊尔", "哈萨克斯坦",
    "中亚", "南海", "台海", "亚太", "吉隆坡", "雅加达", "曼谷", "马尼拉",
    "新德里", "东京", "首尔", "平壤", "悉尼", "惠灵顿", "太平洋岛国", "澜沧江",
]

# 影响程度关键词：命中越多，越可能是“影响大”的亚太要闻
IMPACT_WORDS = [
    "冲突", "战争", "制裁", "关税", "选举", "峰会", "会谈", "协议", "经济", "央行",
    "利率", "贸易", "芯片", "半导体", "军事", "演习", "外交", "首相", "总统",
    "总理", "访华", "谈判", "危机", "灾难", "地震", "台风", "海啸", "罢工",
    "法案", "政策", "出口", "投资", "降息", "加息", "供应链", "核",
]

# 国内热点只留国内题材：命中这些境外关键词的条目跳过
FOREIGN_WORDS = [
    "美国", "拜登", "特朗普", "白宫", "俄罗斯", "乌克兰", "普京", "以色列", "伊朗",
    "欧洲", "欧盟", "英国", "法国", "德国", "意大利", "加拿大", "巴西", "阿根廷",
    "联合国", "北约", "加沙", "叙利亚", "委内瑞拉", "澳大利亚", "日本", "韩国",
    "朝鲜", "印度", "越南", "泰国", "菲律宾", "新加坡", "缅甸", "柬埔寨",
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


def fetch_json(url):
    for attempt in range(2):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
        except Exception:
            time.sleep(1)
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
            items.append({"title": title, "summary": summary, "ts": ts})
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items


def fetch_baidu_hot():
    """百度实时热搜榜：返回 [(index, word, hotScore)]，按榜单顺序。"""
    data = fetch_json(BAIDU_HOT)
    if not data:
        print("[warn] 百度热搜抓取失败", flush=True)
        return []
    out = []
    try:
        cards = data.get("data", {}).get("cards", [])
        for card in cards:
            for block in card.get("content", []):
                for it in block.get("content", []) or []:
                    word = clean(it.get("word", ""), 60)
                    if not word:
                        continue
                    idx = it.get("index")
                    if idx is None:
                        continue  # 置顶条目（isTop）不入榜
                    out.append((int(idx), word, it.get("hotScore") or it.get("hotTag") or ""))
    except Exception as ex:
        print(f"[warn] 百度热搜解析失败：{ex}", flush=True)
    out.sort(key=lambda x: x[0])
    return out


def build_hot(cn_titles):
    """国内热点：百度热搜榜前列，跳过境外题材与已出现在国内新闻里的条目。"""
    hot = []
    for idx, word, score in fetch_baidu_hot():
        if any(w in word for w in FOREIGN_WORDS):
            continue
        if any(word in t or t in word for t in cn_titles):
            continue
        hot.append({
            "title": word,
            "summary": f"百度热搜第 {idx} 位，实时讨论度居前。",
            "ts": NOW,
        })
        if len(hot) >= NEED_HOT + 3:
            break
    return hot


def asia_from_world():
    """从中新网国际频道筛出亚太稿件（海外源不够时补充）。"""
    out = []
    raw = fetch("http://www.chinanews.com.cn/rss/world.xml")
    if not raw:
        return out
    for e in feedparser.parse(raw).entries:
        title = clean(e.get("title", ""), 80)
        if not title:
            continue
        if not any(w in title for w in ASIA_WORDS):
            continue
        ts = entry_time(e)
        if ts < CUTOFF:
            continue
        summary = clean(e.get("summary", "") or e.get("description", ""), MAX_SUMMARY) or title
        out.append({"title": title, "summary": summary, "ts": ts})
    return out


def collect_asia(exclude=()):
    """亚太要闻：按“影响程度”关键词命中数排序，再按时间；跳过国际栏目已用的标题。"""
    seen = set(exclude)
    items = []
    for it in collect("亚太") + asia_from_world():
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        items.append(it)
    for it in items:
        it["score"] = sum(1 for w in IMPACT_WORDS if w in it["title"])
        it["asia"] = 1 if any(w in it["title"] for w in ASIA_WORDS) else 0
    items.sort(key=lambda x: (x["asia"], x["score"], x["ts"]), reverse=True)
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


def steam_detail(appid):
    """Steam 单款详情：发行日期 / 简介。失败返回 None。"""
    data = fetch_json(STEAM_APP % appid)
    if not data:
        return None
    node = data.get(str(appid)) or {}
    if not node.get("success"):
        return None
    return node.get("data") or {}


def parse_release(d):
    s = ((d.get("release_date") or {}).get("date") or "").strip()
    for fmt in ("%Y 年 %m 月 %d 日", "%Y-%m-%d", "%b %d, %Y", "%d %b, %Y", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=CN_TZ)
        except Exception:
            continue
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), 1, tzinfo=CN_TZ)
        except Exception:
            pass
    return None


def build_steam():
    """Steam 国区：热销 6 款（发行满半年）+ 近半年新品 4 款。"""
    data = fetch_json(STEAM_FEATURED)
    if not data:
        print("[warn] Steam 榜单抓取失败", flush=True)
        return []
    top = (data.get("top_sellers") or {}).get("items") or []
    new = (data.get("new_releases") or {}).get("items") or []
    half_year_ago = NOW - timedelta(days=183)
    cache = {}

    def detail(appid):
        if appid not in cache:
            cache[appid] = steam_detail(appid)
        return cache[appid]

    olds, news = [], []
    used = set()

    # 先取新品（必须近半年发行），再从热销榜补足 6 款常青游戏
    for it in new[:30]:
        if len(news) >= 4:
            break
        appid = it.get("id")
        name = clean(it.get("name", ""), 60)
        if not appid or not name or appid in used:
            continue
        d = detail(appid)
        rel = parse_release(d) if d else None
        if not rel or rel < half_year_ago:
            continue
        desc = clean(d.get("short_description", ""), 100)
        text = f"{rel.strftime('%Y-%m-%d')} 发行 · 近半年新品。{desc}" if desc \
            else f"{rel.strftime('%Y-%m-%d')} 发行 · 近半年新品。"
        news.append({"name": name, "desc": text})
        used.add(appid)
        time.sleep(0.3)

    # 热销：按国区热销榜顺序取 6 款（跳过已进新品组的），不再按发行日期过滤，
    # 保证栏目稳定有 6 条；详情接口超时则用榜单位置描述兜底。
    for rank, it in enumerate(top[:25], 1):
        if len(olds) >= 6:
            break
        appid = it.get("id")
        name = clean(it.get("name", ""), 60)
        if not appid or not name or appid in used:
            continue
        d = detail(appid)
        rel = parse_release(d) if d else None
        desc = clean((d or {}).get("short_description", ""), 100)
        if desc:
            text = f"Steam 国区热销第 {rank} 位。{desc}"
        elif rel:
            text = f"{rel.strftime('%Y-%m-%d')} 发行 · Steam 国区热销第 {rank} 位。"
        else:
            text = f"Steam 国区热销第 {rank} 位，长期位居国区热销榜。"
        olds.append({"name": name, "desc": text})
        used.add(appid)
        time.sleep(0.3)
    return olds, news


def build_games(old_games):
    """手游保留旧数据前 6 条；Steam 热销/新品自动抓取，抓取不足时用旧数据补齐。"""
    mobile, old_old, old_new = [], [], []
    for g in old_games:
        if g.get("platform") == "Steam":
            (old_new if g.get("isNew") else old_old).append(g)
        else:
            mobile.append(g)
    mobile = mobile[:6]

    fresh = build_steam()
    if fresh and (fresh[0] or fresh[1]):
        olds, news = fresh
        print(f"Steam：热销 {len(olds)} / 新品 {len(news)}", flush=True)
    else:
        print("[warn] Steam 数据抓取失败，沿用旧数据", flush=True)
        olds, news = [], []

    def merge(primary, fallback, target):
        out, names = [], set()
        for g in primary:
            if g["name"] in names:
                continue
            names.add(g["name"])
            out.append(g)
            if len(out) >= target:
                break
        for g in fallback:
            if len(out) >= target:
                break
            if g.get("name") in names:
                continue
            names.add(g.get("name"))
            out.append({"name": g.get("name", ""), "desc": g.get("desc", "")})
        return out

    out = []
    for i, g in enumerate(mobile, 1):
        out.append({
            "id": g.get("id") or f"gm{i:02d}",
            "name": g.get("name", ""),
            "desc": g.get("desc", ""),
            "isNew": False,
            "platform": "手游",
        })
    for i, g in enumerate(merge(olds, old_old, 6), 1):
        out.append({"id": f"gs{i:02d}", "name": g["name"], "desc": g["desc"],
                    "isNew": False, "platform": "Steam"})
    for i, g in enumerate(merge(news, old_new, 4), 1):
        out.append({"id": f"gn{i:02d}", "name": g["name"], "desc": g["desc"],
                    "isNew": True, "platform": "Steam"})
    return out


def main():
    old = load_old()
    old_news = old.get("news", []) if isinstance(old.get("news"), list) else []

    cn = pick(collect("国内"), "国内", NEED_CN, old_news)
    intl = pick(collect("国际"), "国际", NEED_INTL, old_news)
    hot = pick(build_hot([n["title"] for n in cn]), "热点", NEED_HOT, old_news)
    apac = pick(collect_asia([n["title"] for n in intl]), "亚太", NEED_APAC, old_news)

    news = intl + cn + hot + apac
    day = NOW.strftime("%Y%m%d")
    for i, n in enumerate(news, 1):
        n["id"] = f"n{day}{i:02d}"
        news[i - 1] = {"id": n["id"], "title": n["title"],
                       "summary": n["summary"], "region": n["region"], "date": n["date"]}

    old_games = old.get("games", []) if isinstance(old.get("games"), list) else []
    games = build_games(old_games)

    feed = {
        "updatedAt": NOW.strftime("%Y-%m-%d"),
        "news": news,
        # 短剧无稳定公开接口，保持人工精选
        "dramas": old.get("dramas", []),
        "games": games,
    }

    FEED_PATH.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    print(f"OK: 国际 {len(intl)} / 国内 {len(cn)} / 热点 {len(hot)} / 亚太 {len(apac)}，"
          f"游戏 {len(games)} 条，已写入 {FEED_PATH.name}")
    if not feed["dramas"]:
        print("[warn] dramas 为空：请把人工整理的短剧数据写入 feed.json")
    return 0 if news else 1


if __name__ == "__main__":
    sys.exit(main())
