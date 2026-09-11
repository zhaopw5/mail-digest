"""ADS Agent 操作（本域 CLI 与 mail-digest 共用；不依赖 grants 模块）。"""
from __future__ import annotations


import argparse
import sys

from ...core.imap_client import load_mails_from_dir
from ...core.config import Config
from ...core.llm import LLMError, DeepSeekClient
from ...core.ops import (
    _load_json_obj, _save_json_obj,
    parse_date_arg as _parse_date_arg,
)
from ...core.research_profile import PROFILE_SUMMARY
from ...core.state import StateCorruptError
from .api import ADSAPIError, ADSClient, fill_from_doc
from .delivery import preview, push_official, push_test, state_init
from .manifest import done_source_ids, load_manifest, save_manifest, upsert, write_processed_compat
from .overview import merge_markdown_files
from .models import ADSArticle
from .parser import extract_bibcodes, is_ads_email, parse_myads_sections
from .renderer import build_ads_digest, build_ads_digest_zh
from .summarizer import build_article_messages, parse_article_result


def cmd_ads_run(cfg: Config, args: argparse.Namespace) -> None:
    """处理待办 ADS 邮件；每封独立判定 ready / empty / retryable_error。

    只有真正成功的邮件才会被记入「已处理」——失败（ADS API 或 LLM）保持
    retryable_error，下次运行自动重试，不会被静默跳过。
    """
    if not cfg.ads_enabled:
        print("⏸️  ADS Agent 已关闭（.env 中 ADS_ENABLED=false）。如需启用改为 true。")
        return
    offline = not cfg.ads_api_token
    if offline:
        print("⚠️  未配置 ADS_API_TOKEN，运行在离线模式：只提取 bibcode，不查摘要。")

    mails = load_mails_from_dir(cfg.eml_dir, cfg.default_folder, tz=cfg.tz())
    ads_mails = [m for m in mails if is_ads_email(m)]
    print(f"扫描缓存 {len(mails)} 封邮件，识别出 {len(ads_mails)} 封 ADS 推送")

    try:
        mf = load_manifest(cfg)
    except StateCorruptError as exc:
        sys.exit(f"❌ {exc}")

    force = getattr(args, "force", False)
    done = done_source_ids(mf)
    todo = [m for m in ads_mails if force or m.source_id not in done]
    todo.sort(key=lambda m: (m.received_at or m.date or m.source_id))
    limit = getattr(args, "limit", None) or cfg.default_ads_limit
    if len(todo) > limit:
        print(f"⚠️  本次最多处理 {limit} 封（--limit 可调），其余下次再跑")
        todo = todo[:limit]
    retry_pending = len([m for m in ads_mails if m.source_id not in done]) - len(todo)
    if not todo:
        print("没有待处理的新 ADS 邮件（全部已处理成功；用 --force 强制重跑）")
        write_processed_compat(cfg, mf)
        return
    if retry_pending > 0:
        print(f"ℹ️  另有 {retry_pending} 封待处理，将在后续运行中继续")

    client = ADSClient(cfg.ads_api_token, cfg.ads_api_base,
                       cfg.ads_request_interval) if not offline else None
    llm_key = cfg.ads_llm_key()
    llm = (DeepSeekClient(llm_key, cfg.deepseek_model, cfg.deepseek_base_url,
                          cfg.llm_request_interval,
                          usage_log=str(cfg.llm_usage_log_file)) if llm_key else None)
    if not llm:
        print("ℹ️  未配置 LLM key（ADS_LLM_API_KEY 或 DEEPSEEK_API_KEY），跳过中文翻译/点评")
    zh_cache = _load_json_obj(cfg.llm_cache_file)
    zh_cache_dirty = False
    n_ready = n_failed = n_empty = 0

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
        sid = m.source_id
        sections = parse_myads_sections(m.body_text)
        if not sections:
            bibs = extract_bibcodes(m)
            sections = [("", bibs)] if bibs else []
        total_bibs = sum(len(bibs) for _, bibs in sections)
        if total_bibs == 0:
            print(f"  ▶ [{sid}] 《{m.subject[:40]}》 → 未提取到文献（非失败，已记录）")
            upsert(cfg, mf, sid, status="empty", received_at=_iso(m.received_at),
                   en_file=None, zh_file=None, errors=[])
            save_manifest(cfg, mf)
            n_empty += 1
            continue
        active = [(n, b) for n, b in sections if b]
        print(f"  ▶ [{sid}] 《{m.subject[:40]}》 → {len(active)} 个订阅命中、共 {total_bibs} 条文献")
        cache: dict[str, ADSArticle] = {}
        grouped = [(name, [_make_article(bc, cache) for bc in bibs])
                   for name, bibs in sections if bibs]
        errors: list[str] = [f"{art.bibcode}: {art.error}"
                             for _n, arts in grouped for art in arts if art.error]

        digest = build_ads_digest(m, grouped)
        cfg.digest_dir.mkdir(parents=True, exist_ok=True)
        out, zh_out = digest_paths(cfg, m)
        out.write_text(digest, encoding="utf-8")
        print(f"     📄 英文简报已生成：{out}")

        zh_file = None
        missing_zh: list[str] = []
        if llm:
            zh_map: dict[str, dict] = {}
            for _name, arts in grouped:
                for art in arts:
                    if art.error:
                        continue                     # 已在 errors 里按 API 失败记账
                    if not art.title:
                        missing_zh.append(f"{art.bibcode}: ADS 元数据缺少标题")
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
                        missing_zh.append(f"{bc}: {exc}")
                        print(f"      ⚠️ LLM 失败 [{bc}]：{exc}（下次运行会重试这一封）")
                        continue
                    if not _zh_result_usable(res):
                        # 不写缓存：坏结果不能被下次运行当成"已翻译"复用
                        missing_zh.append(f"{bc}: LLM 返回内容缺少标题或摘要")
                        print(f"      ⚠️ LLM 返回内容不完整 [{bc}]（下次运行会重试这一封）")
                        continue
                    zh_cache[bc] = res
                    zh_map[bc] = res
                    zh_cache_dirty = True
            if zh_map:
                zh_doc = build_ads_digest_zh(m, grouped, zh_map)
                cfg.zh_digest_dir.mkdir(parents=True, exist_ok=True)
                zout = zh_out
                zout.write_text(zh_doc, encoding="utf-8")
                zh_file = zout.name
                print(f"     📄 中文简报已生成：{zout}")

        # 完整性判定：元数据缺失或 LLM 缺失 → 不标记成功，留给下次重试
        errors.extend(missing_zh)
        status = "ready" if not errors else "retryable_error"
        upsert(cfg, mf, sid, status=status, received_at=_iso(m.received_at),
               en_file=out.name, zh_file=zh_file, errors=errors[:20])
        save_manifest(cfg, mf)                    # 逐封落盘：崩溃也不会误标
        if status == "ready":
            n_ready += 1
        else:
            n_failed += 1
            print(f"     ⚠️ 本封未完成（{len(errors)} 项失败）→ 标记为待重试，下次 ads run 自动补做")

    if zh_cache_dirty:
        _save_json_obj(cfg.llm_cache_file, zh_cache)
    write_processed_compat(cfg, mf)
    summary = f"✅ 处理完成：成功 {n_ready} 封"
    if n_empty:
        summary += f"，无文献 {n_empty} 封"
    if n_failed:
        summary += f"，失败待重试 {n_failed} 封（下次运行自动重试）"
    print(summary)


