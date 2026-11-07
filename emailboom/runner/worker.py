"""独立 Worker 进程：负责一个任务的全部发送工作。

    python -m emailboom.runner.worker <task_id>

职责：认领任务 → 逐封发送（限速/重试/多服务器切换/小时限流）
    → 更新进度与日志 → 按配置完成收尾（手动完成 / 计算下一次定时）。
通过轮询 DB 中的 control 字段实现暂停 / 停止，无需额外通信通道。
"""
import os
import random
import sys
import time
from datetime import datetime

from config import Config
from .. import db
from ..models import log as log_model
from ..models import recipient as rcpt_model
from ..models import server as server_model
from ..models import task as task_model
from ..services import mailer
from ..services.schedule import RUN_MODE_HINT, next_run_for

HB_INTERVAL = 5.0  # 心跳 / 控制检查的最小间隔（秒）


# ---------- 基础工具 ----------

def get_control(task_id: int) -> str:
    row = db.query_one("SELECT control FROM tasks WHERE id=?", (task_id,))
    return row["control"] if row else "stop"


def interruptible_sleep(task_id: int, seconds: float) -> bool:
    """可打断的睡眠；返回 False 表示收到暂停/停止指令。"""
    end = time.time() + max(0.0, seconds)
    while True:
        if get_control(task_id) != "run":
            return False
        left = end - time.time()
        if left <= 0:
            return True
        time.sleep(min(0.5, left))


def _exit_interrupt(task_id: int) -> int:
    row = task_model.get(task_id)
    if row is None:
        return 1
    ctl = row.get("control", "stop")
    if ctl == "pause":
        task_model.set_fields(task_id, status="paused", worker_pid=0, heartbeat_at=None)
        log_model.add(task_id, "⏸ 任务已暂停（可随时继续）", "warn")
        print(f"[worker] task {task_id} paused")
        return 0
    task_model.set_fields(task_id, status="stopped", run_active=0, worker_pid=0,
                          heartbeat_at=None)
    log_model.add(task_id, "⏹ 任务已停止（本轮结束，如需再发请重新运行）", "warn")
    print(f"[worker] task {task_id} stopped")
    return 0


def _fatal(task_id: int, message: str) -> int:
    task_model.set_fields(task_id, status="failed", run_active=0, worker_pid=0,
                          heartbeat_at=None, last_error=message[:2000])
    log_model.add(task_id, f"✖ 任务失败：{message}", "error")
    print(f"[worker] task {task_id} failed: {message}")
    return 1


# ---------- 多服务器选择 ----------

class ServerPool:
    """按策略在多个 SMTP 服务器间选择，并在连续失败时切换（故障转移）。"""

    def __init__(self, task: dict, servers: list):
        self.task = task
        self.failover_after = int(task.get("failover_after") or 0)
        strategy = task.get("smtp_strategy") or "round_robin"
        order = {int(sid): i for i, sid in enumerate(task.get("smtp_server_ids") or [])}
        if strategy == "priority":
            servers = sorted(servers, key=lambda s: (s["priority"], order.get(s["id"], 0)))
        else:
            servers = sorted(servers, key=lambda s: order.get(s["id"], 0))
        self.servers = servers
        self.consecutive = {s["id"]: 0 for s in servers}
        self.auth_dead = set()
        self.rr = 0

    def pick(self):
        live = [s for s in self.servers if s["id"] not in self.auth_dead]
        if not live:
            return None  # 全部服务器认证失败
        if self.failover_after > 0:
            healthy = [s for s in live if self.consecutive[s["id"]] < self.failover_after]
        else:
            healthy = live
        candidates = healthy or live
        strategy = self.task.get("smtp_strategy") or "round_robin"
        if strategy == "random":
            return random.choice(candidates)
        if strategy == "round_robin":
            chosen = candidates[self.rr % len(candidates)]
            self.rr += 1
            return chosen
        return candidates[0]  # priority / fixed：取排序后的第一个可用

    def report_failure(self, server_id: int, kind: str):
        if kind == "auth":
            self.auth_dead.add(server_id)
            self.consecutive[server_id] = 999
        else:
            self.consecutive[server_id] = self.consecutive.get(server_id, 0) + 1

    def report_success(self, server_id: int):
        self.consecutive[server_id] = 0


# ---------- 认领与轮次准备 ----------

def claim(task_id: int):
    """原子认领：只有 pending/scheduled 且 control=run 才能进入 running。"""
    now = db.now_str()
    cur = db.execute(
        "UPDATE tasks SET status='running', worker_pid=?, heartbeat_at=?, updated_at=? "
        "WHERE id=? AND status IN ('pending','scheduled') AND control='run' AND enabled=1",
        (os.getpid(), now, now, task_id),
    )
    if cur.rowcount == 0:
        return None
    return task_model.get(task_id)


