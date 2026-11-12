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
python blog.py build     # 生成 index.html、p/（文章页）和 news/（新闻页）
```

`index.html` 和 `p/` 是生成物，**要一起提交**；`news/` 是新闻内容，已在 `.gitignore` 里，不提交。
预览：用 phpstudy 打开本地站点，或在本目录运行 `python -m http.server 8099`。

## 每日国际新闻

`python3 blog.py daily` 每次运行会：

1. 抓取 BBC、The Guardian、Al Jazeera、NPR、DW、France 24 的公开 RSS；
2. 把讲同一件事的报道归成一组，被越多家媒体报道、在各家排得越靠前的越「热」，取前 10 组，每家媒体最多占 4 条，近 7 天发布过的不重复；
3. 写入 `news/data/<日期>.json`，并生成 `news/` 下的页面（按北京时间分天）。

每篇只含标题、摘要、原文链接和其他媒体的相关链接，版权归原媒体所有。当天已满 10 条会自动跳过，所以同一天跑多次是安全的；不足 10 条时再跑一次会补足。
`python3 blog.py daily --force` 强制重抓（只在条数不比已有的少时才覆盖）。

首页的「国际新闻」由浏览器读取 `/news/latest.json` 显示，所以新闻更新不需要改动或提交任何受 git 跟踪的文件。

## 部署

本地修改 → `git push` → 服务器 `cd /var/www/html && git pull`。

服务器上的定时任务只需配置一次（`crontab -e`，服务器是 UTC 时间，00:00 UTC = 北京时间 08:00；12:00 的一次是兜底补抓）：

```
0 0 * * * cd /var/www/html && /usr/bin/python3 blog.py daily >> /var/log/twysx-news.log 2>&1
0 4 * * * cd /var/www/html && /usr/bin/python3 blog.py daily >> /var/log/twysx-news.log 2>&1
```

注意：

- 不要在服务器上运行 `blog.py build`，它会改动受 git 跟踪的文件，之后 `git pull` 可能冲突；服务器上只跑 `daily`。
- 网站根目录就是仓库根目录，所以 `blog.py`、`posts/*.md`、本文件都能被公开访问；不要把密钥、账号、内部文档放进仓库。
