"""ADS 合并 HTML 总览（ADS 域专用渲染逻辑）。

早期实现位于 core/html.py，重构后仅保留通用渲染（_inline/_CSS/md_to_html/…）于
core/html.py；ADS 专属的按日期合并规则移回本模块（processors/ads/）。
"""
from __future__ import annotations

import re
from pathlib import Path

from ...core.html import _CSS, md_to_html


def _body_after_header(text: str) -> str:
    """去掉简报 md 的文档标题行与头部元数据（到第一个 --- 分隔线为止）。"""
    return text.split("---", 1)[1] if "---" in text else text


def _date_of(f: Path) -> str:
    m = re.search(r"ads_(\d{8})_", f.name)
    tag = m.group(1) if m else "????"
    return f"{tag[:4]}-{tag[4:6]}-{tag[6:]}"


def merge_markdown_files(files: list[Path]) -> str:
    """把多份 ADS 中文简报合并为一个完整 HTML 总览文档。

    层级：h1 总标题 / h2 日期（文件名） / h3 订阅 / h4 文献。
    """
    sections: list[str] = []
    total = 0
    subs_seen: set[str] = set()
    for f in sorted(files, key=lambda p: p.name):
        pretty_date = _date_of(f)
        body = _body_after_header(f.read_text(encoding="utf-8"))
        n = len(re.findall(r"^### ", body, re.M))
        total += n
        subs_seen.update(re.findall(r"^## 📚 ([^·]+) ·", body, re.M))
        sections.append(
            f'<h2>📅 {pretty_date} 的 ADS 推送（{n} 条文献）</h2>\n'
            f"{md_to_html(body, base=2)}"
        )
    first = _date_of(files[0])
    last = _date_of(files[-1])
    sub_desc = "、".join(sorted(subs_seen)) if subs_seen else "（无订阅分组）"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>ADS 文献简报（中文版）· 总览</title>
<style>{_CSS}</style>
</head>
<body>
<h1>ADS 文献简报（中文版）</h1>
<p>覆盖 {first} — {last}，共 <strong>{total}</strong> 条文献，
按 myADS 订阅分组（{sub_desc}）。每条含中文翻译、一句话点评与相关性分级；
点「链接」可跳转 ADS 论文主页。</p>
{chr(10).join(sections)}
<hr>
<p style="color:#888">由 mail-digest 自动生成 · 中文翻译为机器辅助翻译，关键术语与公式请以原文为准。</p>
</body>
</html>
"""