def begin_round(task_id: int, row: dict) -> tuple[dict, int, int]:
    """新一轮：重置收件人与统计。返回 (task, total, skipped)。"""
    pending, kept = rcpt_model.prepare_run(task_id, dedupe=bool(row["dedupe"]))
    total = pending + kept
    task_model.reset_run_stats(task_id, total, "")
    run_count = (row["run_count"] or 0) + 1
    task_model.set_fields(
        task_id, run_count=run_count, run_active=1, last_run_at=db.now_str(),
        next_run_at=None, recoveries=0, worker_pid=os.getpid(), heartbeat_at=db.now_str(),
    )
    log_model.prune(task_id)
    mode_hint = RUN_MODE_HINT.get(row["run_mode"], row["run_mode"])
    log_model.add(
        task_id,
        f"▶ 第 {run_count} 轮开始 · 本轮待发 {pending} 封"
        + (f"，按去重规则跳过 {kept} 封" if kept else "")
        + f" · 执行方式：{mode_hint}",
        "info",
    )
    print(f"[worker] task {task_id} round {run_count} start, pending={pending}")
    return task_model.get(task_id), total, kept


# ---------- 发送单个收件人 ----------

def send_one(task_id: int, task: dict, rec: dict, pool: ServerPool,
             attach_paths: list, counters: dict, last_hb: list) -> bool:
    """发送一个收件人；返回 False 表示收到暂停/停止指令（该收件人保持 pending）。"""
    max_retries = int(task.get("max_retries") or 0)
    attempts = 0
    while True:
        attempts += 1
        srv = pool.pick()
        if srv is None:
            raise RuntimeError("全部 SMTP 服务器认证失败，请检查账号密码")
        try:
            mailer.send_once(task, rec, srv, attach_paths)
        except mailer.RecipientRenderError as e:
            rcpt_model.mark_failed(task_id, rec["id"], attempts, str(e))
            task_model.bump_counter(task_id, "failed_count")
            counters["failed"] += 1
            log_model.add(task_id, f"✖ {rec['email']} 模板错误：{e}", "error")
            return True
        except Exception as e:  # noqa: BLE001 —— SMTP 异常族统一分类处理
            kind = mailer.classify_error(e)
            pool.report_failure(srv["id"], kind)
            if kind == "auth":
                log_model.add(
                    task_id,
                    f"✖ SMTP 认证失败（{srv['name']}：{srv['host']}:{srv['port']}）：{e}，"
                    f"本轮已停用该服务器",
                    "error",
                )
            retryable = kind == "transient" or (
                kind == "permanent" and task.get("retry_permanent"))
            if retryable and attempts <= max_retries:
                delay = mailer.backoff_seconds(task, attempts)
                log_model.add(
                    task_id,
                    f"⚠ {rec['email']} 第 {attempts} 次失败（{type(e).__name__}: {e}），"
                    f"{delay:.0f}s 后重试（{attempts}/{max_retries}）",
                    "warn",
                )
                if not interruptible_sleep(task_id, delay):
                    return False
                continue
            rcpt_model.mark_failed(task_id, rec["id"], attempts, f"{type(e).__name__}: {e}")
            task_model.bump_counter(task_id, "failed_count")
            counters["failed"] += 1
            log_model.add(
                task_id,
                f"✖ {rec['email']} 发送失败（已试 {attempts} 次）：{type(e).__name__}: {e}",
                "error",
            )
            return True
        # 成功
        pool.report_success(srv["id"])
        rcpt_model.mark_sent(task_id, rec["id"], attempts)
        task_model.bump_counter(task_id, "sent_count")
        counters["sent"] += 1
        progress = counters["sent"] + counters["failed"] + counters["skipped"]
        log_model.add(
            task_id,
            f"✔ 已发送 {rec['email']}（{progress}/{counters['total']}）",
            "success",
        )
        return True


# ---------- 主流程 ----------

