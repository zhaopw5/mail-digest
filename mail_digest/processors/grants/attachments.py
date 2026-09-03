"""附件提取与递归解压（场景二核心）。

安全要点（README 工程要点）：
  - 附件文件名清洗（去路径分隔符/非法字符，拒绝绝对路径、限长）
  - 单附件大小上限（超出记录并跳过，不静默失败）
  - 压缩包解压防路径穿越（拒绝 .. /绝对路径/symlink/硬链接/设备条目）
  - 压缩炸弹防护：ZIP/TAR 事前逐条预算总量；7z 事前按成员预算；
    RAR 子进程写入限额（RLIMIT_FSIZE）；全部格式解压后统一总量/文件数复核
  - 嵌套解压层数上限
"""
from __future__ import annotations

import email
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from email.header import decode_header, make_header
from pathlib import Path

from ...core.models import Mail

# 默认限制
MAX_ATTACH_SIZE = 30 * 1024 * 1024      # 单附件 30 MB
MAX_EXTRACT_TOTAL = 200 * 1024 * 1024   # 单个归档解压总量 200 MB
MAX_FILES = 2000                        # 单个归档解压文件数上限（防 inode 耗尽）
MAX_DEPTH = 3                           # 嵌套解压层数

_SAFE_NAME_RE = re.compile(r"[\\/:*?\"<>|\r\n]+")
_UNSUPPORTED_ARCHIVE = (".rar", ".wps")
_ARCHIVE_SUFFIXES = (".zip", ".tar", ".gz", ".tgz", ".7z", ".rar")


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


def _harden(dest: Path) -> int:
    """解压后清理：删除符号链接、硬链接及逃逸出 dest 的文件（安全兜底）。

    即使 zip/tar 校验漏过、或 unrar/7z 产生了链接条目，也不让后续代码
    跟随链接读取/写入 dest 之外（如 .env、.bashrc）。返回删除数。
    """
    removed = 0
    for p in dest.rglob("*"):
        try:
            if p.is_symlink() or (p.is_file() and p.stat().st_nlink > 1):
                p.unlink(missing_ok=True)
                removed += 1
                continue
            if not p.resolve().is_relative_to(dest.resolve()):
                p.unlink(missing_ok=True)
                removed += 1
        except OSError:
            try:
                p.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return removed


def _verify_output(dest: Path) -> bool:
    """压缩炸弹复核：解压产物总字节或文件数超限 → True（调用方应整体丢弃）。"""
    total = 0
    count = 0
    for p in dest.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        count += 1
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total > MAX_EXTRACT_TOTAL or count > MAX_FILES


def _run_subprocess_limited(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    """运行外部解压命令，子进程写入总量受 RLIMIT_FSIZE 限制（压缩炸弹防线）。"""
    preexec = None
    if sys.platform.startswith("linux"):
        def _limit() -> None:
            import resource
            resource.setrlimit(
                resource.RLIMIT_FSIZE, (MAX_EXTRACT_TOTAL, MAX_EXTRACT_TOTAL))
        preexec = _limit
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, preexec_fn=preexec)


