"""场景二：学院基金/项目申报通知 → 每日「项目申报机会清单」。

流程（每封基金邮件）：
  提取附件 → 递归解压（zip/tar/gz/7z）→ 文档转文本（docx/pdf/xlsx/…）
  → LLM 结构化提取（项目名/领域/截止/额度/条件/材料/适用性）
  → 汇总为当日 Markdown 清单。

降级原则：rar/.doc/.wps/扫描 PDF 等读不了的，在清单里明确标注「需人工查看」。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from . import attachments as att
from . import doctext as dt
from .config import Config
from .llm import LLMError, DeepSeekClient, build_grant_messages, parse_grant_result
from .models import Mail
from .research_profile import PROFILE

DEFAULT_TEXT_CAP = 12000          # 每封送入 LLM 的合并文本上限（字符）


def _load_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_ids(path: Path, ids: set[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(ids), ensure_ascii=False), encoding="utf-8")


def _load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def process_mail(cfg: Config, mail: Mail, client: DeepSeekClient | None,
                 text_cap: int = DEFAULT_TEXT_CAP) -> dict:
    """处理一封基金邮件 → 提取结果 dict（失败字段用 error 表示）。"""
    result: dict = {
        "uid": mail.uid, "subject": mail.subject or "", "sender": mail.from_ or "",
        "date": mail.date.strftime("%Y-%m-%d %H:%M") if mail.date else "",
        "error": "", "problems": [], "llm": None,
    }
    work = cfg.data_dir / "work" / f"fund_{mail.uid:06d}"
    if work.exists():
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        files = att.extract_attachments(mail, work / "att")
        ok_files = [f["path"] for f in files if f["ok"] and f["path"]]
        for f in files:
            if not f["ok"]:
                result["problems"].append(f"{f['name']}: {f['error']}")
        readable, problems = att.unpack_recursive(ok_files, work / "unpack")
        result["problems"].extend(f"{p.get('file', '附件')}: {p['error']}" for p in problems)
        text, prob2 = dt.extract_many(readable, max_chars=text_cap)
        result["problems"].extend(f"{p['file']}: {p['error']}" for p in prob2)
        result["attach_count"] = len(ok_files)
        # 正文（含校内截止/意向反馈要求）与附件文本合并送 LLM
        body = (mail.body_text or "").strip()
        if body:
            body_head = f"【邮件正文（转发说明，可能含校内截止时间）】\n{body[:3000]}"
            text = f"{body_head}\n\n【附件文本】\n{text}" if text.strip() else body_head
        if not text.strip():
            result["problems"].append("正文与附件均无可用文本")
    except Exception as exc:  # 兜底：单封失败不影响其他
        result["error"] = f"附件处理失败: {exc}"
        text = mail.body_text.strip()[: text_cap]

    if client:
        try:
            msgs = build_grant_messages(mail.subject, mail.from_,
                                        result["date"], text, profile=PROFILE)
            raw = client.complete_json(msgs)
            result["llm"] = parse_grant_result(raw)
        except LLMError as exc:
            result["error"] = f"LLM 提取失败: {exc}"
    else:
        result["error"] = "未配置 DEEPSEEK_API_KEY，仅列出邮件标题"
    return result


def _pretty_deadline(res: dict) -> str:
    llm = res.get("llm") or {}
    return str(llm.get("deadline") or "未知")


_MATCH_RANK = {"高度匹配": 0, "部分匹配": 1, "待确认": 2, "不匹配": 3}


def _sort_key(res: dict) -> tuple:
    llm = res.get("llm") or {}
    d = str(llm.get("deadline_date") or "")
    rank = _MATCH_RANK.get(str(llm.get("match_level") or ""), 4)
    return (rank, 0 if d else 1, d)


def build_daily_list(results: list[dict], when: date) -> str:
    """把当天处理的邮件结果汇总为 Markdown 申报机会清单。

    按能力匹配度（高度匹配优先）再截止日期排序；每条标注与申报人技能/经历的匹配说明。
    """
    if not results:
        return f"# 项目申报机会清单 {when:%Y-%m-%d}\n\n今日无新申报通知。\n"
    lines = [
        f"# 项目申报机会清单 {when:%Y-%m-%d}",
        "",
        f"- 今日申报通知：**{len(results)}** 封（按与你技能/经历的匹配度排序，匹配说明基于附件与你的能力档案）",
        "",
    ]

    def render(res: dict) -> list[str]:
        llm = res.get("llm") or {}
        out = []
        name = llm.get("project_name") or res["subject"] or "(无主题)"
        dl = _pretty_deadline(res)
        mark = "⏰" if llm.get("deadline_date") else ""
        out.append(f"### {name} {mark}")
        out.append(f"- 截止：{dl}")
        if llm.get("match_level") and llm.get("match_reason"):
            lv = llm["match_level"]
            icon = {"高度匹配": "🎯", "部分匹配": "🧩", "待确认": "❓", "不匹配": "⚪"}.get(lv, "🧩")
            out.append(f"- {icon} 能力匹配：**{lv}**——{llm['match_reason']}")
        if llm.get("field"):
            out.append(f"- 领域：{llm['field']}")
        if llm.get("amount") and llm["amount"] != "未提及":
            out.append(f"- 资助：{llm['amount']}")
        if llm.get("eligibility") and llm["eligibility"] != "未提及":
            out.append(f"- 申报条件：{llm['eligibility']}")
        if llm.get("materials") and llm["materials"] != "未提及":
            out.append(f"- 材料：{llm['materials']}")
        if llm.get("notes") and llm["notes"] != "未提及":
            out.append(f"- 注意：{llm['notes']}")
        out.append(f"- 来源：{res['sender']}｜{res['date']}")
        out.append(f"- 主题：{res['subject']}")
        if res.get("problems"):
            out.append("- ⚠️ " + "；".join(res["problems"]))
        if res.get("error") and not llm:
            out.append(f"- ⚠️ {res['error']}")
        out.append("")
        return out

    for res in sorted(results, key=_sort_key):
        lines.extend(render(res))
    return "\n".join(lines)


def run_fund(cfg: Config, grant_mails: list[Mail], force: bool = False,
             limit: int | None = None) -> tuple[int, str | None]:
    """处理基金邮件，返回 (处理封数, 当日清单文本 or None)。

    安全：只处理可信发件人（cfg.grant_allowed_senders 白名单）的邮件附件，
    其余跳过——防止攻击者用伪造的「申报通知」投递恶意压缩包。
    """
    from .config import sender_allowed
    processed = _load_ids(cfg.data_dir / "processed_fund.json")
    cache = _load_cache(cfg.data_dir / "fund_cache.json")
    todo = [m for m in grant_mails if force or m.uid not in processed]
    if not cfg.grant_allowed_senders.strip():
        print("⚠️  未配置 GRANT_ALLOWED_SENDERS（可信发件人白名单）——为防恶意附件，"
              "跳过全部基金邮件附件处理。请在 .env 配置，如：GRANT_ALLOWED_SENDERS=*@mail.sysu.edu.cn")
        return 0, None
    trusted = [m for m in todo if sender_allowed(m.from_, cfg.grant_allowed_senders)]
    skipped = len(todo) - len(trusted)
    if skipped:
        print(f"⏭️  跳过 {skipped} 封非可信发件人的邮件（不做附件处理，防恶意压缩包）")
    todo = trusted
    if limit and len(todo) > limit:
        todo = todo[:limit]
    if not todo:
        return 0, None

    client = None
    if cfg.deepseek_api_key:
        client = DeepSeekClient(cfg.deepseek_api_key, cfg.deepseek_model,
                                cfg.deepseek_base_url, cfg.llm_request_interval)
    results: list[dict] = []
    for m in todo:
        print(f"  ▶ [{m.uid}] 《{m.subject[:44]}》 附件处理中…")
        cached = cache.get(str(m.uid))
        if cached and not force:
            results.append(cached)
            continue
        res = process_mail(cfg, m, client)
        cache[str(m.uid)] = res
        results.append(res)
        if res.get("llm"):
            pn = res["llm"].get("project_name") or res["subject"][:30]
            dl = res["llm"].get("deadline") or "未知"
            print(f"     ✅ {pn}｜截止 {dl}")
        elif res.get("error"):
            print(f"     ⚠️ {res['error']}")
    _save_cache(cfg.data_dir / "fund_cache.json", cache)
    _save_ids(cfg.data_dir / "processed_fund.json", processed | {m.uid for m in todo})

    # 汇总：只汇总「今天收到」的通知（按邮件日期）
    today = date.today()
    today_results = [r for r in results
                     if r["date"].startswith(today.strftime("%Y-%m-%d"))]
    if not today_results:
        return len(todo), None
    return len(todo), build_daily_list(today_results, today)
