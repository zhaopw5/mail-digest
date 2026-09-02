"""文献简报生成（M1/M3 产出）。

简报按 myADS 订阅分组：一封推送邮件 = 多个订阅 section，
每个订阅下列出该订阅命中的文献。
- build_ads_digest()：英文版（数据完整版）
- build_ads_digest_zh()：中文版（LLM 翻译 + 一句话点评 + 分级，缺失时回退英文）
M4 阶段再接入邮件/飞书推送。
"""
from __future__ import annotations

from .ads import subscription_label
from .llm import GRADE_STARS
from .models import ADSArticle, Mail


def _pretty_pubdate(pubdate: str) -> str:
    """ADS pubdate 形如 YYYY-MM-00（只有年月）时去掉 -00 尾巴。"""
    return pubdate[:-3] if pubdate.endswith("-00") else pubdate


def build_ads_digest(mail: Mail, sections: list[tuple[str, list[ADSArticle]]]) -> str:
    """把一封 myADS 推送渲染成按订阅分组的 Markdown 简报。

    sections: [(订阅名, 该订阅命中的文献列表)]；订阅名可为空字符串（未分类）。
    """
    total = sum(len(arts) for _, arts in sections)
    date_str = mail.date.strftime("%Y-%m-%d %H:%M") if mail.date else "(未知)"
    title_date = mail.date.strftime("%Y-%m-%d") if mail.date else "未知日期"
    lines = [
        f"# ADS 文献简报 {title_date}",
        "",
        f"- 来源邮件：{mail.subject or '(无主题)'}",
        f"- 发件人：{mail.from_ or '(未知)'}",
        f"- 邮件日期：{date_str}",
        f"- 订阅命中：{len([s for s in sections if s[1]])} 个（共 {total} 条文献）",
        "",
        "---",
        "",
    ]
    for name, articles in sections:
        if not articles:
            if name:
                lines.append(f"## 📚 {name} · {subscription_label(name)}")
                lines.append("")
                lines.append("（本日无命中）")
                lines.append("")
            continue
        label = subscription_label(name)
        if name:
            head = f"## 📚 {name} · {label}（{len(articles)} 条）"
        else:
            head = f"## 📚 未分类文献（{len(articles)} 条）"
        lines.append(head)
        lines.append("")
        for i, art in enumerate(articles, 1):
            lines.append(f"### {i}. {art.title or art.bibcode}")
            if art.authors:
                shown = ", ".join(art.authors[:8])
                if len(art.authors) > 8:
                    shown += " …"
                lines.append(f"- 作者：{shown}")
            if art.pubdate:
                pretty = _pretty_pubdate(art.pubdate)
                if pretty:
                    lines.append(f"- 发表：{pretty}")
            if art.citation_count is not None:
                lines.append(f"- 引用数：{art.citation_count}")
            if art.doi:
                lines.append(f"- DOI：{art.doi}")
            lines.append(f"- 链接：https://ui.adsabs.harvard.edu/abs/{art.bibcode}/abstract")
            if art.error:
                lines.append(f"- ⚠️ 摘要获取失败：{art.error}")
            elif art.abstract:
                lines.append("")
                lines.append(art.abstract.strip())
            lines.append("")
    return "\n".join(lines)


def build_ads_digest_zh(mail: Mail, sections: list[tuple[str, list[ADSArticle]]],
                        zh_map: dict[str, dict]) -> str:
    """中文版简报：标题/点评/摘要用 LLM 结果，缺失的条目回退英文。

    zh_map: {bibcode: {zh_title, zh_abstract, note, grade}}（grade 可为空串）。
    """
    total = sum(len(arts) for _, arts in sections)
    date_str = mail.date.strftime("%Y-%m-%d %H:%M") if mail.date else "(未知)"
    title_date = mail.date.strftime("%Y-%m-%d") if mail.date else "未知日期"
    lines = [
        f"# ADS 文献简报（中文版）{title_date}",
        "",
        f"- 来源邮件：{mail.subject or '(无主题)'}",
        f"- 发件人：{mail.from_ or '(未知)'}",
        f"- 邮件日期：{date_str}",
        f"- 订阅命中：{len([s for s in sections if s[1]])} 个（共 {total} 条文献）",
        "",
        "---",
        "",
    ]
    for name, articles in sections:
        if not articles:
            if name:
                lines.append(f"## 📚 {name} · {subscription_label(name)}")
                lines.append("")
                lines.append("（本日无命中）")
                lines.append("")
            continue
        label = subscription_label(name)
        head = f"## 📚 {name} · {label}（{len(articles)} 条）" if name else "## 📚 未分类文献（…）"
        lines.append(head)
        lines.append("")
        for i, art in enumerate(articles, 1):
            zh = zh_map.get(art.bibcode, {})
            title = zh.get("zh_title") or art.title or art.bibcode
            lines.append(f"### {i}. {title}")
            note = (zh.get("note") or "").strip()
            grade = zh.get("grade") or ""
            if note:
                stars = GRADE_STARS.get(grade, "")
                lines.append(f"> 💡 **一句话** {stars}：{note}")
            elif zh:
                lines.append("> 💡 **一句话**：LLM 未生成点评")
            if art.authors:
                shown = ", ".join(art.authors[:8])
                if len(art.authors) > 8:
                    shown += " …"
                lines.append(f"- 作者：{shown}")
            if art.pubdate:
                pretty = _pretty_pubdate(art.pubdate)
                if pretty:
                    lines.append(f"- 发表：{pretty}")
            if art.citation_count is not None:
                lines.append(f"- 引用数：{art.citation_count}")
            if art.doi:
                lines.append(f"- DOI：{art.doi}")
            lines.append(f"- 链接：https://ui.adsabs.harvard.edu/abs/{art.bibcode}/abstract")
            abstract = zh.get("zh_abstract") or art.abstract
            if art.error:
                lines.append(f"- ⚠️ 摘要获取失败：{art.error}")
            elif abstract:
                lines.append("")
                lines.append(abstract.strip())
            lines.append("")
    return "\n".join(lines)
