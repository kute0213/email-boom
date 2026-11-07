"""任务：列表 / 详情 / 新建 / 编辑 / 控制动作。"""
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, url_for)
from werkzeug.utils import secure_filename

from config import Config
from .. import db
from ..models import log as log_model
from ..models import recipient as rcpt_model
from ..models import server as server_model
from ..models import task as task_model
from ..services import schedule as schedule_service
from .auth import login_required

bp = Blueprint("tasks", __name__)

STRATEGIES = {
    "round_robin": "轮询（依次轮流）",
    "random": "随机",
    "priority": "按优先级（失败自动换高优先级）",
    "fixed": "固定顺序（按勾选顺序）",
}
RETRY_BACKOFFS = {"fixed": "固定间隔", "linear": "线性递增", "exponential": "指数退避"}
RUN_MODES = {
    "manual": "手动执行（保存后立即排队）",
    "once": "定时单次执行",
    "cron": "Cron 周期执行",
    "interval": "固定间隔重复执行",
}


@bp.app_context_processor
def inject_consts():
    return {
        "STRATEGIES": STRATEGIES,
        "RETRY_BACKOFFS": RETRY_BACKOFFS,
        "RUN_MODES": RUN_MODES,
        "STATUS_LABELS": task_model.STATUS_LABELS,
        "RUN_MODE_LABELS": task_model.RUN_MODE_LABELS,
    }


# ---------- 列表 ----------

@bp.get("/")
@login_required
def index():
    tasks = task_model.list_tasks()
    stats = task_model.stats_summary()
    servers = {s["id"]: s for s in server_model.list_all()}
    return render_template("tasks/index.html", tasks=tasks, stats=stats, servers=servers)


# ---------- 详情 ----------

@bp.get("/tasks/<int:task_id>")
@login_required
def detail(task_id):
    task = task_model.get(task_id)
    if task is None:
        flash("任务不存在", "error")
        return redirect(url_for("tasks.index"))
    counts = rcpt_model.counts(task_id)
    rcpt_filter = request.args.get("rcpt") or ""
    if rcpt_filter not in ("pending", "sent", "failed", "skipped"):
        rcpt_filter = ""
    recipients = rcpt_model.for_task(
        task_id, status=rcpt_filter or None, limit=200
    )
    logs = log_model.recent(task_id, after_id=0, limit=300)
    servers = {s["id"]: s for s in server_model.list_all()}
    return render_template(
        "tasks/detail.html", task=task, counts=counts, recipients=recipients,
        logs=logs, servers=servers, log_latest=log_model.latest_id(task_id),
        rcpt_filter=rcpt_filter,
    )


# ---------- 表单数据处理 ----------

