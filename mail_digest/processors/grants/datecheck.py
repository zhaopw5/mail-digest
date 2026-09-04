"""日期规则提取与交叉校验（防提示词注入 / 防幻觉的第二道防线）。

LLM 提取的 deadline_date 不可全信：附件/正文可能包含注入指令试图篡改它。
这里用正则独立扫描同一份文本中的候选日期，与 LLM 结果交叉检查，
不一致时在清单中给出警告并要求按「原文证据」人工核对。
"""
from __future__ import annotations

import re
from datetime import datetime

# 完整日期模式（优先）
_PATTERNS = [
    re.compile(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?"),
    re.compile(r"((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})"),
    re.compile(r"((?:19|20)\d{2})/(\d{1,2})/(\d{1,2})"),
]
# 无年份的「M月D日」，按邮件年份补齐（跨年场景可能不准，仅作候选参考）
_NO_YEAR = re.compile(r"(?<![\d年])(\d{1,2})月(\d{1,2})日")


def rule_dates(text: str, ref_year: int) -> list[dict]:
    """规则独立提取文本中的候选日期，返回 [{'iso', 'quote'}]（按出现顺序去重）。"""
    found: list[dict] = []
    seen: set[str] = set()

    def add(iso: str, pos: int, end: int) -> None:
        if iso in seen:
            return
        try:
            datetime.strptime(iso, "%Y-%m-%d")
        except ValueError:
            return
        seen.add(iso)
        s = max(0, pos - 12)
        quote = text[s:end + 12].replace("\n", " ").strip()
        found.append({"iso": iso, "quote": quote})

    for pat in _PATTERNS:
        for m in pat.finditer(text):
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            add(f"{y:04d}-{mo:02d}-{d:02d}", m.start(), m.end())
    for m in _NO_YEAR.finditer(text):
        mo, d = int(m.group(1)), int(m.group(2))
        add(f"{ref_year:04d}-{mo:02d}-{d:02d}", m.start(), m.end())
    return found


def validate_deadline_iso(iso: str, ref_year: int) -> tuple[bool, str]:
    """deadline_date 格式与合理范围校验。返回 (是否合法, 错误说明)。"""
    if not iso:
        return False, "模型未给出日期"
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso)
    if not m:
        return False, f"日期格式非法: {iso!r}"
    y, mo, d = map(int, m.groups())
    try:
        datetime(y, mo, d)
    except ValueError:
        return False, f"日期不存在: {iso}"
    if not (ref_year - 2 <= y <= ref_year + 3):
        return False, f"年份超出合理范围: {iso}（邮件年份 {ref_year}）"
    return True, ""


def cross_check(deadline_iso: str, rule: list[dict], ref_year: int) -> str:
    """LLM 日期 vs 规则日期。一致返回空串；否则返回可展示的警告文本。"""
    ok, err = validate_deadline_iso(deadline_iso, ref_year)
    if not ok:
        return f"⚠️ 截止日期校验失败：{err}"
    rule_isos = {r["iso"] for r in rule}
    if not rule_isos:
        return "⚠️ 文本中规则未能独立提取到任何完整日期，截止日期请对照证据人工核对"
    if deadline_iso not in rule_isos:
        others = "、".join(sorted(rule_isos)[:8])
        return (f"⚠️ 模型截止 {deadline_iso} 与规则独立提取的日期（{others}）"
                f"不一致——请对照下方原文证据核对（谨防文档注入篡改）")
    # 命中但文本里还有多个「截止/受理/申报」语境候选 → 保守提示人工确认
    ctx = [r for r in rule
           if any(w in r["quote"] for w in ("截止", "受理", "申报", "提交"))]
    ctx_isos = sorted({r["iso"] for r in ctx})
    if len(ctx_isos) > 1:
        return (f"⚠️ 文本存在多个截止相关日期候选（{'、'.join(ctx_isos[:8])}），"
                f"模型采用 {deadline_iso}——请对照原文证据人工确认")
    return ""
