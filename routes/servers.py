"""SMTP 服务器管理。"""
from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)

from core.models import server as server_model
from core.models import task as task_model
from services import mailer
from .auth import login_required

bp = Blueprint("servers", __name__, url_prefix="/servers")

SECURITIES = {"ssl": "SSL（隐式 TLS，通常 465）",
              "tls": "STARTTLS（通常 587）",
              "none": "明文（不加密，仅内网测试）"}


def _form_data(form) -> dict:
    return {
        "name": (form.get("name") or "").strip(),
        "host": (form.get("host") or "").strip(),
        "port": form.get("port") or 465,
        "security": form.get("security") if form.get("security") in SECURITIES else "ssl",
        "username": (form.get("username") or "").strip(),
        "password": form.get("password") or "",
        "from_email": (form.get("from_email") or "").strip(),
        "from_name": (form.get("from_name") or "").strip(),
        "timeout_seconds": form.get("timeout_seconds") or 15,
        "enabled": 1 if form.get("enabled") == "on" else 0,
        "priority": form.get("priority") or 100,
    }


def _validate(d: dict) -> list:
    errors = []
    if not d["name"]:
        errors.append("名称不能为空")
    if not d["host"]:
        errors.append("主机地址不能为空")
    try:
        port = int(d["port"])
        if not (0 < port < 65536):
            raise ValueError
    except (TypeError, ValueError):
        errors.append("端口必须是 1-65535 的整数")
    if d["from_email"] and "@" not in d["from_email"]:
        errors.append("默认发件邮箱格式不正确")
    return errors


@bp.get("/")
@login_required
def index():
    servers = server_model.list_all()
    # 统计每个服务器被多少任务使用
    usage = {}
    for t in task_model.list_tasks():
        for sid in t["smtp_server_ids"]:
            usage[sid] = usage.get(sid, 0) + 1
    return render_template("servers/index.html", servers=servers, usage=usage,
                           SECURITIES=SECURITIES)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    if request.method == "POST":
        data = _form_data(request.form)
        errors = _validate(data)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("servers/form.html", server=data, creating=True,
                                   SECURITIES=SECURITIES)
        sid = server_model.create(data)
        ok, msg = mailer.test_connection(server_model.get(sid))
        server_model.record_test(sid, ok, msg)
        flash(f"服务器已保存 · 连接测试：{'成功' if ok else '失败 — ' + msg}", "success" if ok else "error")
        return redirect(url_for("servers.index"))
    return render_template("servers/form.html", server=None, creating=True,
                           SECURITIES=SECURITIES)


@bp.route("/<int:server_id>/edit", methods=["GET", "POST"])
@login_required
def edit(server_id):
    server = server_model.get(server_id)
    if server is None:
        flash("服务器不存在", "error")
        return redirect(url_for("servers.index"))
    if request.method == "POST":
        data = _form_data(request.form)
        errors = _validate(data)
        if errors:
            for e in errors:
                flash(e, "error")
            data["id"] = server_id
            data["password"] = data["password"] or server["password"]
            return render_template("servers/form.html", server=data, creating=False,
                                   SECURITIES=SECURITIES)
        # 密码留空 = 保持原密码
        if not data["password"]:
            data["password"] = server["password"]
        server_model.update(server_id, data)
        flash("服务器已保存", "success")
        return redirect(url_for("servers.index"))
    return render_template("servers/form.html", server=server, creating=False,
                           SECURITIES=SECURITIES)


@bp.post("/<int:server_id>/test")
@login_required
def test(server_id):
    server = server_model.get(server_id)
    if server is None:
        flash("服务器不存在", "error")
        return redirect(url_for("servers.index"))
    ok, msg = mailer.test_connection(server)
    server_model.record_test(server_id, ok, msg)
    flash(f"连接测试：{msg}", "success" if ok else "error")
    return redirect(request.referrer or url_for("servers.index"))


@bp.post("/<int:server_id>/delete")
@login_required
def delete(server_id):
    used = [t["name"] for t in task_model.list_tasks() if server_id in t["smtp_server_ids"]]
    if used:
        flash(f"该服务器正被 {len(used)} 个任务使用：{'、'.join(used[:3])}… 请先调整任务", "error")
        return redirect(url_for("servers.index"))
    server_model.delete(server_id)
    flash("服务器已删除", "success")
    return redirect(url_for("servers.index"))
