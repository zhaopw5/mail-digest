"""ADS 正式推送状态机（四种状态严格分离）。

- **邮件日期**：只用于标题与展示，绝不作为「是否推送」的判断依据。
- **是否处理成功**：由 `ads_manifest.json` 管理（status=ready 才允许推送）。
- **是否测试发送过**：不记录——测试推送不改变任何正式状态。
- **是否正式发送过**：本模块 `data/ads_state.json` 按完整 source_id 逐封记录。

正式推送语义（cron 每天 9:00 调用 `ads push --official`）::

    发送「本次计划截止点以前、处理成功、且尚未正式发送」的全部简报。

截止点固定为**计划时刻**（`MAIL_DIGEST_PUSH_TIME`，默认 09:00 的北京时间），
不是进程启动时间：09:10 才跑起来时，09:05 到达的邮件仍属于下一个窗口。

安全约束：

- 状态文件损坏 → 直接中止，不发送（绝不在状态可疑时发信）。
- 正式推送全程持有 `data/.ads_official.lock`，防止两个进程并发重复发送。
- 状态文件原子写入；SMTP 抛出异常时不推进任何状态，下次自动重发。
- source_id = ``文件夹:UIDVALIDITY:UID``：UIDVALIDITY 变化后不会把新邮件
  当成"旧邮件已发送"而跳过。
"""
from __future__ import annotations

import re
from datetime import date, datetime

from ...core.html import _CSS, md_to_html
from ...core.imap_client import load_index, load_imap_state, load_mails_from_dir
from ...core.push import send_html
from ...core.state import FileLock, LockBusyError, StateCorruptError, load_json_strict, write_json_atomic
from .manifest import failed_source_ids, load_manifest
from .overview import _body_after_header
from .parser import is_ads_email

MODES = ("official", "test", "dry-run")
SCHEMA_VERSION = 2


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


# ---------------- 状态文件 ----------------

def _fresh_state() -> dict:
    return {"schema_version": SCHEMA_VERSION, "last_official_cutoff": None,
            "last_official_sent_at": None, "items": {}}


def load_state(cfg, *, strict: bool = True) -> dict:
    """读正式推送状态。

    strict=True（正式推送/预览）：文件存在但损坏 → 抛 StateCorruptError，
    由调用方中止，绝不把损坏状态当空状态继续发送。
    """
    path = cfg.ads_state_file
    if not path.exists():
        return _fresh_state()
    try:
        raw = load_json_strict(path, None)
    except StateCorruptError:
        if strict:
            raise
        print(f"⚠️  正式推送状态损坏：{path}（已按空状态继续，仅供只读场景）")
        return _fresh_state()
    if not isinstance(raw, dict):
        if strict:
            raise StateCorruptError(f"正式推送状态格式异常：{path}")
        return _fresh_state()
    if raw.get("schema_version") != SCHEMA_VERSION:
        # 旧版本状态（v1 按日期记录）语义不同，不能猜测逐封状态 → 中止并指引迁移
        if strict:
            raise StateCorruptError(
                f"正式推送状态是旧版本（schema_version={raw.get('schema_version')}）：{path}\n"
                "请先运行 `mail-digest ads state-init --last-official \"<上次成功推送时间>\"` 迁移。")
        return _fresh_state()
    if not isinstance(raw.get("items"), dict):
        raise StateCorruptError(f"正式推送状态 items 字段异常：{path}")
    st = _fresh_state()
    st["last_official_cutoff"] = raw.get("last_official_cutoff")
    st["last_official_sent_at"] = raw.get("last_official_sent_at")
    st["items"] = raw["items"]
    return st


def save_state(cfg, st: dict) -> None:
    write_json_atomic(cfg.ads_state_file, st,
                      backup=cfg.ads_state_file.with_suffix(".json.bak"))


# ---------------- 候选收集 ----------------

def _fallback_candidates(cfg) -> list[dict]:
    """manifest 缺失时的兜底：仅在「简报文件的 UID 在索引中身份唯一」时采用。

    绝不使用「按裸 UID 取最新索引」的做法——那正是新邮件被旧简报冒名登记的根因。
    """
    index = load_index(cfg.eml_dir)
    by_uid: dict[int, list[dict]] = {}
    for name, rec in index["emails"].items():
        if rec.get("uid") is not None:
            by_uid.setdefault(int(rec["uid"]), []).append(rec)
    out: list[dict] = []
    for f in sorted(cfg.zh_digest_dir.glob("ads_*.zh.md")):
        m = re.search(r"ads_(?:(\d{8})|nodate)_(\d+)\.zh\.md$", f.name)
        if not m:
            continue
        uid = int(m.group(2))
        recs = by_uid.get(uid) or []
        if len(recs) != 1:
            print(f"⚠️  简报 {f.name} 无法唯一确定来源邮件（UID {uid} 命中 {len(recs)} 条索引），"
                  "已跳过；请重新运行 ads run 以建立 ads_manifest.json")
            continue
        rec = recs[0]
        out.append({"source_id": rec.get("source_id"), "file": f, "en_file": None,
                    "received_at": _parse_iso(rec.get("received_at"), cfg)})
    return out


