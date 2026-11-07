"""JSON 轮询接口（列表 / 详情页自动刷新）。"""
from flask import Blueprint, request

from ..models import log as log_model
from ..models import recipient as rcpt_model
from ..models import task as task_model
from ..runner import process as runner_process
from .auth import login_required

bp = Blueprint("api", __name__, url_prefix="/api")


def _percent(t: dict) -> float:
    total = t.get("total_recipients") or 0
    if not total:
        return 0.0
    done = (t.get("sent_count") or 0) + (t.get("failed_count") or 0) + (t.get("skipped_count") or 0)
    return round(min(100.0, done * 100.0 / total), 1)


@bp.get("/tasks/summary")
@login_required
def tasks_summary():
    return {
        "stats": task_model.stats_summary(),
        "tasks": [
            dict(t, percent=_percent(t))
            for t in task_model.status_summary()
        ],
        "scheduler": runner_process.scheduler_running(),
    }


@bp.get("/tasks/<int:task_id>/status")
@login_required
def task_status(task_id):
    task = task_model.get(task_id)
    if task is None:
        return {"error": "任务不存在"}, 404
    return {
        "id": task["id"],
        "status": task["status"],
        "control": task["control"],
        "enabled": task["enabled"],
        "sent_count": task["sent_count"],
        "failed_count": task["failed_count"],
        "skipped_count": task["skipped_count"],
        "total_recipients": task["total_recipients"],
        "percent": _percent(task),
        "run_count": task["run_count"],
        "next_run_at": task["next_run_at"],
        "last_run_at": task["last_run_at"],
        "heartbeat_at": task["heartbeat_at"],
        "worker_pid": task["worker_pid"],
        "last_error": task["last_error"],
        "recipient_counts": rcpt_model.counts(task_id),
        "scheduler": runner_process.scheduler_running(),
    }


@bp.get("/tasks/<int:task_id>/logs")
@login_required
def task_logs(task_id):
    after = request.args.get("after", 0, type=int)
    limit = min(request.args.get("limit", 300, type=int), 1000)
    logs = log_model.recent(task_id, after_id=after, limit=limit)
    return {"logs": logs, "latest": logs[-1]["id"] if logs else after}


@bp.get("/tasks/<int:task_id>/recipients")
@login_required
def task_recipients(task_id):
    status = request.args.get("status") or None
    if status not in ("pending", "sent", "failed", "skipped", None):
        status = None
    offset = max(0, request.args.get("offset", 0, type=int))
    limit = min(request.args.get("limit", 200, type=int), 500)
    rows = rcpt_model.for_task(task_id, status=status, limit=limit, offset=offset)
    return {"rows": rows, "counts": rcpt_model.counts(task_id), "offset": offset}
