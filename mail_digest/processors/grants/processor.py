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
from . import datecheck as dc
from . import doctext as dt
from ...core.config import Config
from ...core.llm import LLMError, DeepSeekClient
from .extractor import build_grant_messages, parse_grant_result
from ...core.models import Mail
from ...core.research_profile import PROFILE

DEFAULT_TEXT_CAP = 12000          # 每封送入 LLM 的合并文本上限（字符）


_AUTH_FAIL_RE = re.compile(r"(?:spf|dkim|dmarc)\s*=\s*(fail|hardfail|softfail)(?=\s|;|$)", re.I)
# 对齐域参数：spf=pass smtp.mailfrom=域 / dkim=pass header.d=域 / dmarc=pass from=域
_AUTH_DOMAIN_RE = re.compile(
    r"(?:spf|dkim|dmarc)\s*=\s*pass\b[^;]*?(?:smtp\.mailfrom|header\.d|from)\s*=\s*([^\s;]+)", re.I)


def _from_domain(from_header: str) -> str:
    m = re.search(r"<([^>]+)>", from_header or "")
    addr = (m.group(1) if m else from_header or "").strip().lower()
    return addr.split("@")[-1] if "@" in addr else ""


def auth_sender_trusted(mail) -> bool:
    """发件人真实性（第二层）：Authentication-Results 判定。

    - 任一 SPF/DKIM/DMARC fail 族 → 不可信（拒绝）
    - 存在 pass 结果时：至少一个 pass 的对齐域与 From 域一致才放行，
      否则（错域 pass / 无对齐信息）视为不可信
    - 完全无 Authentication-Results（校内互发常见）→ 放行，仅依赖白名单
    """
    res = (mail.headers or {}).get("authentication-results", "") or ""
    if not res.strip():
        return True
    if _AUTH_FAIL_RE.search(res):
        return False
    from_dom = _from_domain(mail.from_ or "")
    pass_domains = {m.group(1).strip("<>").lower().split("@")[-1]
                    for m in _AUTH_DOMAIN_RE.finditer(res)}
    if not pass_domains:
        return True                       # 无 fail 也无 pass（neutral 等）→ 白名单兜底
    return from_dom in pass_domains       # 需有 pass 域与 From 域一致（防错域 pass）




def validate_evidence(llm: dict, text: str, attach_names: list) -> list[str]:
    """校验 LLM 返回的原文证据是否真实存在于输入文本与附件清单中。

    防提示词注入/幻觉：deadline/amount/limit 的 quote 必须是原文精确子串，
    source 必须对应真实附件名或『邮件正文』；否则给出警告（证据不可信）。
    """
    warns: list[str] = []
    for field, qk, sk in (("截止", "deadline_quote", "deadline_source"),
                          ("资助", "amount_quote", "amount_source"),
                          ("限项", "limit_quote", "limit_source")):
        q = str(llm.get(qk) or "").strip()
        if not q or q == "未提及":
            continue
        if text.find(q) == -1:
            warns.append(f"{field}证据原句未能在附件/正文原文中找到"
                         f"（“{q[:40]}…”）——疑似注入/幻觉，请人工核对")
        src = str(llm.get(sk) or "").strip()
        if src and src != "邮件正文" and not any(src in n for n in attach_names):
            warns.append(f"{field}证据来源“{src}”不在附件清单中")
    return warns


def auth_results_fail(mail) -> bool:
    """解析邮件服务器写入的 Authentication-Results：SPF/DKIM/DMARC 判定失败 → 不可信。

    From 头可伪造；若邮箱服务器（MX 层）已判定认证失败，即使 From 域在白名单内也拒绝。
    无 Authentication-Results 头（如校内互发）不拦截，仅依赖白名单。
    """
    res = (mail.headers or {}).get("authentication-results", "") or ""
    return bool(_AUTH_FAIL_RE.search(res))


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
    except Exception as exc:  # 兜底：单封失败不影响其他；清理疑似炸弹残留
        result["error"] = f"附件处理失败: {exc}"
        text = mail.body_text.strip()[: text_cap]
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)

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

    # 防提示词注入/防幻觉：① 证据 quote/source 校验 ② 规则独立提取日期交叉校验
    if result.get("llm") and not result.get("error"):
        ref_year = mail.date.year if mail.date else date.today().year
        attach_names: list[str] = []
        try:
            attach_names = [p.name for p in readable]
        except Exception:                       # 附件处理失败路径，readable 未定义
            attach_names = []
        try:
            result["evidence_warns"] = validate_evidence(
                result["llm"], text, attach_names)
            rule = dc.rule_dates(text, ref_year)
            result["deadline_check"] = dc.cross_check(
                str(result["llm"].get("deadline_date") or ""), rule, ref_year)
        except Exception:
            result["evidence_warns"] = []
            result["deadline_check"] = ""
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

        def ev(tag: str, quote_key: str, src_key: str) -> None:
            """原文证据行：〔来源〕“原句”"""
            q = str(llm.get(quote_key) or "").strip()
            if not q or q == "未提及":
                return
            src = str(llm.get(src_key) or "").strip()
            where = f"〔{src}〕" if src else ""
            out.append(f"- {tag} {where}“{q[:120]}”")

        ev("📜 截止证据", "deadline_quote", "deadline_source")
        if res.get("deadline_check"):
            out.append(f"- {res['deadline_check']}")
        for w in res.get("evidence_warns") or []:
            out.append(f"- ⚠️ {w}")
        ev("📜 资助证据", "amount_quote", "amount_source")
        ev("📜 限项证据", "limit_quote", "limit_source")
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
        if res.get("error"):
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
    from ...core.config import sender_allowed
    processed = _load_ids(cfg.grants_processed_file)
    cache = _load_cache(cfg.grants_cache_file)
    todo = [m for m in grant_mails if force or m.uid not in processed]
    if not cfg.grant_allowed_senders.strip():
        print("⚠️  未配置 GRANT_ALLOWED_SENDERS（可信发件人白名单）——为防恶意附件，"
              "跳过全部基金邮件附件处理。请在 .env 配置，如：GRANT_ALLOWED_SENDERS=*@mail.sysu.edu.cn")
        return 0, None
    trusted = [m for m in todo
               if sender_allowed(m.from_, cfg.grant_allowed_senders)
               and auth_sender_trusted(m)]
    skipped = len(todo) - len(trusted)
    if skipped:
        print(f"⏭️  跳过 {skipped} 封未通过信任检查的邮件（发件人白名单/认证结果 fail，不做附件处理）")
    todo = trusted
    if limit and len(todo) > limit:
        todo = todo[:limit]
    if not todo:
        return 0, None

    client = None
    if cfg.grants_llm_key():
        client = DeepSeekClient(cfg.grants_llm_key(), cfg.deepseek_model,
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
    _save_cache(cfg.grants_cache_file, cache)
    _save_ids(cfg.grants_processed_file, processed | {m.uid for m in todo})

    # 汇总：只汇总「今天收到」的通知（按邮件日期）
    today = date.today()
    today_results = [r for r in results
                     if r["date"].startswith(today.strftime("%Y-%m-%d"))]
    if not today_results:
        return len(todo), None
    return len(todo), build_daily_list(today_results, today)
