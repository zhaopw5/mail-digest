"""通用邮件发送（core）：SMTP HTML 邮件。

仅保留跨 Agent 通用能力（send_html / send_markdown）。
ADS 专属的简报汇总与按日推送见 processors/ads/delivery.py。
"""
from __future__ import annotations

import html as html_mod
import smtplib
from email.header import Header
from email.mime.text import MIMEText

from .html import _CSS, md_to_html


def send_html(cfg, to: str, subject: str, html_body: str,
             agent: str = "mail-digest") -> None:
    """通过 SMTP 发送 HTML 邮件（SSL，使用 IMAP 同款授权码）。"""
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg.imap_user
    msg["To"] = to
    msg["X-Mail-Digest-Agent"] = agent   # 自推送标记：接收侧据此排除，防循环
    with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
        server.login(cfg.imap_user, cfg.imap_auth_code)
        server.send_message(msg)


def send_markdown(cfg, subject: str, md_text: str, agent: str = "grants") -> None:
    """把一段 Markdown（如申报机会清单）作为完整 HTML 邮件发出。"""
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html_mod.escape(subject)}</title><style>{_CSS}</style></head>
<body>{md_to_html(md_text, base=1)}<hr>
<p style="color:#888">mail-digest 每日自动推送 · 关键信息（日期/金额）请以附件原文为准。</p>
</body></html>"""
    send_html(cfg, cfg.imap_user, subject, doc, agent=agent)
