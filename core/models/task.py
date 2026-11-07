"""任务模型：字段读写、状态机与运行控制。"""
import json

from ..db import execute, now_str, query_all, query_one

# 可由表单写入的字段（不含运行时统计/时间戳）
EDITABLE_FIELDS = [
    "name", "description", "enabled",
    "subject", "body_html", "body_text", "reply_to", "cc", "bcc",
    "from_email", "from_name", "custom_headers", "attachments",
    "smtp_server_ids", "smtp_strategy", "failover_after",
    "send_interval_ms", "jitter_ms", "batch_size", "batch_interval_s", "hourly_limit",
    "max_retries", "retry_backoff", "retry_backoff_base_s",
    "connect_timeout_s", "send_timeout_s", "retry_permanent",
    "stop_on_fail_percent", "dedupe",
    "run_mode", "schedule_at", "cron_expr", "interval_minutes", "max_runs",
]

STATUS_LABELS = {
    "pending": "待运行",
    "scheduled": "已定时",
    "running": "运行中",
    "paused": "已暂停",
    "completed": "已完成",
    "failed": "已失败",
    "stopped": "已停止",
}

RUN_MODE_LABELS = {
    "manual": "手动执行",
    "once": "定时单次",
    "cron": "Cron 表达式",
    "interval": "固定间隔重复",
}


def parse(row):
    """把行记录转成 dict，并解析 JSON 字段。"""
    if row is None:
        return None
    d = dict(row)
    try:
        d["smtp_server_ids"] = json.loads(d.get("smtp_server_ids") or "[]")
    except (ValueError, TypeError):
        d["smtp_server_ids"] = []
    try:
        d["custom_headers"] = json.loads(d.get("custom_headers") or "{}")
    except (ValueError, TypeError):
        d["custom_headers"] = {}
    try:
        d["attachments"] = json.loads(d.get("attachments") or "[]")
    except (ValueError, TypeError):
        d["attachments"] = []
    return d


def list_tasks() -> list:
    return [parse(r) for r in query_all("SELECT * FROM tasks ORDER BY id DESC")]


def get(task_id: int):
    return parse(query_one("SELECT * FROM tasks WHERE id = ?", (task_id,)))


def get_raw(task_id: int):
    return query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))


def create(data: dict) -> int:
    cols = EDITABLE_FIELDS
    values = [_normalize(c, data.get(c)) for c in cols]
    placeholders = ",".join("?" * len(cols))
    cur = execute(
        f"INSERT INTO tasks ({','.join(cols)}, status, control, created_at, updated_at) "
        f"VALUES ({placeholders}, 'pending', 'run', ?, ?)",
        tuple(values) + (now_str(), now_str()),
    )
    return cur.lastrowid


def update(task_id: int, data: dict):
    sets = ",".join(f"{c}=?" for c in EDITABLE_FIELDS)
    values = [_normalize(c, data.get(c)) for c in EDITABLE_FIELDS]
    execute(
        f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",
        tuple(values) + (now_str(), task_id),
    )


def delete(task_id: int):
    execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def _normalize(field: str, value):
    if field == "smtp_server_ids":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = []
        return json.dumps([int(v) for v in (value or [])])
    if field == "custom_headers":
        if isinstance(value, str):
            try:
                value = json.loads(value or "{}")
            except ValueError:
                value = {}
        return json.dumps(value or {}, ensure_ascii=False)
    if field == "attachments":
        if isinstance(value, str):
            try:
                value = json.loads(value or "[]")
            except ValueError:
                value = []
        return json.dumps(value or [], ensure_ascii=False)
    if field in ("enabled", "retry_permanent", "dedupe"):
        return int(bool(value))
    if field in (
        "failover_after", "send_interval_ms", "jitter_ms", "batch_size",
        "batch_interval_s", "hourly_limit", "max_retries", "retry_backoff_base_s",
        "connect_timeout_s", "send_timeout_s", "stop_on_fail_percent",
        "interval_minutes", "max_runs",
    ):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0
    if field == "schedule_at" and value:
        return str(value).replace("T", " ")[:19]
    return value if value is not None else ""