def digest_paths(cfg, mail) -> tuple:
    """简报产物路径（英文, 中文）。

    文件名带完整身份 ``_u<UIDVALIDITY>``：同一天、同一 UID、不同 UIDVALIDITY 的
    两封邮件若共用文件名，后处理的会直接覆盖前者的内容，正式邮件就会缺文献。
    """
    tag = f"{mail.date:%Y%m%d}" if mail.date else "nodate"
    ident = f"_u{mail.uidvalidity if mail.uidvalidity is not None else 0}"
    return (cfg.digest_dir / f"ads_{tag}_{mail.uid:06d}{ident}.md",
            cfg.zh_digest_dir / f"ads_{tag}_{mail.uid:06d}{ident}.zh.md")


def _zh_result_usable(res: dict) -> bool:
    """LLM 结果是否算完整：标题与至少一项正文内容都不能为空。

    只看"没有抛异常"是不够的——模型返回合法 JSON `{}` 时字段会被归一化成空串，
    若当成成功，正式推送里就会出现没有标题、没有摘要的空条目。
    """
    if not str(res.get("zh_title") or "").strip():
        return False
    return any(str(res.get(k) or "").strip()
               for k in ("zh_abstract", "note"))


def _iso(dt) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _parse_cutoff(arg, cfg):
    if not arg:
        return None
    from datetime import datetime
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(arg, fmt)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=cfg.tz())
    sys.exit(f"截止点格式错误：{arg}（示例：2026-09-11 09:00:00+08:00）")


