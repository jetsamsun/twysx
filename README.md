# twysx

极简博客 + 每天 10 条国际热门新闻。纯静态页面，生成器只用 Python 标准库，没有数据库、没有后台。

## 写文章

在 `posts/` 下新建 `xxx.md`：

```
---
title: 文章标题
date: 2026-11-12
---

正文。支持 # 标题、- 列表、> 引用、``` 代码块、[链接](url)、![图片](url)、**粗体**、*斜体*。
```

头部加一行 `draft: true` 的文章不会发布。

## 本地开发与预览

```bash
python blog.py build     # 生成 index.html、page/（文章分页）、p/（文章页）、search/（搜索页和文章索引）和 news/（新闻页）
```

`index.html`、`page/`、`p/`、`search/` 是生成物，**要一起提交**；`news/` 是新闻内容，已在 `.gitignore` 里，不提交。
预览：用 phpstudy 打开本地站点，或在本目录运行 `python -m http.server 8099`。

CSS 链接自带内容哈希（`/style.css?v=xxxx`）：改了 `style.css` 后重新 `build` 并提交，Cloudflare 和浏览器就不会再用旧样式。服务器上的新闻页面在下次 `daily` 或手动 `python3 blog.py news` 后换成新链接。

## 分页与搜索

**分页**（每页条数在 `blog.py` 顶部：`POSTS_PER_PAGE`、`NEWS_PER_PAGE`、`SEARCH_PER_PAGE`，默认都是 10）：

- 首页文章列表：`/`、`/page/2/`、`/page/3/`……（国际新闻区块只在第 1 页）；
- 国际新闻列表 `/news/`：全部新闻按时间倒序，`/news/`、`/news/page/2/`……，每行的日期点进去是当天的 10 条；日期页底部有「前一天 / 后一天」；
- 页码条形如 `← 1 … 4 [5] 6 … 20 →`；某一页没有内容时（比如文章删少了）多出来的分页会在下次生成时自动清理。

**搜索**：每个页面的页头都有搜索框，提交到 `/search/?q=关键词`。

- 纯前端：搜索页第一次搜索时才下载索引，文章在 `search/posts.json`，新闻在 `news/idx/<年-月>.json`（按月分片，老月份不再变化，浏览器重复搜索只会收到 304）；
- 多个关键词用空格隔开，必须全部出现（标题、来源、正文摘要任一处都算），标题命中的排前面，同分新的在前，命中词高亮，每页 10 条，翻页和前进后退都不刷新页面；
- 文章索引随 `build` 生成并提交；新闻索引由服务器上的 `daily` / `news` 生成，不进 git。每篇文章最多收录前 20000 字（`SEARCH_BODY_MAX`）。

## 每日国际新闻

`python3 blog.py daily` 每次运行会：

1. 抓取 BBC、The Guardian、Al Jazeera、NPR、DW、France 24 的公开 RSS；
2. 把讲同一件事的报道归成一组，被越多家媒体报道、在各家排得越靠前的越「热」，取前 10 组，每家媒体最多占 4 条，近 7 天发布过的不重复；
3. 写入 `news/data/<日期>.json`，并生成 `news/` 下的页面（按北京时间分天）、列表分页和搜索索引。

每篇只含标题、摘要、原文链接和其他媒体的相关链接，版权归原媒体所有。当天已满 10 条会自动跳过，所以同一天跑多次是安全的；不足 10 条时再跑一次会补足。
`python3 blog.py daily --force` 强制重抓（只在条数不比已有的少时才覆盖）；`python3 blog.py news` 不抓取，只根据已有数据重建 `news/`。

首页的「国际新闻」由浏览器读取 `/news/latest.json` 显示，所以新闻更新不需要改动或提交任何受 git 跟踪的文件。内容没变的页面不会被重写，修改时间保持不变。

## 部署

本地修改 → `git push` → 服务器 `cd /var/www/html && git pull`。

**改了生成器、样式或页头之后**（比如加搜索、加分页那次），`git pull` 完要在服务器上再手动跑一次 `python3 blog.py news`，让 `news/` 下的页面换成新版式和新索引；不跑的话要等下一次 cron，期间新闻页没有搜索框、`/search/` 也搜不到新闻。

服务器上的定时任务只需配置一次（`crontab -e`，服务器是 UTC 时间，00:00 UTC = 北京时间 08:00；12:00 的一次是兜底补抓）：

```
0 0 * * * cd /var/www/html && /usr/bin/python3 blog.py daily >> /var/log/twysx-news.log 2>&1
0 4 * * * cd /var/www/html && /usr/bin/python3 blog.py daily >> /var/log/twysx-news.log 2>&1
```

注意：

- 不要在服务器上运行 `blog.py build`，它会改动受 git 跟踪的文件，之后 `git pull` 可能冲突；服务器上只跑 `daily`。
- 网站根目录就是仓库根目录，所以 `blog.py`、`posts/*.md`、本文件都能被公开访问；不要把密钥、账号、内部文档放进仓库。