def _form_data(form) -> dict:
    data = {k: form.get(k, "") for k in task_model.EDITABLE_FIELDS}
    data["enabled"] = 1 if form.get("enabled") == "on" else 0
    data["retry_permanent"] = 1 if form.get("retry_permanent") == "on" else 0
    data["dedupe"] = 1 if form.get("dedupe") == "on" else 0
    data["smtp_server_ids"] = [int(v) for v in form.getlist("smtp_server_ids") if v.isdigit()]
    # 定时时间 datetime-local → DB 格式
    if data.get("schedule_at"):
        data["schedule_at"] = data["schedule_at"].replace("T", " ")[:19]
    else:
        data["schedule_at"] = None
    # 自定义头：多行 K: V
    headers = {}
    for line in (form.get("custom_headers") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError(f"自定义头格式应为「名称: 值」：{line}")
        k, v = line.split(":", 1)
        headers[k.strip()] = v.strip()
    data["custom_headers"] = headers
    return data


def _validate(data: dict) -> list:
    errors = []
    if not (data.get("name") or "").strip():
        errors.append("任务名称不能为空")
    if not data.get("subject", "").strip():
        errors.append("邮件主题不能为空")
    if not data.get("smtp_server_ids"):
        errors.append("请至少勾选一个 SMTP 服务器")
    if int(data.get("failover_after") or 0) > 1000:
        errors.append("失败切换阈值过大")
    mode = data.get("run_mode") or "manual"
    if mode == "once":
        if not data.get("schedule_at"):
            errors.append("定时单次执行需要选择执行时间")
        else:
            try:
                datetime.strptime(data["schedule_at"][:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                errors.append("执行时间格式不正确")
    elif mode == "cron":
        try:
            schedule_service.validate(data.get("cron_expr") or "")
        except ValueError as e:
            errors.append(str(e))
    elif mode == "interval":
        if int(data.get("interval_minutes") or 0) < 1:
            errors.append("重复间隔至少 1 分钟")
    return errors


def _apply_schedule(task_id: int, data: dict, is_create: bool):
    """保存后重新编排状态（running 中的任务不动，修改在下轮生效）。"""
    mode = data.get("run_mode") or "manual"
    row = task_model.get(task_id)
    if row["status"] in ("running", "paused") and not is_create:
        return
    if mode == "manual":
        if is_create:
            task_model.set_fields(task_id, status="pending", next_run_at=None, control="run")
        elif row["status"] in ("scheduled",):
            task_model.set_fields(task_id, status="pending", next_run_at=None)
        return
    if mode == "once":
        task_model.set_fields(task_id, status="scheduled",
                              next_run_at=data.get("schedule_at"))
        return
    try:
        nxt, _ex = schedule_service.next_run_for(data, datetime.now())
    except ValueError as e:
        flash(f"定时配置：{e}", "error")
        nxt = None
    if is_create and mode == "interval":
        # 首轮立即执行，完成后按间隔滚动
        task_model.set_fields(task_id, status="pending", next_run_at=None, control="run")
    elif row["status"] == "pending" and not is_create:
        task_model.set_fields(task_id, next_run_at=nxt)
    else:
        task_model.set_fields(task_id, status="scheduled", next_run_at=nxt, control="run")


def _save_recipients(task_id: int, form, is_create: bool):
    """收件人：文本域 + 可选 CSV 上传；编辑时空文本域表示不改动。"""
    text = form.get("recipients_text") or ""
    f = request.files.get("recipients_file")
    items = None
    if f and f.filename:
        raw = f.read().decode("utf-8-sig", errors="replace")
        items = rcpt_model.parse_csv(raw)
        text = ""
    if items is None and text.strip():
        items = rcpt_model.parse_lines(text)
    if items is not None:
        if not items and is_create:
            raise ValueError("至少需要一个收件人")
        if items:
            rcpt_model.replace_all(task_id, items)
        return len(items)
    if is_create:
        raise ValueError("请填写收件人列表（每行一个邮箱）")
    return rcpt_model.count(task_id)


def _save_attachments(task_id: int, task: dict):
    files = [f for f in request.files.getlist("attachments") if f and f.filename]
    if not files:
        return  # 未上传 → 保持原附件
    folder = Config.ATTACHMENTS_DIR / f"task_{task_id}"
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    meta = []
    for f in files:
        original = secure_filename(f.filename) or "file"
        stored = f"{uuid.uuid4().hex[:8]}_{original}"
        path = folder / stored
        f.save(path)
        meta.append({"file": stored, "name": f.filename, "size": path.stat().st_size})
    task_model.set_fields(task_id, attachments=json.dumps(meta, ensure_ascii=False))


# ---------- 新建 / 编辑 ----------

@bp.route("/tasks/new", methods=["GET", "POST"])
@login_required
def create():
    servers = server_model.list_all()
    if request.method == "POST":
        try:
            data = _form_data(request.form)
            errors = _validate(data)
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("tasks/form.html", task=data, servers=servers,
                                       creating=True)
            task_id = task_model.create(data)
            _save_recipients(task_id, request.form, is_create=True)
            _save_attachments(task_id, data)
            _apply_schedule(task_id, data, is_create=True)
            log_model.add(task_id, "任务已创建", "info")
            flash("任务已创建，已进入调度队列", "success")
            return redirect(url_for("tasks.detail", task_id=task_id))
        except ValueError as e:
            flash(str(e), "error")
    return render_template("tasks/form.html", task=None, servers=servers, creating=True)


@bp.route("/tasks/<int:task_id>/edit", methods=["GET", "POST"])
@login_required
def edit(task_id):
    task = task_model.get(task_id)
    if task is None:
        flash("任务不存在", "error")
        return redirect(url_for("tasks.index"))
    servers = server_model.list_all()
    if request.method == "POST":
        try:
            data = _form_data(request.form)
            errors = _validate(data)
            if errors:
                for e in errors:
                    flash(e, "error")
                task.update(data)
                return render_template("tasks/form.html", task=task, servers=servers,
                                       creating=False)
            task_model.update(task_id, data)
            _save_recipients(task_id, request.form, is_create=False)
            _save_attachments(task_id, data)
            _apply_schedule(task_id, data, is_create=False)
            log_model.add(task_id, "任务配置已更新", "info")
            flash("任务已保存（运行中的修改将在下一轮生效）", "success")
            return redirect(url_for("tasks.detail", task_id=task_id))
        except ValueError as e:
            flash(str(e), "error")
    return render_template("tasks/form.html", task=task, servers=servers, creating=False)


# ---------- 控制动作 ----------

ACTIONS = {"start": "运行", "pause": "暂停", "stop": "停止"}


@bp.post("/tasks/<int:task_id>/<action>")
@login_required
def action(task_id, action):
    task = task_model.get(task_id)
    if task is None:
        flash("任务不存在", "error")
        return redirect(url_for("tasks.index"))
    back = request.form.get("next") or url_for("tasks.detail", task_id=task_id)

    if action in ACTIONS:
        if action == "start":
            if task["status"] == "running":
                flash("任务正在运行中", "info")
            else:
                task_model.start(task_id)
                log_model.add(task_id, "已请求运行", "info")
                flash("已加入调度，即将开始运行", "success")
        elif action == "pause":
            task_model.pause(task_id)
            log_model.add(task_id, "已请求暂停", "warn")
            flash("已请求暂停", "info")
        else:  # stop
            task_model.stop(task_id)
            log_model.add(task_id, "已请求停止", "warn")
            flash("已请求停止", "info")
    elif action == "toggle":
        new = 0 if task["enabled"] else 1
        if not new and task["status"] == "running":
            flash("运行中任务请先暂停/停止，再禁用", "error")
            return redirect(back)
        task_model.set_fields(task_id, enabled=new)
        flash("任务已启用" if new else "任务已停用", "success")
    elif action == "delete":
        if task["status"] == "running":
            flash("任务正在运行，请先停止再删除", "error")
            return redirect(back)
        task_model.delete(task_id)
        folder = Config.ATTACHMENTS_DIR / f"task_{task_id}"
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        flash("任务已删除", "success")
        return redirect(url_for("tasks.index"))
    elif action == "clear_logs":
        log_model.clear(task_id)
        flash("日志已清空", "success")
    else:
        flash("未知操作", "error")
    return redirect(back)
