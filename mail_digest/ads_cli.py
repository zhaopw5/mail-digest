"""ADS 文献 Agent 独立入口。

安装后使用：ads-digest fetch | run | push | html
（仅展示 ADS 相关命令；内部复用 cli.py 的实现）
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    if hasattr(os, "umask"):
        os.umask(0o077)
    from mail_digest.core.config import Config
    from mail_digest.core.ops import cmd_fetch
    from mail_digest.processors.ads.ops import cmd_ads_push, cmd_ads_run, cmd_html

    parser = argparse.ArgumentParser(
        prog="ads-digest",
        description="ADS 文献 Agent：NASA ADS 文献推送 → 中文简报（翻译/点评/星星分级）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_f = sub.add_parser("fetch", help="拉取最近邮件到 data/emails")
    p_f.add_argument("--recent", type=int, default=None)
    p_f.add_argument("--folder", default=None)

    p_r = sub.add_parser("run", help="识别 ADS 推送 → ADS API → 中文简报")
    p_r.add_argument("--force", action="store_true")
    p_r.add_argument("--limit", type=int, default=None)

    p_p = sub.add_parser("push", help="把当天 ADS 中文简报邮件发给自己")
    p_p.add_argument("--date", default=None)

    sub.add_parser("html", help="生成合并 HTML 总览")

    args = parser.parse_args()
    cfg = Config.load()
    if args.cmd == "fetch":
        cmd_fetch(cfg, args)
    elif args.cmd == "run":
        cmd_ads_run(cfg, args)
    elif args.cmd == "push":
        cmd_ads_push(cfg, args)
    else:
        cmd_html(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
