"""收件人模型：导入解析、状态流转、按轮次重置。"""
import csv
import io
import json
import re
import uuid
from datetime import datetime, timedelta

from ..db import execute, now_str, query_all, query_one

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_email(raw: str) -> str:
    raw = raw.strip().strip("<>")
    m = re.search(r"<([^>]+)>", raw)
    if m:
        raw = m.group(1).strip()
    return raw


def parse_lines(text: str) -> list:
    """逐行解析：邮箱[,姓名[,JSON变量]]，支持 姓名 <邮箱>。"""
    result, seen = [], set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        email = _clean_email(parts[0])
        if not email or email.lower() in seen:
            continue
        if not _EMAIL_RE.match(email):
            raise ValueError(f"邮箱格式不正确：{parts[0]}")
        seen.add(email.lower())
        name = parts[1] if len(parts) > 1 else ""
        # 兼容 "张三 <a@b.com>" 单列写法
        if not name and "<" in parts[0]:
            m = re.match(r"^(.*?)\s*<", parts[0])
            name = m.group(1).strip().strip('"') if m else ""
        vars_ = {}
        if len(parts) > 2:
            rest = ",".join(parts[2:])
            if rest:
                try:
                    parsed = json.loads(rest)
                    if isinstance(parsed, dict):
                        vars_ = parsed
                except ValueError:
                    vars_ = {"extra": rest}
        result.append({"email": email, "name": name, "vars": vars_})
    return result


def parse_csv(text: str) -> list:
    """CSV（首行为表头）：必须含 email/邮箱 列，name/姓名 列可选，其余进变量。"""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV 没有表头")
    field_map = {f.strip().lower(): f for f in reader.fieldnames if f}
    email_key = next((field_map[k] for k in ("email", "mail", "邮箱", "电子邮件")
                      if k in field_map), None)
    if not email_key:
        raise ValueError("CSV 缺少 email/邮箱 列")
    name_key = next((field_map[k] for k in ("name", "姓名", "称呼") if k in field_map), None)
    result, seen = [], set()
    for row in reader:
        email = _clean_email(str(row.get(email_key) or ""))
        if not email or email.lower() in seen:
            continue
        if not _EMAIL_RE.match(email):
            raise ValueError(f"邮箱格式不正确：{email}")
        seen.add(email.lower())
        vars_ = {
            k: (v or "").strip()
            for k, v in row.items()
            if k and k not in (email_key, name_key) and (v or "").strip()
        }
        result.append({
            "email": email,
            "name": (str(row.get(name_key) or "").strip() if name_key else ""),
            "vars": vars_,
        })
    return result


def replace_all(task_id: int, items: list):
    """整体替换任务的收件人列表。"""
    execute("DELETE FROM recipients WHERE task_id = ?", (task_id,))
    if not items:
        return
    now = now_str()
    conn_rows = [
        (task_id, it["email"], it["name"], json.dumps(it.get("vars") or {}, ensure_ascii=False),
         now)
        for it in items
    ]
    from ..db import get_db
    conn = get_db()
    conn.executemany(
        "INSERT OR IGNORE INTO recipients (task_id, email, name, vars, created_at) "
        "VALUES (?,?,?,?,?)",
        conn_rows,
    )
    conn.commit()


def count(task_id: int, status: str = None) -> int:
    if status:
        row = query_one(
            "SELECT COUNT(*) FROM recipients WHERE task_id=? AND status=?",
            (task_id, status),
        )
    else:
        row = query_one("SELECT COUNT(*) FROM recipients WHERE task_id=?", (task_id,))
    return int(row[0]) if row else 0


def counts(task_id: int) -> dict:
    rows = query_all(
        "SELECT status, COUNT(*) AS c FROM recipients WHERE task_id=? GROUP BY status",
        (task_id,),
    )
    base = {"pending": 0, "sent": 0, "failed": 0, "skipped": 0}
    base.update({r["status"]: r["c"] for r in rows})
    base["total"] = sum(v for k, v in base.items() if k != "total")
    return base


def for_task(task_id: int, status: str = None, limit: int = 500, offset: int = 0) -> list:
    if status:
        rows = query_all(
            "SELECT * FROM recipients WHERE task_id=? AND status=? "
            "ORDER BY id LIMIT ? OFFSET ?",
            (task_id, status, limit, offset),
        )
    else:
        rows = query_all(
            "SELECT * FROM recipients WHERE task_id=? ORDER BY id LIMIT ? OFFSET ?",
            (task_id, limit, offset),
        )
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["vars"] = json.loads(d.get("vars") or "{}")
        except ValueError:
            d["vars"] = {}
        out.append(d)
    return out


# ---------- 新一轮准备 ----------

def prepare_run(task_id: int, dedupe: bool) -> tuple[int, int]:
    """按去重策略把收件人重置为待发送。

    dedupe=True：历史成功者标记为 skipped（本轮不再发），失败者重新排队。
    dedupe=False：全部重置为 pending（全量重发）。
    返回 (本轮待发送数, 本轮跳过数)。
    """
    if dedupe:
        execute(
            "UPDATE recipients SET status='pending', attempts=0, last_error='', run_tag='' "
            "WHERE task_id=? AND status='failed'",
            (task_id,),
        )
        execute(
            "UPDATE recipients SET status='skipped', run_tag='', "
            "last_error='此前已发送成功，按去重规则跳过' "
            "WHERE task_id=? AND status='sent'",
            (task_id,),
        )
        execute(
            "UPDATE recipients SET last_error='此前已发送成功，按去重规则跳过' "
            "WHERE task_id=? AND status='skipped'",
            (task_id,),
        )
    else:
        execute(
            "UPDATE recipients SET status='pending', attempts=0, last_error='', "
            "sent_at=NULL, run_tag='' WHERE task_id=?",
            (task_id,),
        )
    pending = count(task_id, "pending")
    skipped = count(task_id, "skipped")
    return pending, skipped


def pending_batch(task_id: int, limit: int = 500) -> list:
    rows = query_all(
        "SELECT * FROM recipients WHERE task_id=? AND status='pending' ORDER BY id LIMIT ?",
        (task_id, limit),
    )
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["vars"] = json.loads(d.get("vars") or "{}")
        except ValueError:
            d["vars"] = {}
        out.append(d)
    return out


def mark_sent(task_id: int, recipient_id: int, attempts: int):
    execute(
        "UPDATE recipients SET status='sent', attempts=?, last_error='', sent_at=?, "
        "run_tag=? WHERE id=?",
        (attempts, now_str(), uuid.uuid4().hex[:12], recipient_id),
    )


def mark_failed(task_id: int, recipient_id: int, attempts: int, error: str):
    execute(
        "UPDATE recipients SET status='failed', attempts=?, last_error=? WHERE id=?",
        (attempts, (error or "")[:2000], recipient_id),
    )


def mark_skipped(task_id: int, recipient_id: int, reason: str = "已发送过，按去重规则跳过"):
    execute(
        "UPDATE recipients SET status='skipped', last_error=? WHERE id=?",
        (reason[:500], recipient_id),
    )


def sent_within_hours(task_id: int, hours: int = 1) -> int:
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    row = query_one(
        "SELECT COUNT(*) FROM recipients WHERE task_id=? AND sent_at >= ?",
        (task_id, since),
    )
    return int(row[0]) if row else 0
