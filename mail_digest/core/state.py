"""状态文件读写基础设施：原子写入、损坏检测、进程互斥锁。

设计要点（对应审查结论 P2「状态非原子保存、损坏静默重置、无并发互斥」）：

- **原子写**：先写同目录临时文件并 fsync，再 ``os.replace`` 覆盖目标。
  任何时刻磁盘上的状态文件要么是旧的完整内容，要么是新的完整内容，
  不会出现被截断的半截 JSON。
- **损坏即中止**：`load_json_strict` 在文件存在但无法解析时抛出
  `StateCorruptError`，由调用方决定中止（正式推送绝不在状态可疑时发信）；
  只有文件**不存在**才返回默认值。
- **文件锁**：`FileLock` 基于 `O_CREAT|O_EXCL` 写 pid，用于保证同一时刻
  只有一个正式推送进程在跑；持有者崩溃留下的陈旧锁超过 TTL 会被回收。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class StateCorruptError(RuntimeError):
    """状态文件存在但内容无法解析（不能当作空状态继续跑）。"""


class LockBusyError(RuntimeError):
    """另一个同类任务正在运行。"""


def write_json_atomic(path: Path, obj, *, backup: Path | None = None) -> None:
    """原子写入 JSON；backup 非空时先把现有内容复制一份。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup is not None and path.exists():
        try:
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(path.read_bytes())
        except OSError:
            pass
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    data = json.dumps(obj, ensure_ascii=False, indent=2)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)          # 同目录 rename：POSIX 原子
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def load_json_strict(path: Path, default):
    """读 JSON：文件不存在返回 default；存在但损坏抛 StateCorruptError。"""
    if not path.exists():
        return default
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise StateCorruptError(f"状态文件为空：{path}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateCorruptError(f"状态文件损坏无法解析：{path}（{exc}）") from exc


class FileLock:
    """跨进程互斥锁（context manager）。

    用法::

        with FileLock(cfg.data_dir / ".ads_official.lock"):
            ...发送与状态推进...

    获取失败抛 `LockBusyError`；持有者超过 stale_after 秒未释放（例如被 kill -9）
    时视为陈旧锁并接管，避免永久卡死。
    """

    def __init__(self, path: Path, stale_after: float = 1800.0) -> None:
        self.path = path
        self.stale_after = stale_after
        self._fd: int | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if self._is_stale():
                try:
                    self.path.unlink()
                except OSError:
                    pass
                return self.__enter__()
            raise LockBusyError(
                f"另一个 ADS 正式推送正在进行（锁文件 {self.path}）。"
                "如确认没有进程在跑，可删除该锁文件后重试。")
        os.write(self._fd, f"{os.getpid()} {time.time():.0f}\n".encode())
        os.fsync(self._fd)
        return self

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        return age > self.stale_after

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            self.path.unlink()
        except OSError:
            pass
