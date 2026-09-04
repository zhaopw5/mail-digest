"""项目申报 Agent 独立入口。

安装后使用：grants-digest fetch | run | push
（仅展示申报相关命令；内部复用 cli.py 的实现）
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
    from mail_digest.processors.grants.ops import cmd_grants_push, cmd_grants_run

    parser = argparse.ArgumentParser(
        prog="grants-digest",
        description="项目申报 Agent：学院基金/申报通知 → 附件解析 → 申报机会清单（含能力匹配与证据）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_f = sub.add_parser("fetch", help="拉取最近邮件到 data/emails")
    p_f.add_argument("--recent", type=int, default=None)
    p_f.add_argument("--folder", default=None)

    p_r = sub.add_parser("run", help="申报通知 → 附件安全解析 → 今日申报机会清单")
    p_r.add_argument("--force", action="store_true")
    p_r.add_argument("--limit", type=int, default=None)

    p_p = sub.add_parser("push", help="把当天申报清单邮件发给自己")
    p_p.add_argument("--date", default=None)

    args = parser.parse_args()
    cfg = Config.load()
    if args.cmd == "fetch":
        cmd_fetch(cfg, args)
    elif args.cmd == "run":
        cmd_grants_run(cfg, args)
    else:
        cmd_grants_push(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