def digest_candidates(cfg) -> list[dict]:
    """当前可推送的 ADS 简报 → [{source_id, file, received_at}]。

    来源是 `ads_manifest.json` 中 status=ready 的条目（按 source_id 精确关联），
    与文件名里的日期无关，因此没有 Date 头的邮件同样能被推送。
    """
    index = load_index(cfg.eml_dir)
    by_name = {rec.get("source_id"): rec for rec in index["emails"].values()
               if rec.get("source_id")}
    out: list[dict] = []
    mf = load_manifest(cfg)
    for sid, it in mf.get("items", {}).items():
        if it.get("status") != "ready":
            continue
        zh = it.get("zh_file")
        en = it.get("en_file")
        f = (cfg.zh_digest_dir / zh) if zh else None
        if f is None or not f.exists():
            f = (cfg.digest_dir / en) if en else None
        if f is None or not f.exists():
            print(f"⚠️  已处理记录的简报文件丢失：{sid}（{zh or en}），跳过")
            continue
        recv = _parse_iso(it.get("received_at"), cfg)
        if recv is None:
            rec = by_name.get(sid) or {}
            recv = _parse_iso(rec.get("received_at"), cfg)
        if recv is None:
            try:
                recv = datetime.fromtimestamp(f.stat().st_mtime).astimezone(cfg.tz())
            except Exception:
                recv = None
        out.append({"source_id": sid, "file": f, "en_file": en, "received_at": recv})
    if not out:
        out = _fallback_candidates(cfg)
    return out


def ads_mail_sources(cfg) -> dict | None:
    """当前邮箱里 ADS 原始邮件：{source_id: received_at}。

    读取失败时返回 None（表示「未知」），调用方不得把未知当成「没有新邮件」。
    """
    out: dict = {}
    try:
        for m in load_mails_from_dir(cfg.eml_dir, cfg.default_folder, tz=cfg.tz()):
            if is_ads_email(m):
                out[m.source_id] = m.received_at or m.date
    except Exception as exc:
        print(f"⚠️  读取本地邮件缓存失败（{exc}）：无法确认是否存在未处理的 ADS 邮件")
        return None
    return out


def fetch_evidence(cfg) -> dict:
    """最近一次拉取记录（用于判断「没有新推送」是否有依据）。"""
    rec = (load_imap_state(cfg).get(cfg.default_folder) or {})
    return {"last_fetch_at": _parse_iso(rec.get("last_fetch_at"), cfg),
            "uidvalidity": rec.get("uidvalidity"),
            "gaps": rec.get("gaps") or [],
            "uncovered_below": rec.get("uncovered_below")}


def select_official(cfg, state: dict, cutoff: datetime):
    """按正式语义挑选待发送项。

    返回 (to_send, failed_pending, unknown)：
      to_send        —— 截止点之前、处理成功、尚未正式发送的简报
      failed_pending —— 截止点之前已到达但处理失败的 ADS 邮件（下次 ads run 重试）
      unknown        —— True 表示本地邮件缓存读不出来，无法断言是否有新邮件
    """
    sent_ids = {sid for sid, it in state["items"].items() if it.get("official_sent_at")}
    cands = digest_candidates(cfg)
    to_send = [c for c in cands
               if c["source_id"]
               and (c["received_at"] is None or c["received_at"] <= cutoff)
               and c["source_id"] not in sent_ids]
    known_ids = {c["source_id"] for c in cands}
    mf = load_manifest(cfg)
    failed = set()
    for sid in failed_source_ids(mf):
        if sid in sent_ids or sid in known_ids:
            continue
        rec = (mf["items"].get(sid) or {})
        r = _parse_iso(rec.get("received_at"), cfg)
        if r is None or r <= cutoff:
            failed.add(sid)
    sources = ads_mail_sources(cfg)
    unknown = sources is None
    if sources:
        for sid, recv in sources.items():
            if sid in sent_ids or sid in known_ids:
                continue
            r = _aware(recv, cfg)
            if r is None or r <= cutoff:
                failed.add(sid)
    return to_send, failed, unknown


