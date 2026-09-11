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


def _load_pushed(cfg) -> dict:
    import json
    try:
        return json.loads(cfg.ads_pushed_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_pushed(cfg, pushed: dict) -> None:
    import json
    try:
        cfg.ads_pushed_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.ads_pushed_file.write_text(
            json.dumps(pushed, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _latest_digest_group(cfg) -> tuple[str, list[Path]]:
    """按文件名日期分组，返回 (最新日期 YYYYMMDD, 该日期的全部简报文件)。"""
    import re as _re
    groups: dict[str, list[Path]] = {}
    for f in cfg.zh_digest_dir.glob("ads_*.zh.md"):
        m = _re.search(r"ads_(\d{8})_", f.name)
        if m:
            groups.setdefault(m.group(1), []).append(f)
    if not groups:
        return "", []
    latest = max(groups)
    return latest, sorted(groups[latest])


def push(cfg, when: date | None = None, max_age_days: int = 3) -> bool:
    """推送 ADS 简报。

    - 不带日期（每日 cron）：推送「最新一份且尚未推送过」的简报——
      这样跨天到达的推送（如昨天 19:41 到、今早 9:00 才处理）也能正确发出，
      不会因文件名是邮件日期而误判为「无新推送」。
    - 带日期：按指定日期推送（测试/补发用）。
    无新内容返回 False（调用方据此发送状态邮件）。
    """
    from datetime import timedelta
    pushed = _load_pushed(cfg)

    if when is not None:                     # 指定日期：按原逻辑
        files = collect_zh_for_date(cfg, when)
        if not files:
            return False
        _send_digest(cfg, when, files)
        return True

    latest, files = _latest_digest_group(cfg)
    if not files or not latest:
        return False
    if latest in pushed:                     # 该日期已推送过 → 无新内容
        return False
    # 只推「较新」的简报，避免首次运行把历史简报全发一遍
    try:
        d = date(int(latest[:4]), int(latest[4:6]), int(latest[6:]))
    except ValueError:
        return False
    if d < date.today() - timedelta(days=max_age_days):
        return False
    _send_digest(cfg, d, files)
    pushed[latest] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    _save_pushed(cfg, pushed)
    return True


def _send_digest(cfg, when: date, files: list[Path]) -> None:
    doc = _assemble_doc(cfg, files)
    n_arts = 0
    for f in files:
        n_arts += sum(1 for line in f.read_text(encoding="utf-8").splitlines()
                      if line.startswith("### "))
    subject = f"ADS 文献简报 {when:%Y-%m-%d}（{n_arts} 条文献）"
    send_html(cfg, cfg.imap_user, subject, doc, agent="ads")
