#!/usr/bin/env python3
"""Mail Digest CLI：自动邮件整理底座 + 两个独立 Agent（处理器）。

入口说明：
  mail-digest / python3 main.py —— 全量入口（两个 Agent）
  ads-digest / grants-digest    —— 各自独立入口（见 ads_cli.py / grants_cli.py）
"""
from __future__ import annotations

import argparse
import os

from mail_digest.core.config import Config
from mail_digest.core.ops import cmd_fetch
from mail_digest.processors.ads.ops import cmd_ads_push, cmd_ads_run, cmd_html
from mail_digest.processors.grants.ops import cmd_grants_push, cmd_grants_run


def cmd_all(cfg: Config, args: argparse.Namespace) -> None:
    cmd_fetch(cfg, args)
    print()
    if cfg.ads_enabled:
        cmd_ads_run(cfg, args)
    if cfg.grants_enabled:
        print()
        cmd_grants_run(cfg, args)


def main() -> None:
    if hasattr(os, "umask"):
        os.umask(0o077)
    parser = argparse.ArgumentParser(
        prog="mail-digest",
        description="自动邮件整理底座（core）+ ADS 文献 / 项目申报 两个独立 Agent",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="公共：IMAP 拉最近 N 封邮件到 data/emails")
    p_fetch.add_argument("--recent", type=int, default=None)
    p_fetch.add_argument("--folder", default=None)

    p_ads = sub.add_parser("ads", help="ADS 文献 Agent")
    ads_sub = p_ads.add_subparsers(dest="ads_cmd", required=True)
    a_run = ads_sub.add_parser("run", help="识别 ADS 推送 → ADS API → 中文简报")
    a_run.add_argument("--force", action="store_true")
    a_run.add_argument("--limit", type=int, default=None)
    a_push = ads_sub.add_parser("push", help="把某天 ADS 中文简报邮件发给自己")
    a_push.add_argument("--date", default=None)

    p_grants = sub.add_parser("grants", help="项目申报 Agent")
    g_sub = p_grants.add_subparsers(dest="grants_cmd", required=True)
    g_run = g_sub.add_parser("run", help="申报通知 → 附件解析 → 今日申报机会清单")
    g_run.add_argument("--force", action="store_true")
    g_run.add_argument("--limit", type=int, default=None)
    g_push = g_sub.add_parser("push", help="把某天申报清单邮件发给自己")
    g_push.add_argument("--date", default=None)

    p_all = sub.add_parser("all", help="一键：fetch + 各已启用 Agent 的 run")
    p_all.add_argument("--recent", type=int, default=None)
    p_all.add_argument("--limit", type=int, default=None)

    sub.add_parser("html", help="公共：重新生成中文合并 HTML 总览")

    args = parser.parse_args()
    cfg = Config.load()
    if args.cmd == "fetch":
        cmd_fetch(cfg, args)
    elif args.cmd == "ads":
        (cmd_ads_run if args.ads_cmd == "run" else cmd_ads_push)(cfg, args)
    elif args.cmd == "grants":
        (cmd_grants_run if args.grants_cmd == "run" else cmd_grants_push)(cfg, args)
    elif args.cmd == "html":
        cmd_html(cfg, args)
    else:
        cmd_all(cfg, args)


if __name__ == "__main__":
    main()