def cmd_ads_push(cfg: Config, args: argparse.Namespace) -> None:
    """ADS 推送（三模式互斥必填）：--official / --test / --dry-run。"""
    if getattr(args, "official", False) and getattr(args, "date", None):
        sys.exit("--official 与 --date 不能同时使用：正式推送的边界由计划截止点"
                 "（MAIL_DIGEST_PUSH_TIME，默认 09:00）决定，不能按日期挑选内容。")
    when = _parse_date_arg(getattr(args, "date", None))
    if getattr(args, "dry_run", False):
        print(preview(cfg, when))
        return
    if getattr(args, "test", False):
        try:
            r = push_test(cfg, when)
        except RuntimeError as exc:
            sys.exit(f"❌ {exc}")
        print(f"✅ [TEST] 测试推送已发送（不改变正式状态）：{r['subject']}")
        return
    if getattr(args, "official", False):
        cutoff = _parse_cutoff(getattr(args, "cutoff", None), cfg)
        try:
            r = push_official(cfg, cutoff)
        except StateCorruptError as exc:
            sys.exit(f"❌ 状态文件异常，已中止本次正式推送（未发送任何邮件）：\n{exc}")
        except RuntimeError as exc:
            sys.exit(f"❌ {exc}")
        if r.get("skipped_reason"):
            print(f"⏸️  跳过正式推送：{r['skipped_reason']}")
            return
        extra = ""
        if r.get("failed_pending"):
            extra = f"；另有 {r['failed_pending']} 封处理失败待重试"
        if r.get("unknown"):
            extra += "；⚠️ 本地邮件缓存读取失败，无法断言邮箱无新邮件"
        print(f"（截止点 {r['cutoff']}）")
        if r.get("sent"):
            print(f"✅ 正式推送完成：{r['subject']}（合并 {r['n_items']} 份简报）{extra}")
        else:
            print(f"ℹ️  本次无新内容可推送，已发送状态邮件：{r.get('status_mail','')}{extra}")
        return
    sys.exit("请明确指定模式：--official（正式推送）/ --test（测试）/ --dry-run（预览）")


def cmd_ads_state_init(cfg: Config, args: argparse.Namespace) -> None:
    """初始化/迁移 ADS 正式推送状态（默认拒绝覆盖现有状态）。"""
    try:
        msg = state_init(cfg, args.last_official,
                         mark_existing_sent=getattr(args, "mark_existing_sent", False),
                         confirm=getattr(args, "confirm", False),
                         force=getattr(args, "force", False),
                         dry_run=getattr(args, "dry_run", False))
    except (ValueError, StateCorruptError) as exc:
        sys.exit(str(exc))
    print("✅ " + msg)


def cmd_html(cfg: Config, args: argparse.Namespace) -> None:
    files = sorted(cfg.zh_digest_dir.glob("*.zh.md"))
    if not files:
        print("ℹ️  当前无 ADS 中文简报（等新推送即可），跳过总览生成")
        return
    out = cfg.digest_dir / "ADS文献简报-中文总览.html"
    out.write_text(merge_markdown_files(files), encoding="utf-8")
    print(f"已生成合并 HTML（{len(files)} 份）: {out}")
