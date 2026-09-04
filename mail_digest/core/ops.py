"""公共操作（core）：fetch 与 json/幂等工具，供 cli 与各域 cli 复用。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Config
from .imap_client import fetch_recent


def _load_json_obj(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json_obj(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_processed(path: Path) -> set[int]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_processed(path: Path, processed: set[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(processed), ensure_ascii=False, indent=2),
                    encoding="utf-8")


def parse_date_arg(arg: str | None) -> object:
    from datetime import datetime
    if not arg:
        return None
    try:
        return datetime.strptime(arg, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"日期格式错误：{arg}（应为 YYYY-MM-DD）")


def cmd_fetch(cfg: Config, args: argparse.Namespace) -> None:
    if not cfg.imap_user or not cfg.imap_auth_code:
        sys.exit("未配置邮箱：复制 .env.example 为 .env，填写 IMAP_USER 和 IMAP_AUTH_CODE（16 位授权码）")
    recent = args.recent or cfg.default_recent
    folder = getattr(args, "folder", None) or cfg.default_folder
    mails = fetch_recent(cfg, recent=recent, folder=folder)
    print(f"✅ 拉取 {len(mails)} 封邮件 → {cfg.eml_dir}")
    for m in mails:
        when = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "(无日期)"
        print(f"   {when}  [{m.uid:>6}]  {m.from_[:32]:<32} | {m.subject[:60]}")
