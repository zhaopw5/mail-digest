"""ADS 每日邮件推送（ADS 域专用；通用 SMTP 在 core/push.py）。

CLI：ads-digest push [--date]（见 cli.py / ads_cli.py）
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from ...core.push import send_html
from .overview import _body_after_header
from ...core.html import _CSS, md_to_html


def collect_zh_for_date(cfg, when: date) -> list[Path]:
    """找指定日期生成的 ADS 中文简报文件（按文件名日期匹配）。"""
    return sorted(cfg.zh_digest_dir.glob(f"ads_{when:%Y%m%d}_*.zh.md"))


def _assemble_doc(cfg, files: list[Path]) -> str:
    """把当天若干份 ADS zh 简报合并成一个内联样式的 HTML 文档。"""
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


def push(cfg, when: date | None = None) -> bool:
    """推送指定日期的 ADS 中文简报；无内容返回 False（不发送）。"""
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
