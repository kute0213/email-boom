"""系统设置：调度器管理、修改密码、系统信息。"""
import os

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   url_for)

from config import BASE_DIR, Config
from .. import db
from ..models import user as user_model
from ..runner import process as runner_process
from .auth import login_required

bp = Blueprint("settings", __name__, url_prefix="/settings")


def _system_info() -> dict:
    db_size = os.path.getsize(Config.DATABASE) if os.path.exists(Config.DATABASE) else 0
    sched_pid = None
    try:
        sched_pid = int((Config.DATA_DIR / "scheduler.pid").read_text().strip())
    except (OSError, ValueError):
        pass
    worker_logs = []
    for f in sorted(os.listdir(Config.DATA_DIR)):
        if f.startswith("worker_") and f.endswith(".log"):
            try:
                size = os.path.getsize(Config.DATA_DIR / f)
            except OSError:
                size = 0
            if size >= 1024 * 1024:
                human = f"{size / 1024 / 1024:.1f} MB"
            elif size >= 1024:
                human = f"{size / 1024:.1f} KB"
            else:
                human = f"{size} B"
            worker_logs.append({"name": f, "size": human})
    return {
        "db_size": f"{db_size / 1024 / 1024:.2f} MB",
        "data_dir": str(Config.DATA_DIR),
        "base_dir": str(BASE_DIR),
        "scheduler_running": runner_process.scheduler_running(),
        "scheduler_pid": sched_pid,
        "worker_logs": worker_logs,
        "workers": sorted(
            int(f.split("_", 1)[1].split(".")[0])
            for f in os.listdir(Config.DATA_DIR)
            if f.startswith("worker_") and f.endswith(".log")
            and f.split("_", 1)[1].split(".")[0].isdigit()
        ),
    }


@bp.get("/")
@login_required
def index():
    return render_template("settings/index.html", info=_system_info())


@bp.post("/password")
@login_required
def change_password():
    old = request.form.get("old_password") or ""
    new1 = request.form.get("new_password") or ""
    new2 = request.form.get("new_password2") or ""
    if new1 != new2:
        flash("两次输入的新密码不一致", "error")
    elif len(new1) < 6:
        flash("新密码长度至少 6 位", "error")
    else:
        ok, msg = user_model.change_password(g.user["id"], old, new1)
        flash(msg, "success" if ok else "error")
    return redirect(url_for("settings.index"))


@bp.post("/scheduler/restart")
@login_required
def restart_scheduler():
    ok = runner_process.restart_scheduler()
    flash("调度器已重启" if ok else "调度器重启失败，请查看 data/scheduler.log",
          "success" if ok else "error")
    return redirect(url_for("settings.index"))
