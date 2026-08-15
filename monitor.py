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

注意：本脚本不直接推送消息。真正的推送由运行在本地电脑的
qq_relay.py 完成（本地 NapCat 登录小号 → 私聊推送）。

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

REQUEST_TIMEOUT = 20  # 秒
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
MAX_ENTRIES_PER_ACCOUNT = 5   # 每个博主最多处理前 N 条
MAX_HANDLED_PER_ACCOUNT = 20  # state 中每个博主保留最近 N 个已处理 id


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


def fetch_feed(feed_url: str, timeout: int = REQUEST_TIMEOUT):
    """抓取并解析 RSS feed，返回 feedparser 对象"""
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

def get_nitter_urls(username: str, instances: list) -> list:
    return [f"{inst.rstrip('/')}/{username}/rss" for inst in instances if inst]


def get_rsshub_urls(username: str, instances: list) -> list:
    return [f"{inst.rstrip('/')}/twitter/user/{username}" for inst in instances if inst]


def fetch_recent_tweets(username: str, config: dict) -> list:
    """尝试所有 RSS 源，返回最新若干条推文（按发布时间从新到旧排序）。全部失败返回 []"""
    nitter_instances = config.get("nitter_instances", [])
    rsshub_instances = config.get("rsshub_instances", [])
    feed_urls = get_nitter_urls(username, nitter_instances) + get_rsshub_urls(username, rsshub_instances)

    for url in feed_urls:
        try:
            log(f"  尝试源: {url}")
            parsed = fetch_feed(url)
            if not parsed.entries:
                log(f"    该源无内容，跳过")
                continue
            tweets = [entry_to_tweet(e) for e in parsed.entries[:MAX_ENTRIES_PER_ACCOUNT]]
            tweets = [t for t in tweets if t["id"]]
            if tweets:
                log(f"    抓到 {len(tweets)} 条，最新 id={tweets[0]['id']} published={tweets[0]['published']}")
                return tweets
        except Exception as e:
            log(f"    失败: {e}")
    return []


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

    for acc in accounts:
        username = acc.get("username", "").strip().lstrip("@")
        display_name = acc.get("display_name") or username
        if not username:
            continue

        log(f"▶ 检查 @{username} ({display_name})")
        tweets = fetch_recent_tweets(username, config)
        if not tweets:
            log(f"  ⚠️ {username} 所有源均失败，本轮跳过")
            continue

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

    # 落盘
    save_json(STATE_FILE, state)
    save_json(PENDING_FILE, {"pending": pending_list})
    log(f"===== 本轮完成：新增 {new_count} 条到待推送队列，队列当前共 {len(pending_list)} 条 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
