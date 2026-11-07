"""用户（管理员）模型。"""
from werkzeug.security import check_password_hash, generate_password_hash

from ..db import execute, now_str, query_one


def get_by_username(username: str):
    return query_one("SELECT * FROM users WHERE username = ?", (username,))


def get_by_id(user_id: int):
    return query_one("SELECT * FROM users WHERE id = ?", (user_id,))


def verify(username: str, password: str):
    user = get_by_username(username)
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None


def change_password(user_id: int, old_password: str, new_password: str) -> tuple[bool, str]:
    user = get_by_id(user_id)
    if not user:
        return False, "用户不存在"
    if not check_password_hash(user["password_hash"], old_password):
        return False, "当前密码不正确"
    if len(new_password) < 6:
        return False, "新密码长度至少 6 位"
    execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (generate_password_hash(new_password), now_str(), user_id),
    )
    return True, "密码修改成功"
