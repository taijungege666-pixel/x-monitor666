#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
推文分析辅助脚本：拉取待分析推文 + 维护已分析记录
=================================================
运行环境：本地电脑（配合 WorkBuddy 自动化使用）

职责：
1. 从 GitHub 仓库拉取 pending.json（云端 monitor.py 抓取的推文队列）
2. 与本地 analyzed.json 对比，找出"尚未分析过"的新推文
3. 输出新推文清单（供 AI 识别上市公司、拉数据、写分析）
4. AI 完成分析后，用 --mark-all 标记本轮推文为已分析

用法：
  python analyze_runner.py                # 输出新推文清单（JSON）
  python analyze_runner.py --mark-all     # 把当前所有新推文标记为已分析
  python analyze_runner.py --local        # 从本地 pending.json 读取（测试用）

依赖：pip install requests
"""

import argparse
import json
import os
import sys
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ANALYZED_FILE = os.path.join(BASE_DIR, "analyzed.json")
PENDING_LOCAL = os.path.join(BASE_DIR, "pending.json")

# GitHub 上 pending.json 的 raw 地址（公开仓库无需认证）
GITHUB_RAW_URL = os.environ.get("GITHUB_RAW_URL", "").strip()

REQUEST_TIMEOUT = 20


def log(msg: str) -> None:
    print(msg, flush=True)


def load_json(path: str, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"⚠️ 读取 {path} 失败（{e}），使用默认值")
    return default if default is not None else {}


def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_pending(use_local: bool = False) -> list:
    """获取待分析推文列表。优先 GitHub raw，失败或指定时读本地文件。"""
    if use_local or not GITHUB_RAW_URL:
        if os.path.exists(PENDING_LOCAL):
            data = load_json(PENDING_LOCAL, {})
            return data.get("pending", [])
        log("⚠️ 本地无 pending.json，且未设置 GITHUB_RAW_URL")
        return []
    try:
        resp = requests.get(GITHUB_RAW_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("pending", [])
    except Exception as e:
        log(f"⚠️ 拉取 GitHub pending.json 失败: {e}")
        log("   可先设置环境变量 GITHUB_RAW_URL，或确认仓库已部署")
        return []


def get_new_tweets(pending: list, analyzed_ids: set) -> list:
    """过滤出未分析过的推文（新推文）"""
    return [t for t in pending if t.get("id") not in analyzed_ids]


def main() -> int:
    parser = argparse.ArgumentParser(description="推文分析辅助脚本")
    parser.add_argument("--mark-all", action="store_true", help="把当前所有新推文标记为已分析")
    parser.add_argument("--local", action="store_true", help="从本地 pending.json 读取（测试用）")
    args = parser.parse_args()

    pending = fetch_pending(use_local=args.local)
    if not pending:
        log("暂无待分析推文（队列为空或拉取失败）")
        return 0

    analyzed = load_json(ANALYZED_FILE, {})
    analyzed_ids = set(analyzed.get("analyzed", []))
    new_tweets = get_new_tweets(pending, analyzed_ids)

    if args.mark_all:
        for t in new_tweets:
            analyzed_ids.add(t.get("id"))
        analyzed["analyzed"] = sorted(analyzed_ids)
        save_json(ANALYZED_FILE, analyzed)
        log(f"✅ 已标记 {len(new_tweets)} 条推文为已分析（累计 {len(analyzed_ids)} 条）")
        return 0

    if not new_tweets:
        log("无新推文（本轮所有推文均已分析过）")
        return 0

    # 输出新推文清单（供 AI 逐条分析）
    log(f"发现 {len(new_tweets)} 条新推文：")
    print(json.dumps(new_tweets, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
