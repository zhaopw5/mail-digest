"""bibcode 提取与校验（场景一核心）。

ADS bibcode 为 19 位字符串：YYYYJJJJJVVVVMPPPPA，如 `2024ApJ...963..100A`。
"""
from __future__ import annotations

import re

from ...core.models import Mail

_BIBCODE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789&.+"
)
# 主路径：从 ADS 链接 URL 中提取（如 https://ui.adsabs.harvard.edu/abs/<bibcode>/abstract）
_URL_RE = re.compile(r"https?://ui\.adsabs\.harvard\.edu/abs/([^/\s?#<>\"']+)")
# 兜底：正文中直接出现的裸 19 位 bibcode（带边界，防从长串中误切）
_BARE_RE = re.compile(
    r"(?<![A-Za-z0-9&.+])[0-9]{4}[A-Za-z0-9&.+]{14}[A-Za-z](?![A-Za-z0-9&.+])"
)


def is_valid_bibcode(bibcode: str) -> bool:
    """ADS bibcode 校验：19 位，前 4 位数字，末位字母，其余在合法字符集内。"""
    if len(bibcode) != 19:
        return False
    if not bibcode[:4].isdigit():
        return False
    if not bibcode[-1].isalpha():
        return False
    return all(c in _BIBCODE_CHARS for c in bibcode)


def extract_bibcodes(mail: Mail) -> list[str]:
    """从邮件正文（文本 + HTML）提取全部 bibcode，去重保序。

    先扫链接 URL（最可靠），再对纯文本兜底扫裸 bibcode。
    """
    found: list[str] = []
    for blob in (mail.body_text, mail.body_html):
        for m in _URL_RE.finditer(blob):
            cand = m.group(1).rstrip("/")
            if is_valid_bibcode(cand) and cand not in found:
                found.append(cand)
    for m in _BARE_RE.finditer(mail.body_text):
        cand = m.group(0)
        if is_valid_bibcode(cand) and cand not in found:
            found.append(cand)
    return found


# ---------------- myADS 订阅分组 ----------------

# myADS 邮件按订阅分 section：一行是「订阅名 (查询URL)」，其后是该订阅命中的文献
_MYADS_SECTION_RE = re.compile(r"^([A-Za-z0-9_\-]+) \(https?://", re.M)

# 订阅名 → 中文含义（只作展示用，不影响解析）
SUBSCRIPTION_LABELS: dict[str, str] = {
    "grb_cosmicray": "伽马射线暴与宇宙线",
    "solaractivity_cosmicray": "太阳活动与宇宙线",
}


def subscription_label(name: str) -> str:
    """返回订阅名的中文含义；未登记的订阅原样返回。"""
    return SUBSCRIPTION_LABELS.get(name, name)


def parse_myads_sections(body_text: str) -> list[tuple[str, list[str]]]:
    """解析 myADS 推送正文 → [(订阅名, [bibcode, ...])]，保序、组内去重。

    结构：`订阅名 (https://...search?...)` 行开始一个 section，
    到下一个订阅行前的内容都归属当前订阅，从中提取 19 位 bibcode。
    若正文不含订阅行（非 myADS 格式），返回空列表。
    """
    matches = list(_MYADS_SECTION_RE.finditer(body_text))
    if not matches:
        return []
    sections: list[tuple[str, list[str]]] = []
    for i, m in enumerate(matches):
        name = m.group(1)
        seg_start = m.end()
        nl = body_text.find("\n", seg_start)      # 跳过订阅行的整行 URL
        if nl != -1:
            seg_start = nl + 1
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(body_text)
        seg = body_text[seg_start:seg_end]
        bibs: list[str] = []
        for b in _BARE_RE.finditer(seg):
            cand = b.group(0)
            if is_valid_bibcode(cand) and cand not in bibs:
                bibs.append(cand)
        sections.append((name, bibs))
    return sections


# ---------------- ADS 邮件识别（processor.matches）----------------
_ADS_LINK_RE = re.compile(r"ui\.adsabs\.harvard\.edu/abs/")
_ADS_DOMAIN_HINTS = ("adsabs.harvard.edu", "cfa.harvard.edu")
_ADS_EXCLUDE_SUBJECT = ("verify", "welcome", "confirm your email")
# 我们自己推送/测试的邮件（主题特征），正文虽含 ADS 链接但不能当推送再处理（防循环）
_ADS_SELF_PUSH_HINTS = ("ads 文献简报", "[mail-digest]")
_ADS_SUBJECT_HINTS = (
    "astrophysics data system", "new article", "toc alert",
    "table of contents", "citation alert", "notification criteria",
    "new notifications",
)


def is_ads_email(mail: Mail) -> bool:
    subject_lower = mail.subject.lower()
    # 自推送邮件排除（防循环：正文含 ADS 链接的邮件可能是我们发出的简报）
    if any(w in subject_lower for w in _ADS_SELF_PUSH_HINTS):
        return False
    blob = f"{mail.body_html}\n{mail.body_text}"
    if _ADS_LINK_RE.search(blob):
        return True
    from_lower = mail.from_.lower()
    if any(d in from_lower for d in _ADS_DOMAIN_HINTS):
        if any(w in subject_lower for w in _ADS_EXCLUDE_SUBJECT):
            return False
        return True
    return any(h in subject_lower for h in _ADS_SUBJECT_HINTS)


