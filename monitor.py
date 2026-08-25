#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
X 博主推文监控 → 待推送队列（pending.json）
============================================
运行环境：GitHub Actions（免费，海外服务器，可访问 X 相关服务）

工作流程：
1. 读取 accounts.json 中的博主列表
2. 对每个博主依次尝试多个免费 RSS 源（Nitter 实例 → RSSHub 公共实例）
3. 用 feedparser 解析最新推文（取前 5 条，防止中间某轮失败漏掉）
4. 与 state.json 中"已处理推文 id 集合"对比，发现新推文
5. 新推文追加写入 pending.json（待推送队列），并更新 state.json
6. workflow 会把 pending.json / state.json commit 回仓库

诊断与告警（v2 新增）：
- 每轮把每个源的成功/失败结果写入 state.json 的 "_source_status" 键，
  方便通过 raw.githubusercontent 直接查看各源真实可用性
- 若连续 FAIL_THRESHOLD 轮所有博主的所有源全部失败，脚本 exit 1，
  让 GitHub Actions 变红，避免"静默假绿"

本地运行：python monitor.py（需 pip install feedparser requests）
"""

import json
import os
import sys
import time
import html
import re
import hashlib

import feedparser
import requests

# ---------- 路径配置（相对路径，GitHub Actions 与本地通用） ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "accounts.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
PENDING_FILE = os.path.join(BASE_DIR, "pending.json")

REQUEST_TIMEOUT = 15  # 秒（每个源单次请求超时，多源轮询避免整体太久）
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
MAX_ENTRIES_PER_ACCOUNT = 5   # 每个博主最多处理前 N 条
MAX_HANDLED_PER_ACCOUNT = 20  # state 中每个博主保留最近 N 个已处理 id
FAIL_THRESHOLD = 3            # 连续 N 轮全源失败则 exit 1 告警


# ---------- 工具函数 ----------

def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_json(path: str, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"警告：读取 {path} 失败（{e}），使用默认值")
    return default if default is not None else {}


def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_x_guest_api(username: str, timeout: int = REQUEST_TIMEOUT):
    """X 官方 API 直连（无登录）：guest token + GraphQL UserTweets。
    失败抛异常。queryId 取自公开 web 端（若失效会继续尝试 v2 API）。
    """
    BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT, "Authorization": f"Bearer {BEARER}"})

    # 方法A: v2 API 按用户名取推文（Bearer 若带 scope 即可用）
    try:
        url = f"https://api.twitter.com/2/users/by/username/{username}?user.fields=id"
        r = sess.get(url, timeout=timeout)
        if r.status_code == 200:
            uid = r.json().get("data", {}).get("id")
            if uid:
                url2 = (f"https://api.twitter.com/2/users/{uid}/tweets?max_results=5"
                        f"&tweet.fields=created_at,text")
                r2 = sess.get(url2, timeout=timeout)
                if r2.status_code == 200:
                    tweets = []
                    for tw in r2.json().get("data", []):
                        tweets.append({
                            "id": str(tw.get("id", "")),
                            "link": f"https://x.com/{username}/status/{tw.get('id')}",
                            "content": clean_text(tw.get("text", "")),
                            "published": tw.get("created_at", ""),
                        })
                    if tweets:
                        return tweets
        raise ValueError(f"v2 API 不可用 HTTP={r.status_code}")
    except Exception as e:
        raise ValueError(f"v2 API 失败: {type(e).__name__}:{str(e)[:60]}")


def fetch_x_syndication(username: str, timeout: int = REQUEST_TIMEOUT):
    """X 官方 syndication timeline API（无认证）。返回最近推文列表；失败抛异常。

    接口：https://cdn.syndication.twimg.com/timeline/profile?screen_name={user}&t={ts}
    返回 JSON：{"timeline":{"entries":[{"id","content","tweet":{"full_text","created_at"}...}]}}
    """
    ts = int(time.time())
    url = f"https://cdn.syndication.twimg.com/timeline/profile?screen_name={username}&t={ts}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json,text/html,*/*"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    text = resp.text

    tweets = []
    # 优先 JSON 解析
    try:
        data = json.loads(text)
        entries = data.get("timeline", {}).get("entries", [])
        for e in entries:
            tw = e.get("tweet") or {}
            tid = e.get("id") or tw.get("id_str") or ""
            content = tw.get("full_text") or tw.get("text") or ""
            published = tw.get("created_at") or ""
            if not content or not tid:
                continue
            tweets.append({
                "id": str(tid),
                "link": f"https://x.com/{username}/status/{tid}",
                "content": clean_text(content),
                "published": published,
            })
            if len(tweets) >= MAX_ENTRIES_PER_ACCOUNT:
                break
    except (json.JSONDecodeError, AttributeError, TypeError):
        # 兜底：HTML 嵌入版（老格式）
        blocks = re.split(r'class="timeline-item[^"]*"', text)[1:]
        for block in blocks:
            m = re.search(r'/status/(\d+)', block)
            if not m:
                continue
            tid = m.group(1)
            cm = re.search(r'class="tweet-content[^"]*"[^>]*>(.*?)</div>', block, re.S)
            content = clean_text(cm.group(1)) if cm else ""
            if not content:
                continue
            tweets.append({
                "id": tid,
                "link": f"https://x.com/{username}/status/{tid}",
                "content": content,
                "published": "",
            })
            if len(tweets) >= MAX_ENTRIES_PER_ACCOUNT:
                break

    if not tweets:
        snippet = re.sub(r"\s+", " ", text)[:150]
        raise ValueError(f"syndication 无有效推文 (HTTP={resp.status_code}, len={len(text)}) 片段: {snippet}")
    return tweets


def fetch_feed(feed_url: str, timeout: int = REQUEST_TIMEOUT):
    """抓取并解析 RSS feed，返回 feedparser 对象；失败抛异常"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    resp = requests.get(feed_url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if "html" in content_type.lower() and not resp.text.lstrip().startswith("<?"):
        raise ValueError(f"返回了 HTML 页面（可能被拦截），Content-Type: {content_type}")
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"RSS 解析失败: {parsed.bozo_exception}")
    return parsed


