"""登录 / 登出 / 登录保护。"""
import functools

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   session, url_for)

from ..models import user as user_model

bp = Blueprint("auth", __name__)


def _current_user():
    uid = session.get("user_id")
    if uid is None:
        return None
    return user_model.get_by_id(uid)


@bp.before_app_request
def load_user():
    g.user = _current_user()


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return {"error": "未登录"}, 401
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("tasks.index"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = user_model.verify(username, password)
        if user:
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            nxt = request.args.get("next") or ""
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("tasks.index"))
        error = "用户名或密码不正确"
        flash(error, "error")
    return render_template("login.html", error=error)


@bp.post("/logout")
def logout():
    session.clear()
    flash("已退出登录", "info")
    return redirect(url_for("auth.login"))
