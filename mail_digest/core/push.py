"""邮件推送（core）：把每日中文简报 / 申报清单以 HTML 邮件发给自己。

CLI：ads push [--date]、grants push [--date]（见 cli.py）。
"""
from __future__ import annotations

import html as html_mod
import smtplib
from datetime import date
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

from .html import _CSS, _body_after_header, md_to_html


def collect_zh_for_date(cfg, when: date) -> list[Path]:
    """找指定日期生成的中文简报文件（按文件名日期匹配）。"""
    return sorted(cfg.zh_digest_dir.glob(f"ads_{when:%Y%m%d}_*.zh.md"))


def _assemble_doc(cfg, files: list[Path]) -> str:
    """把当天若干份 zh 简报合并成一个内联样式的 HTML 文档。"""
    sections = []
    for f in files:
        body = _body_after_header(f.read_text(encoding="utf-8"))
        sections.append(md_to_html(body, base=2))
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>ADS 文献简报</title><style>{_CSS}</style></head>
<body>{chr(10).join(sections)}<hr>
<p style="color:#888">mail-digest 每日自动推送 · 中文为机器辅助翻译，关键内容请核对原文。</p>
</body></html>"""


def send_html(cfg, to: str, subject: str, html_body: str) -> None:
    """通过 SMTP 发送 HTML 邮件（SSL，使用 IMAP 同款授权码）。"""
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg.imap_user
    msg["To"] = to
    with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
        server.login(cfg.imap_user, cfg.imap_auth_code)
        server.send_message(msg)


def send_markdown(cfg, subject: str, md_text: str) -> None:
    """把一段 Markdown（如申报机会清单）作为完整 HTML 邮件发出。"""
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html_mod.escape(subject)}</title><style>{_CSS}</style></head>
<body>{md_to_html(md_text, base=1)}<hr>
<p style="color:#888">mail-digest 每日自动推送 · 关键信息（日期/金额）请以附件原文为准。</p>
</body></html>"""
    send_html(cfg, cfg.imap_user, subject, doc)


def push(cfg, when: date | None = None) -> bool:
    """推送指定日期的中文简报；无内容返回 False（不发送）。"""
    when = when or date.today()
    files = collect_zh_for_date(cfg, when)
    if not files:
        return False
    doc = _assemble_doc(cfg, files)
    n_arts = 0
    for f in files:
        n_arts += sum(1 for line in f.read_text(encoding="utf-8").splitlines()
                      if line.startswith("### "))
    subject = f"ADS 文献简报 {when:%Y-%m-%d}（{n_arts} 条文献）"
    send_html(cfg, cfg.imap_user, subject, doc)
    return True