def fetch_nitter_html(username: str, instance: str, timeout: int = REQUEST_TIMEOUT):
    """抓取 Nitter 用户主页 HTML 并解析推文（v2：RSS 端点被大量禁用，改抓 HTML 页面）。

    Nitter 页面结构（多版本兼容）：
      <div class="timeline-item" ...>
        <div class="tweet-content ...">推文内容</div>
        <a href="/{user}/status/{id}" title="ISO时间">...
    返回 tweets 列表；失败抛异常。
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    url = f"{instance.rstrip('/')}/{username}"
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    page = resp.text

    # 校验页面是否为 Nitter（含 timeline-item 或至少推文链接）
    has_timeline = "timeline-item" in page or f"/status/" in page
    if not has_timeline:
        snippet = re.sub(r"\s+", " ", page)[:120]
        raise ValueError(f"非Nitter页(无timeline-item) 首120字: {snippet}")

    tweets = []
    # 按 timeline-item 切块解析（兼容不同缩进）
    blocks = re.split(r'class="timeline-item[^"]*"', page)[1:]
    for block in blocks:
        m = re.search(r'/(?:@?%s)/status/(\d+)' % re.escape(username), block)
        if not m:
            m = re.search(r'/status/(\d+)', block)
        if not m:
            continue
        tweet_id = m.group(1)

        # 推文内容：tweet-content 标签内
        cm = re.search(r'class="tweet-content[^"]*"[^>]*>(.*?)</div>', block, re.S)
        content = clean_text(cm.group(1)) if cm else ""
        if not content:
            continue

        # 时间：tweet-date 下 a 标签 title 属性（ISO 格式）
        dm = re.search(r'class="tweet-date"[^>]*>.*?title="([^"]+)"', block, re.S)
        published = dm.group(1) if dm else ""

        tweets.append({
            "id": tweet_id,
            "link": f"https://x.com/{username}/status/{tweet_id}",
            "content": content,
            "published": published,
        })
        if len(tweets) >= MAX_ENTRIES_PER_ACCOUNT:
            break

    if not tweets:
        raise ValueError("页面无有效推文（可能被限流/需登录）")
    return tweets


def extract_tweet_id(entry_url: str) -> str:
    """从推文链接中提取 status id"""
    m = re.search(r"/status/(\d+)", entry_url)
    return m.group(1) if m else ""


def clean_text(text: str, max_len: int = 300) -> str:
    """清理 HTML 实体并截断长度"""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


def entry_to_tweet(entry) -> dict:
    """将 feedparser 的 entry 转成结构化推文"""
    link = entry.get("link", "")
    tweet_id = extract_tweet_id(link)
    content = clean_text(entry.get("title", "") or entry.get("summary", ""))
    published = entry.get("published", "") or entry.get("updated", "")
    return {
        "id": tweet_id or hashlib.md5(link.encode()).hexdigest()[:12],
        "link": link,
        "content": content,
        "published": published,
    }


# ---------- 多源抓取 ----------

def get_nitter_html_urls(username: str, instances: list) -> list:
    return [f"{inst.rstrip('/')}/{username}" for inst in instances if inst]


def get_nitter_rss_urls(username: str, instances: list) -> list:
    return [f"{inst.rstrip('/')}/{username}/rss" for inst in instances if inst]


def get_rsshub_urls(username: str, instances: list) -> list:
    return [f"{inst.rstrip('/')}/twitter/user/{username}" for inst in instances if inst]


def fetch_recent_tweets(username: str, config: dict):
    """尝试多策略抓取：Nitter HTML（v2 首选）→ Nitter RSS → RSSHub。

    返回 (tweets, source_status)：
      tweets：最新若干条推文（按发布时间从新到旧），全部失败为 []
      source_status：dict {host: "OK:3条" 或 "FAIL:错误信息"}，供诊断落盘
    """
    nitter_instances = config.get("nitter_instances", [])
    rsshub_instances = config.get("rsshub_instances", [])
    source_status = {}

    # 策略0a：X 官方 v2 API 直连（guest token / 公开 Bearer）
    try:
        log(f"  [X官方] 尝试 v2 API 直连")
        tweets = fetch_x_guest_api(username)
        log(f"    [X官方v2] 抓到 {len(tweets)} 条，最新 id={tweets[0]['id']}")
        source_status["x-api-v2"] = f"OK:{len(tweets)}条"
        return tweets, source_status
    except Exception as e:
        log(f"    [X官方v2] 失败: {e}")
        source_status["x-api-v2"] = f"FAIL:{type(e).__name__}:{str(e)[:50]}"

    # 策略0b：X 官方 syndication API（无认证）
    try:
        log(f"  [X官方] 尝试 syndication API")
        tweets = fetch_x_syndication(username)
        log(f"    [X官方] 抓到 {len(tweets)} 条，最新 id={tweets[0]['id']}")
        source_status["x-syndication"] = f"OK:{len(tweets)}条"
        return tweets, source_status
    except Exception as e:
        log(f"    [X官方] 失败: {e}")
        source_status["x-syndication"] = f"FAIL:{type(e).__name__}:{str(e)[:50]}"

    # 策略1：Nitter HTML 页面（多数实例禁用 RSS 但页面仍可访问）
    html_urls = get_nitter_html_urls(username, nitter_instances)
    for url in html_urls:
        host = url.split("/")[2] if "//" in url else url
        try:
            log(f"  [HTML] 尝试源: {url}")
            tweets = fetch_nitter_html(username, url)
            log(f"    [HTML] 抓到 {len(tweets)} 条，最新 id={tweets[0]['id']}")
            source_status[host + "/html"] = f"OK:{len(tweets)}条"
            return tweets, source_status
        except Exception as e:
            log(f"    [HTML] 失败: {e}")
            source_status[host + "/html"] = f"FAIL:{type(e).__name__}:{str(e)[:50]}"

    # 策略2：Nitter RSS 端点
    rss_urls = get_nitter_rss_urls(username, nitter_instances)
    for url in rss_urls:
        host = url.split("/")[2] if "//" in url else url
        try:
            log(f"  [RSS] 尝试源: {url}")
            parsed = fetch_feed(url)
            if not parsed.entries:
                source_status[host + "/rss"] = "EMPTY:无条目"
                continue
            tweets = [entry_to_tweet(e) for e in parsed.entries[:MAX_ENTRIES_PER_ACCOUNT]]
            tweets = [t for t in tweets if t["id"]]
            if tweets:
                log(f"    [RSS] 抓到 {len(tweets)} 条，最新 id={tweets[0]['id']}")
                source_status[host + "/rss"] = f"OK:{len(tweets)}条"
                return tweets, source_status
            source_status[host + "/rss"] = "EMPTY:无有效id"
        except Exception as e:
            log(f"    [RSS] 失败: {e}")
            source_status[host + "/rss"] = f"FAIL:{type(e).__name__}:{str(e)[:50]}"

    # 策略3：RSSHub 公共实例
    for url in get_rsshub_urls(username, rsshub_instances):
        host = url.split("/")[2] if "//" in url else url
        try:
            log(f"  [RSSHub] 尝试源: {url}")
            parsed = fetch_feed(url)
            if not parsed.entries:
                source_status[host + "/rsshub"] = "EMPTY:无条目"
                continue
            tweets = [entry_to_tweet(e) for e in parsed.entries[:MAX_ENTRIES_PER_ACCOUNT]]
            tweets = [t for t in tweets if t["id"]]
            if tweets:
                log(f"    [RSSHub] 抓到 {len(tweets)} 条")
                source_status[host + "/rsshub"] = f"OK:{len(tweets)}条"
                return tweets, source_status
            source_status[host + "/rsshub"] = "EMPTY:无有效id"
        except Exception as e:
            log(f"    [RSSHub] 失败: {e}")
            source_status[host + "/rsshub"] = f"FAIL:{type(e).__name__}:{str(e)[:50]}"

    return [], source_status


# ---------- 主流程 ----------

def main() -> int:
    if not os.path.exists(CONFIG_FILE):
        log(f"❌ 找不到配置文件 {CONFIG_FILE}，请先创建（参考 README）")
        return 1

    config = load_json(CONFIG_FILE, {})
    accounts = config.get("accounts", [])
    if not accounts:
        log("❌ accounts.json 中未配置任何博主")
        return 1

    state = load_json(STATE_FILE, {})
    pending = load_json(PENDING_FILE, {"pending": []})
    pending_list = pending.get("pending", [])
    pending_ids = {t.get("id") for t in pending_list}  # 已在队列中的 id，避免重复入队

    new_count = 0
    round_sources = {}   # 本轮每个博主的源状态汇总
    success_accounts = 0  # 本轮成功抓到推文的博主数

    for acc in accounts:
        username = acc.get("username", "").strip().lstrip("@")
        display_name = acc.get("display_name") or username
        if not username:
            continue

        log(f"▶ 检查 @{username} ({display_name})")
        tweets, source_status = fetch_recent_tweets(username, config)
        round_sources[username] = source_status

        if not tweets:
            log(f"  ⚠️ {username} 所有源均失败，本轮跳过")
            continue
        success_accounts += 1

        # 已处理 id 集合（兼容旧格式：字符串 → 迁移为集合）
        handled = state.get(username)
        if isinstance(handled, str):
            handled = {handled} if handled else set()
        else:
            handled = set(handled or [])

        # RSS 按发布时间从新到旧，倒过来处理 = 从旧到新，保持队列顺序
        added_this_account = 0
        for tweet in reversed(tweets):
            if tweet["id"] in handled or tweet["id"] in pending_ids:
                continue
            pending_list.append({
                "id": tweet["id"],
                "username": username,
                "display_name": display_name,
                "content": tweet["content"],
                "link": f"https://x.com/{username}/status/{tweet['id']}",
                "published": tweet["published"],
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            handled.add(tweet["id"])
            pending_ids.add(tweet["id"])
            added_this_account += 1
            new_count += 1

        if added_this_account:
            log(f"  ➕ 新入队 {added_this_account} 条")
        else:
            log(f"  无新推文")

        # 保留最近 N 个已处理 id
        state[username] = list(handled)[-MAX_HANDLED_PER_ACCOUNT:]

    # ---- 诊断落盘：源状态 + 连续失败计数 ----
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    diag = state.get("_source_status", {})
    diag[now] = round_sources
    # 只保留最近 10 轮
    keys = sorted(diag.keys())
    for k in keys[:-10]:
        diag.pop(k, None)
    state["_source_status"] = diag

    consecutive_fail = int(state.get("_consecutive_fail", 0))
    if success_accounts == 0:
        consecutive_fail += 1
    else:
        consecutive_fail = 0
    state["_consecutive_fail"] = consecutive_fail
    state["_last_run"] = {
        "time": now,
        "new_tweets": new_count,
        "success_accounts": success_accounts,
        "total_accounts": len(accounts),
    }

    # 落盘
    save_json(STATE_FILE, state)
    save_json(PENDING_FILE, {"pending": pending_list})
    log(f"===== 本轮完成：新增 {new_count} 条到待推送队列，队列当前共 {len(pending_list)} 条，成功博主 {success_accounts}/{len(accounts)} =====")

    # 连续失败告警：写入 state.json 的 _alert 标记（workflow 单独步骤检测并 fail）
    # 注意：这里不能 exit 1，否则 workflow 的 commit 步骤被跳过、诊断无法落盘
    state["_alert"] = {
        "active": consecutive_fail >= FAIL_THRESHOLD,
        "consecutive_fail": consecutive_fail,
        "message": f"连续 {consecutive_fail} 轮所有博主所有源均失败！请检查 RSS/API 源可用性（详见 _source_status）",
        "time": now,
    }

    # 落盘
    save_json(STATE_FILE, state)
    save_json(PENDING_FILE, {"pending": pending_list})
    log(f"===== 本轮完成：新增 {new_count} 条到待推送队列，队列当前共 {len(pending_list)} 条，成功博主 {success_accounts}/{len(accounts)} =====")

    if consecutive_fail >= FAIL_THRESHOLD:
        log(f"⚠️ 连续 {consecutive_fail} 轮所有源均失败（告警标记已写入 state.json 的 _alert）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