def run_task(task_id: int) -> int:
    row = claim(task_id)
    if row is None:
        print(f"[worker] task {task_id} claim failed (state changed), exit")
        return 0

    if not row["run_active"]:
        task, total, kept = begin_round(task_id, row)
    else:
        task, total = task_model.get(task_id), row["total_recipients"]
        kept = row["skipped_count"]
        task_model.set_fields(task_id, worker_pid=os.getpid(), heartbeat_at=db.now_str())
        log_model.add(task_id, "▶ 任务从暂停处继续", "info")
        print(f"[worker] task {task_id} resumed")

    counters = {
        "total": total,
        "sent": task["sent_count"],
        "failed": task["failed_count"],
        "skipped": kept,
    }

    # 服务器
    servers = [s for s in server_model.get_many(task["smtp_server_ids"]) if s["enabled"]]
    if not servers:
        return _fatal(task_id, "任务没有绑定可用的 SMTP 服务器")
    pool = ServerPool(task, servers)

    # 附件
    attach_paths = []
    for a in task.get("attachments") or []:
        p = Config.ATTACHMENTS_DIR / f"task_{task_id}" / (a.get("file") or "")
        if p.exists():
            attach_paths.append(p)

    fail_pct = int(task.get("stop_on_fail_percent") or 0)
    hourly_limit = int(task.get("hourly_limit") or 0)
    interval_s = int(task.get("send_interval_ms") or 0) / 1000.0
    jitter_s = int(task.get("jitter_ms") or 0) / 1000.0
    batch_size = max(1, int(task.get("batch_size") or 1))
    batch_pause = int(task.get("batch_interval_s") or 0)
    in_batch = 0
    last_hb = [time.time()]
    waiting_hourly = [False]

    while True:
        # ---- 控制 / 心跳 ----
        ctl = get_control(task_id)
        if ctl != "run":
            return _exit_interrupt(task_id)
        if time.time() - last_hb[0] >= HB_INTERVAL:
            task_model.heartbeat(task_id, os.getpid())
            last_hb[0] = time.time()

        # ---- 小时限流 ----
        if hourly_limit > 0 and rcpt_model.sent_within_hours(task_id) >= hourly_limit:
            if not waiting_hourly[0]:
                log_model.add(task_id, f"⏳ 达到每小时上限（{hourly_limit} 封/小时），等待窗口释放…", "warn")
                waiting_hourly[0] = True
            if not interruptible_sleep(task_id, 30):
                return _exit_interrupt(task_id)
            continue
        if waiting_hourly[0]:
            waiting_hourly[0] = False
            log_model.add(task_id, "➤ 小时窗口释放，继续发送", "info")

        # ---- 取下一个待发收件人 ----
        batch = rcpt_model.pending_batch(task_id, limit=1)
        if not batch:
            break  # 本轮全部处理完
        rec = batch[0]

        ok = send_one(task_id, task, rec, pool, attach_paths, counters, last_hb)
        if not ok:
            return _exit_interrupt(task_id)

        # ---- 失败率安全阀 ----
        done = counters["sent"] + counters["failed"]
        if fail_pct > 0 and done >= 5 and (counters["failed"] / done * 100) >= fail_pct:
            return _fatal(
                task_id,
                f"失败率达到 {counters['failed']}/{done} ≈ {counters['failed'] / done * 100:.1f}%"
                f"（阈值 {fail_pct}%），已自动中止",
            )

        # ---- 限速 ----
        in_batch += 1
        if in_batch >= batch_size:
            in_batch = 0
            if batch_pause > 0 and not interruptible_sleep(task_id, batch_pause):
                return _exit_interrupt(task_id)
        delay = interval_s + (random.uniform(0, jitter_s) if jitter_s > 0 else 0)
        if delay > 0 and not interruptible_sleep(task_id, delay):
            return _exit_interrupt(task_id)

    # ---- 本轮完成 → 收尾 ----
    return finalize(task_id)


def finalize(task_id: int) -> int:
    task = task_model.get(task_id)
    now = datetime.now()
    sent, failed = task["sent_count"], task["failed_count"]
    total = task["total_recipients"]
    log_model.add(
        task_id,
        f"✔ 本轮完成：成功 {sent} · 失败 {failed} · 跳过 {task['skipped_count']} · 共 {total}",
        "success" if failed == 0 else "info",
    )

    mode = task["run_mode"]
    status, next_at = "completed", None
    try:
        if mode == "manual":
            status = "completed"
        elif mode == "once":
            sched = (task.get("schedule_at") or "").strip().replace("T", " ")
            if sched and sched > now.strftime("%Y-%m-%d %H:%M:%S"):
                status, next_at = "scheduled", sched[:19]
            else:
                status = "completed"
        else:
            next_at, exhausted = next_run_for(task, now)
            if exhausted or not next_at:
                status = "completed"
            else:
                status = "scheduled"
    except ValueError as e:
        task_model.set_fields(task_id, status="failed", run_active=0, worker_pid=0,
                              heartbeat_at=None, last_error=str(e)[:2000])
        log_model.add(task_id, f"✖ 定时配置错误：{e}", "error")
        return 1

    task_model.set_fields(
        task_id, status=status, run_active=0, worker_pid=0, heartbeat_at=None,
        next_run_at=next_at, last_error="",
    )
    if status == "scheduled" and next_at:
        log_model.add(task_id, f"⏱ 下一次执行：{next_at}", "info")
    print(f"[worker] task {task_id} finished -> {status}")
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python -m emailboom.runner.worker <task_id>")
        return 2
    try:
        task_id = int(argv[0])
    except ValueError:
        print(f"invalid task_id: {argv[0]}")
        return 2
    db.init_db()
    try:
        return run_task(task_id)
    except Exception as e:  # noqa: BLE001 —— 顶层兜底：写失败状态而不是无声崩溃
        import traceback
        traceback.print_exc()
        try:
            return _fatal(task_id, f"Worker 异常：{type(e).__name__}: {e}")
        except Exception:
            return 1


if __name__ == "__main__":
    sys.exit(main())
