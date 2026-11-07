"""全局配置：路径、密钥、后台调度参数。"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("EMAILBOOM_DATA") or BASE_DIR / "data")


def _load_secret_key() -> str:
    """密钥优先取环境变量，否则持久化生成到 data/secret_key。"""
    env = os.environ.get("EMAILBOOM_SECRET_KEY")
    if env:
        return env
    key_file = DATA_DIR / "secret_key"
    if key_file.exists():
        value = key_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(32)
    key_file.write_text(value, encoding="utf-8")
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return value


class Config:
    SECRET_KEY = _load_secret_key()

    # 路径（供各模块统一引用）
    BASE_DIR = BASE_DIR
    DATA_DIR = DATA_DIR

    # 数据库 / 上传
    DATABASE = str(DATA_DIR / "emailboom.db")
    ATTACHMENTS_DIR = DATA_DIR / "attachments"
    MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # 表单整体上限 32MB（附件）

    # 管理员初始账号（首次初始化时写入）
    ADMIN_USERNAME = os.environ.get("EMAILBOOM_ADMIN_USER", "admin")
    ADMIN_PASSWORD = os.environ.get("EMAILBOOM_ADMIN_PASS", "admin1324")

    # 后台调度器
    SCHEDULER_POLL_SECONDS = float(os.environ.get("EMAILBOOM_SCHEDULER_POLL", "2"))
    SCHEDULER_LOCK = DATA_DIR / "scheduler.lock"
    SCHEDULER_LOG = DATA_DIR / "scheduler.log"
    HEARTBEAT_TIMEOUT_SECONDS = 90      # 心跳超时判定 worker 崩溃
    MAX_CRASH_RECOVERIES = 5            # 单轮崩溃恢复上限，超过则任务判失败

    # Web
    HOST = os.environ.get("EMAILBOOM_HOST", "0.0.0.0")
    PORT = int(os.environ.get("EMAILBOOM_PORT", "5000"))
    AUTO_START_SCHEDULER = os.environ.get("EMAILBOOM_AUTO_SCHEDULER", "1") == "1"
