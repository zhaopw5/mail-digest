"""IMAP 拉信（M0）：增量拉取 + 完整邮件身份。

零第三方依赖：imaplib + email 标准库。

三条不能退让的规则（对应独立审查 P1-2 / P1-3）：

1. **按 UID 增量拉取**，不是「取最后 N 封」。每个文件夹记录
   ``uidvalidity / last_uid / gaps``：只请求 ``UID last_uid+1:*``，
   并把上次失败的 UID 放进 gaps 一起重试；单封失败时游标不越过它。
   首次接管邮箱（无游标）默认**全量**拉取（上限 fetch_initial_max），
   只有用户显式 ``--recent N`` 才截断。
2. **UIDVALIDITY 变化即重置**：服务器重新编号后，旧 UID 不再指向同一封信，
   此时不能拿旧 ``last_uid`` 做增量，也不能让新旧邮件互相冒名。
3. **身份写进文件名**：``20260910_000001_u1.eml``（尾部 ``_u<UIDVALIDITY>``）。
   恢复缓存时从文件名直接得到 ``source_id = 文件夹:UIDVALIDITY:UID``，
   不再用裸 UID 去「查最新的索引」——那正是新邮件被旧简报冒名登记的根因。
"""
from __future__ import annotations

import email
import imaplib
import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

from .models import Mail
from .state import StateCorruptError, load_json_strict, write_json_atomic

INDEX_SCHEMA = 2
IMAP_SCHEMA = 1