# ---------- 状态机 / 运行控制 ----------

def set_fields(task_id: int, **kw):
    sets = ",".join(f"{k}=?" for k in kw)
    execute(f"UPDATE tasks SET {sets}, updated_at=? WHERE id=?",
            tuple(kw.values()) + (now_str(), task_id))


def start(task_id: int):
    """立即运行（新一轮或恢复暂停的那一轮）。"""
    row = query_one("SELECT run_active, next_run_at FROM tasks WHERE id=?", (task_id,))
    if row is None:
        return
    if row["run_active"]:
        # 暂停打断的一轮 → 立即继续
        set_fields(task_id, control="run", status="pending", next_run_at=None, enabled=1)
    else:
        set_fields(task_id, control="run", status="pending", next_run_at=None,
                   enabled=1, last_error="")


def pause(task_id: int):
    row = query_one("SELECT status, run_active FROM tasks WHERE id=?", (task_id,))
    if row is None:
        return
    set_fields(task_id, control="pause")
    if row["status"] in ("pending", "scheduled"):
        # 还没开始跑，直接转暂停
        set_fields(task_id, status="paused")


def stop(task_id: int):
    """停止：worker 观察到 control=stop 后自行退出；无 worker 则立即置停。"""
    row = query_one("SELECT status, worker_pid, run_active FROM tasks WHERE id=?", (task_id,))
    if row is None:
        return
    set_fields(task_id, control="stop")
    if not row["worker_pid"]:
        set_fields(task_id, status="stopped", run_active=0, worker_pid=0, heartbeat_at=None)


def schedule_now(task_id: int):
    """加入调度（保持既有周期配置）。"""
    set_fields(task_id, control="run", status="pending", next_run_at=None, enabled=1)


# ---------- 运行统计（Worker 使用） ----------

def reset_run_stats(task_id: int, total: int, run_tag: str):
    execute(
        "UPDATE tasks SET total_recipients=?, sent_count=0, failed_count=0, "
        "skipped_count=0, last_error='', recoveries=0, updated_at=? WHERE id=?",
        (total, now_str(), task_id),
    )


def bump_counter(task_id: int, field: str):
    if field not in ("sent_count", "failed_count", "skipped_count"):
        raise ValueError(field)
    execute(f"UPDATE tasks SET {field} = {field} + 1, updated_at=? WHERE id=?",
            (now_str(), task_id))


def heartbeat(task_id: int, pid: int):
    execute("UPDATE tasks SET heartbeat_at=?, worker_pid=?, updated_at=? WHERE id=?",
            (now_str(), pid, now_str(), task_id))


def clear_worker(task_id: int):
    execute("UPDATE tasks SET worker_pid=0, heartbeat_at=NULL WHERE id=?", (task_id,))


def stats_summary() -> dict:
    """列表页顶部统计。"""
    row = query_one(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,"
        " SUM(CASE WHEN status IN ('pending','scheduled') THEN 1 ELSE 0 END) AS waiting,"
        " SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,"
        " SUM(total_recipients) AS recipients,"
        " SUM(sent_count) AS sent,"
        " SUM(failed_count) AS failed "
        "FROM tasks"
    )
    return dict(row or {}) | {
        "total": (row["total"] or 0) if row else 0,
        "running": (row["running"] or 0) if row else 0,
        "waiting": (row["waiting"] or 0) if row else 0,
        "completed": (row["completed"] or 0) if row else 0,
        "recipients": (row["recipients"] or 0) if row else 0,
        "sent": (row["sent"] or 0) if row else 0,
        "failed": (row["failed"] or 0) if row else 0,
    }


def status_summary() -> list:
    """列表页轮询用的轻量摘要。"""
    rows = query_all(
        "SELECT id, status, control, sent_count, failed_count, skipped_count, "
        "total_recipients, next_run_at, run_count, last_error FROM tasks"
    )
    return [dict(r) for r in rows]
