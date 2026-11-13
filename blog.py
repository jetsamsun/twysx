#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""twysx 极简博客生成器（只依赖 Python 标准库，Python >= 3.8）。

  python blog.py build        本地用：重建首页（含分页）、文章页、搜索页（都受 git 跟踪）和新闻页（news/，git 忽略）
  python blog.py daily        服务器 cron 用：抓取当天 10 条国际热门新闻，只重建 news/
  python blog.py daily --force   当天已满 10 条时也强制重抓
  python blog.py news         只根据 news/data/*.json 重建 news/ 下的页面

文章：posts/*.md（头部 title/date 两行元数据）  ->  p/<slug>.html；首页文章列表分页为 page/N/
搜索：search/index.html（搜索页）、search/posts.json（文章索引），随 build 生成并进 git
新闻：服务器每天写 news/data/YYYY-MM-DD.json  ->  news/ 下的页面（含分页 news/page/N/）和搜索索引 news/idx/，这些文件不进 git
"""
from __future__ import annotations

import email.utils
import hashlib
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
SEARCH_DIR = ROOT / "search"

POSTS_PER_PAGE = 10      # 首页文章列表每页条数
NEWS_PER_PAGE = 10       # 国际新闻列表每页条数
SEARCH_PER_PAGE = 10     # 搜索结果每页条数
SEARCH_BODY_MAX = 20000  # 每篇文章最多收录进搜索索引的字数

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
    """原子写文件（UTF-8、LF）。内容没变就不动，保留修改时间，浏览器/CDN 的条件请求才能继续返回 304。"""
    path = Path(path)
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except (OSError, UnicodeDecodeError):
        pass
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

_css_url = None


def css_url():
    """CSS 链接带上内容哈希（/style.css?v=xxxx）：改了样式，URL 就变，Cloudflare 和浏览器的旧缓存不会再卡住。
    哈希前统一成 LF，避免 Windows(CRLF) 和服务器(LF) 算出不同的值。"""
    global _css_url
    if _css_url is None:
        try:
            data = (ROOT / "style.css").read_bytes().replace(b"\r\n", b"\n")
            _css_url = "/style.css?v=" + hashlib.md5(data).hexdigest()[:8]
        except OSError:
            _css_url = "/style.css"
    return _css_url


SEARCH_FORM = (
    '<form class="search" action="/search/" method="get" role="search">'
    '<input type="search" name="q" placeholder="搜索文章和新闻" aria-label="搜索" '
    'autocomplete="off" enterkeyhint="search" maxlength="100"></form>'
)


def page(title, body, desc="", extra_head=""):
    full = "%s · %s" % (title, SITE) if title else SITE
    year = datetime.now(TZ).year
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>%s</title>\n"
        '<meta name="description" content="%s">\n'
        '<link rel="stylesheet" href="%s">\n'
        "%s</head>\n<body>\n<div class=\"wrap\">\n"
        '<header class="site"><a class="brand" href="/">%s<span>.</span></a>'
        + SEARCH_FORM +
        '<nav><a href="/">文章</a><a href="/news/">国际新闻</a></nav></header>\n'
        "<main>\n%s\n</main>\n"
        '<footer class="site">© %d %s</footer>\n</div>\n</body>\n</html>\n'
    ) % (esc_text(full), esc(desc), esc(css_url()), extra_head, SITE, body, year, SITE)


# ----------------------------------------------------------------- 分页

def pages_of(n, size):
    return max(1, (n + size - 1) // size)


def page_numbers(cur, total):
    """要显示的页码：首页、末页、当前页及前后各 1 页；中间隔开的用 None（省略号）表示，只隔 1 页就直接显示那一页。
    搜索页的 JS（pageNums）是同一算法，改这里要同步改那边。"""
    keep = {n for n in (1, total, cur - 1, cur, cur + 1) if 1 <= n <= total}
    out, last = [], 0
    for n in sorted(keep):
        if n - last == 2:
            out.append(last + 1)
        elif n - last > 2:
            out.append(None)
        out.append(n)
        last = n
    return out


def pager(cur, total, url_of):
    """静态页面的分页导航（← 1 … 4 [5] 6 … 20 →）；只有一页时不输出。"""
    if total <= 1:
        return ""
    parts = []
    if cur > 1:
        parts.append('<a href="%s" rel="prev" aria-label="上一页">←</a>' % esc(url_of(cur - 1)))
    for n in page_numbers(cur, total):
        if n is None:
            parts.append('<span class="gap">…</span>')
        elif n == cur:
            parts.append('<span class="cur" aria-current="page">%d</span>' % n)
        else:
            parts.append('<a href="%s">%d</a>' % (esc(url_of(n)), n))
    if cur < total:
        parts.append('<a href="%s" rel="next" aria-label="下一页">→</a>' % esc(url_of(cur + 1)))
    return '<nav class="pager" aria-label="分页">%s</nav>' % "".join(parts)


def posts_page_url(n):
    return "/" if n == 1 else "/page/%d/" % n


def news_page_url(n):
    return "/news/" if n == 1 else "/news/page/%d/" % n


def drop_stale_pages(base, total):
    """清理 base/<n>/index.html 里 n > total 的旧分页（只动纯数字目录里的 index.html）。"""
    if not base.is_dir():
        return
    for d in base.iterdir():
        if d.is_dir() and d.name.isdigit() and int(d.name) > total:
            f = d / "index.html"
            if f.is_file():
                f.unlink()
            try:
                d.rmdir()
            except OSError:
                pass
    try:
        base.rmdir()  # 一页都不剩时把空目录也收掉；非空会抛 OSError，忽略
    except OSError:
        pass


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
        t.textContent = String(it.d);
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
    """首页：文章列表分页（第 1 页是 /，之后是 /page/N/），国际新闻只出现在第 1 页。"""
    total = pages_of(len(posts), POSTS_PER_PAGE)
    for pg in range(1, total + 1):
        chunk = posts[(pg - 1) * POSTS_PER_PAGE: pg * POSTS_PER_PAGE]
        parts = []
        if chunk:
            rows = "".join(
                '<li><time datetime="%s">%s</time><a href="/p/%s.html">%s</a></li>\n'
                % (p["date"], p["date"], esc(p["slug"]), esc_text(p["title"]))
                for p in chunk
            )
            parts.append('<section>\n<div class="sec">文章</div>\n<ul class="list">\n%s</ul>\n%s\n</section>'
                         % (rows, pager(pg, total, posts_page_url)))
        if pg == 1:
            parts.append(
                '<section id="news" hidden>\n<div class="sec"><span>国际新闻</span><a href="/news/">全部 →</a></div>\n'
                '<ul class="list" id="news-list"></ul>\n</section>\n'
                '<noscript><p><a href="/news/">查看国际新闻 →</a></p></noscript>'
            )
            parts.append(HOME_JS)
            write(ROOT / "index.html", page("", "\n".join(parts), desc="twysx：极简博客与每日国际新闻"))
        else:
            write(ROOT / "page" / str(pg) / "index.html",
                  page("文章 · 第 %d 页" % pg, "\n".join(parts), desc="twysx 文章 第 %d 页" % pg))
    drop_stale_pages(ROOT / "page", total)


# ----------------------------------------------------------------- 搜索

# 搜索页脚本：纯前端，首次搜索时才下载索引（文章 search/posts.json + 新闻按月分片 news/idx/*.json）。
# 用户输入和索引里的文字一律用 textContent / 文本节点写入页面，不拼 HTML，所以不会被注入。
# 分页算法（pageNums）与 Python 的 page_numbers 保持一致。
SEARCH_JS = r"""<script>
(function () {
  var PER = __PER__;
  var box = document.getElementById('res');
  var form = document.querySelector('form.search');
  var input = form && form.elements.q;
  var docs = null, loading = null, warn = 0, seq = 0, cur = {q: '', p: 1};

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function note(text) { box.textContent = ''; box.appendChild(el('p', 'hint', text)); }
  function norm(q) { return String(q || '').replace(/\s+/g, ' ').trim().slice(0, 100); }
  function url(q, p) { return '/search/' + (q ? '?q=' + encodeURIComponent(q) + (p > 1 ? '&p=' + p : '') : ''); }

  // 404（还没有新闻索引之类）当作空；网络错误或坏 JSON 返回 null
  function getJSON(u) {
    return fetch(u, {cache: 'no-cache'})
      .then(function (r) { return r.status === 404 ? [] : (r.ok ? r.json() : null); })
      .catch(function () { return null; });
  }

  function load() {
    if (loading) return loading;
    loading = getJSON('/news/idx/index.json').then(function (months) {
      var urls = ['/search/posts.json'].concat((Array.isArray(months) ? months : []).map(function (m) {
        return '/news/idx/' + m + '.json';
      }));
      return Promise.all(urls.map(function (u) { return getJSON(u); })).then(function (parts) {
        var out = [], bad = Array.isArray(months) ? 0 : 1, total = urls.length + 1;
        parts.forEach(function (list, i) {
          if (!Array.isArray(list)) { bad++; return; }
          list.forEach(function (d) {
            var u = String(d && d.u || '');
            if (u.charAt(0) !== '/' || u.charAt(1) === '/' || u.charAt(1) === '\\') return;   // 只收站内链接
            d.k = i ? 'n' : 'p';
            d.tl = String(d.t || '').toLowerCase();
            d.h = d.tl + '\n' + String(d.s || '').toLowerCase() + '\n' + String(d.x || '').toLowerCase();
            out.push(d);
          });
        });
        return {docs: out, bad: bad, failed: bad >= total};
      });
    });
    return loading;
  }

  function pageNums(c, t) {
    var set = {}, out = [], last = 0;
    [1, t, c - 1, c, c + 1].forEach(function (n) { if (n >= 1 && n <= t) set[n] = 1; });
    Object.keys(set).map(Number).sort(function (a, b) { return a - b; }).forEach(function (n) {
      if (n - last === 2) out.push(last + 1);
      else if (n - last > 2) out.push(0);   // 0 = 省略号
      out.push(n);
      last = n;
    });
    return out;
  }

  function pager(c, t, q) {
    var nav = el('nav', 'pager');
    nav.setAttribute('aria-label', '分页');
    function link(n, text, label) {
      var a = el('a', '', text);
      a.href = url(q, n);
      a.setAttribute('data-p', n);
      if (label) a.setAttribute('aria-label', label);
      nav.appendChild(a);
    }
    if (c > 1) link(c - 1, '←', '上一页');
    pageNums(c, t).forEach(function (n) {
      if (!n) nav.appendChild(el('span', 'gap', '…'));
      else if (n === c) { var s = el('span', 'cur', String(n)); s.setAttribute('aria-current', 'page'); nav.appendChild(s); }
      else link(n, String(n));
    });
    if (c < t) link(c + 1, '→', '下一页');
    return nav;
  }

  function rx(ts) {
    return new RegExp(ts.slice().sort(function (a, b) { return b.length - a.length; }).map(function (t) {
      return t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }).join('|'), 'gi');
  }

  // 把 text 里命中关键词的部分包进 <mark>，其余作为文本节点
  function hl(parent, text, re) {
    var last = 0, m;
    re.lastIndex = 0;
    while ((m = re.exec(text)) !== null) {
      if (!m[0]) { re.lastIndex++; continue; }
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      parent.appendChild(el('mark', '', m[0]));
      last = m.index + m[0].length;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function snippet(text, re) {
    text = String(text || '').replace(/\s+/g, ' ').trim();
    if (!text) return '';
    re.lastIndex = 0;
    var m = re.exec(text), at = m ? m.index : 0;
    var from = Math.max(0, at - 40), to = Math.min(text.length, from + 150);
    return (from > 0 ? '…' : '') + text.slice(from, to) + (to < text.length ? '…' : '');
  }

  function render(q, p) {
    var ts = q.toLowerCase().split(' ').slice(0, 8), list = [];
    docs.forEach(function (d) {
      var sc = 0;
      for (var i = 0; i < ts.length; i++) {          // 每个词都要出现（标题/来源/正文任一处）
        if (d.h.indexOf(ts[i]) < 0) return;
        sc += d.tl.indexOf(ts[i]) >= 0 ? 10 : 1;       // 标题命中权重更高
      }
      list.push({d: d, s: sc});
    });
    list.sort(function (a, b) { return b.s - a.s || (a.d.d < b.d.d ? 1 : a.d.d > b.d.d ? -1 : 0); });
    var total = Math.max(1, Math.ceil(list.length / PER));
    p = Math.min(Math.max(1, p || 1), total);
    cur.q = q; cur.p = p;
    box.textContent = '';
    if (warn) box.appendChild(el('p', 'hint', '部分搜索索引加载失败，结果可能不完整，稍后再搜一次试试。'));
    if (!list.length) {
      box.appendChild(el('p', 'hint', '没有找到与「' + q + '」相关的内容，试试更短或不同的关键词。'));
      return;
    }
    var re = rx(ts);
    box.appendChild(el('p', 'meta', '找到 ' + list.length + ' 条结果' + (total > 1 ? '，第 ' + p + ' / ' + total + ' 页' : '')));
    var ul = el('ul', 'res');
    list.slice((p - 1) * PER, p * PER).forEach(function (r) {
      var d = r.d, news = d.k === 'n', li = el('li'), a = el('a'), meta = el('div', 'meta');
      a.href = d.u;
      if (news) a.lang = 'en';
      hl(a, String(d.t || ''), re);
      li.appendChild(a);
      meta.appendChild(document.createTextNode(String(d.d || '') + ' · '));
      if (news) hl(meta, String(d.s || ''), re); else meta.appendChild(document.createTextNode('文章'));
      li.appendChild(meta);
      var sn = snippet(d.x, re);
      if (sn) { var para = el('p'); if (news) para.lang = 'en'; hl(para, sn, re); li.appendChild(para); }
      ul.appendChild(li);
    });
    box.appendChild(ul);
    if (total > 1) box.appendChild(pager(p, total, q));
  }

  function run(q, p) {
    q = norm(q);
    if (input) input.value = q;
    document.title = (q ? q + ' · ' : '') + '搜索 · twysx';
    var my = ++seq;
    if (!q) { note('输入关键词，搜索文章和国际新闻。'); if (input) input.focus(); return; }
    if (docs) { render(q, p); return; }
    note('搜索中…');
    load().then(function (r) {
      if (my !== seq) return;                         // 期间又发起了新的搜索，丢弃这次
      if (r.failed) { loading = null; note('搜索索引加载失败，请稍后再试。'); return; }
      docs = r.docs; warn = r.bad;
      render(q, p);
      if (warn) { docs = null; loading = null; }      // 部分失败：下次搜索重新加载
    });
  }

  function go(q, p) {
    q = norm(q);
    try { history.pushState(null, '', url(q, p)); } catch (e) {}
    run(q, p);
  }

  function fromURL() {
    var u = new URLSearchParams(location.search);
    run(u.get('q') || '', parseInt(u.get('p'), 10) || 1);
  }

  if (form) form.addEventListener('submit', function (e) { e.preventDefault(); go(input.value, 1); });
  box.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('a[data-p]') : null;
    if (!a || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    go(cur.q, parseInt(a.getAttribute('data-p'), 10));
    window.scrollTo(0, 0);
  });
  window.addEventListener('popstate', fromURL);
  fromURL();
})();
</script>
"""


def build_search(posts):
    """文章索引（search/posts.json）和搜索页（search/index.html）；新闻索引由 build_news 生成。"""
    docs = [{
        "t": p["title"],
        "u": "/p/%s.html" % p["slug"],
        "d": p["date"],
        "x": strip_html(md_to_html(p["body"]))[:SEARCH_BODY_MAX],
    } for p in posts]
    write(SEARCH_DIR / "posts.json", json.dumps(docs, ensure_ascii=False, separators=(",", ":")))
    body = ('<h1>搜索</h1>\n<div id="res" aria-live="polite"><p class="hint">输入关键词，搜索文章和国际新闻。</p></div>\n'
            '<noscript><p class="hint">搜索需要启用 JavaScript。</p></noscript>\n'
            + SEARCH_JS.replace("__PER__", str(SEARCH_PER_PAGE)))
    write(SEARCH_DIR / "index.html",
          page("搜索", body, desc="搜索 twysx 的文章和国际新闻", extra_head='<meta name="robots" content="noindex">\n'))


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


def load_days():
    """[(日期, 条目列表)]，新的在前；顺便给每条算好页面地址 _u。"""
    days = []
    if DATA_DIR.is_dir():
        for f in sorted(DATA_DIR.glob("*.json"), reverse=True):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem):
                continue
            items = load_day(f)
            if items:
                for i, it in enumerate(items, 1):
                    it["_u"] = "/news/%s/%02d-%s.html" % (f.stem, i, slugify(it["title"]))
                days.append((f.stem, items))
    return days


def day_nav(newer, older):
    """日期页底部的「前一天 / 后一天」：和列表分页同一个方向（左＝更新，右＝更早）。"""
    parts = []
    if newer:
        parts.append('<a href="/news/%s/" rel="prev">← %s</a>' % (newer, newer))
    if older:
        parts.append('<a href="/news/%s/" rel="next">%s →</a>' % (older, older))
    return '<nav class="pager day" aria-label="按日期翻页">%s</nav>' % "".join(parts) if parts else ""


def prune(dirpath, keep_names, pattern):
    """删掉 dirpath 里匹配 pattern、但不在 keep_names 里的文件（先写新的、再删旧的，访客不会撞上空窗）。"""
    if not dirpath.is_dir():
        return
    for old in dirpath.glob(pattern):
        if old.is_file() and old.name not in keep_names:
            old.unlink()


def build_news():
    days = load_days()
    flat = [(day, it) for day, items in days for it in items]   # 全部新闻，新的在前

    # 1. 每天一个目录：当天列表页 + 每条新闻一页
    for n, (day, items) in enumerate(days):
        dd = NEWS_DIR / day
        keep = {"index.html"}
        for it in items:
            name = Path(it["_u"]).name
            keep.add(name)
            write(dd / name, item_page(day, it))
        rows = "".join(
            '<li><a href="%s" lang="en">%s</a><small>%s</small></li>\n' % (esc(it["_u"]), esc_text(it["title"]), esc_text(it["source"]))
            for it in items)
        body = ('<h1>%s</h1>\n<p class="meta">国际热门新闻 %d 条</p>\n<ul class="list">\n%s</ul>\n%s\n'
                '<p class="note"><a href="/news/">← 国际新闻</a></p>') % (
            cn_date(day), len(items), rows,
            day_nav(days[n - 1][0] if n > 0 else None, days[n + 1][0] if n + 1 < len(days) else None))
        write(dd / "index.html", page(cn_date(day) + " 国际新闻", body, desc=cn_date(day) + "国际热门新闻"))
        prune(dd, keep, "*.html")
    shown = {day for day, _ in days}
    if NEWS_DIR.is_dir():
        for d in NEWS_DIR.iterdir():   # 数据文件已删的日期，把它生成的页面也收掉
            if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name) and d.name not in shown:
                prune(d, set(), "*.html")
                try:
                    d.rmdir()
                except OSError:
                    pass

    # 2. 国际新闻列表：全部新闻按时间倒序，每页 NEWS_PER_PAGE 条（第 1 页是 /news/，之后 /news/page/N/）
    total = pages_of(len(flat), NEWS_PER_PAGE)
    names = "、".join(s for s, _, _ in FEEDS)
    for pg in range(1, total + 1):
        chunk = flat[(pg - 1) * NEWS_PER_PAGE: pg * NEWS_PER_PAGE]
        rows = "".join(
            '<li><time datetime="%s"><a href="/news/%s/">%s</a></time><a href="%s" lang="en">%s</a><small>%s</small></li>\n'
            % (day, day, day, esc(it["_u"]), esc_text(it["title"]), esc_text(it["source"]))
            for day, it in chunk) or '<li><a href="/">暂无新闻，稍后再来。</a></li>\n'
        intro = ("每天 %d 条，整理自 %s 的公开 RSS，只含标题与摘要，版权归原媒体所有。" % (NEWS_PER_DAY, names)
                 if pg == 1 else "第 %d / %d 页" % (pg, total))
        body = '<h1>国际新闻</h1>\n<p class="meta">%s</p>\n<ul class="list">\n%s</ul>\n%s' % (
            intro, rows, pager(pg, total, news_page_url))
        out = NEWS_DIR / "index.html" if pg == 1 else NEWS_DIR / "page" / str(pg) / "index.html"
        write(out, page("国际新闻" if pg == 1 else "国际新闻 · 第 %d 页" % pg, body,
                        desc="每日国际热门新闻" if pg == 1 else "每日国际热门新闻 第 %d 页" % pg))
    drop_stale_pages(NEWS_DIR / "page", total)

    # 3. 首页用的最新 30 条
    latest = [{"d": day, "t": it["title"], "s": it["source"], "u": it["_u"]} for day, it in flat[:30]]
    write(NEWS_DIR / "latest.json", json.dumps(latest, ensure_ascii=False, separators=(",", ":")))

    # 4. 搜索索引：按月分片（老的月份不再变化，浏览器重复搜索时只会拿到 304），index.json 列出有哪些月份
    by_month = {}
    for day, it in flat:
        by_month.setdefault(day[:7], []).append(
            {"t": it["title"], "u": it["_u"], "d": day, "s": it["source"], "x": it.get("summary", "")})
    idx = NEWS_DIR / "idx"
    for month, docs in by_month.items():
        write(idx / (month + ".json"), json.dumps(docs, ensure_ascii=False, separators=(",", ":")))
    write(idx / "index.json", json.dumps(sorted(by_month, reverse=True)))
    prune(idx, {m + ".json" for m in by_month} | {"index.json"}, "*.json")

    log("新闻页面已生成：%d 天、%d 条、列表 %d 页" % (len(days), len(flat), total))


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
        build_search(posts)
        build_news()
        log("已生成 index.html、page/、p/（%d 篇文章）、search/ 和 news/" % len(posts))
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
