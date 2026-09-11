"""ADS 正式推送状态机（四种状态严格分离）。

- **邮件日期**（ads_YYYYMMDD_*.zh.md 的 YYYYMMDD / Mail.date）：只用于标题与展示，
  绝不作为「是否推送」的判断依据。
- **是否处理过**：由 ads run 的 processed 记录管理，与本模块无关。
- **是否测试发送过**：不记录——测试推送不改变任何正式状态。
- **是否正式发送过**：本模块 data/ads_state.json 按「原始邮件」逐封记录（source_id）。

正式推送语义（cron 每天 9:00 调用 `ads push --official`）：

    发送「本次截止点以前、所有尚未正式发送的 ADS 原始邮件」对应的简报。

正常运行时等价于「上次 9:00 → 本次 9:00 窗口」；停机/处理失败时会自动补发，
不会因为日期或时间窗边界而漏推（也没有任何“多少天以内”的静默丢弃限制）。

source_id = ``文件夹:UIDVALIDITY:UID``；收件时间取 IMAP INTERNALDATE
（Mail.received_at），不用邮件 Date 头。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

from ...core.html import _CSS, md_to_html
from ...core.imap_client import load_mails_from_dir
from ...core.push import send_html
from .overview import _body_after_header
from .parser import is_ads_email

MODES = ("official", "test", "dry-run")
SCHEMA_VERSION = 2


# ---------------- 状态文件 ----------------

def load_state(cfg) -> dict:
    """读取正式推送状态（缺失/损坏/旧格式 → 初始化为 v2 空状态）。"""
    st = {"schema_version": SCHEMA_VERSION, "last_official_cutoff": None, "items": {}}
    try:
        raw = json.loads(cfg.ads_state_file.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("schema_version") == SCHEMA_VERSION:
            st["last_official_cutoff"] = raw.get("last_official_cutoff")
            st["items"] = raw.get("items") or {}
    except Exception:
        pass
    return st


def save_state(cfg, st: dict) -> None:
    cfg.ads_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ads_state_file.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                                  encoding="utf-8")


def _now(cfg) -> datetime:
    return datetime.now(cfg.tz())


def _aware(dt, cfg):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=cfg.tz())


def _parse_iso(s, cfg):
    if not s:
        return None
    try:
        return _aware(datetime.fromisoformat(s), cfg)
    except Exception:
        return None


# ---------------- 候选收集 ----------------

def _index(cfg) -> dict:
    """emails/index.json：uid → {source_id, received_at, eml}（fetch 时写入）。"""
    f = cfg.eml_dir / "index.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def digest_candidates(cfg) -> list[dict]:
    """当前所有 ADS 简报文件 → [{source_id, file, date, received_at}]。

    source_id / received_at 优先取 sidecar 索引，缺失时按文件名与落盘时间兜底。
    """
    idx = _index(cfg)
    out: list[dict] = []
    for f in sorted(cfg.zh_digest_dir.glob("ads_*.zh.md")):
        m = re.search(r"ads_(\d{8})_(\d+)\.zh\.md$", f.name)
        if not m:
            continue
        tag, uid_txt = m.group(1), m.group(2)
        try:
            d = date(int(tag[:4]), int(tag[4:6]), int(tag[6:]))
        except ValueError:
            continue
        rec = idx.get(str(int(uid_txt))) or {}
        source_id = rec.get("source_id") or f"INBOX:?:{int(uid_txt)}"
        recv = _parse_iso(rec.get("received_at"), cfg)
        if recv is None:
            try:
                recv = datetime.fromtimestamp(f.stat().st_mtime).astimezone(cfg.tz())
            except Exception:
                recv = None
        out.append({"source_id": source_id, "file": f, "date": d, "received_at": recv})
    return out


def ads_mail_sources(cfg) -> dict:
    """当前邮箱里 ADS 原始邮件：{source_id: received_at}（用于识别“有邮件但简报缺失”）。"""
    out: dict = {}
    try:
        for m in load_mails_from_dir(cfg.eml_dir):
            if is_ads_email(m):
                sid = m.source_id or f"INBOX:?:{m.uid}"
                out[sid] = m.received_at or m.date
    except Exception:
        pass
    return out


def select_official(cfg, state: dict, cutoff: datetime):
    """按正式语义挑选待发送项。

    返回 (to_send, failed_pending)：
      to_send        —— 截止点之前、尚未正式发送的简报候选（跨日/多封全部包含）
      failed_pending —— 截止点之前已到达、但没有生成简报的 ADS 邮件（处理失败，需重试）
    """
    sent_ids = {sid for sid, it in state["items"].items() if it.get("official_sent_at")}
    cands = digest_candidates(cfg)
    to_send = [c for c in cands
               if (c["received_at"] is None or c["received_at"] <= cutoff)
               and c["source_id"] not in sent_ids]
    known_ids = {c["source_id"] for c in cands}
    failed = []
    for sid, recv in ads_mail_sources(cfg).items():
        if sid in sent_ids or sid in known_ids:
            continue
        r = _aware(recv, cfg)
        if r is None or r <= cutoff:
            failed.append(sid)
    return to_send, failed


# ---------------- 邮件组装与发送 ----------------

def _assemble_doc(cfg, files: list[Path]) -> str:
    sections = []
    for f in files:
        body = _body_after_header(f.read_text(encoding="utf-8"))
        sections.append(md_to_html(body, base=2))
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>ADS 文献简报</title><style>{_CSS}</style></head>
<body>{chr(10).join(sections)}<hr>
<p style="color:#888">mail-digest 每日自动推送 · 中文为机器辅助翻译，关键内容请核对原文。</p>
</body></html>"""


