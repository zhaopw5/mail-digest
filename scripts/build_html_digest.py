"""把 Markdown 文献简报转成可点击链接的 HTML。

支持的新结构：
  # 简报标题
  ## 📚 <订阅名> · <中文名>（N 条）      ← 订阅分组
  ### N. 文献标题                        ← 文献条目

用法：
  python3 scripts/build_html_digest.py            # 合并 data/digests/backfill_old_mailbox/zh/*.zh.md
  python3 scripts/build_html_digest.py <md文件>   # 单文件转换（输出同目录 .html）

以后每日简报可在 main.py 里复用 md_to_html() 生成 HTML 版（为 M4 HTML 邮件做准备）。
"""
from __future__ import annotations

import html as html_mod
import re
import sys
from pathlib import Path

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_URL_RE = re.compile(r"(https?://[^\s<>\"'）)\]】]+)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

_CSS = """
html { color-scheme: light; }
body { max-width: 880px; margin: 2em auto; padding: 0 1em;
       font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
       line-height: 1.7; color: #222; background: #ffffff; }
h1 { color: #0b3d91; border-bottom: 2px solid #0b3d91; padding-bottom: .3em; }
h2 { color: #0b3d91; margin-top: 2em; border-bottom: 1px solid #ccc; padding-bottom: .2em; }
h3 { margin-bottom: .2em; color: #333; }
h4 { margin-bottom: .4em; }
section, .art { margin: .9em 0; padding: 0 0 .1em 1em; border-left: 3px solid #e8e8e8; }
section:hover, .art:hover { border-left-color: #0b3d91; }
.item { margin: .2em 0; color: #444; font-size: .93em; }
mark { background: #fff3a3; color: #222; padding: 0 .15em; border-radius: 3px; }
blockquote { margin: .6em 0 .8em 0; padding: .4em .9em; background: #f5f8ff;
             border-left: 4px solid #0b3d91; border-radius: 0 6px 6px 0; color: #1a355e; }
a { color: #0b3d91; }
p { text-align: justify; }
"""


def _inline(text: str) -> str:
    """转义并把 Markdown 语法转为 HTML：**加粗** → <mark> 高亮，链接 → <a>。"""
    text = html_mod.escape(text)
    # ==高亮== → <mark> 黄色高亮（摘要重点）；**加粗** → <strong>（如「一句话」标签）
    text = re.sub(r"==(.+?)==", r"<mark>\1</mark>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = _MD_LINK_RE.sub(r'<a href="\2">\1</a>', text)
    return _URL_RE.sub(r'<a href="\1">\1</a>', text)


def md_to_html(md_text: str, base: int = 1) -> str:
    """把简报 md 转成 HTML 片段。

    base=1：单文件文档（# → h1, ## → h2, ### → h3）
    base=2：合并文档内的单份（# → h2, ## → h3, ### → h4）
    """
    out: list[str] = []
    in_para = False

    def close_para() -> None:
        nonlocal in_para
        if in_para:
            out.append("</p>")
            in_para = False

    for raw in md_text.splitlines():
        line = raw.rstrip()
        if not line:
            close_para()
            continue
        m = _HEADING_RE.match(line)
        if m:
            close_para()
            level = min(len(m.group(1)) + base - 1, 6)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
        elif line.startswith("> "):
            close_para()
            out.append(f"<blockquote>{_inline(line[2:])}</blockquote>")
        elif line.startswith("- "):
            close_para()
            out.append(f"<p class='item'>{_inline(line[2:])}</p>")
        else:
            if not in_para:
                out.append("<p>")
                in_para = True
            out.append(_inline(line))
    close_para()
    return "\n".join(out)


def _body_after_header(text: str) -> str:
    """去掉简报 md 的文档标题行与头部元数据（到第一个 --- 分隔线为止）。"""
    if "---" in text:
        return text.split("---", 1)[1]
    return text


def build_merged(files: list[Path]) -> str:
    """把所有 zh 简报合并成一个 HTML 文档（按文件名日期排序）。

    层级：h1 总标题 / h2 日期（文件名） / h3 订阅 / h4 文献。
    """
    date_re = re.compile(r"ads_(\d{8})_")
    sections: list[str] = []
    total = 0
    subs_seen: set[str] = set()
    for f in sorted(files, key=lambda p: p.name):
        m = date_re.search(f.name)
        date_tag = m.group(1) if m else f.stem
        pretty_date = f"{date_tag[:4]}-{date_tag[4:6]}-{date_tag[6:]}"
        body = _body_after_header(f.read_text(encoding="utf-8"))
        # 统计该份文献数（### 行数）与出现的订阅名
        n = len(re.findall(r"^### ", body, re.M))
        total += n
        subs_seen.update(
            re.findall(r"^## 📚 ([^·]+) ·", body, re.M)
        )
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


def collect_zh_files() -> list[Path]:
    """收集全部中文简报：历史回补目录 + 增量 zh 目录，按文件名去重（回补优先）。"""
    root = Path(__file__).resolve().parent.parent
    dirs = [
        root / "data" / "digests" / "backfill_old_mailbox" / "zh",
        root / "data" / "digests" / "zh",
    ]
    seen: dict[str, Path] = {}
    for d in dirs:
        for p in sorted(d.glob("*.zh.md")):
            seen.setdefault(p.name, p)   # 先到的（回补目录）优先
    return sorted(seen.values(), key=lambda p: p.name)


def _date_of(f: Path) -> str:
    m = re.search(r"ads_(\d{8})_", f.name)
    tag = m.group(1) if m else "????"
    return f"{tag[:4]}-{tag[4:6]}-{tag[6:]}"


def main() -> None:
    if len(sys.argv) > 1:
        src = Path(sys.argv[1])
        text = src.read_text(encoding="utf-8")
        out = src.with_suffix(".html")
        doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{html_mod.escape(src.stem)}</title><style>{_CSS}</style></head><body>
{md_to_html(text, base=1)}
<hr><p style="color:#888">由 mail-digest 生成</p></body></html>"""
        out.write_text(doc, encoding="utf-8")
        print(f"已生成: {out}")
        return

    files = collect_zh_files()
    if not files:
        sys.exit("未找到中文简报文件（data/digests/zh/ 或 backfill_old_mailbox/zh/）")
    out = Path(__file__).resolve().parent.parent / "data" / "digests" / "ADS文献简报-中文总览.html"
    out.write_text(build_merged(files), encoding="utf-8")
    total = len(re.findall(r"^### ", "".join(f.read_text(encoding="utf-8") for f in files), re.M))
    print(f"已生成合并 HTML（{len(files)} 份、{total} 条文献）: {out}")


if __name__ == "__main__":
    main()