# ---------------- 邮件组装与发送 ----------------

def _display_date(entry: dict) -> str:
    r = entry.get("received_at")
    return f"{r:%Y-%m-%d}" if r else "日期未知"


def _assemble_doc(cfg, files: list, notes: list[str] | None = None) -> str:
    sections = []
    for f in files:
        body = _body_after_header(f.read_text(encoding="utf-8"))
        sections.append(md_to_html(body, base=2))
    note_html = ""
    if notes:
        note_html = ("<hr><h3>处理状态</h3>" +
                     "".join(f"<p>{n}</p>" for n in notes))
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>ADS 文献简报</title><style>{_CSS}</style></head>
<body>{chr(10).join(sections)}{note_html}<hr>
<p style="color:#888">mail-digest 每日自动推送 · 中文为机器辅助翻译，关键内容请核对原文。</p>
</body></html>"""


def _count_articles(f) -> int:
    try:
        return sum(1 for line in f.read_text(encoding="utf-8").splitlines()
                   if line.startswith("### "))
    except OSError:
        return 0


def _send_digest(cfg, entries: list[dict], prefix: str = "",
                 notes: list[str] | None = None) -> str:
    ordered = sorted(entries, key=lambda x: (x.get("received_at") or datetime.min.replace(
        tzinfo=cfg.tz()), x["file"].name))
    files = [e["file"] for e in ordered]
    labels = sorted({_display_date(e) for e in entries})
    n_arts = sum(_count_articles(f) for f in files)
    label = labels[0] if len(labels) == 1 else f"{labels[0]} ~ {labels[-1]}"
    subject = f"{prefix}ADS 文献简报 {label}（{n_arts} 条文献）"
    send_html(cfg, cfg.imap_user, subject, _assemble_doc(cfg, files, notes), agent="ads")
    return subject


def _status_html(title: str, lines: list[str]) -> str:
    body = "".join(f"<p>{l}</p>" for l in lines)
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title></head>
<body style="font-family:sans-serif;max-width:640px;margin:2em auto;line-height:1.7">
<h2 style="color:#0b3d91">{title}</h2>{body}
<p style="color:#888">mail-digest 每日自动运行 · 有推送时你会收到详细简报，无推送时收到本状态邮件。</p>
</body></html>"""


def _situation_notes(cfg, cutoff, n_sent: int, failed: set, unknown: bool,
                     evidence: dict) -> list[str]:
    """邮件正文里如实列出本次的处理范围与未完成部分。"""
    notes = [f"本次截止点：{cutoff:%Y-%m-%d %H:%M:%S %Z}（计划推送时刻，非进程启动时间）",
             f"本次成功推送简报：{n_sent} 份"]
    notes.append(f"处理失败待重试：{len(failed)} 封" if failed else "处理失败待重试：0 封")
    lf = evidence.get("last_fetch_at")
    notes.append(f"最近一次拉取邮箱：{lf:%Y-%m-%d %H:%M:%S}" if lf else
                 "最近一次拉取邮箱：无记录（无法确认邮箱是否还有新邮件）")
    if unknown:
        notes.append("⚠️ 本地邮件缓存读取失败：无法断言邮箱里是否还有未处理的 ADS 邮件。")
    if evidence.get("gaps"):
        notes.append(f"⚠️ 有 {len(evidence['gaps'])} 封邮件上次拉取失败，仍在待重试队列。")
    if evidence.get("uncovered_below"):
        notes.append("⚠️ 首次接管邮箱尚未完成：更早的邮件将分批补拉，暂未纳入统计。")
    return notes


def push_official(cfg, cutoff: datetime | None = None) -> dict:
    """正式推送：发送截止点前所有处理成功且未正式发送的简报；SMTP 成功才推进状态。"""
    if not cfg.ads_enabled:
        return {"sent": False, "n_items": 0, "skipped_reason": "ADS_ENABLED=false"}
    try:
        with FileLock(cfg.ads_lock_file):
            return _push_official_locked(cfg, cutoff)
    except LockBusyError as exc:
        raise RuntimeError(str(exc)) from exc


