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
    from mail_digest.processors.ads.ops import (
        cmd_ads_push, cmd_ads_run, cmd_ads_state_init, cmd_html,
    )

    parser = argparse.ArgumentParser(
        prog="ads-digest",
        description="ADS 文献 Agent：NASA ADS 文献推送 → 中文简报（翻译/点评/星星分级）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_f = sub.add_parser("fetch", help="按 UID 增量拉取邮件（首次接管默认全量）")
    p_f.add_argument("--recent", type=int, default=None)
    p_f.add_argument("--folder", default=None)

    p_r = sub.add_parser("run", help="识别 ADS 推送 → ADS API → 中文简报")
    p_r.add_argument("--force", action="store_true")
    p_r.add_argument("--limit", type=int, default=None)

    p_p = sub.add_parser("push", help="推送 ADS 简报（必须指定模式）")
    _g = p_p.add_mutually_exclusive_group(required=True)
    _g.add_argument("--official", action="store_true", help="正式推送（cron 用）")
    _g.add_argument("--test", action="store_true", help="测试推送（[TEST] 标题，不改状态）")
    _g.add_argument("--dry-run", action="store_true", help="预览，不发送不改状态")
    p_p.add_argument("--date", default=None, help="仅 --test/--dry-run 选择内容日期")
    p_p.add_argument("--cutoff", default=None, help="仅供测试/补跑：显式指定正式推送截止点")
    si = sub.add_parser("state-init", help="初始化正式推送状态（旧版本迁移）")
    si.add_argument("--last-official", required=True)
    si.add_argument("--mark-existing-sent", action="store_true",
                    help="把截止点之前的现有简报标记为已发送（需 --confirm）")
    si.add_argument("--dry-run", action="store_true",
                    help="只预览将标记/保留哪些简报，不写任何文件")
    si.add_argument("--confirm", action="store_true", help="确认执行不可逆操作")
    si.add_argument("--force", action="store_true", help="覆盖已存在的状态文件（会先备份）")

    sub.add_parser("html", help="生成合并 HTML 总览")

    args = parser.parse_args()
    cfg = Config.load()
    if args.cmd == "fetch":
        cmd_fetch(cfg, args)
    elif args.cmd == "run":
        cmd_ads_run(cfg, args)
    elif args.cmd == "push":
        cmd_ads_push(cfg, args)
    elif args.cmd == "state-init":
        cmd_ads_state_init(cfg, args)
    else:
        cmd_html(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
