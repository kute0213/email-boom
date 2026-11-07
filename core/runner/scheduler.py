"""调度器守护进程：派发到期任务、崩溃恢复、回收 Worker。

单实例运行（文件锁），由 Web 启动时自动拉起，也可手动运行：
    python -m core.runner.scheduler
"""
import fcntl
import logging
import os
import signal
import sys
import time
from datetime import datetime

from config import Config
from .. import db
from ..models import log as log_model
from ..models import task as task_model
from .process import pid_alive, spawn_worker

log = logging.getLogger("core.scheduler")


def setup_logging():
    Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(Config.SCHEDULER_LOG, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ---------- 崩溃恢复 ----------

def recover_stale_tasks():
    """status=running 但 Worker 已死/心跳超时 → 恢复或判失败。"""
    now = datetime.now()
    rows = db.query_all("SELECT * FROM tasks WHERE status='running'")
    for row in rows:
        pid = row["worker_pid"] or 0
        hb = row["heartbeat_at"] or ""
        stale = True
        if hb:
            try:
                age = (now - datetime.strptime(hb, "%Y-%m-%d %H:%M:%S")).total_seconds()
                stale = age > Config.HEARTBEAT_TIMEOUT_SECONDS
            except ValueError:
                stale = True
        if pid_alive(pid) and not stale:
            continue

        recoveries = (row["recoveries"] or 0) + 1
        tid = row["id"]
        if recoveries > Config.MAX_CRASH_RECOVERIES:
            task_model.set_fields(
                tid, status="failed", run_active=0, worker_pid=0, heartbeat_at=None,
                recoveries=recoveries, last_error="Worker 反复崩溃，已停止恢复",
            )
            log_model.add(tid, f"Worker 反复崩溃（{recoveries} 次），任务判定失败", "error")
            log.warning("task %s crashed too often -> failed", tid)
            continue

        if row["control"] == "pause":
            new_status = "paused"
        elif row["control"] == "stop":
            new_status = "stopped"
        else:
            new_status = "pending"
        task_model.set_fields(
            tid, status=new_status, worker_pid=0, heartbeat_at=None,
            recoveries=recoveries, next_run_at=None if new_status == "pending" else row["next_run_at"],
        )
        log_model.add(tid, f"检测到 Worker 异常退出（pid={pid}），已自动恢复为「待运行」", "warn")
        log.warning("task %s worker dead (pid=%s) -> recovered as %s", tid, pid, new_status)


# ---------- 到期派发 ----------

def dispatch_due_tasks():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = db.query_all(
        "SELECT id, run_count, max_runs FROM tasks "
        "WHERE enabled=1 AND control='run' AND status IN ('pending','scheduled') "
        "AND (next_run_at IS NULL OR next_run_at <= ?)",
        (now,),
    )
    for row in rows:
        if row["max_runs"] and row["run_count"] >= row["max_runs"]:
            continue  # 轮次已用尽
        try:
            pid = spawn_worker(row["id"])
            log.info("task %s dispatched -> worker pid %s", row["id"], pid)
        except OSError as e:
            log.error("task %s spawn failed: %s", row["id"], e)


# ---------- 主循环 ----------

def run_loop():
    setup_logging()
    Config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    lock_fd = open(Config.SCHEDULER_LOCK, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("another scheduler holds the lock, exit")
        return 0
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    (Config.DATA_DIR / "scheduler.pid").write_text(str(os.getpid()))

    stop = {"flag": False}

    def _term(_sig, _frm):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    log.info("scheduler started, pid=%s, poll=%.1fs", os.getpid(), Config.SCHEDULER_POLL_SECONDS)
    try:
        while not stop["flag"]:
            try:
                recover_stale_tasks()
                dispatch_due_tasks()
            except Exception:  # noqa: BLE001 —— 守护循环不能因单次异常退出
                log.exception("scheduler iteration failed")
            # 最多睡 poll 秒（响应退出信号）
            for _ in range(int(Config.SCHEDULER_POLL_SECONDS * 10)):
                if stop["flag"]:
                    break
                time.sleep(0.1)
    finally:
        log.info("scheduler stopping")
        try:
            (Config.DATA_DIR / "scheduler.pid").unlink(missing_ok=True)
        except OSError:
            pass
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            lock_fd.close()
    return 0


def main() -> int:
    db.init_db()
    return run_loop()


if __name__ == "__main__":
    sys.exit(main())