def _decode_header(value) -> str:
    """解码 RFC2047 编码的主题/发件人（如 =?utf-8?B?...?=）。"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _decode_payload(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _body_parts(msg: email.message.Message) -> tuple[str, str]:
    """返回 (纯文本正文, HTML 正文原文)，按 MIME walk 顺序取第一个出现的。"""
    text, html = "", ""
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and not text:
            text = _decode_payload(part)
        elif ctype == "text/html" and not html:
            html = _decode_payload(part)
    return text, html


# ---------------- 文件名 ↔ 身份 ----------------

def eml_name(date_tag: str, uid: int, uidvalidity: int | None) -> str:
    """缓存文件名：日期仅用于人眼排序，身份由 ``_u<UIDVALIDITY>`` 承担。"""
    uv = uidvalidity if uidvalidity is not None else 0
    return f"{date_tag}_{uid:06d}_u{uv}.eml"


_EML_RE = re.compile(r"^(?P<tag>\d{8}|nodate)_(?P<uid>\d+)_u(?P<uv>\d+)\.eml$")
_LEGACY_EML_RE = re.compile(r"^(?P<tag>\d{8}|nodate)_(?P<uid>\d+)\.eml$")


def parse_eml_name(name: str) -> tuple[str, int, int | None] | None:
    """→ (date_tag, uid, uidvalidity)；不匹配返回 None。"""
    m = _EML_RE.match(name)
    if m:
        uv = int(m.group("uv"))
        return m.group("tag"), int(m.group("uid")), (uv or None)
    m = _LEGACY_EML_RE.match(name)          # 旧格式（无 UIDVALIDITY）
    if m:
        return m.group("tag"), int(m.group("uid")), None
    return None


# ---------------- 索引（email 文件 → 身份）----------------

def load_index(eml_dir: Path) -> dict:
    """读 sidecar 索引。返回 ``{"emails": {eml_name: {...}}}``（自动升级旧格式）。

    旧格式以裸 UID 为键（``{"1": {...}}``），升级时改用 ``rec["eml"]`` 作为键，
    因此**永远不需要用 UID 反查身份**。
    """
    f = eml_dir / "index.json"
    try:
        raw = load_json_strict(f, {})
    except StateCorruptError:
        # 索引损坏时按文件名重建（新命名自带完整身份，因此重建不会造成错误推送），
        # 但必须显式告知并留下现场备份，不能静默丢弃。
        backup = f.with_name(f.name + ".corrupt.bak")
        try:
            if not backup.exists():
                backup.write_bytes(f.read_bytes())
        except OSError:
            pass
        print(f"⚠️  邮件索引损坏，已备份为 {backup.name} 并按文件名重建：{f}")
        raw = {}
    if not isinstance(raw, dict):
        return {"emails": {}}
    if raw.get("schema_version") == INDEX_SCHEMA and isinstance(raw.get("emails"), dict):
        return {"emails": dict(raw["emails"])}
    emails: dict[str, dict] = {}
    for key, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        name = rec.get("eml") or (key if _EML_RE.match(str(key)) else None)
        if not name:
            continue
        uid = rec.get("uid")
        if uid is None:
            parsed = parse_eml_name(name)
            uid = parsed[1] if parsed else None
        emails[name] = {
            "source_id": rec.get("source_id"),
            "folder": rec.get("folder") or "INBOX",
            "uid": uid,
            "uidvalidity": rec.get("uidvalidity"),
            "received_at": rec.get("received_at"),
        }
    return {"emails": emails}


def save_index(eml_dir: Path, index: dict) -> None:
    eml_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(eml_dir / "index.json",
                      {"schema_version": INDEX_SCHEMA, "emails": index.get("emails", {})})


def migrate_legacy_files(cfg, eml_dir: Path | None = None) -> int:
    """把旧缓存文件 ``20260910_000001.eml`` 升级为带 UIDVALIDITY 的新名。

    幂等：只处理旧格式文件，且只在该 UID 的 UIDVALIDITY 已知时改名。
    返回改名的文件数。
    """
    eml_dir = eml_dir or cfg.eml_dir
    if not eml_dir.exists():
        return 0
    index = load_index(eml_dir)
    by_uid = {rec.get("uid"): rec for rec in index["emails"].values() if rec.get("uid")}
    n = 0
    for p in sorted(eml_dir.glob("*.eml")):
        parsed = parse_eml_name(p.name)
        if not parsed or parsed[2] is not None:
            continue                        # 已是新格式
        _tag, uid, _uv = parsed
        rec = by_uid.get(uid) or {}
        uv = rec.get("uidvalidity")
        if uv is None:
            continue                        # UIDVALIDITY 未知：保持原样，不做推测
        new_name = eml_name(_tag, uid, int(uv))
        target = eml_dir / new_name
        if target.exists():
            continue
        try:
            p.rename(target)
        except OSError:
            continue
        old_rec = index["emails"].pop(p.name, None)
        index["emails"][new_name] = {
            "source_id": f"{rec.get('folder') or cfg.default_folder}:{uv}:{uid}",
            "folder": rec.get("folder") or cfg.default_folder,
            "uid": uid,
            "uidvalidity": int(uv),
            "received_at": (old_rec or rec).get("received_at"),
        }
        n += 1
    if n:
        save_index(eml_dir, index)
        print(f"ℹ️  已把 {n} 个旧缓存邮件文件名升级为带 UIDVALIDITY 的新格式（身份不再依赖裸 UID）")
    return n


def unidentified_uids(eml_dir: Path) -> dict[int, list[Path]]:
    """本地缓存里身份不完整的邮件：文件名没有 UIDVALIDITY（``_u<N>``）后缀。

    这类文件无法证明自己属于哪个 source_id，既不能安全推送，也不该被当成
    「已处理的旧邮件」。处理方式：重新按 UID 拉一次（服务器上还在就能补全身份），
    拉不到则移入 quarantine 目录，避免继续污染扫描与判定。
    """
    out: dict[int, list[Path]] = {}
    if not eml_dir.exists():
        return out
    for p in sorted(eml_dir.glob("*.eml")):
        parsed = parse_eml_name(p.name)
        if parsed and parsed[2] is None and parsed[1] > 0:
            out.setdefault(parsed[1], []).append(p)
    return out


def quarantine_unidentified(eml_dir: Path, uids) -> int:
    """把身份不明的旧缓存移入 emails/_legacy_unidentified/（保留文件，不删除）。"""
    targets = unidentified_uids(eml_dir)
    dest = eml_dir / "_legacy_unidentified"
    n = 0
    for uid in uids:
        for p in targets.get(int(uid), []):
            try:
                dest.mkdir(parents=True, exist_ok=True)
                p.rename(dest / p.name)
                n += 1
            except OSError:
                continue
    return n


# ---------------- 拉取游标 ----------------

def load_imap_state(cfg) -> dict:
    try:
        raw = load_json_strict(cfg.imap_state_file, {})
    except StateCorruptError as exc:
        print(f"⚠️  拉取游标损坏（{exc}）；本次按首次接管处理（全量拉取，不会漏信）")
        return {}
    return raw if isinstance(raw, dict) else {}


def _uidvalidity(conn, folder: str) -> int | None:
    try:
        typ, data = conn.status(folder, "(UIDVALIDITY)")
        if typ == "OK" and data and data[0]:
            m = re.search(rb"UIDVALIDITY\s+(\d+)", data[0])
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def _parse_message(uid: int, folder: str, msg: email.message.Message,
                   uidvalidity: int | None = None,
                   received_at=None, tz=None) -> Mail:
    text, html = _body_parts(msg)
    date = None
    try:
        date = parsedate_to_datetime(msg.get("Date", "")).astimezone(tz)
    except Exception:
        date = None
    headers: dict[str, str] = {}
    for key, val in msg.items():
        k = key.lower()
        if k == "authentication-results":
            # RFC5322 折行 unfold：CRLF + 空格/Tab → 空格（防止续行被误当独立认证结果）
            val = re.sub(r"\r?\n[ \t]+", " ", val)
        if k in headers:
            headers[k] += "\n" + val          # 同名头聚合（认证头可能多个，不能只信第一个）
        else:
            headers[k] = val
    return Mail(
        uid=uid,
        folder=folder,
        message_id=str(msg.get("Message-ID", "")).strip(),
        subject=_decode_header(msg.get("Subject", "")),
        from_=_decode_header(msg.get("From", "")),
        date=date,
        body_text=text,
        body_html=html,
        raw_path=Path(""),
        headers=headers,
        source_id=f"{folder}:{uidvalidity if uidvalidity is not None else '?'}:{uid}",
        uidvalidity=uidvalidity,
        received_at=received_at,
    )


def fetch_recent(cfg, recent: int | None = None, folder: str | None = None,
                 eml_dir: Path | None = None) -> list[Mail]:
    """增量拉取邮箱新邮件并落盘，返回本轮拿到的 Mail 列表。

    - 无游标（首次接管）：``recent`` 为 None → 全量（上限 fetch_initial_max）；
      显式 ``--recent N`` → 只取最近 N 封（会在输出里明确提示可能有更早邮件未纳入）。
    - 有游标：只取 ``last_uid+1:*`` 的增量，外加上次失败的 gaps。
    - UIDVALIDITY 变化：重置游标并重新全量接管，旧记录不做复用。
    - 以只读方式拉信（不改变任何已读/未读状态）；当前只处理 INBOX。
    """
    folder = folder or cfg.default_folder
    eml_dir = eml_dir or cfg.eml_dir
    eml_dir.mkdir(parents=True, exist_ok=True)
    migrate_legacy_files(cfg, eml_dir)
    index = load_index(eml_dir)
    imap_state = load_imap_state(cfg)
    rec = dict(imap_state.get(folder) or {})

    mails: list[Mail] = []
    conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=30)
    try:
        conn.login(cfg.imap_user, cfg.imap_auth_code)
        typ, _ = conn.select(folder, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"无法打开文件夹 {folder!r}: {typ}")

        validity = _uidvalidity(conn, folder)
        prev_validity = rec.get("uidvalidity")
        validity_changed = (prev_validity is not None and validity is not None
                            and int(prev_validity) != int(validity))
        if validity_changed:
            print(f"⚠️  文件夹 {folder!r} 的 UIDVALIDITY 变化（{prev_validity} → {validity}）："
                  "服务器已重新编号，旧 UID 不再可信 → 重置拉取游标并按全量重新接管。")
            rec = {}

        last_uid = rec.get("last_uid")
        uncovered_below = rec.get("uncovered_below")     # 初始接管尚未纳入的邮件里最大的 UID
        gaps = {int(u) for u in (rec.get("gaps") or [])}
        counters = {"initial": 0, "incremental": 0, "gap_retry": 0,
                    "failed": 0, "skipped": 0}
        limit = max(1, int(cfg.fetch_initial_max))

        target: set[int] = set()
        new_last = last_uid

        # (a) 增量：上次游标之后新到的邮件
        if last_uid is not None:
            last_uid = int(last_uid)
            # IMAP 序列集语义：* 在 "UID n:*" 中表示最大 UID，n 已最大时会返回该封，
            # 因此必须显式过滤掉 <= last_uid 的结果。
            typ, data = conn.uid("search", None, f"UID {last_uid + 1}:*")
            if typ != "OK":
                raise RuntimeError(f"IMAP 增量检索失败（UID {last_uid + 1}:* 返回 {typ}）："
                                   "本次未检查邮箱，不能记为成功拉取")
            new_uids = {int(x) for x in (data[0].split() if (data and data[0]) else [])}
            new_uids = {u for u in new_uids if u > last_uid}
            counters["incremental"] = len(new_uids)
            target |= new_uids

        # (b) 初始接管：首次没有游标，或上次因上限截断尚未覆盖更早的邮件。
        #     分批推进，保证「更早的邮件」最终一定会被纳入，而不是被永久跳过。
        if last_uid is None or uncovered_below is not None:
            typ, data = conn.uid("search", None, "ALL")
            if typ != "OK":
                raise RuntimeError(f"IMAP 检索失败（SEARCH ALL 返回 {typ}）："
                                   "本次未检查邮箱，不能记为成功拉取")
            all_uids = sorted(int(x) for x in (data[0].split() if (data and data[0]) else []))
            if all_uids:
                if recent and last_uid is None:
                    window = all_uids[-recent:]
                    target |= set(window)
                    counters["initial"] = len(window)
                    if len(all_uids) > len(window):
                        # 保留未覆盖范围：下次不带 --recent 的 fetch 会自动继续补拉
                        uncovered_below = all_uids[-recent - 1]
                        print(f"⚠️  指定了 --recent {recent}：更早的 {len(all_uids) - len(window)} 封"
                              "本次未纳入，将在后续 fetch（不带 --recent）中自动分批补拉")
                else:
                    pending = [u for u in all_uids
                               if uncovered_below is None or u <= int(uncovered_below)]
                    window = pending[-limit:]
                    target |= set(window)
                    counters["initial"] = len(window)
                    if len(pending) > len(window):
                        uncovered_below = pending[-limit - 1]      # 剩余中最大的 UID
                        print(f"⚠️  邮箱共 {len(all_uids)} 封，本轮按上限纳入 {len(window)} 封；"
                              f"更早的 {len(pending) - len(window)} 封将在后续 fetch 中继续分批补拉"
                              f"（可通过 MAIL_DIGEST_FETCH_INITIAL_MAX 调大批量）")
                    else:
                        uncovered_below = None
                        print(f"ℹ️  首次接管邮箱完成：纳入 {len(window)} 封并建立 UID 游标")
            if last_uid is None and all_uids:
                new_last = max(all_uids)          # 之后的增量从最大 UID 之后开始

        # 缺口永远重试：即使它比游标小（例如旧邮件当时拉取失败），也不能被游标越过，
        # 否则那封邮件就再也没有机会被拉回来。
        gap_uids = set(gaps)
        counters["gap_retry"] = len(gap_uids)
        target |= gap_uids
        # 身份不完整的本地缓存：重拉一次以补全 source_id（拉不到则隔离，不进判定）
        unidentified = unidentified_uids(eml_dir)
        counters["identity_repair"] = len(unidentified)
        target |= set(unidentified)
        if counters["identity_repair"]:
            print(f"ℹ️  发现 {counters['identity_repair']} 封本地缓存缺少 UIDVALIDITY 身份，"
                  "本次重新拉取以补全（补不全的将移入 emails/_legacy_unidentified/）")
        if counters["gap_retry"]:
            print(f"ℹ️  重试上次拉取失败的 {counters['gap_retry']} 封邮件"
                  f"（UID {sorted(gap_uids)[:10]}{'…' if len(gap_uids) > 10 else ''}）")

        failed: set[int] = set()
        skipped: set[int] = set()
        for uid in sorted(set(target)):
            typ, msg_data = conn.uid("fetch", str(uid), "(INTERNALDATE RFC822)")
            if typ != "OK":
                failed.add(uid)
                counters["failed"] += 1
                print(f"  ⚠️  UID {uid} 拉取失败（{typ}）：保留在待重试队列，游标不越过它")
                continue
            if not msg_data or msg_data[0] is None:
                counters["skipped"] += 1
                skipped.add(uid)                # 服务器上已不存在（被删/移动），不必再重试
                continue
            meta = msg_data[0][0] or b""
            raw = msg_data[0][1]
            recv = None
            _mi = re.search(rb'INTERNALDATE "([^"]+)"', meta)
            if _mi:
                try:
                    recv = parsedate_to_datetime(_mi.group(1).decode())
                except Exception:
                    recv = None
            msg = email.message_from_bytes(raw)
            mail = _parse_message(uid, folder, msg, uidvalidity=validity,
                                  received_at=recv, tz=cfg.tz())
            tag = f"{mail.date:%Y%m%d}" if mail.date else "nodate"
            name = eml_name(tag, uid, validity)
            raw_path = eml_dir / name
            if not raw_path.exists():           # 不覆盖已有缓存（保留首次落盘内容）
                raw_path.write_bytes(raw)
            mail.raw_path = raw_path
            mails.append(mail)
            index["emails"][name] = {
                "source_id": mail.source_id,
                "folder": folder,
                "uid": uid,
                "uidvalidity": validity,
                "received_at": recv.isoformat() if recv else None,
            }

        # 游标推进：只越过确认拿到的 UID；失败项留在 gaps
        ok_uids = {m.uid for m in mails}
        base = int(new_last) if new_last is not None else 0
        progressed = max(ok_uids | {base})
        remaining_gaps = sorted((gaps - ok_uids - skipped) | failed)
        if unidentified:
            # 一律隔离旧的无身份文件：身份补全成功时它已被新文件取代（内容相同），
            # 否则它会与新文件重复出现，导致同一封邮件被处理两次。
            moved = quarantine_unidentified(eml_dir, set(unidentified))
            if moved:
                print(f"ℹ️  已隔离 {moved} 个旧缓存文件 → {eml_dir / '_legacy_unidentified'}"
                      "（身份已由新的带 UIDVALIDITY 文件取代；确认无误后可自行删除该目录）")
        complete = not remaining_gaps and uncovered_below is None
        imap_state[folder] = {
            "uidvalidity": validity,
            "last_uid": progressed,
            "uncovered_below": uncovered_below,
            "gaps": remaining_gaps,
            "last_fetch_at": _now_iso(cfg),
            "last_fetch_ok": complete,        # 只有完整成功才允许断言「邮箱没有新邮件」
            "last_fetch_counts": counters,
        }
        save_index(eml_dir, index)
        write_json_atomic(cfg.imap_state_file, imap_state)
        if remaining_gaps:
            print(f"⚠️  仍有 {len(remaining_gaps)} 封邮件待重试（UID {remaining_gaps[:10]}"
                  f"{'…' if len(remaining_gaps) > 10 else ''}），下次 fetch 自动重试")
    except Exception as exc:
        # 检索/拉取过程本身失败：明确记为「本次没有成功检查邮箱」，
        # 不更新时间戳（否则推送阶段会把"没检查"当成"没有新邮件"）。
        rec_fail = dict(rec) if isinstance(rec, dict) else {}
        rec_fail.update({"last_fetch_ok": False, "last_fetch_error": str(exc)[:300]})
        if validity is not None:
            rec_fail["uidvalidity"] = validity
        imap_state[folder] = rec_fail
        try:
            write_json_atomic(cfg.imap_state_file, imap_state)
        except OSError:
            pass
        raise
    finally:
        conn.logout()
    return mails


def _now_iso(cfg) -> str:
    from datetime import datetime
    return datetime.now(cfg.tz()).isoformat(timespec="seconds")


def load_mails_from_dir(eml_dir: Path, folder: str | None = None,
                        tz=None) -> list[Mail]:
    """从 data/emails 读回缓存邮件（离线处理用）。

    身份来自**文件名**（``..._u<UIDVALIDITY>.eml``）+ sidecar 索引按文件名精确匹配，
    不再用裸 UID 反查，因此不会把新邮件误认成旧邮件。
    无索引信息时 source_id 用 ``文件夹:?:UID``，绝不会被当成已知的完整身份。
    """
    from datetime import datetime as _dt
    folder = folder or "INBOX"
    index = load_index(eml_dir)
    mails: list[Mail] = []
    for p in sorted(eml_dir.glob("*.eml")):
        try:
            msg = email.message_from_bytes(p.read_bytes())
        except OSError:
            continue
        parsed = parse_eml_name(p.name)
        if not parsed:
            continue
        _tag, uid, uv = parsed
        rec = index["emails"].get(p.name) or {}
        if uv is None:
            uv = rec.get("uidvalidity")
        mail = _parse_message(uid, folder, msg, uidvalidity=uv, tz=tz)
        mail.raw_path = p
        sid = rec.get("source_id")
        if sid:
            mail.source_id = sid
        elif uv is not None:
            mail.source_id = f"{rec.get('folder') or folder}:{uv}:{uid}"
        ra = rec.get("received_at")
        if ra:
            try:
                mail.received_at = _dt.fromisoformat(ra)
            except Exception:
                pass
        if mail.received_at is None:          # 兜底：.eml 落盘时间
            try:
                mail.received_at = _dt.fromtimestamp(p.stat().st_mtime).astimezone(tz)
            except Exception:
                pass
        mails.append(mail)
    return mails


__all__ = [
    "fetch_recent", "load_mails_from_dir", "load_index", "save_index",
    "eml_name", "parse_eml_name", "migrate_legacy_files", "load_imap_state",
    "INDEX_SCHEMA",
]
