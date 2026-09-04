"""IMAP 拉信（M0）。

零第三方依赖：imaplib + email 标准库。
腾讯企业邮箱：imap.exmail.qq.com:993（SSL），用户名 = 完整邮箱地址，
登录凭据 = 16 位授权码（不是登录密码）。
"""
from __future__ import annotations

import email
import imaplib
import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

from .models import Mail


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


def _parse_message(uid: int, folder: str, msg: email.message.Message) -> Mail:
    text, html = _body_parts(msg)
    date = None
    try:
        date = parsedate_to_datetime(msg.get("Date", "")).astimezone()
    except Exception:
        date = None
    headers: dict[str, str] = {}
    for key, val in msg.items():
        k = key.lower()
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
    )


def _eml_name(mail: Mail, uid: int) -> str:
    if mail.date:
        return f"{mail.date:%Y%m%d}_{uid:06d}.eml"
    return f"nodate_{uid:06d}.eml"


def fetch_recent(cfg, recent: int, folder: str = "INBOX",
                 eml_dir: Path | None = None) -> list[Mail]:
    """连 IMAP 拉最近 recent 封邮件，逐封落盘为 .eml，返回 Mail 列表。

    以只读方式拉信（不改变任何已读/未读状态）。当前只处理 INBOX；
    中文文件夹名需要 IMAP UTF-7 编码，后续需要时再扩展。
    """
    eml_dir = eml_dir or cfg.eml_dir
    eml_dir.mkdir(parents=True, exist_ok=True)

    mails: list[Mail] = []
    conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=30)
    try:
        conn.login(cfg.imap_user, cfg.imap_auth_code)
        typ, _ = conn.select(folder, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"无法打开文件夹 {folder!r}: {typ}")

        # 用 UID 系列命令（非序号）：UID 在文件夹内稳定，删信不影响，幂等可靠。
        # 用标准 STATUS 命令取 UIDVALIDITY（imaplib 内部响应容器是私有属性，不可依赖）
        validity = None
        try:
            typ_s, data_s = conn.status(folder, "(UIDVALIDITY)")
            if typ_s == "OK" and data_s and data_s[0]:
                m = re.search(rb"UIDVALIDITY\s+(\d+)", data_s[0])
                if m:
                    validity = int(m.group(1))
        except Exception:
            validity = None

        typ, data = conn.uid("search", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return mails

        all_uids = data[0].split()
        recent_uids = all_uids[-recent:] if recent else all_uids
        for uid_b in recent_uids:
            uid = int(uid_b)
            typ, msg_data = conn.uid("fetch", str(uid), "(RFC822)")
            if typ != "OK" or not msg_data or msg_data[0] is None:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            mail = _parse_message(uid, folder, msg)
            raw_path = eml_dir / _eml_name(mail, uid)
            raw_path.write_bytes(raw)
            mail.raw_path = raw_path
            mails.append(mail)
        if validity is not None:
            try:
                vf = cfg.data_dir / "imap_uidvalidity.json"
                vf.parent.mkdir(parents=True, exist_ok=True)
                import json as _json
                rec = {}
                if vf.exists():
                    try:
                        rec = _json.loads(vf.read_text(encoding="utf-8"))
                    except Exception:
                        rec = {}
                prev = rec.get(folder)
                if prev is not None and prev != validity:
                    print(f"⚠️  文件夹 {folder!r} 的 UIDVALIDITY 变化（{prev} → {validity}），"
                          "历史处理记录可能失效，建议核对")
                rec[folder] = validity
                vf.write_text(_json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass  # 记录失败不影响拉信
    finally:
        conn.logout()
    return mails


def load_mails_from_dir(eml_dir: Path, folder: str = "INBOX") -> list[Mail]:
    """从 data/emails 目录读回 .eml（ads 子命令用，避免重复连服务器）。

    文件名形如 `20250115_000123.eml`，uid 取最后一段数字。
    """
    mails: list[Mail] = []
    for p in sorted(eml_dir.glob("*.eml")):
        try:
            msg = email.message_from_bytes(p.read_bytes())
        except OSError:
            continue
        stem = p.stem
        uid = 0
        if "_" in stem and stem.split("_")[-1].isdigit():
            uid = int(stem.split("_")[-1])
        mail = _parse_message(uid, folder, msg)
        mail.raw_path = p
        mails.append(mail)
    return mails
