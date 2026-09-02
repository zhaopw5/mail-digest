#!/usr/bin/env python3
"""邮件智能摘要 Agent —— 项目一：NASA ADS 文献推送

用法：
  python main.py fetch [--recent N] [--folder X]
       M0：IMAP 拉最近 N 封邮件到 data/emails（只读，不改已读状态）
  python main.py ads [--force] [--limit N]
       M1：识别 ADS 推送邮件 → 提取 bibcode → 查 ADS API → 生成简报
       幂等：已处理的邮件自动跳过；--force 强制重跑
       M3：若 .env 配了 DEEPSEEK_API_KEY，同时生成中文简报
           （中文标题/摘要 + 一句话点评 + 相关性分级，按篇缓存）
  python main.py html
       重新生成中文合并 HTML 总览（data/digests/ADS文献简报-中文总览.html）
  python main.py all [--recent N] [--limit N]
       一键：fetch + ads

首次使用：cp .env.example .env，填写 IMAP_USER / IMAP_AUTH_CODE / ADS_API_TOKEN
（可选 DEEPSEEK_API_KEY 启用中文翻译与点评）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from mail_digest.ads import extract_bibcodes, parse_myads_sections
from mail_digest.ads_api import ADSAPIError, ADSClient, fill_from_doc
from mail_digest.classify import classify
from mail_digest.config import Config
from mail_digest.digest import build_ads_digest, build_ads_digest_zh
from mail_digest.imap_client import fetch_recent, load_mails_from_dir
from mail_digest.llm import (
    LLMError,
    DeepSeekClient,
    build_article_messages,
    parse_article_result,
)
from mail_digest.models import ADSArticle, Mail
from mail_digest.push import push
from mail_digest.research_profile import PROFILE_SUMMARY

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"

# ---------------- 幂等 / 缓存记录 ----------------

def _load_json_obj(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json_obj(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def _load_processed(path: Path) -> set[int]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_processed(path: Path, processed: set[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sorted(processed), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------- 子命令 ----------------

def cmd_fetch(cfg: Config, args: argparse.Namespace) -> None:
    if not cfg.imap_user or not cfg.imap_auth_code:
        sys.exit("未配置邮箱：复制 .env.example 为 .env，填写 IMAP_USER 和 IMAP_AUTH_CODE（16 位授权码）")
    recent = args.recent or cfg.default_recent
    folder = getattr(args, "folder", None) or cfg.default_folder
    mails = fetch_recent(cfg, recent=recent, folder=folder)
    print(f"✅ 拉取 {len(mails)} 封邮件 → {cfg.eml_dir}")
    for m in mails:
        when = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "(无日期)"
        print(f"   {when}  [{m.uid:>6}]  {m.from_[:32]:<32} | {m.subject[:60]}")


def cmd_ads(cfg: Config, args: argparse.Namespace) -> None:
    offline = not cfg.ads_api_token
    if offline:
        print("⚠️  未配置 ADS_API_TOKEN，运行在离线模式：只提取 bibcode，不查摘要。")
        print("    在 https://ui.adsabs.harvard.edu/user/settings/token 免费申请后填入 .env 即可。")

    mails = load_mails_from_dir(cfg.eml_dir)
    ads_mails = []
    for m in mails:
        classify(m)
        if m.is_ads:
            ads_mails.append(m)
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

    client = (
        ADSClient(cfg.ads_api_token, cfg.ads_api_base, cfg.ads_request_interval)
        if not offline else None
    )
    llm = None
    if cfg.deepseek_api_key:
        llm = DeepSeekClient(
            cfg.deepseek_api_key, cfg.deepseek_model,
            cfg.deepseek_base_url, cfg.llm_request_interval,
        )
    else:
        print("ℹ️  未配置 DEEPSEEK_API_KEY，跳过中文翻译/点评（只输出英文简报）")
    zh_cache = _load_json_obj(cfg.llm_cache_file)   # bibcode → {zh_title, zh_abstract, note, grade}
    zh_cache_dirty = False
    newly_processed: set[int] = set()

    def _make_article(bc: str, cache: dict[str, ADSArticle]) -> ADSArticle:
        """查询单个 bibcode 并缓存（同一 bibcode 跨订阅只查一次 API）。"""
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
        # 按 myADS 订阅分组解析；非 myADS 格式则整封归入「未分类」
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

        # ---- M3：LLM 中文翻译 + 一句话点评 + 分级（按篇缓存，避免重复烧 token）----
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

    # 无论是否 --force，都把本次处理的邮件记为已处理，避免下次重复烧 API
    _save_processed(cfg.processed_file, processed | newly_processed)


def cmd_all(cfg: Config, args: argparse.Namespace) -> None:
    cmd_fetch(cfg, args)
    print()
    cmd_ads(cfg, args)


def cmd_html(cfg: Config, args: argparse.Namespace) -> None:
    """重新生成中文合并 HTML 总览（历史回补 + 增量目录）。"""
    sys.path.insert(0, str(SCRIPTS_DIR))
    import build_html_digest as bhd

    files = bhd.collect_zh_files()
    if not files:
        sys.exit("未找到中文简报文件（data/digests/zh/ 或 backfill_old_mailbox/zh/）")
    out = cfg.digest_dir / "ADS文献简报-中文总览.html"
    out.write_text(bhd.build_merged(files), encoding="utf-8")
    total = len(re.findall(
        r"^### ", "".join(f.read_text(encoding="utf-8") for f in files), re.M))
    print(f"已生成合并 HTML（{len(files)} 份、{total} 条文献）: {out}")


def cmd_push(cfg: Config, args: argparse.Namespace) -> None:
    """把指定日期（默认今天）的中文简报以 HTML 邮件发给自己。"""
    when = None
    if getattr(args, "date", None):
        try:
            when = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            sys.exit(f"日期格式错误：{args.date}（应为 YYYY-MM-DD）")
    if not cfg.smtp_host:
        sys.exit("未配置 SMTP_HOST（.env），无法推送")
    ok = push(cfg, when)
    label = when.strftime("%Y-%m-%d") if when else "今天"
    if ok:
        print(f"✅ 已将 {label} 的中文简报发送到 {cfg.imap_user}")
    else:
        print(f"ℹ️  {label} 没有 ADS 推送邮件，未发送")


# ---------------- CLI ----------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mail-digest",
        description="邮件智能摘要 Agent — 项目一：NASA ADS 文献推送",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="M0：IMAP 拉最近 N 封邮件到 data/emails")
    p_fetch.add_argument("--recent", type=int, default=None, help="拉最近多少封（默认 50）")
    p_fetch.add_argument("--folder", default=None, help="IMAP 文件夹（默认 INBOX）")

    p_ads = sub.add_parser("ads", help="M1：识别 ADS 邮件 → 提取 bibcode → 查 API → 生成简报")
    p_ads.add_argument("--force", action="store_true", help="忽略幂等记录，重新处理")
    p_ads.add_argument("--limit", type=int, default=None, help="本次最多处理 N 封")

    p_all = sub.add_parser("all", help="一键：fetch + ads")
    p_all.add_argument("--recent", type=int, default=None)
    p_all.add_argument("--limit", type=int, default=None)

    sub.add_parser("html", help="重新生成中文合并 HTML 总览")

    p_push = sub.add_parser("push", help="把某天中文简报以 HTML 邮件发给自己（默认今天）")
    p_push.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD（用于测试/补发）")

    args = parser.parse_args()
    cfg = Config.load()
    if args.cmd == "fetch":
        cmd_fetch(cfg, args)
    elif args.cmd == "ads":
        cmd_ads(cfg, args)
    elif args.cmd == "html":
        cmd_html(cfg, args)
    elif args.cmd == "push":
        cmd_push(cfg, args)
    else:
        cmd_all(cfg, args)


if __name__ == "__main__":
    main()
