#!/usr/bin/env python3
"""薄封装：Markdown 简报 → HTML（渲染逻辑已在 mail_digest/core/html.py）。

用法（仓库内直接运行）：
  python3 scripts/build_html_digest.py                 # 合并 data/digests/zh/*.zh.md
  python3 scripts/build_html_digest.py <md文件>        # 单文件转 HTML（同目录输出 .html）

注意：打包安装后用 `python3 main.py html` 或模块内函数即可，无需本脚本。
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from mail_digest.core import html as h  # noqa: E402


def main() -> None:
    if len(sys.argv) > 1:
        src = Path(sys.argv[1])
        text = src.read_text(encoding="utf-8")
        out = src.with_suffix(".html")
        out.write_text(h.md_to_html_doc(text, title=src.stem), encoding="utf-8")
        print(f"已生成: {out}")
        return
    zh_dir = _REPO / "data" / "digests" / "zh"
    files = sorted(zh_dir.glob("*.zh.md"))
    if not files:
        sys.exit(f"未找到中文简报文件: {zh_dir}")
    out = _REPO / "data" / "digests" / "ADS文献简报-中文总览.html"
    out.write_text(h.merge_markdown_files(files), encoding="utf-8")
    print(f"已生成合并 HTML（{len(files)} 份）: {out}")


if __name__ == "__main__":
    main()
