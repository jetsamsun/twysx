#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""twysx 极简博客生成器（只依赖 Python 标准库，Python >= 3.8）。

  python blog.py build        本地用：重建首页、文章页（受 git 跟踪）和新闻页（news/，git 忽略）
  python blog.py daily        服务器 cron 用：抓取当天 10 条国际热门新闻，只重建 news/
  python blog.py daily --force   当天已满 10 条时也强制重抓
  python blog.py news         只根据 news/data/*.json 重建 news/ 下的页面

文章：posts/*.md（头部 title/date 两行元数据）  ->  p/<slug>.html
新闻：服务器每天写 news/data/YYYY-MM-DD.json  ->  news/ 下的页面，这些文件不进 git
"""
from __future__ import annotations

import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = "twysx"
TZ = timezone(timedelta(hours=8))  # 新闻按北京时间分天
POSTS_DIR = ROOT / "posts"
OUT_POSTS = ROOT / "p"
NEWS_DIR = ROOT / "news"
DATA_DIR = NEWS_DIR / "data"

NEWS_PER_DAY = 10      # 每天发布条数
PER_SOURCE_CAP = 4     # 同一家媒体最多占几条，避免被一家刷屏
PER_FEED_LIMIT = 15    # 每个源只看前 N 条（RSS 本身按编辑重要性/时间排序）
MAX_AGE_H = 48         # 只收 48 小时内的新闻
SEEN_DAYS = 7          # 近 7 天发布过的不再重复
UA = "Mozilla/5.0 (compatible; twysx-news/1.0; +https://twysx.com)"

# (名称, RSS 地址, 权重)
FEEDS = [
    ("BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml", 1.0),
    ("The Guardian", "https://www.theguardian.com/world/rss", 1.0),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", 0.8),  # 该源混有专题/互动，降权
    ("NPR", "https://feeds.npr.org/1004/rss.xml", 0.9),
    ("DW", "https://rss.dw.com/xml/rss-en-top", 0.8),
    ("France 24", "https://www.france24.com/en/rss", 0.8),
]


# ----------------------------------------------------------------- 通用工具

def log(msg):
    print("[%s] %s" % (datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def esc(s):
    return html.escape(str(s), quote=True)


def esc_text(s):
    return html.escape(str(s), quote=False)


def write(path, text):
    """原子写文件（UTF-8、LF）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def safe_http(u):
    return bool(re.match(r"^https?://", (u or "").strip(), re.I))


def safe_url(u):
    u = (u or "").strip()
    if re.match(r"^(https?://|mailto:|/|#)", u, re.I):
        return u
    if re.match(r"^[a-z][a-z0-9+.\-]*:", u, re.I):  # javascript:、data: 等一律拒绝
        return "#"
    return u


def slugify(s, n=50):
    s = re.sub(r"['’‘`]", "", s.lower())  # Ukraine's -> ukraines，不拆成 ukraine-s
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:n].strip("-") or "news"


# ----------------------------------------------------------------- 页面外壳

def page(title, body, desc="", extra_head=""):
    full = "%s · %s" % (title, SITE) if title else SITE
    year = datetime.now(TZ).year
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>%s</title>\n"
        '<meta name="description" content="%s">\n'
        '<link rel="stylesheet" href="/style.css">\n'
        "%s</head>\n<body>\n<div class=\"wrap\">\n"
        '<header class="site"><a class="brand" href="/">%s<span>.</span></a>'
        '<nav><a href="/">文章</a><a href="/news/">国际新闻</a></nav></header>\n'
        "<main>\n%s\n</main>\n"
        '<footer class="site">© %d %s</footer>\n</div>\n</body>\n</html>\n'
    ) % (esc_text(full), esc(desc), extra_head, SITE, body, year, SITE)


# ----------------------------------------------------------------- Markdown（够用的子集）

INLINE = re.compile(
    r"!\[([^\]]*)\]\(([^)\s]+)\)"      # 1 alt, 2 src
    r"|\[([^\]]+)\]\(([^)\s]+)\)"      # 3 text, 4 href
    r"|\*\*([^*\n]+)\*\*"              # 5 bold
    r"|\*([^*\n]+)\*"                  # 6 italic
)


def md_text(s):
    out, pos = [], 0
    for part in re.split(r"(`[^`\n]+`)", s):
        if len(part) > 2 and part.startswith("`") and part.endswith("`"):
            out.append("<code>%s</code>" % esc_text(part[1:-1]))
            continue
        pos = 0
        for m in INLINE.finditer(part):
            out.append(esc_text(part[pos:m.start()]))
            if m.group(2) is not None:
                out.append('<img src="%s" alt="%s" loading="lazy">' % (esc(safe_url(m.group(2))), esc(m.group(1))))
            elif m.group(4) is not None:
                out.append('<a href="%s">%s</a>' % (esc(safe_url(m.group(4))), esc_text(m.group(3))))
            elif m.group(5) is not None:
                out.append("<strong>%s</strong>" % esc_text(m.group(5)))
            else:
                out.append("<em>%s</em>" % esc_text(m.group(6)))
            pos = m.end()
        out.append(esc_text(part[pos:]))
    return "".join(out)


def md_to_html(text):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, i, n = [], 0, len(lines)

    def is_ul(l):
        return re.match(r"^\s*[-*+]\s+", l) is not None

    def is_ol(l):
        return re.match(r"^\s*\d+[.)]\s+", l) is not None

    def starts_block(l):
        return l.startswith(("```", "#", ">")) or is_ul(l) or is_ol(l)

    while i < n:
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        if ln.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>%s</code></pre>" % esc_text("\n".join(buf)))
            continue
        m = re.match(r"^(#{1,3})\s+(.+?)\s*#*\s*$", ln)
        if m:
            lv = len(m.group(1)) + 1  # 文章标题是 h1，正文从 h2 开始
            out.append("<h%d>%s</h%d>" % (lv, md_text(m.group(2)), lv))
            i += 1
            continue
        if ln.startswith(">"):
            buf = []
            while i < n and lines[i].startswith(">"):
                buf.append(lines[i].lstrip(">").strip())
                i += 1
            out.append("<blockquote><p>%s</p></blockquote>" % md_text("\n".join(buf)))
            continue
        if is_ul(ln) or is_ol(ln):
            tag, chk = ("ul", is_ul) if is_ul(ln) else ("ol", is_ol)
            items = []
            while i < n and chk(lines[i]):
                items.append(re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", lines[i]))
                i += 1
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % md_text(x) for x in items), tag))
            continue
        buf = []
        while i < n and lines[i].strip() and not (buf and starts_block(lines[i])):
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>%s</p>" % md_text("\n".join(buf)))
    return "\n".join(out)


# ----------------------------------------------------------------- 文章

def load_posts():
    posts, seen = [], set()
    if not POSTS_DIR.is_dir():
        return posts
    for f in sorted(POSTS_DIR.glob("*.md")):
        raw = f.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        meta, body = {}, raw
        m = re.match(r"^---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)(.*)$", raw, re.S)
        if m:
            for ln in m.group(1).split("\n"):
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    meta[k.strip().lower()] = v.strip().strip("\"'")
            body = m.group(2)
        if meta.get("draft", "").lower() in ("1", "true", "yes"):
            continue
        date = meta.get("date", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            date = datetime.fromtimestamp(f.stat().st_mtime, TZ).date().isoformat()
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", meta.get("slug") or f.stem).strip("-") or "post"
        if slug in seen:
            raise SystemExit("文章 slug 重复：%s（%s）" % (slug, f.name))
        seen.add(slug)
        posts.append({"title": meta.get("title") or f.stem, "date": date, "slug": slug, "body": body})
    posts.sort(key=lambda p: (p["date"], p["slug"]), reverse=True)
    return posts


def build_posts(posts):
    OUT_POSTS.mkdir(exist_ok=True)
    keep = set()
    for p in posts:
        name = p["slug"] + ".html"
        keep.add(name)
        body = '<article>\n<h1>%s</h1>\n<p class="meta"><time datetime="%s">%s</time></p>\n%s\n</article>' % (
            esc_text(p["title"]), p["date"], p["date"], md_to_html(p["body"]))
        write(OUT_POSTS / name, page(p["title"], body, desc=re.sub(r"\s+", " ", p["body"]).strip()[:120]))
    for old in OUT_POSTS.glob("*.html"):  # 只清理 p/ 里已不存在对应 md 的页面
        if old.name not in keep:
            old.unlink()


HOME_JS = """<script>
(function () {
  var box = document.getElementById('news');
  fetch('/news/latest.json', {cache: 'no-cache'})
    .then(function (r) { if (!r.ok) throw 0; return r.json(); })
    .then(function (items) {
      var ul = document.getElementById('news-list'), n = 0;
      items.forEach(function (it) {
        if (n >= 12 || String(it.u).indexOf('/news/') !== 0) return;
        var li = document.createElement('li'), t = document.createElement('time'),
            a = document.createElement('a'), s = document.createElement('small');
        t.textContent = String(it.d).slice(5);
        a.textContent = it.t; a.href = it.u; a.lang = 'en';
        s.textContent = it.s;
        li.appendChild(t); li.appendChild(a); li.appendChild(s);
        ul.appendChild(li); n++;
      });
      if (n) box.hidden = false;
    })
    .catch(function () {});
})();
</script>
"""


def build_home(posts):
    parts = []
    if posts:
        rows = "".join(
            '<li><time datetime="%s">%s</time><a href="/p/%s.html">%s</a></li>\n'
            % (p["date"], p["date"][5:], esc(p["slug"]), esc_text(p["title"]))
            for p in posts[:20]
        )
        parts.append('<section>\n<div class="sec">文章</div>\n<ul class="list">\n%s</ul>\n</section>' % rows)
    parts.append(
        '<section id="news" hidden>\n<div class="sec"><span>国际新闻</span><a href="/news/">全部 →</a></div>\n'
        '<ul class="list" id="news-list"></ul>\n</section>\n'
        '<noscript><p><a href="/news/">查看国际新闻 →</a></p></noscript>'
    )
    parts.append(HOME_JS)
    write(ROOT / "index.html", page("", "\n".join(parts), desc="twysx：极简博客与每日国际新闻"))


# ----------------------------------------------------------------- 新闻：抓取

def http_get(url, timeout=20, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(5_000_000)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def clip(s, n=300):
    s = re.sub(r"\s*(Continue reading|Read more)[.…\s]*$", "", s, flags=re.I)
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(" ")
    if sp > n * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,.;:-—") + "…"


def parse_dt(s):
    s = (s or "").strip()
    if not s:
        return None
    d = None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except Exception:  # noqa: BLE001
        pass
    if d is None:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def child_text(el, *names):
    for name in names:  # 按 names 的先后优先
        for c in el:
            if local(c.tag) == name and (c.text or "").strip():
                return c.text.strip()
    return ""


def entry_link(el):
    for c in el:
        if local(c.tag) == "link":
            if c.get("href") and c.get("rel", "alternate") == "alternate":
                return c.get("href").strip()
            if (c.text or "").strip():
                return c.text.strip()
    return ""


def parse_feed(raw):
    if b"<!entity" in raw.lower():  # 拒绝实体声明，防 XML 实体膨胀
        raise ValueError("feed contains entity declarations")
    root = ET.fromstring(raw)
    out = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title = strip_html(child_text(el, "title"))
        url = entry_link(el)
        if not title or not safe_http(url):
            continue
        out.append({
            "title": title,
            "url": url,
            "summary": strip_html(child_text(el, "description", "summary", "encoded", "content")),
            "published": parse_dt(child_text(el, "pubDate", "published", "updated", "date")),
        })
    return out


STOP = set("""the and for with from that this these those after before over under into amid says say said new
has have had not but about than more most will would could can may its his her their are was were been what
when where who why how also just out off our you your""".split())


def toks(s):
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2 and w not in STOP}


def similar(a, b, thr):
    inter = len(a & b)
    return inter >= 2 and inter / max(1, min(len(a), len(b))) >= thr


def gather():
    now = datetime.now(timezone.utc)
    cands, ok = [], 0
    for source, url, weight in FEEDS:
        try:
            items = parse_feed(http_get(url))
        except Exception as e:  # noqa: BLE001
            log("  ! %s 抓取失败：%s: %s" % (source, type(e).__name__, e))
            continue
        ok += 1
        used = 0
        for rank, it in enumerate(items[:PER_FEED_LIMIT]):
            if it["published"] and now - it["published"] > timedelta(hours=MAX_AGE_H):
                continue
            used += 1
            it.update(source=source, pts=weight / (1.0 + rank / 4.0), tk=toks(it["title"]))
            cands.append(it)
        log("  - %s：%d 条（可用 %d）" % (source, len(items), used))
    return cands, ok


def pick(cands, avoid_urls, avoid_titles, n):
    """把同一事件的多家报道归成一簇，按「被几家报道 + 在各家排得多靠前」打分，取前 n 簇。"""
    cands = sorted(cands, key=lambda c: -c["pts"])
    clusters = []
    for c in cands:
        for cl in clusters:
            if similar(c["tk"], cl["tk"], 0.6):
                cl["members"].append(c)
                break
        else:
            clusters.append({"tk": c["tk"], "members": [c]})
    ranked = []
    for cl in clusters:
        best = {}
        for m in cl["members"]:  # 每家媒体只计最靠前的一条
            if m["source"] not in best:
                best[m["source"]] = m
        rep = cl["members"][0]
        if any(m["url"] in avoid_urls for m in cl["members"]):
            continue
        if len(rep["tk"]) >= 4 and any(similar(rep["tk"], t, 0.85) for t in avoid_titles):
            continue
        # 被越多家媒体同时报道越「热」：每多一家，总分额外放大 60%
        score = sum(m["pts"] for m in best.values()) * (1 + 0.6 * (len(best) - 1))
        ranked.append((score, rep, [m for m in best.values() if m is not rep]))
    ranked.sort(key=lambda x: -x[0])
    chosen, per = [], {}
    for score, rep, others in ranked:  # 第一轮：限制单家媒体条数
        if len(chosen) >= n:
            break
        if per.get(rep["source"], 0) >= PER_SOURCE_CAP:
            continue
        per[rep["source"]] = per.get(rep["source"], 0) + 1
        chosen.append((score, rep, others))
    if len(chosen) < n:  # 第二轮：不够就放开上限补齐
        for row in ranked:
            if len(chosen) >= n:
                break
            if row not in chosen:
                chosen.append(row)
    chosen.sort(key=lambda x: -x[0])
    out = []
    for score, rep, others in chosen:
        out.append({
            "title": rep["title"],
            "summary": clip(rep["summary"]) if rep["summary"] != rep["title"] else "",
            "url": rep["url"],
            "source": rep["source"],
            "published": rep["published"].isoformat() if rep["published"] else "",
            "also": [{"source": o["source"], "title": o["title"], "url": o["url"]} for o in others],
            "score": round(score, 2),
        })
    return out


def load_day(path):
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log("  ! 读取 %s 失败：%s" % (Path(path).name, e))
        return None
    items = []
    for it in d.get("items", []) if isinstance(d, dict) else []:
        if not (isinstance(it, dict) and it.get("title") and it.get("source") and safe_http(it.get("url", ""))):
            continue
        it["also"] = [a for a in it.get("also", []) if isinstance(a, dict) and a.get("title")
                      and a.get("source") and safe_http(a.get("url", ""))]
        items.append(it)
    return items


def daily(force=False):
    today = datetime.now(TZ).date().isoformat()
    f = DATA_DIR / (today + ".json")
    existing = load_day(f) if f.exists() else None
    if existing is not None and len(existing) >= NEWS_PER_DAY and not force:
        log("%s 已有 %d 条，跳过抓取（--force 可强制重抓）" % (today, len(existing)))
        build_news()
        return 0

    log("抓取 %s 的国际新闻……" % today)
    cands, ok = gather()
    if not cands:
        log("没有拿到任何可用新闻（成功的源：%d/%d），保持现状" % (ok, len(FEEDS)))
        return 1

    avoid_urls, avoid_titles = set(), []
    cutoff = (datetime.now(TZ).date() - timedelta(days=SEEN_DAYS)).isoformat()
    for p in sorted(DATA_DIR.glob("*.json")) if DATA_DIR.is_dir() else []:
        if cutoff <= p.stem < today:
            for it in load_day(p) or []:
                avoid_urls.add(it["url"])
                avoid_titles.append(toks(it["title"]))

    items = pick(cands, avoid_urls, avoid_titles, NEWS_PER_DAY)
    if existing is not None and len(items) < len(existing):
        log("本次只得到 %d 条，少于已有的 %d 条，保持现状" % (len(items), len(existing)))
    else:
        doc = {"date": today, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": items}
        write(f, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        log("已写入 %s：%d 条（成功的源 %d/%d）" % (f.name, len(items), ok, len(FEEDS)))
        if len(items) < NEWS_PER_DAY:
            log("  ! 不足 %d 条，之后再运行一次会自动补充" % NEWS_PER_DAY)
    build_news()
    return 0


# ----------------------------------------------------------------- 新闻：页面

def cn_date(day):
    y, m, d = day.split("-")
    return "%s 年 %d 月 %d 日" % (y, int(m), int(d))


def bj_time(iso):
    try:
        return datetime.fromisoformat(iso).astimezone(TZ).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return ""


def item_page(day, it):
    pub = bj_time(it.get("published", ""))
    meta = "%s · %s%s" % (day, esc_text(it["source"]), " · 发布于 %s（北京时间）" % pub if pub else "")
    parts = ['<article>\n<p class="meta">%s</p>\n<h1 lang="en">%s</h1>' % (meta, esc_text(it["title"]))]
    if it.get("summary"):
        parts.append('<p class="lead" lang="en">%s</p>' % esc_text(it["summary"]))
    parts.append('<p><a class="btn" href="%s" rel="noopener nofollow" target="_blank">阅读 %s 原文 ↗</a></p>'
                 % (esc(it["url"]), esc_text(it["source"])))
    if it.get("also"):
        lis = "".join(
            '<li><a href="%s" rel="noopener nofollow" target="_blank">%s</a>：<span lang="en">%s</span></li>'
            % (esc(a["url"]), esc_text(a["source"]), esc_text(a["title"])) for a in it["also"])
        parts.append('<h2>其他媒体的相关报道</h2>\n<ul class="also">%s</ul>' % lis)
    parts.append('<p class="note">摘要整理自 %s 的公开 RSS，版权归原媒体所有。<a href="/news/%s/">← %s 全部新闻</a></p>\n</article>'
                 % (esc_text(it["source"]), day, cn_date(day)))
    return page(it["title"], "\n".join(parts), desc=it.get("summary", "")[:140])


def build_news():
    days = []
    if DATA_DIR.is_dir():
        for f in sorted(DATA_DIR.glob("*.json"), reverse=True):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem):
                continue
            items = load_day(f)
            if items:
                days.append((f.stem, items))

    latest = []
    for day, items in days:
        for i, it in enumerate(items, 1):
            it["_u"] = "/news/%s/%02d-%s.html" % (day, i, slugify(it["title"]))
        dd = NEWS_DIR / day
        dd.mkdir(parents=True, exist_ok=True)
        for old in dd.glob("*.html"):
            old.unlink()
        for it in items:
            write(ROOT / it["_u"].lstrip("/"), item_page(day, it))
        rows = "".join(
            '<li><a href="%s" lang="en">%s</a><small>%s</small></li>\n' % (esc(it["_u"]), esc_text(it["title"]), esc_text(it["source"]))
            for it in items)
        body = ('<h1>%s</h1>\n<p class="meta">国际热门新闻 %d 条</p>\n<ul class="list">\n%s</ul>\n'
                '<p class="note"><a href="/news/">← 全部日期</a></p>') % (cn_date(day), len(items), rows)
        write(dd / "index.html", page(cn_date(day) + " 国际新闻", body, desc=cn_date(day) + "国际热门新闻"))
        if len(latest) < 30:
            for it in items:
                latest.append({"d": day, "t": it["title"], "s": it["source"], "u": it["_u"]})

    if days:
        rows = "".join(
            '<li><time datetime="%s">%s</time><a href="/news/%s/" lang="en">%s</a><small>%d 条</small></li>\n'
            % (d, d, d, esc_text(items[0]["title"]), len(items)) for d, items in days)
    else:
        rows = '<li><a href="/">暂无新闻，稍后再来。</a></li>\n'
    names = "、".join(s for s, _, _ in FEEDS)
    body = ('<h1>国际新闻</h1>\n<p class="meta">每天 %d 条，整理自 %s 的公开 RSS，只含标题与摘要，版权归原媒体所有。</p>\n'
            '<ul class="list full">\n%s</ul>') % (NEWS_PER_DAY, names, rows)
    write(NEWS_DIR / "index.html", page("国际新闻", body, desc="每日国际热门新闻"))
    write(NEWS_DIR / "latest.json", json.dumps(latest[:30], ensure_ascii=False, separators=(",", ":")))
    log("新闻页面已生成：%d 天" % len(days))


# ----------------------------------------------------------------- 入口

def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    cmd = argv[1] if len(argv) > 1 else "build"
    force = "--force" in argv
    if cmd == "build":
        posts = load_posts()
        build_posts(posts)
        build_home(posts)
        build_news()
        log("已生成 index.html、p/（%d 篇文章）和 news/" % len(posts))
        return 0
    if cmd == "daily":
        return daily(force)
    if cmd == "news":
        build_news()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
