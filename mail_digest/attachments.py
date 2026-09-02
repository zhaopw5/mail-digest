"""附件提取与递归解压（场景二核心）。

安全要点（README 工程要点）：
  - 附件文件名清洗（去路径分隔符/非法字符，拒绝绝对路径）
  - 单附件大小上限（超出记录并跳过，不静默失败）
  - 压缩包解压防路径穿越（拒绝 .. 与绝对路径条目）、总量上限、嵌套深度上限
"""
from __future__ import annotations

import email
import re
import shutil
import tarfile
import zipfile
from email.header import decode_header, make_header
from pathlib import Path

from .models import Mail

# 默认限制
MAX_ATTACH_SIZE = 30 * 1024 * 1024      # 单附件 30 MB
MAX_EXTRACT_TOTAL = 200 * 1024 * 1024   # 解压总量 200 MB
MAX_DEPTH = 3                           # 嵌套解压层数

_SAFE_NAME_RE = re.compile(r"[\\/:*?\"<>|\r\n]+")
_UNSUPPORTED_ARCHIVE = (".rar", ".wps")


class AttachmentError(Exception):
    """附件处理失败（超出限制/损坏等），message 供直接展示。"""


def _decode_name(raw: str) -> str:
    """解码 RFC2047/长文件名编码（=?UTF-8?B?...?=）。"""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def _safe_name(raw: str) -> str:
    """清洗附件名：去路径/非法字符、解码、限长（防目录穿越与超长文件名）。"""
    name = _decode_name(raw).replace("\\", "/").split("/")[-1]
    name = _SAFE_NAME_RE.sub("_", name).strip(" .") or "unnamed"
    p = Path(name)
    stem, ext = p.stem[:60], p.suffix[:12]   # 主体限 60 字符，防 Errno 36
    return (stem + ext) or "unnamed"


def _attachments_from_msg(msg: email.message.Message):
    """产出 (原始文件名, payload, content_type)，跳过正文部件。"""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        fn = part.get_filename()
        if not fn:
            continue
        yield _safe_name(fn), part.get_payload(decode=True), part.get_content_type()


def extract_attachments(mail: Mail, dest: Path,
                        max_size: int = MAX_ATTACH_SIZE) -> list[dict]:
    """从邮件落盘的 .eml 提取附件到 dest，返回清单。

    返回每项: {name, path, size, ok, error}
    ok=False 且 error 非空表示未能安全提取（大小超限/损坏）。
    """
    dest.mkdir(parents=True, exist_ok=True)
    msg = email.message_from_bytes(mail.raw_path.read_bytes())
    results: list[dict] = []
    for fname, payload, ctype in _attachments_from_msg(msg):
        item = {"name": fname, "path": None, "size": len(payload or b""), "ok": True, "error": ""}
        if payload is None:
            item["ok"] = False
            item["error"] = "附件体为空"
            results.append(item)
            continue
        if len(payload) > max_size:
            item["ok"] = False
            item["error"] = f"附件 {item['size']//1024//1024}MB 超过 {max_size//1024//1024}MB 上限，跳过（可手动查看）"
            results.append(item)
            continue
        try:
            target = dest / fname
            target.write_bytes(payload)
            item["path"] = target
        except OSError as exc:
            item["ok"] = False
            item["error"] = f"写入失败: {exc}"
        results.append(item)
    return results


def _zip_safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    total = 0
    for info in zf.infolist():
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in Path(name).parts:
            raise AttachmentError(f"压缩包含危险路径条目，已拒绝: {name[:60]}")
        total += info.file_size
        if total > MAX_EXTRACT_TOTAL:
            raise AttachmentError("解压总量超过上限，已中止")
        if info.is_dir():
            continue
        out = (dest / name).resolve()
        if not str(out).startswith(str(dest.resolve())):
            raise AttachmentError(f"压缩包条目逃逸目录: {name[:60]}")
        out.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _extract_one(path: Path, dest: Path) -> list[Path]:
    """解压单个归档到 dest，返回解出的文件列表；不支持的类型返回空并在 error 注明。"""
    suffix = path.suffix.lower()
    extracted: list[Path] = []
    if suffix in (".zip",):
        with zipfile.ZipFile(path) as zf:
            _zip_safe_extract(zf, dest)
        extracted = [p for p in dest.rglob("*") if p.is_file()]
    elif suffix in (".tar", ".gz", ".tgz"):
        with tarfile.open(path) as tf:
            for member in tf.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise AttachmentError(f"压缩包含危险路径条目，已拒绝: {member.name[:60]}")
            tf.extractall(dest)
        extracted = [p for p in dest.rglob("*") if p.is_file()]
    elif suffix == ".7z":
        try:
            import py7zr
            with py7zr.SevenZipFile(path) as sz:
                sz.extractall(dest)
            extracted = [p for p in dest.rglob("*") if p.is_file()]
        except ImportError:
            raise AttachmentError("未安装 py7zr，无法解压 .7z")
    else:
        raise AttachmentError(f"{suffix} 格式需 7-Zip/对应工具，当前环境未安装，需人工解压查看")
    return extracted


def unpack_recursive(paths: list[Path], work: Path, depth: int = 0) -> tuple[list[Path], list[dict]]:
    """递归展开归档：返回 (可读文件列表, 问题报告)。

    work 为本次工作目录；嵌套归档在子目录展开。
    """
    if depth > MAX_DEPTH:
        return [], [{"error": f"嵌套解压超过 {MAX_DEPTH} 层，深层内容需人工查看"}]
    readable: list[Path] = []
    problems: list[dict] = []
    for p in paths:
        if p.suffix.lower() in (".zip", ".tar", ".gz", ".tgz", ".7z"):
            sub = work / f"unpack_{p.stem[:20]}_{depth}"
            sub.mkdir(parents=True, exist_ok=True)
            try:
                files = _extract_one(p, sub)
                # 归档里可能还有归档 → 递归
                r, pr = unpack_recursive(files, work / f"sub_{depth}", depth + 1)
                readable.extend(r)
                problems.extend(pr)
            except (AttachmentError, zipfile.BadZipFile, tarfile.TarError) as exc:
                problems.append({"file": p.name, "error": str(exc)})
        else:
            readable.append(p)
    return readable, problems
