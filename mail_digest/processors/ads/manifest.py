"""ADS 逐封处理状态（manifest）：把「处理成功」与「处理失败」严格分开。

对应独立审查 P1-1：旧实现只要写出英文简报就把 UID 记进 ``processed.json``，
因此 ADS API 或 LLM 失败后，下次运行直接跳过该邮件——"下次自动重试"是空话。

状态取值：

- ``ready``             ：元数据齐全（配了 LLM 时中文也齐全）→ 允许进入正式推送
- ``empty``             ：该邮件确实没有可提取文献（不是失败）→ 视为已处理完毕
- ``retryable_error``   ：ADS API / LLM / 解析失败，或只完成了一部分
                          → **不写入已处理**，下次 ``ads run`` 自动重试

键是完整 ``source_id``（``文件夹:UIDVALIDITY:UID``），不是裸 UID：
UIDVALIDITY 变化后 UID 会重复使用，用 UID 做键会让新邮件被旧记录冒名跳过。
"""
from __future__ import annotations

from datetime import datetime

from ...core.state import StateCorruptError, load_json_strict, write_json_atomic

SCHEMA_VERSION = 1
DONE_STATUSES = ("ready", "empty")      # 视为「已处理完毕」，不再重跑


def load_manifest(cfg) -> dict:
    """读处理状态；文件缺失时自动从旧的 processed.json + 简报文件推导一次。"""
    path = cfg.ads_manifest_file
    try:
        raw = load_json_strict(path, None)
    except StateCorruptError as exc:
        raise StateCorruptError(
            f"ADS 处理状态文件损坏：{exc}。为避免重复发送或漏推，请先人工处理后删除该文件。"
        ) from exc
    if raw is None:
        mf = {"schema_version": SCHEMA_VERSION, "items": {}}
        migrated = bootstrap_from_legacy(cfg, mf)
        if migrated:
            save_manifest(cfg, mf)
        return mf
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        mf = {"schema_version": SCHEMA_VERSION, "items": {}}
        bootstrap_from_legacy(cfg, mf)
        return mf
    raw.setdefault("items", {})
    return raw


def save_manifest(cfg, mf: dict) -> None:
    write_json_atomic(cfg.ads_manifest_file, mf)


def upsert(cfg, mf: dict, source_id: str, **fields) -> None:
    item = mf["items"].setdefault(source_id, {})
    item.update(fields)
    item["updated_at"] = datetime.now(cfg.tz()).isoformat(timespec="seconds")


def done_source_ids(mf: dict) -> set[str]:
    """处理完毕（无需重跑）的 source_id 集合。"""
    return {sid for sid, it in mf.get("items", {}).items()
            if it.get("status") in DONE_STATUSES}


def failed_source_ids(mf: dict) -> set[str]:
    return {sid for sid, it in mf.get("items", {}).items()
            if it.get("status") == "retryable_error"}


def bootstrap_from_legacy(cfg, mf: dict) -> int:
    """一次性迁移：旧的 processed.json（裸 UID）→ manifest（完整 source_id）。

    只有能同时证明「该邮件在本地有完整身份」且「对应简报文件确实存在」的条目
    才会被认定为 ready；证不出来的条目**不标记**，留给下一次 ``ads run`` 重跑
    （宁可重跑一次，也不要把暂时失败当成已完成）。
    """
    from ...core.imap_client import load_index, parse_eml_name
    from ...core.ops import _load_processed

    processed = _load_processed(cfg.processed_file)
    if not processed:
        return 0
    index = load_index(cfg.eml_dir)
    by_uid: dict[int, dict] = {}
    for rec in index["emails"].values():
        if rec.get("uid") is not None:
            by_uid[int(rec["uid"])] = rec

    n = 0
    for entry in processed:
        uid: int | None = None
        rec: dict = {}
        if isinstance(entry, int):
            uid = entry
            rec = by_uid.get(uid) or {}
            sid = rec.get("source_id")
            if not sid:
                continue                    # 身份不完整：不迁移，等重跑
        else:
            sid = str(entry)
            parts = sid.split(":")
            if len(parts) == 3 and parts[2].isdigit():
                uid = int(parts[2])
        if not sid:
            continue
        tag = None
        name = rec.get("eml")
        if name:
            parsed = parse_eml_name(name)
            tag = parsed[0] if parsed else None
        en = zh = None
        if uid is not None:
            for cand_tag in ([tag] if tag else []) + ["*"]:
                pats = ([f"ads_{cand_tag}_{uid:06d}.md"] if cand_tag != "*"
                        else [f"ads_*_{uid:06d}.md"])
                for pat in pats:
                    hits = sorted(cfg.digest_dir.glob(pat))
                    if hits:
                        en = hits[-1].name
                        break
                if en:
                    break
            zh_hits = sorted(cfg.zh_digest_dir.glob(f"ads_*_{uid:06d}.zh.md"))
            if zh_hits:
                zh = zh_hits[-1].name
        if not en and not zh:
            continue                        # 没有简报 → 属于失败，留给重跑
        mf["items"][sid] = {
            "status": "ready",
            "received_at": rec.get("received_at"),
            "en_file": en,
            "zh_file": zh,
            "errors": [],
            "migrated_from": "processed.json",
            "updated_at": datetime.now(cfg.tz()).isoformat(timespec="seconds"),
        }
        n += 1
    if n:
        print(f"ℹ️  已从旧的 processed.json 迁移 {n} 封邮件的处理状态到 ads_manifest.json"
              "（仅迁移能证明简报确实存在的条目）")
    return n


def write_processed_compat(cfg, mf: dict) -> None:
    """同步写一份 processed.json（内容为完整 source_id），仅作向后兼容展示。"""
    from ...core.ops import _save_processed
    _save_processed(cfg.processed_file, sorted(done_source_ids(mf)))


def manifest_digest_files(cfg) -> dict:
    """{source_id: {"zh_file": Path|None, "en_file": Path|None, "received_at": str|None}}"""
    mf = load_manifest(cfg)
    out: dict[str, dict] = {}
    for sid, it in mf.get("items", {}).items():
        if it.get("status") != "ready":
            continue
        zh = it.get("zh_file")
        en = it.get("en_file")
        out[sid] = {
            "zh_file": (cfg.zh_digest_dir / zh) if zh and (cfg.zh_digest_dir / zh).exists() else None,
            "en_file": (cfg.digest_dir / en) if en and (cfg.digest_dir / en).exists() else None,
            "received_at": it.get("received_at"),
        }
    return out


__all__ = ["load_manifest", "save_manifest", "upsert", "done_source_ids",
           "failed_source_ids", "bootstrap_from_legacy", "write_processed_compat",
           "manifest_digest_files", "DONE_STATUSES", "SCHEMA_VERSION"]
