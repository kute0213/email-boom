"""SQLite 访问层：线程本地连接、PRAGMA、建表与初始数据。

每个进程（Web / 调度器 / Worker）各自持有连接；
WAL + busy_timeout 保证多进程并发读写安全。
"""
import os
import sqlite3
import threading
from datetime import datetime

from werkzeug.security import generate_password_hash

from config import Config

SCHEMA_VERSION = 1

_local = threading.local()


def now_str() -> str:
    """本地时间，统一格式（字典序 == 时间序）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db() -> sqlite3.Connection:
    """取当前线程的连接（线程本地缓存）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(Config.DATABASE, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def close_db(_exc=None):
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def query_all(sql: str, params=()) -> list:
    return get_db().execute(sql, params).fetchall()


def query_one(sql: str, params=()):
    return get_db().execute(sql, params).fetchone()


def execute(sql: str, params=()) -> sqlite3.Cursor:
    conn = get_db()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur


def executescript(sql: str):
    conn = get_db()
    conn.executescript(sql)
    conn.commit()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS smtp_servers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    host            TEXT NOT NULL,
    port            INTEGER NOT NULL DEFAULT 465,
    security        TEXT NOT NULL DEFAULT 'ssl',   -- ssl | tls | none
    username        TEXT NOT NULL DEFAULT '',
    password        TEXT NOT NULL DEFAULT '',
    from_email      TEXT NOT NULL DEFAULT '',
    from_name       TEXT NOT NULL DEFAULT '',
    timeout_seconds INTEGER NOT NULL DEFAULT 15,
    enabled         INTEGER NOT NULL DEFAULT 1,
    priority        INTEGER NOT NULL DEFAULT 100,  -- 数字越小优先级越高
    last_test_at    TEXT,
    last_test_ok    INTEGER,
    last_test_msg   TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    enabled             INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'pending',
    control             TEXT NOT NULL DEFAULT 'run',   -- run | pause | stop
    run_active          INTEGER NOT NULL DEFAULT 0,    -- 1 = 某一轮正在进行（用于暂停后恢复）

    -- 邮件内容
    subject             TEXT NOT NULL DEFAULT '',
    body_html           TEXT NOT NULL DEFAULT '',
    body_text           TEXT NOT NULL DEFAULT '',
    reply_to            TEXT NOT NULL DEFAULT '',
    cc                  TEXT NOT NULL DEFAULT '',
    bcc                 TEXT NOT NULL DEFAULT '',
    from_email          TEXT NOT NULL DEFAULT '',      -- 空 = 用服务器发件人
    from_name           TEXT NOT NULL DEFAULT '',
    custom_headers      TEXT NOT NULL DEFAULT '{}',
    attachments         TEXT NOT NULL DEFAULT '[]',    -- JSON [{name,size}]

    -- 多 SMTP 服务器
    smtp_server_ids     TEXT NOT NULL DEFAULT '[]',    -- JSON 数组
    smtp_strategy       TEXT NOT NULL DEFAULT 'round_robin', -- round_robin|random|priority|fixed
    failover_after      INTEGER NOT NULL DEFAULT 3,    -- 连续失败 N 次切换服务器，0=不切换

    -- 速度控制
    send_interval_ms    INTEGER NOT NULL DEFAULT 1000, -- 两封邮件之间的间隔
    jitter_ms           INTEGER NOT NULL DEFAULT 0,    -- 随机附加延迟 0..jitter
    batch_size          INTEGER NOT NULL DEFAULT 1,    -- 每批发送数量
    batch_interval_s    INTEGER NOT NULL DEFAULT 0,    -- 批次之间的停顿（秒）
    hourly_limit        INTEGER NOT NULL DEFAULT 0,    -- 每小时上限，0=不限

    -- 重试与超时
    max_retries         INTEGER NOT NULL DEFAULT 2,
    retry_backoff       TEXT NOT NULL DEFAULT 'exponential', -- fixed|linear|exponential
    retry_backoff_base_s INTEGER NOT NULL DEFAULT 5,
    connect_timeout_s   INTEGER NOT NULL DEFAULT 10,
    send_timeout_s      INTEGER NOT NULL DEFAULT 30,
    retry_permanent     INTEGER NOT NULL DEFAULT 0,    -- 5xx 永久失败是否也重试

    -- 安全阀
    stop_on_fail_percent INTEGER NOT NULL DEFAULT 0,   -- 失败率达到该百分比中止，0=关闭
    dedupe              INTEGER NOT NULL DEFAULT 0,    -- 1=跳过历史上已发送成功的收件人

    -- 定时与重复
    run_mode            TEXT NOT NULL DEFAULT 'manual', -- manual|once|cron|interval
    schedule_at         TEXT,                           -- run_mode=once 的执行时刻
    cron_expr           TEXT NOT NULL DEFAULT '',
    interval_minutes    INTEGER NOT NULL DEFAULT 60,
    max_runs            INTEGER NOT NULL DEFAULT 0,     -- 0=不限轮次
    run_count           INTEGER NOT NULL DEFAULT 0,
    next_run_at         TEXT,
    last_run_at         TEXT,

    -- 运行统计
    total_recipients    INTEGER NOT NULL DEFAULT 0,
    sent_count          INTEGER NOT NULL DEFAULT 0,
    failed_count        INTEGER NOT NULL DEFAULT 0,
    skipped_count       INTEGER NOT NULL DEFAULT 0,
    worker_pid          INTEGER NOT NULL DEFAULT 0,
    heartbeat_at        TEXT,
    recoveries          INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipients (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    email      TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    vars       TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'pending', -- pending|sent|failed|skipped
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    sent_at    TEXT,
    run_tag    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (task_id, email)
);
CREATE INDEX IF NOT EXISTS idx_recipients_task_status ON recipients(task_id, status);

CREATE TABLE IF NOT EXISTS task_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    level      TEXT NOT NULL DEFAULT 'info',   -- info|success|warn|error
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs(task_id, id);

PRAGMA user_version = %d;
""" % SCHEMA_VERSION


def init_db(force: bool = False):
    """建表（幂等）并初始化管理员。"""
    import config
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    Config.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

    if force and os.path.exists(Config.DATABASE):
        close_db()
        os.remove(Config.DATABASE)

    conn = get_db()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        conn.executescript(_SCHEMA)
        conn.commit()

    # 种子管理员
    row = query_one("SELECT id FROM users WHERE username = ?", (Config.ADMIN_USERNAME,))
    if row is None:
        execute(
            "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES (?,?,?,?)",
            (Config.ADMIN_USERNAME, generate_password_hash(Config.ADMIN_PASSWORD),
             now_str(), now_str()),
        )


def app_context_teardown(_exc=None):
    close_db(_exc)
