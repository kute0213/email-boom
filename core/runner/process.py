"""进程管理：调度器守护的启动/探测，Worker 子进程的拉起。"""
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from config import BASE_DIR, Config

PY = sys.executable or "python3"


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def scheduler_running() -> bool:
    """通过独占锁判断调度器是否存活。"""
    lock_path = Config.SCHEDULER_LOCK
    if not lock_path.exists():
        return False
    try:
        with open(lock_path, "w") as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False  # 锁能拿到 → 没人在跑
            except OSError:
                return True  # 锁被持有 → 调度器在跑
    except OSError:
        return False


def _spawn(cmd: list, log_file: Path = None, truncate: bool = False) -> int:
    kwargs = dict(
        cwd=str(BASE_DIR),
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # 与父进程脱离：Web 重启不影响后台
        close_fds=True,
    )
    if log_file is not None:
        mode = "w" if truncate else "a"
        fh = open(log_file, mode, encoding="utf-8")
        kwargs.update(stdout=fh, stderr=subprocess.STDOUT)
    else:
        kwargs.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc = subprocess.Popen(cmd, **kwargs)
    if log_file is not None:
        proc._stdout_fh = fh  # 交给 GC 关闭
    return proc.pid


def ensure_scheduler(wait: float = 1.0) -> bool:
    """确保调度器在运行（未运行则拉起）。返回最终是否在运行。"""
    if scheduler_running():
        return True
    if not Config.AUTO_START_SCHEDULER:
        return False
    Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _spawn([PY, "-m", "core.runner.scheduler"], Config.SCHEDULER_LOG, truncate=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        if scheduler_running():
            return True
        time.sleep(0.1)
    return scheduler_running()


def restart_scheduler() -> bool:
    """重启调度器（先终止旧进程再拉起）。"""
    pid = None
    pid_file = Config.DATA_DIR / "scheduler.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        pid = None
    if pid and pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                if not pid_alive(pid):
                    break
                time.sleep(0.1)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(0.3)
    return ensure_scheduler(wait=3.0)


def spawn_worker(task_id: int) -> int:
    """为任务拉起独立 Worker 进程。"""
    Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = Config.DATA_DIR / f"worker_{task_id}.log"
    return _spawn(
        [PY, "-m", "core.runner.worker", str(task_id)],
        log, truncate=True,
    )