def _push_official_locked(cfg, cutoff: datetime | None) -> dict:
    state = load_state(cfg)                       # 损坏 → StateCorruptError，中止发送
    cutoff = _aware(cutoff, cfg) or cfg.planned_cutoff()
    to_send, failed, unknown = select_official(cfg, state, cutoff)
    evidence = fetch_evidence(cfg)
    today = _now(cfg)

    if to_send:
        notes = _situation_notes(cfg, cutoff, len(to_send), failed, unknown, evidence)
        subject = _send_digest(cfg, to_send, notes=notes)   # 抛异常则状态不推进
        sent_at = _now(cfg).isoformat(timespec="seconds")
        for c in to_send:
            state["items"][c["source_id"]] = {
                "received_at": (c["received_at"].isoformat(timespec="seconds")
                                if c["received_at"] else None),
                "digest_file": c["file"].name,
                "official_sent_at": sent_at,
                "cutoff": cutoff.isoformat(timespec="seconds"),
            }
        state["last_official_cutoff"] = cutoff.isoformat(timespec="seconds")
        state["last_official_sent_at"] = sent_at
        save_state(cfg, state)
        return {"sent": True, "n_items": len(to_send), "subject": subject,
                "failed_pending": len(failed), "unknown": unknown,
                "cutoff": state["last_official_cutoff"]}

    # 本次没有可发送内容：发送状态邮件，如实说明检查范围
    lf = evidence.get("last_fetch_at")
    # 「没有新推送」只有在截止点之后确实拉取过邮箱时才成立（cron 顺序：all → push）
    fetched_recently = bool(lf and lf >= cutoff)
    notes = _situation_notes(cfg, cutoff, 0, failed, unknown, evidence)
    if failed:
        subject = f"ADS 文献状态 {today:%Y-%m-%d}：发现新邮件但处理失败"
        head = (f"发现 <strong>{len(failed)}</strong> 封 ADS 邮件尚未处理成功"
                "（可能原因：ADS API 临时不可用、LLM 失败，或本次 ads run 尚未跑完）。")
        tail = "这些邮件<strong>没有</strong>被标记为已处理，下次 `ads run` 会自动重试。"
    elif unknown or not fetched_recently:
        subject = f"ADS 文献状态 {today:%Y-%m-%d}：本次未检测到有效的邮件拉取记录"
        head = ("本地已处理邮件中没有待推送内容，但<strong>没有</strong>截止点之后的拉取记录，"
                "因此不能确认邮箱里没有新邮件。")
        tail = "请确认 cron 中 `fetch`（`mail-digest all` 已包含）在 push 之前成功执行。"
    else:
        subject = f"ADS 文献状态 {today:%Y-%m-%d}：今日无新推送"
        head = ("截止点之前没有收到新的 myADS 文献推送（已核对本地邮件缓存与处理状态），"
                "因此没有新的文献简报。")
        tail = "本邮件用于确认每日自动检查已正常运行。"
    send_html(cfg, cfg.imap_user, subject,
              _status_html(f"ADS 文献 Agent · 每日状态 {today:%Y-%m-%d}",
                           [head, tail] + notes), agent="ads")
    state["last_official_cutoff"] = cutoff.isoformat(timespec="seconds")
    state["last_official_sent_at"] = _now(cfg).isoformat(timespec="seconds")
    save_state(cfg, state)
    return {"sent": False, "n_items": 0, "status_mail": subject,
            "failed_pending": len(failed), "unknown": unknown,
            "cutoff": state["last_official_cutoff"]}


def push_test(cfg, when: date | None = None) -> dict:
    """测试推送：真实发送（标题加 [TEST]），但不修改任何正式状态。"""
    entries = digest_candidates(cfg)
    if when is not None:
        entries = [e for e in entries
                   if e.get("received_at") and e["received_at"].date() == when]
    if not entries:
        raise RuntimeError("没有可用于测试的 ADS 简报（请先 ads run 生成简报）")
    subject = _send_digest(cfg, entries, prefix="[TEST] ")
    return {"sent": True, "n_items": len(entries), "subject": subject}


