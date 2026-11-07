"""EmailBoom —— 应用工厂（模板位于 static/templates，资源零 CDN）。"""
import atexit
import hmac
import secrets

from flask import Flask, g, request, session

from config import BASE_DIR, Config
from . import db


def _create_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(16)
        session["csrf_token"] = token
    return token


def create_app(config_object=Config) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "static" / "templates"),
        static_folder=str(BASE_DIR / "static"),
        static_url_path="/static",
    )
    app.config.from_object(config_object)

    # 数据库（幂等建表 + 种子管理员）；连接为线程本地，进程退出时统一关闭
    db.init_db()
    atexit.register(db.close_db)

    # ---- CSRF（表单 POST 一律校验；测试模式跳过） ----
    @app.before_request
    def csrf_protect():
        if request.method != "POST" or app.config.get("TESTING"):
            return None
        token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
        expected = session.get("csrf_token") or ""
        if not token or not expected or not hmac.compare_digest(token, expected):
            return {"error": "CSRF 校验失败，请刷新页面后重试"}, 400
        return None

    # ---- 蓝图 ----
    from routes import api, auth, servers, settings, tasks
    app.register_blueprint(auth.bp)
    app.register_blueprint(tasks.bp)
    app.register_blueprint(servers.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(api.bp)

    # ---- 模板全局 ----
    @app.context_processor
    def inject_globals():
        from .runner import process as runner_process
        return {
            "csrf_token": _create_csrf_token,
            "current_user": g.get("user"),
            "app_name": "EmailBoom",
            "scheduler_alive": runner_process.scheduler_running(),
        }

    @app.template_filter("hhmmss")
    def hhmmss(value):
        """日志时间只显示时分秒。"""
        s = str(value or "")
        return s[11:19] if len(s) >= 19 else s

    @app.template_global()
    def dict_to_lines(mapping):
        """自定义头 dict → 表单多行文本。"""
        if isinstance(mapping, dict):
            return "\n".join(f"{k}: {v}" for k, v in mapping.items())
        return str(mapping or "")

    @app.template_global()
    def to_local_dt(db_value):
        """DB 的 'YYYY-MM-DD HH:MM:SS' → datetime-local 输入值。"""
        s = str(db_value or "").strip()
        if not s:
            return ""
        return s.replace(" ", "T")[:16]

    # ---- 调度器守护（Web 启动即拉起，独立进程） ----
    if app.config.get("AUTO_START_SCHEDULER"):
        from .runner import process as runner_process
        runner_process.ensure_scheduler(wait=2.0)

    return app
