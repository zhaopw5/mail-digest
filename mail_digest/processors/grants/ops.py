"""Grants Agent 操作（本域 CLI 与 mail-digest 共用；不依赖 ads 模块）。"""
from __future__ import annotations

import sys
from datetime import date

from ...core.config import Config
from ...core.ops import parse_date_arg as _parse_date_arg
from .classifier import is_grant_email
from .processor import run_fund

def cmd_grants_run(cfg: Config, args: argparse.Namespace) -> None:
    if not cfg.grants_enabled:
        print("⏸️  Grant Agent 已关闭（.env 中 GRANTS_ENABLED=false）。如需启用改为 true。")
        return
    try:
        import pypdf  # noqa: F401
    except ImportError:
        print("ℹ️  未安装文档解析依赖（pypdf/python-docx/openpyxl/py7zr），附件只能解析 txt/csv；")
        print("    可执行  pip install -e \".[grants]\"  以完整解析 docx/pdf/xlsx/压缩包。")
    if not cfg.grant_allowed_senders.strip():
        print("⚠️  未配置 GRANT_ALLOWED_SENDERS（可信发件人白名单）——为防恶意附件，跳过全部基金邮件附件处理。")
        print("    请在 .env 配置，如：GRANT_ALLOWED_SENDERS=*@mail.sysu.edu.cn")
        return
    mails = load_mails_from_dir(cfg.eml_dir)
    grant_mails = [m for m in mails if is_grant_email(m)]
    print(f"扫描 {len(mails)} 封邮件，识别出 {len(grant_mails)} 封基金/项目申报通知")
    force = getattr(args, "force", False)
    limit = getattr(args, "limit", None)
    n, digest_text = run_fund(cfg, grant_mails, force=force, limit=limit)
    if n == 0:
        print("没有待处理的新申报通知（已全部处理过；用 --force 强制重跑）")
        return
    print(f"共处理 {n} 封")
    if digest_text:
        out = cfg.digest_dir / f"fund_{date.today():%Y%m%d}.md"
        out.write_text(digest_text, encoding="utf-8")
        print(f"📄 今日申报清单已生成：{out}")
    else:
        print("（今天没有当天收到的通知，清单未生成；结果已缓存）")

def cmd_grants_push(cfg: Config, args: argparse.Namespace) -> None:
    when = _parse_date_arg(args) or date.today()
    f = cfg.digest_dir / f"fund_{when:%Y%m%d}.md"
    if not f.exists():
        print(f"ℹ️  {when:%Y-%m-%d} 无申报清单文件（当日无通知或未先运行 grants run）")
        return
    send_markdown(cfg, f"项目申报机会清单 {when:%Y-%m-%d}",
                  f.read_text(encoding="utf-8"))
    print(f"✅ 已发送申报清单到 {cfg.imap_user}")
