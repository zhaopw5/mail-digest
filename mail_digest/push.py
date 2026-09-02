"""邮件推送（M4 雏形）：把每日中文简报以 HTML 邮件发给自己。

流程：main.py push [--date YYYY-MM-DD]
  - 默认发「今天」有推送的中文简报；当天没有 ADS 推送则不发送。
  - --date 可指定历史日期（用于测试或补发）。
"""
from __future__ import annotations

import html as html_mod
import smtplib
import sys
from datetime import date, datetime
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

# 复用 build_html_digest 的 md → HTML 转换
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import build_html_digest as bhd  # noqa: E402


def collect_zh_for_date(cfg, when: date) -> list[Path]:
    """找指定日期生成的中文简报文件（按文件名日期匹配）。"""
    return sorted(cfg.zh_digest_dir.glob(f"ads_{when:%Y%m%d}_*.zh.md"))


def _assemble_doc(cfg, files: list[Path]) -> str:
    """把当天若干份 zh 简报合并成一个内联样式的 HTML 文档。"""
    sections = []
    for f in files:
        body = bhd._body_after_header(f.read_text(encoding="utf-8"))
        sections.append(bhd.md_to_html(body, base=2))
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>ADS 文献简报</title><style>{bhd._CSS}</style></head>
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


def push(cfg, when: date | None = None) -> bool:
    """推送指定日期的中文简报；无内容返回 False（不发送）。"""
    when = when or date.today()
    files = collect_zh_for_date(cfg, when)
    if not files:
        return False
    doc = _assemble_doc(cfg, files)
    # 统计文献条数（### 行）
    n_arts = 0
    for f in files:
        n_arts += sum(1 for line in f.read_text(encoding="utf-8").splitlines()
                      if line.startswith("### "))
    subject = f"ADS 文献简报 {when:%Y-%m-%d}（{n_arts} 条文献）"
    send_html(cfg, cfg.imap_user, subject, doc)
    return True
