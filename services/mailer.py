"""邮件构造与 SMTP 发送（含错误分类、连接测试）。"""
import mimetypes
import smtplib
import socket
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from jinja2 import Template, TemplateError, UndefinedError

from config import Config


class RecipientRenderError(ValueError):
    """收件人级模板渲染失败（不应重试整封，仅该收件人失败）。"""


def render(text: str, context: dict) -> str:
    """用 Jinja2 渲染邮件内容，变量缺失渲染为空串。"""
    if not text or "{{" not in text and "{%" not in text:
        return text or ""
    try:
        return Template(text, autoescape=False).render(**context)
    except (TemplateError, UndefinedError) as e:
        raise RecipientRenderError(f"模板渲染失败：{e}") from e


def build_context(recipient: dict) -> dict:
    ctx = dict(recipient.get("vars") or {})
    ctx.setdefault("name", recipient.get("name") or "")
    ctx["email"] = recipient["email"]
    return ctx


def _split_addresses(raw: str) -> list:
    out = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.append(part)
    return out


def build_message(task: dict, recipient: dict, server: dict,
                  attachment_paths: list) -> tuple[EmailMessage, list]:
    """构造一封待发邮件（已渲染个性化内容）。"""
    ctx = build_context(recipient)
    subject = render(task.get("subject") or "", ctx)
    body_text = render(task.get("body_text") or "", ctx)
    body_html = render(task.get("body_html") or "", ctx)

    from_email = (task.get("from_email") or "").strip() or (server.get("from_email") or "").strip()
    from_name = (task.get("from_name") or "").strip() or (server.get("from_name") or "").strip()
    if not from_email:
        # 最后兜底：用登录用户名（若像邮箱）
        un = (server.get("username") or "").strip()
        from_email = un if "@" in un else (recipient["email"])

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, from_email)) if from_name else from_email
    msg["To"] = recipient["email"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_email.split("@")[-1] if "@" in from_email else None)

    cc = _split_addresses(task.get("cc"))
    bcc = _split_addresses(task.get("bcc"))
    if cc:
        msg["Cc"] = ", ".join(cc)
    reply_to = (task.get("reply_to") or "").strip()
    if reply_to:
        msg["Reply-To"] = reply_to
    for k, v in (task.get("custom_headers") or {}).items():
        if k and v and k.lower() not in ("from", "to", "subject", "date", "message-id"):
            try:
                msg[k] = str(v)
            except Exception:
                pass

    if body_html:
        if body_text:
            msg.set_content(body_text)
            msg.add_alternative(body_html, subtype="html")
        else:
            msg.set_content(body_html, subtype="html")
    else:
        msg.set_content(body_text or subject)

    # 存储名 uuid_原名 → 原始文件名（secure_filename 会剥离中文，需用元数据还原）
    meta_names = {
        a.get("file"): a.get("name")
        for a in (task.get("attachments") or [])
        if isinstance(a, dict) and a.get("file") and a.get("name")
    }
    for path in attachment_paths:
        try:
            ctype, _enc = mimetypes.guess_type(str(path))
            if ctype is None or "/" not in ctype:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            original = meta_names.get(path.name) or path.name.split("_", 1)[-1]
            with open(path, "rb") as f:
                msg.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                                   filename=original)
        except OSError:
            continue

    # 收件人合并（To + Cc + Bcc）
    recipients = [recipient["email"]] + cc + bcc
    return msg, recipients


def classify_error(exc: Exception) -> str:
    """错误分类：transient=可重试 / permanent=不可重试 / auth=服务器级失败。"""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "auth"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [c[0] for c in exc.recipients.values()] if getattr(exc, "recipients", None) else [550]
        return "permanent" if all(c >= 500 for c in codes) else "transient"
    if isinstance(exc, smtplib.SMTPDataError):
        code = exc.smtp_code or 500
        return "permanent" if code >= 500 else "transient"
    if isinstance(exc, smtplib.SMTPResponseException):
        code = exc.smtp_code or 500
        return "permanent" if code >= 500 else "transient"
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPException,
                        OSError, socket.error, TimeoutError)):
        return "transient"
    return "transient"


def send_once(task: dict, recipient: dict, server: dict, attachment_paths: list) -> list:
    """单次发送（连接-投递-断开）。成功返回收件人地址列表；失败抛异常。"""
    host = server["host"]
    port = int(server.get("port") or 465)
    security = server.get("security") or "ssl"
    timeout = int(server.get("send_timeout_s") or server.get("timeout_seconds") or 15)
    connect_timeout = int(task.get("connect_timeout_s") or timeout)

    msg, recipients = build_message(task, recipient, server, attachment_paths)

    if security == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=connect_timeout)
    else:
        smtp = smtplib.SMTP(host, port, timeout=connect_timeout)
    try:
        smtp.ehlo()
        if security == "tls":
            try:
                smtp.starttls()
                smtp.ehlo()
            except smtplib.SMTPNotSupportedError:
                pass
        username = (server.get("username") or "").strip()
        if username:
            smtp.login(username, server.get("password") or "")
        # 投递阶段用发送超时
        if smtp.sock is not None:
            smtp.sock.settimeout(timeout)
        smtp.send_message(msg, to_addrs=recipients)
        return recipients
    finally:
        try:
            smtp.quit()
        except Exception:
            try:
                smtp.close()
            except Exception:
                pass


def test_connection(server: dict) -> tuple[bool, str]:
    """后台/表单里的“测试连接”。"""
    host = (server.get("host") or "").strip()
    if not host:
        return False, "缺少主机地址"
    port = int(server.get("port") or 465)
    security = server.get("security") or "ssl"
    timeout = int(server.get("timeout_seconds") or 15)
    smtp = None
    try:
        if security == "ssl":
            smtp = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            smtp = smtplib.SMTP(host, port, timeout=timeout)
        code, banner = smtp.ehlo()
        if security == "tls":
            try:
                smtp.starttls()
                code, banner = smtp.ehlo()
            except smtplib.SMTPNotSupportedError:
                pass
        username = (server.get("username") or "").strip()
        if username:
            smtp.login(username, server.get("password") or "")
            msg = f"连接、登录成功（{host}:{port}）"
        else:
            msg = f"连接成功，未配置账号免登录（{host}:{port}）"
        if banner:
            msg += f" · {str(banner).splitlines()[0][:80]}"
        return True, msg
    except smtplib.SMTPAuthenticationError as e:
        return False, f"认证失败：{e.smtp_error!s}"[:400]
    except Exception as e:  # noqa: BLE001 —— 测试接口需要给出完整原因
        return False, f"{type(e).__name__}: {e}"[:400]
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass


def backoff_seconds(task: dict, attempt: int) -> float:
    """第 attempt 次失败后的等待秒数（attempt 从 1 开始）。"""
    base = max(1, int(task.get("retry_backoff_base_s") or 5))
    mode = task.get("retry_backoff") or "exponential"
    if mode == "fixed":
        delay = base
    elif mode == "linear":
        delay = base * attempt
    else:
        delay = base * (2 ** (attempt - 1))
    return float(min(delay, 3600))


__all__ = [
    "RecipientRenderError", "build_context", "build_message", "classify_error",
    "send_once", "test_connection", "backoff_seconds", "render",
]
