"""SMTP 服务器模型。"""
from ..db import execute, now_str, query_all, query_one

_FIELDS = (
    "name, host, port, security, username, password, from_email, from_name, "
    "timeout_seconds, enabled, priority"
)


def _to_dict(row):
    """sqlite3.Row → dict（模板与 mailer 需要 .get()）。"""
    return dict(row) if row is not None else None


def list_all(only_enabled: bool = False) -> list:
    if only_enabled:
        rows = query_all(
            f"SELECT * FROM smtp_servers WHERE enabled = 1 ORDER BY priority ASC, id ASC"
        )
    else:
        rows = query_all("SELECT * FROM smtp_servers ORDER BY priority ASC, id ASC")
    return [dict(r) for r in rows]


def get(server_id: int):
    return _to_dict(query_one("SELECT * FROM smtp_servers WHERE id = ?", (server_id,)))


def get_many(ids) -> list:
    ids = [int(i) for i in ids]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = query_all(
        f"SELECT * FROM smtp_servers WHERE id IN ({placeholders}) ORDER BY priority ASC, id ASC",
        tuple(ids),
    )
    return [dict(r) for r in rows]


def create(data: dict) -> int:
    cur = execute(
        f"INSERT INTO smtp_servers ({_FIELDS}, created_at, updated_at) "
        f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            data["name"], data["host"], int(data.get("port", 465)),
            data.get("security", "ssl"), data.get("username", ""),
            data.get("password", ""), data.get("from_email", ""),
            data.get("from_name", ""), int(data.get("timeout_seconds", 15)),
            int(data.get("enabled", 1)), int(data.get("priority", 100)),
            now_str(), now_str(),
        ),
    )
    return cur.lastrowid


def update(server_id: int, data: dict):
    execute(
        "UPDATE smtp_servers SET name=?, host=?, port=?, security=?, username=?, password=?, "
        "from_email=?, from_name=?, timeout_seconds=?, enabled=?, priority=?, updated_at=? "
        "WHERE id=?",
        (
            data["name"], data["host"], int(data.get("port", 465)),
            data.get("security", "ssl"), data.get("username", ""),
            data.get("password", ""), data.get("from_email", ""),
            data.get("from_name", ""), int(data.get("timeout_seconds", 15)),
            int(data.get("enabled", 1)), int(data.get("priority", 100)),
            now_str(), server_id,
        ),
    )


def delete(server_id: int):
    execute("DELETE FROM smtp_servers WHERE id = ?", (server_id,))


def record_test(server_id: int, ok: bool, message: str):
    execute(
        "UPDATE smtp_servers SET last_test_at=?, last_test_ok=?, last_test_msg=? WHERE id=?",
        (now_str(), 1 if ok else 0, message[:2000], server_id),
    )


def public_dict(row, hide_password: bool = True) -> dict:
    d = dict(row)
    if hide_password:
        d["password"] = ""
    return d