def preview(cfg, when: date | None = None) -> str:
    """dry-run：只描述将发送的内容，不发送、不改状态。"""
    if when is not None:
        entries = [c for c in digest_candidates(cfg)
                   if c.get("received_at") and c["received_at"].date() == when]
        failed: set = set()
        unknown = False
        kind = f"指定日期 {when:%Y-%m-%d}"
        cutoff = None
    else:
        state = load_state(cfg)                  # 损坏同样中止（与正式行为一致）
        cutoff = cfg.planned_cutoff()
        entries, failed, unknown = select_official(cfg, state, cutoff)
        kind = f"正式推送语义（截止点 {cutoff:%Y-%m-%d %H:%M} 前全部未正式发送）"
    if not entries:
        extra = f"；另有 {len(failed)} 封处理失败待重试" if failed else ""
        if unknown:
            extra += "；⚠️ 本地邮件缓存读取失败，无法断言邮箱无新邮件"
        return f"（dry-run）{kind}{extra}：无待发送内容 → 正式运行将发送状态邮件"
    labels = sorted({_display_date(c) for c in entries})
    span = labels[0] if len(labels) == 1 else f"{labels[0]} ~ {labels[-1]}"
    extra = f"；另有 {len(failed)} 封处理失败待重试" if failed else ""
    return (f"（dry-run）{kind}：将合并发送 {len(entries)} 份简报（{span}）"
            f"→ {cfg.imap_user}，不连接 SMTP{extra}")


def state_init(cfg, last_official: str, mark_existing_sent: bool = False,
               confirm: bool = False, force: bool = False) -> str:
    """初始化/迁移正式推送状态（安全优先）。

    规则（对应审查 P1-4）：

    - 已存在 v2 状态时**拒绝覆盖**，除非显式 ``--force``；覆盖前一定先备份。
    - ``--last-official`` 只声明「截止点」，不擅自把历史简报标记为已发送。
    - ``--mark-existing-sent`` 必须与 ``--confirm`` 同时给出，且只标记
      **收件时间 <= 截止点** 的简报；晚于截止点的邮件保持待发送。
    """
    cutoff = _parse_iso(last_official, cfg)
    if cutoff is None:
        raise ValueError(f"无法解析时间：{last_official!r}（示例：2026-09-11 09:27:00+08:00）")

    exists = cfg.ads_state_file.exists()
    if exists and not force:
        prev = load_state(cfg, strict=False)
        raise ValueError(
            f"正式推送状态已存在（{cfg.ads_state_file}，last_official_cutoff="
            f"{prev.get('last_official_cutoff')}，已发送 {len(prev['items'])} 项）。\n"
            "state-init 会覆盖它，因此默认拒绝执行；确实要重建请加 --force（会先备份）。")

    backup = cfg.ads_state_file.with_name(
        f"ads_state.json.bak.{_now(cfg):%Y%m%d%H%M%S}") if exists else None
    state = _fresh_state()
    state["last_official_cutoff"] = cutoff.isoformat(timespec="seconds")

    lines: list[str] = []
    n_marked = n_kept = 0
    if mark_existing_sent:
        if not confirm:
            raise ValueError(
                "--mark-existing-sent 会把历史简报标成「已正式发送」，属于不可逆的状态声明。\n"
                "请先核对下方清单（加 --dry-run 预览不需要确认），确认无误后加 --confirm 执行。")
        for c in sorted(digest_candidates(cfg),
                        key=lambda x: x.get("received_at") or cutoff):
            recv = c.get("received_at")
            if recv is not None and recv > cutoff:
                n_kept += 1
                lines.append(f"  · 保留待发送（收件 {recv:%Y-%m-%d %H:%M} 晚于截止点）："
                             f"{c['source_id']} ← {c['file'].name}")
                continue
            state["items"][c["source_id"]] = {
                "received_at": recv.isoformat(timespec="seconds") if recv else None,
                "digest_file": c["file"].name,
                "official_sent_at": cutoff.isoformat(timespec="seconds"),
                "migrated": True,
            }
            n_marked += 1
            when_txt = f"{recv:%Y-%m-%d %H:%M}" if recv else "收件时间未知"
            lines.append(f"  · 标记已发送（收件 {when_txt}）："
                         f"{c['source_id']} ← {c['file'].name}")
    # 覆盖式初始化必须先落备份（带时间戳，不会被后续覆盖）
    write_json_atomic(cfg.ads_state_file, state, backup=backup)
    msg = [f"状态已初始化：last_official_cutoff={state['last_official_cutoff']}"]
    if backup:
        msg.append(f"原状态已备份：{backup.name}")
    msg.append(f"标记为已发送 {n_marked} 份，保留待发送 {n_kept} 份")
    if lines:
        msg.extend(lines)
    if not mark_existing_sent:
        msg.append("注意：未标记任何历史简报，它们会在下次正式推送时发出"
                   "（如需声明已发送，请用 --mark-existing-sent --confirm）")
    return "\n".join(msg)


__all__ = ["load_state", "save_state", "digest_candidates", "select_official",
           "push_official", "push_test", "preview", "state_init", "fetch_evidence",
           "ads_mail_sources", "MODES", "SCHEMA_VERSION"]