def _send_digest(cfg, entries: list[dict], prefix: str = "") -> str:
    """发送合并简报，返回邮件主题。"""
    ordered = sorted(entries, key=lambda x: (x["date"], x["file"].name))
    files = [e["file"] for e in ordered]
    dates = sorted({e["date"] for e in entries})
    n_arts = 0
    for f in files:
        n_arts += sum(1 for line in f.read_text(encoding="utf-8").splitlines()
                      if line.startswith("### "))
    label = (f"{dates[0]:%Y-%m-%d}" if len(dates) == 1
             else f"{min(dates):%Y-%m-%d} ~ {max(dates):%Y-%m-%d}")
    subject = f"{prefix}ADS 文献简报 {label}（{n_arts} 条文献）"
    send_html(cfg, cfg.imap_user, subject, _assemble_doc(cfg, files), agent="ads")
    return subject


def _status_html(title: str, lines: list[str]) -> str:
    body = "".join(f"<p>{l}</p>" for l in lines)
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title></head>
<body style="font-family:sans-serif;max-width:640px;margin:2em auto;line-height:1.7">
<h2 style="color:#0b3d91">{title}</h2>{body}
<p style="color:#888">mail-digest 每日自动运行 · 有推送时你会收到详细简报，无推送时收到本状态邮件。</p>
</body></html>"""


# ---------------- 三种模式 ----------------

def push_official(cfg, cutoff: datetime | None = None) -> dict:
    """正式推送：发送截止点以前所有尚未正式发送的 ADS 简报；SMTP 成功才推进状态。"""
    state = load_state(cfg)
    cutoff = cutoff or _now(cfg)
    to_send, failed = select_official(cfg, state, cutoff)
    today = date.today()

    if to_send:
        subject = _send_digest(cfg, to_send)          # 抛异常则状态不推进
        now_iso = _now(cfg).isoformat(timespec="seconds")
        for c in to_send:
            state["items"][c["source_id"]] = {
                "received_at": (c["received_at"].isoformat(timespec="seconds")
                                if c["received_at"] else None),
                "digest_file": c["file"].name,
                "official_sent_at": now_iso,
            }
        state["last_official_cutoff"] = now_iso
        save_state(cfg, state)
        return {"sent": True, "n_items": len(to_send), "subject": subject,
                "failed_pending": len(failed)}

    now_iso = _now(cfg).isoformat(timespec="seconds")
    if failed:
        # 有 ADS 邮件但处理失败：如实报告，且不推进 cutoff（保留待重试）
        subject = f"ADS 文献状态 {today:%Y-%m-%d}：发现新邮件但处理失败"
        send_html(cfg, cfg.imap_user, subject, _status_html(
            f"ADS 文献 Agent · {today:%Y-%m-%d}",
            [f"发现 <strong>{len(failed)}</strong> 封新的 ADS 邮件，但简报生成失败"
             "（可能原因：ADS API 临时不可用、LLM 失败，或尚未运行 <code>ads run</code>）。",
             "已保留这些邮件，<strong>下次运行会自动重试</strong>；本次不推进正式推送状态。"]),
            agent="ads")
        return {"sent": False, "n_items": 0, "status_mail": subject,
                "failed_pending": len(failed)}

    subject = f"ADS 文献状态 {today:%Y-%m-%d}：今日无新推送"
    send_html(cfg, cfg.imap_user, subject, _status_html(
        f"ADS 文献 Agent · 每日状态 {today:%Y-%m-%d}",
        ["今天没有收到新的 myADS 文献推送，因此没有新的文献简报。",
         "本邮件用于确认每日自动检查已正常运行。"]), agent="ads")
    state["last_official_cutoff"] = now_iso
    save_state(cfg, state)
    return {"sent": False, "n_items": 0, "status_mail": subject, "failed_pending": 0}


def push_test(cfg, when: date | None = None) -> dict:
    """测试推送：真实发送（标题加 [TEST]），但不修改任何正式状态。"""
    entries = [c for c in digest_candidates(cfg)
               if when is None or c["date"] == when]
    if not entries:
        raise RuntimeError("没有可用于测试的 ADS 简报（请先 ads run 生成简报）")
    subject = _send_digest(cfg, entries, prefix="[TEST] ")
    return {"sent": True, "n_items": len(entries), "subject": subject}


def preview(cfg, when: date | None = None) -> str:
    """dry-run：只描述将发送的内容，不发送、不改状态。"""
    if when is not None:
        entries = [c for c in digest_candidates(cfg) if c["date"] == when]
        kind = f"指定日期 {when:%Y-%m-%d}"
        failed: list = []
    else:
        state = load_state(cfg)
        entries, failed = select_official(cfg, state, _now(cfg))
        kind = "正式推送语义（截止点前全部未正式发送）"
    if not entries:
        extra = f"；另有 {len(failed)} 封 ADS 邮件处理失败待重试" if failed else ""
        return f"（dry-run）{kind}{extra}：无待发送内容 → 正式运行将发送状态邮件"
    dates = sorted({c["date"] for c in entries})
    return (f"（dry-run）{kind}：将合并发送 {len(entries)} 份简报"
            f"（{min(dates):%Y-%m-%d} ~ {max(dates):%Y-%m-%d}）→ {cfg.imap_user}，不连接 SMTP")


def state_init(cfg, last_official: str, mark_existing_sent: bool = False) -> str:
    """初始化正式推送状态（旧版本按日期记录无法自动判断逐封状态，需人工确认起点）。

    mark_existing_sent=True 时，把当前已有简报全部标记为“起点之前已正式发送”，
    避免初始化后把历史简报重发一遍。
    """
    cutoff = _parse_iso(last_official, cfg)
    if cutoff is None:
        raise ValueError(f"无法解析时间：{last_official!r}（示例：2026-09-11 09:27:00+08:00）")
    state = {"schema_version": SCHEMA_VERSION,
             "last_official_cutoff": cutoff.isoformat(timespec="seconds"), "items": {}}
    n_marked = 0
    if mark_existing_sent:
        for c in digest_candidates(cfg):
            state["items"][c["source_id"]] = {
                "received_at": (c["received_at"].isoformat(timespec="seconds")
                                if c["received_at"] else None),
                "digest_file": c["file"].name,
                "official_sent_at": cutoff.isoformat(timespec="seconds"),
            }
            n_marked += 1
    save_state(cfg, state)
    return (f"状态已初始化：last_official_cutoff={state['last_official_cutoff']}，"
            f"标记为已发送的简报 {n_marked} 份")
