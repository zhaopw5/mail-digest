#!/usr/bin/env python3
"""Mail Digest CLI：自动邮件整理底座 + 两个独立 Agent（处理器）。

结构（core + processors）：
  core        IMAP 拉信 / 配置 / 状态 / SMTP 推送 / LLM 客户端（公共底座）
  ads         ADS 文献 Agent：推送识别 → bibcode → ADS API → 中文翻译点评分级 → 简报
  grants      项目申报 Agent：申报通知 → 附件安全解压 → 文档解析 → LLM 提取+证据校验 → 清单

用法：
  公共：python main.py fetch | html
  ADS  Agent：python main.py ads run [--force] [--limit N]
              python main.py ads push [--date YYYY-MM-DD]
  Grant Agent：python main.py grants run [--force] [--limit N]
              python main.py grants push [--date YYYY-MM-DD]
  一键：python main.py all [--recent N]

域开关：.env 中 ADS_ENABLED / GRANTS_ENABLED（默认 true）可单独关闭；
LLM key 用 ADS_LLM_API_KEY / GRANTS_LLM_API_KEY 独立配置，缺省回退 DEEPSEEK_API_KEY
（只想用 ADS、不想把基金附件发云端时，只填 ADS_LLM_API_KEY 即可）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

from mail_digest.core.config import Config
from mail_digest.core.imap_client import fetch_recent, load_mails_from_dir
from mail_digest.core.llm import LLMError, DeepSeekClient
from mail_digest.core.models import Mail
from mail_digest.core.push import send_markdown
from mail_digest.processors.ads.delivery import push
from mail_digest.core.research_profile import PROFILE_SUMMARY

from mail_digest.processors.ads.api import ADSAPIError, ADSClient, fill_from_doc
from mail_digest.processors.ads.models import ADSArticle
from mail_digest.processors.ads.parser import (
    extract_bibcodes,
    is_ads_email,
    parse_myads_sections,
)
from mail_digest.processors.ads.renderer import build_ads_digest, build_ads_digest_zh
from mail_digest.processors.ads.summarizer import build_article_messages, parse_article_result
from mail_digest.processors.grants.classifier import is_grant_email
from mail_digest.processors.grants.processor import run_fund


# ---------------- 幂等 / 缓存 ----------------

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
    path.write_text(json.dumps(sorted(processed), ensure_ascii=False, indent=2),
                    encoding="utf-8")


# ---------------- 公共：fetch ----------------

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


# ---------------- ADS Agent ----------------

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
    when = _parse_date_arg(args)
    if not cfg.smtp_host:
        sys.exit("未配置 SMTP_HOST（.env），无法推送")
    ok = push(cfg, when)
    label = when.strftime("%Y-%m-%d") if when else "今天"
    if ok:
        print(f"✅ 已将 {label} 的 ADS 中文简报发送到 {cfg.imap_user}")
    else:
        print(f"ℹ️  {label} 没有 ADS 推送邮件，未发送")


# ---------------- Grant Agent ----------------

def cmd_grants_run(cfg: Config, args: argparse.Namespace) -> None:
    if not cfg.grants_enabled:
        print("⏸️  Grant Agent 已关闭（.env 中 GRANTS_ENABLED=false）。如需启用改为 true。")
        return
    try:
        import pypdf  # noqa: F401
    except ImportError:
        print("ℹ️  未安装文档解析依赖（pypdf/python-docx/openpyxl/py7zr），附件只能解析 txt/csv；")
        print("    可执行  pip install -e \".[grants]\"  以完整解析 docx/pdf/xlsx/压缩包。")
    if not cfg.grant_allowed_senders.strip():
        print("⚠️  未配置 GRANT_ALLOWED_SENDERS（可信发件人白名单）——为防恶意附件，跳过全部基金邮件附件处理。")
        print("    请在 .env 配置，如：GRANT_ALLOWED_SENDERS=*@mail.sysu.edu.cn")
        return
    mails = load_mails_from_dir(cfg.eml_dir)
    grant_mails = [m for m in mails if is_grant_email(m)]
    print(f"扫描 {len(mails)} 封邮件，识别出 {len(grant_mails)} 封基金/项目申报通知")
    force = getattr(args, "force", False)
    limit = getattr(args, "limit", None)
    n, digest_text = run_fund(cfg, grant_mails, force=force, limit=limit)
    if n == 0:
        print("没有待处理的新申报通知（已全部处理过；用 --force 强制重跑）")
        return
    print(f"共处理 {n} 封")
    if digest_text:
        out = cfg.digest_dir / f"fund_{date.today():%Y%m%d}.md"
        out.write_text(digest_text, encoding="utf-8")
        print(f"📄 今日申报清单已生成：{out}")
    else:
        print("（今天没有当天收到的通知，清单未生成；结果已缓存）")


def cmd_grants_push(cfg: Config, args: argparse.Namespace) -> None:
    when = _parse_date_arg(args) or date.today()
    f = cfg.digest_dir / f"fund_{when:%Y%m%d}.md"
    if not f.exists():
        print(f"ℹ️  {when:%Y-%m-%d} 无申报清单文件（当日无通知或未先运行 grants run）")
        return
    send_markdown(cfg, f"项目申报机会清单 {when:%Y-%m-%d}",
                  f.read_text(encoding="utf-8"))
    print(f"✅ 已发送申报清单到 {cfg.imap_user}")


# ---------------- 公共：html / all ----------------

def cmd_html(cfg: Config, args: argparse.Namespace) -> None:
    from mail_digest.processors.ads.overview import merge_markdown_files
    files = sorted(cfg.zh_digest_dir.glob("*.zh.md"))
    if not files:
        sys.exit("未找到中文简报文件（data/digests/zh/）")
    out = cfg.digest_dir / "ADS文献简报-中文总览.html"
    out.write_text(merge_markdown_files(files), encoding="utf-8")
    print(f"已生成合并 HTML（{len(files)} 份）: {out}")


def cmd_all(cfg: Config, args: argparse.Namespace) -> None:
    cmd_fetch(cfg, args)
    print()
    if cfg.ads_enabled:
        cmd_ads_run(cfg, args)
    if cfg.grants_enabled:
        print()
        cmd_grants_run(cfg, args)


def _parse_date_arg(args: argparse.Namespace) -> date | None:
    d = getattr(args, "date", None)
    if not d:
        return None
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"日期格式错误：{d}（应为 YYYY-MM-DD）")


# ---------------- CLI ----------------

def main() -> None:
    import os as _os
    if hasattr(_os, "umask"):          # Unix：收紧默认权限，防同机其他用户读邮件数据
        _os.umask(0o077)
    parser = argparse.ArgumentParser(
        prog="mail-digest",
        description="自动邮件整理底座（core）+ ADS 文献 / 项目申报 两个独立 Agent",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="公共：IMAP 拉最近 N 封邮件到 data/emails")
    p_fetch.add_argument("--recent", type=int, default=None)
    p_fetch.add_argument("--folder", default=None)

    p_ads = sub.add_parser("ads", help="ADS 文献 Agent")
    ads_sub = p_ads.add_subparsers(dest="ads_cmd", required=True)
    a_run = ads_sub.add_parser("run", help="识别 ADS 推送 → ADS API → 中文简报")
    a_run.add_argument("--force", action="store_true")
    a_run.add_argument("--limit", type=int, default=None)
    a_push = ads_sub.add_parser("push", help="把某天 ADS 中文简报邮件发给自己")
    a_push.add_argument("--date", default=None)

    p_grants = sub.add_parser("grants", help="项目申报 Agent")
    g_sub = p_grants.add_subparsers(dest="grants_cmd", required=True)
    g_run = g_sub.add_parser("run", help="申报通知 → 附件解析 → 今日申报机会清单")
    g_run.add_argument("--force", action="store_true")
    g_run.add_argument("--limit", type=int, default=None)
    g_push = g_sub.add_parser("push", help="把某天申报清单邮件发给自己")
    g_push.add_argument("--date", default=None)

    p_all = sub.add_parser("all", help="一键：fetch + 各已启用 Agent 的 run")
    p_all.add_argument("--recent", type=int, default=None)
    p_all.add_argument("--limit", type=int, default=None)

    sub.add_parser("html", help="公共：重新生成中文合并 HTML 总览")

    args = parser.parse_args()
    cfg = Config.load()
    if args.cmd == "fetch":
        cmd_fetch(cfg, args)
    elif args.cmd == "ads":
        if args.ads_cmd == "run":
            cmd_ads_run(cfg, args)
        else:
            cmd_ads_push(cfg, args)
    elif args.cmd == "grants":
        if args.grants_cmd == "run":
            cmd_grants_run(cfg, args)
        else:
            cmd_grants_push(cfg, args)
    elif args.cmd == "html":
        cmd_html(cfg, args)
    else:
        cmd_all(cfg, args)


if __name__ == "__main__":
    main()
