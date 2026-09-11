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


def _digest_groups(cfg) -> dict[str, list[Path]]:
    """按文件名日期（= 邮件日期）分组：{YYYYMMDD: [文件...]}。"""
    import re as _re
    groups: dict[str, list[Path]] = {}
    for f in cfg.zh_digest_dir.glob("ads_*.zh.md"):
        m = _re.search(r"ads_(\d{8})_", f.name)
        if m:
            groups.setdefault(m.group(1), []).append(f)
    return groups


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
        _send_digest(cfg, [when], files)
        return True

    # 推送「上次运行以来新处理、且尚未推送过」的全部简报——
    # 覆盖跨天到达、以及窗口内多封推送（合并为一封发出）。
    groups = _digest_groups(cfg)
    fresh: list[tuple[date, list[Path]]] = []
    for tag, files in sorted(groups.items()):
        if tag in pushed:
            continue
        try:
            d = date(int(tag[:4]), int(tag[4:6]), int(tag[6:]))
        except ValueError:
            continue
        if d < date.today() - timedelta(days=max_age_days):
            continue                          # 太久远的历史简报不补推
        fresh.append((d, sorted(files)))
    if not fresh:
        return False
    all_files = [f for _d, fs in fresh for f in fs]
    _send_digest(cfg, [d for d, _ in fresh], all_files)
    now = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    for d, _fs in fresh:
        pushed[f"{d:%Y%m%d}"] = now
    _save_pushed(cfg, pushed)
    return True


def _send_digest(cfg, dates: list[date], files: list[Path]) -> None:
    """发送简报（支持跨多个邮件日期合并为一封）。"""
    doc = _assemble_doc(cfg, files)
    n_arts = 0
    for f in files:
        n_arts += sum(1 for line in f.read_text(encoding="utf-8").splitlines()
                      if line.startswith("### "))
    if len(dates) == 1:
        label = f"{dates[0]:%Y-%m-%d}"
    else:
        label = f"{min(dates):%Y-%m-%d} ~ {max(dates):%Y-%m-%d}"
    send_html(cfg, cfg.imap_user, f"ADS 文献简报 {label}（{n_arts} 条文献）", doc, agent="ads")
