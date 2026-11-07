"""任务日志模型。"""
from ..db import execute, now_str, query_all, query_one

KEEP_RECENT = 5000


def add(task_id: int, message: str, level: str = "info"):
    if level not in ("info", "success", "warn", "error"):
        level = "info"
    execute(
        "INSERT INTO task_logs (task_id, level, message, created_at) VALUES (?,?,?,?)",
        (task_id, level, str(message)[:4000], now_str()),
    )


def recent(task_id: int, after_id: int = 0, limit: int = 300) -> list:
    rows = query_all(
        "SELECT * FROM task_logs WHERE task_id=? AND id>? ORDER BY id LIMIT ?",
        (task_id, after_id, limit),
    )
    return [dict(r) for r in rows]


def latest_id(task_id: int) -> int:
    row = query_one(
        "SELECT COALESCE(MAX(id), 0) AS m FROM task_logs WHERE task_id=?", (task_id,))
    return int(row["m"]) if row else 0


def count(task_id: int) -> int:
    row = query_one("SELECT COUNT(*) FROM task_logs WHERE task_id=?", (task_id,))
    return int(row[0]) if row else 0


def prune(task_id: int, keep: int = KEEP_RECENT):
    """只保留最近 keep 条，避免日志表无限膨胀（在每轮开始时调用）。"""
    execute(
        "DELETE FROM task_logs WHERE task_id=? AND id NOT IN "
        "(SELECT id FROM task_logs WHERE task_id=? ORDER BY id DESC LIMIT ?)",
        (task_id, task_id, keep),
    )


def clear(task_id: int):
    execute("DELETE FROM task_logs WHERE task_id=?", (task_id,))
