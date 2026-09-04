"""ADS Agent 操作（本域 CLI 与 mail-digest 共用；不依赖 grants 模块）。"""
from __future__ import annotations


import argparse
import sys

from ...core.imap_client import load_mails_from_dir
from ...core.config import Config
from ...core.llm import LLMError, DeepSeekClient
from ...core.ops import (
    _load_json_obj, _load_processed, _save_json_obj, _save_processed,
    parse_date_arg as _parse_date_arg,
)
from ...core.research_profile import PROFILE_SUMMARY
from .api import ADSAPIError, ADSClient, fill_from_doc
from .delivery import push
from .overview import merge_markdown_files
from .models import ADSArticle
from .parser import extract_bibcodes, is_ads_email, parse_myads_sections
from .renderer import build_ads_digest, build_ads_digest_zh
from .summarizer import build_article_messages, parse_article_result

def cmd_ads_run(cfg: Config, args: argparse.Namespace) -> None:
    if not cfg.ads_enabled:
        print("⏸️  ADS Agent 已关闭（.env 中 ADS_ENABLED=false）。如需启用改为 true。")
        return
    offline = not cfg.ads_api_token
    if offline:
        print("⚠️  未配置 ADS_API_TOKEN，运行在离线模式：只提取 bibcode，不查摘要。")
        print("    在 https://ui.adsabs.harvard.edu/user/settings/token 免费申请后填入 .env 即可。")

    mails = load_mails_from_dir(cfg.eml_dir)
    ads_mails = [m for m in mails if is_ads_email(m)]
    print(f"扫描 {len(mails)} 封邮件，识别出 {len(ads_mails)} 封 ADS 推送")

    processed = _load_processed(cfg.processed_file)
    force = getattr(args, "force", False)
    todo = [m for m in ads_mails if force or m.uid not in processed]
    limit = getattr(args, "limit", None) or cfg.default_ads_limit
    if len(todo) > limit:
        print(f"⚠️  本次最多处理 {limit} 封（--limit 可调），其余下次再跑")
        todo = todo[:limit]
    if not todo:
        print("没有待处理的新 ADS 邮件（已全部处理过；用 --force 强制重跑）")
        return

    client = ADSClient(cfg.ads_api_token, cfg.ads_api_base,
                       cfg.ads_request_interval) if not offline else None
    llm_key = cfg.ads_llm_key()
    llm = (DeepSeekClient(llm_key, cfg.deepseek_model, cfg.deepseek_base_url,
                          cfg.llm_request_interval) if llm_key else None)
    if not llm:
        print("ℹ️  未配置 LLM key（ADS_LLM_API_KEY 或 DEEPSEEK_API_KEY），跳过中文翻译/点评")
    zh_cache = _load_json_obj(cfg.llm_cache_file)
    zh_cache_dirty = False
    newly_processed: set[int] = set()

    def _make_article(bc: str, cache: dict[str, ADSArticle]) -> ADSArticle:
        if bc in cache:
            return cache[bc]
        art = ADSArticle(bibcode=bc)
        if client:
            try:
                doc = client.fetch_bibcode(bc, list(cfg.ads_fields))
                if doc:
                    fill_from_doc(art, doc)
                else:
                    art.error = "ADS API 未找到该 bibcode"
            except ADSAPIError as exc:
                art.error = str(exc)
        else:
            art.error = "未配置 ADS_API_TOKEN（离线模式）"
        cache[bc] = art
        return art

    for m in todo:
        sections = parse_myads_sections(m.body_text)
        if not sections:
            bibs = extract_bibcodes(m)
            sections = [("", bibs)] if bibs else []
        total_bibs = sum(len(bibs) for _, bibs in sections)
        if total_bibs == 0:
            print(f"  ▶ [{m.uid}] 《{m.subject[:40]}》 → 未提取到文献，跳过")
            newly_processed.add(m.uid)
            continue
        active = [(n, b) for n, b in sections if b]
        print(f"  ▶ [{m.uid}] 《{m.subject[:40]}》 → {len(active)} 个订阅命中、共 {total_bibs} 条文献")
        cache: dict[str, ADSArticle] = {}
        grouped = [(name, [_make_article(bc, cache) for bc in bibs])
                   for name, bibs in sections if bibs]
        digest = build_ads_digest(m, grouped)
        date_tag = m.date.strftime("%Y%m%d") if m.date else "nodate"
        out = cfg.digest_dir / f"ads_{date_tag}_{m.uid:06d}.md"
        cfg.digest_dir.mkdir(parents=True, exist_ok=True)
        out.write_text(digest, encoding="utf-8")
        newly_processed.add(m.uid)
        print(f"     📄 简报已生成：{out}")

        if llm:
            zh_map: dict[str, dict] = {}
            for _name, arts in grouped:
                for art in arts:
                    if art.error or not art.title:
                        continue
                    bc = art.bibcode
                    hit = zh_cache.get(bc)
                    if hit:
                        zh_map[bc] = hit
                        continue
                    try:
                        msgs = build_article_messages(PROFILE_SUMMARY, art)
                        raw = llm.complete_json(msgs)
                        res = parse_article_result(raw, bc)
                    except LLMError as exc:
                        print(f"      ⚠️ LLM 失败 [{bc}]: {exc}")
                        continue
                    zh_cache[bc] = res
                    zh_map[bc] = res
                    zh_cache_dirty = True
            if zh_map:
                zh_doc = build_ads_digest_zh(m, grouped, zh_map)
                zout = cfg.zh_digest_dir / f"ads_{date_tag}_{m.uid:06d}.zh.md"
                cfg.zh_digest_dir.mkdir(parents=True, exist_ok=True)
                zout.write_text(zh_doc, encoding="utf-8")
                print(f"     📄 中文简报已生成：{zout}")

    if zh_cache_dirty:
        _save_json_obj(cfg.llm_cache_file, zh_cache)
    _save_processed(cfg.processed_file, processed | newly_processed)

def cmd_ads_push(cfg: Config, args: argparse.Namespace) -> None:
    when = _parse_date_arg(getattr(args, "date", None))
    if getattr(args, "dry_run", False):
        label = (when or __import__("datetime").date.today()).strftime("%Y-%m-%d")
        files = sorted(cfg.zh_digest_dir.glob(f"ads_{label.replace('-','')}_*.zh.md"))
        print(f"（dry-run）将发送 {label} 的 ADS 简报（{len(files)} 份）→ {cfg.imap_user}，不连接 SMTP")
        return
    if not cfg.smtp_host:
        sys.exit("未配置 SMTP_HOST（.env），无法推送")
    ok = push(cfg, when)
    label = when.strftime("%Y-%m-%d") if when else "今天"
    if ok:
        print(f"✅ 已将 {label} 的 ADS 中文简报发送到 {cfg.imap_user}")
    else:
        print(f"ℹ️  {label} 没有 ADS 推送邮件，未发送")

def cmd_html(cfg: Config, args: argparse.Namespace) -> None:
    files = sorted(cfg.zh_digest_dir.glob("*.zh.md"))
    if not files:
        print("ℹ️  当前无 ADS 中文简报（等新推送即可），跳过总览生成")
        return
    out = cfg.digest_dir / "ADS文献简报-中文总览.html"
    out.write_text(merge_markdown_files(files), encoding="utf-8")
    print(f"已生成合并 HTML（{len(files)} 份）: {out}")