def _zip_safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    total = 0
    count = 0
    for info in zf.infolist():
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in Path(name).parts:
            raise AttachmentError(f"压缩包含危险路径条目，已拒绝: {name[:60]}")
        total += info.file_size
        count += 1
        if total > MAX_EXTRACT_TOTAL:
            raise AttachmentError("解压总量超过上限，已中止")
        if count > MAX_FILES:
            raise AttachmentError("解压文件数超过上限，已中止")
        if info.is_dir():
            continue
        out = (dest / name).resolve()
        if not str(out).startswith(str(dest.resolve())):
            raise AttachmentError(f"压缩包条目逃逸目录: {name[:60]}")
        out.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _extract_one(path: Path, dest: Path) -> list[Path]:
    """解压单个归档到 dest，返回解出的文件列表。

    安全防线（全格式统一）：
      1) 路径穿越/链接条目：zip/tar 手动校验；7z 预检 is_symlink；
      2) 压缩炸弹：zip/tar 逐条预算；7z 按成员 uncompressed 预算；
         rar（外部 unrar/7z）子进程 RLIMIT_FSIZE 限写；
      3) 收尾：_harden 清理链接/越界文件 + _verify_output 总量/文件数复核，
         超限则整体丢弃。
    """
    suffix = path.suffix.lower()
    extracted: list[Path] = []
    try:
        if suffix in (".zip",):
            with zipfile.ZipFile(path) as zf:
                _zip_safe_extract(zf, dest)
            extracted = [p for p in dest.rglob("*") if p.is_file()]
        elif suffix in (".tar", ".gz", ".tgz"):
            # 手动逐成员安全提取：拒绝链接/设备条目与路径穿越，不用 extractall
            with tarfile.open(path) as tf:
                total = 0
                count = 0
                for member in tf.getmembers():
                    if member.issym() or member.islnk() or member.isdev():
                        raise AttachmentError(
                            f"压缩包含链接/设备条目，已拒绝: {member.name[:60]}")
                    name = member.name
                    if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                        raise AttachmentError(f"压缩包含危险路径条目，已拒绝: {name[:60]}")
                    total += member.size
                    count += 1
                    if total > MAX_EXTRACT_TOTAL:
                        raise AttachmentError("解压总量超过上限，已中止")
                    if count > MAX_FILES:
                        raise AttachmentError("解压文件数超过上限，已中止")
                    out = (dest / name).resolve()
                    if not str(out).startswith(str(dest.resolve())):
                        raise AttachmentError(f"压缩包条目逃逸目录: {name[:60]}")
                    if member.isdir():
                        out.mkdir(parents=True, exist_ok=True)
                        continue
                    out.parent.mkdir(parents=True, exist_ok=True)
                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    with src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst)
            extracted = [p for p in dest.rglob("*") if p.is_file()]
        elif suffix == ".7z":
            try:
                import py7zr
            except ImportError:
                raise AttachmentError("未安装 py7zr，无法解压 .7z")
            with py7zr.SevenZipFile(path) as sz:
                # 事前预算 + symlink 预检（防 7z 炸弹/链接条目）
                files = sz.list()
                if len(files) > MAX_FILES:
                    raise AttachmentError("7z 解压文件数超过上限，已中止")
                budget = 0
                for fi in files:
                    if getattr(fi, "is_symlink", False):
                        raise AttachmentError(f"7z 包含链接条目，已拒绝: {fi.filename[:60]}")
                    budget += int(getattr(fi, "uncompressed", 0) or 0)
                    if budget > MAX_EXTRACT_TOTAL:
                        raise AttachmentError("7z 解压总量超过上限，已中止（疑似压缩炸弹）")
                sz.extractall(dest)
            extracted = [p for p in dest.rglob("*") if p.is_file()]
        elif suffix == ".rar":
            # 优先 RARLAB unrar（完整 RAR5 支持）；否则退回系统 7-Zip
            unrar = shutil.which("unrar") or shutil.which("unar")
            if unrar:
                sub = _run_subprocess_limited(
                    [unrar, "x", "-o+", str(path),
                     f"{dest}{'/' if not str(dest).endswith('/') else ''}"])
                if sub.returncode != 0:
                    raise AttachmentError(f"unrar 解压失败: {sub.stderr[:120] or '未知错误'}")
            else:
                sevenzip = shutil.which("7z") or shutil.which("7za")
                if not sevenzip:
                    raise AttachmentError("未安装 unrar/7-Zip，无法解压 .rar，需人工解压查看")
                sub = _run_subprocess_limited(
                    [sevenzip, "x", "-y", f"-o{dest}", str(path)])
                if sub.returncode != 0:
                    raise AttachmentError(
                        f"7z 解压 .rar 失败（可能 RAR5 新方法或超写入限额）: {sub.stderr[:120]}")
            extracted = [p for p in dest.rglob("*") if p.is_file()]
        else:
            raise AttachmentError(f"{suffix} 格式需 7-Zip/对应工具，当前环境未安装，需人工解压查看")
    finally:
        # 收尾防线：清理链接/越界文件；总量/文件数复核，超限整体丢弃（压缩炸弹）
        removed = _harden(dest)
        if removed:
            raise AttachmentError(f"解压产物中发现并清除了 {removed} 个危险条目（链接/越界），已丢弃")
        if _verify_output(dest):
            shutil.rmtree(dest, ignore_errors=True)
            raise AttachmentError("解压产物超过安全上限（总量/文件数），已整体丢弃（疑似压缩炸弹）")
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
        if p.suffix.lower() in _ARCHIVE_SUFFIXES:
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
                # 部分失败（如 rar 内个别文件名超长）时，已解出的文件仍保留可用
                partial = [q for q in sub.rglob("*") if q.is_file()]
                readable.extend(partial)
        else:
            readable.append(p)
    return readable, problems
