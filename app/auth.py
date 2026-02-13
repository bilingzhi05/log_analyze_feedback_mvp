import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import HTTPException, Request, Response

from .db import DB
from .utils import now_iso


def get_client_ip(request: Request) -> str:
    """
    功能：获取客户端 IP 地址。
    参数：request（请求对象）。
    返回值：IP 字符串。
    异常：无显式异常。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def record_login_failure(db: DB, username: str, user_ip: str) -> int:
    """
    功能：记录登录失败次数并返回最新次数。
    参数：connection（数据库连接）、username（用户名）、user_ip（IP 地址）。
    返回值：最新失败次数。
    异常：sqlite3.Error 数据库执行错误。
    """
    existing = db.fetch_one(
        """
        SELECT id, failed_count FROM admin_login_attempts
        WHERE username = ? AND user_ip = ?
        """,
        (username, user_ip),
    )
    now = now_iso()
    if existing:
        new_count = int(existing["failed_count"]) + 1
        db.execute_query(
            """
            UPDATE admin_login_attempts
            SET failed_count = ?, last_failed_at = ?
            WHERE id = ?
            """,
            (new_count, now, existing["id"]),
        )
        return new_count
    db.execute_query(
        """
        INSERT INTO admin_login_attempts (username, user_ip, failed_count, last_failed_at)
        VALUES (?, ?, ?, ?)
        """,
        (username, user_ip, 1, now),
    )
    return 1


def reset_login_failures(db: DB, username: str, user_ip: str) -> None:
    """
    功能：清除登录失败次数记录。
    参数：connection（数据库连接）、username（用户名）、user_ip（IP 地址）。
    返回值：无。
    异常：sqlite3.Error 数据库执行错误。
    """
    db.execute_query(
        """
        DELETE FROM admin_login_attempts WHERE username = ? AND user_ip = ?
        """,
        (username, user_ip),
    )


def get_failure_count(db: DB, username: str, user_ip: str) -> int:
    """
    功能：获取登录失败次数。
    参数：connection（数据库连接）、username（用户名）、user_ip（IP 地址）。
    返回值：失败次数。
    异常：sqlite3.Error 数据库执行错误。
    """
    count = db.fetch_value(
        """
        SELECT failed_count FROM admin_login_attempts
        WHERE username = ? AND user_ip = ?
        """,
        (username, user_ip),
    )
    return int(count or 0)


def create_captcha(db: DB) -> Tuple[str, str]:
    """
    功能：生成验证码并写入数据库。
    参数：connection（数据库连接）。
    返回值：captcha_id 与 captcha_code。
    异常：sqlite3.Error 数据库执行错误。
    """
    captcha_id = secrets.token_hex(8)
    captcha_code = secrets.token_hex(2).upper()
    expires_at = (datetime.now() + timedelta(minutes=5)).isoformat()
    db.execute_query(
        """
        INSERT INTO admin_captchas (id, code, expires_at)
        VALUES (?, ?, ?)
        """,
        (captcha_id, captcha_code, expires_at),
    )
    return captcha_id, captcha_code


def verify_captcha(db: DB, captcha_id: str, captcha_code: str) -> bool:
    """
    功能：校验验证码是否有效。
    参数：connection（数据库连接）、captcha_id（验证码 ID）、captcha_code（验证码文本）。
    返回值：校验是否通过。
    异常：sqlite3.Error 数据库执行错误。
    """
    row = db.fetch_one(
        "SELECT code, expires_at FROM admin_captchas WHERE id = ?",
        (captcha_id,),
    )
    if not row:
        return False
    if row["code"].upper() != captcha_code.upper():
        return False
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        return False
    db.execute_query(
        "DELETE FROM admin_captchas WHERE id = ?",
        (captcha_id,),
    )
    return True


def create_session(db: DB, username: str) -> Tuple[str, str]:
    """
    功能：创建管理员会话。
    参数：connection（数据库连接）、username（用户名）。
    返回值：session_id 与过期时间字符串。
    异常：sqlite3.Error 数据库执行错误。
    """
    session_id = secrets.token_hex(16)
    created_at = now_iso()
    expires_at = (datetime.now() + timedelta(hours=24)).isoformat()
    db.execute_query(
        """
        INSERT INTO admin_sessions (id, username, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (session_id, username, created_at, expires_at),
    )
    return session_id, expires_at


def require_admin(request: Request, db: DB) -> str:
    """
    功能：校验管理员会话并返回用户名。
    参数：request（请求对象）、connection（数据库连接）。
    返回值：管理员用户名。
    异常：HTTPException 未授权或会话过期。
    """
    session_id = request.cookies.get("admin_session")
    if not session_id:
        raise HTTPException(status_code=401, detail="未登录")
    row = db.fetch_one(
        "SELECT username, expires_at FROM admin_sessions WHERE id = ?",
        (session_id,),
    )
    if not row:
        raise HTTPException(status_code=401, detail="会话无效")
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        db.execute_query(
            "DELETE FROM admin_sessions WHERE id = ?",
            (session_id,),
        )
        raise HTTPException(status_code=401, detail="会话已过期")
    return row["username"]


def clear_session(response: Response, db: DB, request: Request) -> None:
    """
    功能：清理管理员会话并清除 Cookie。
    参数：response（响应对象）、connection（数据库连接）、request（请求对象）。
    返回值：无。
    异常：sqlite3.Error 数据库执行错误。
    """
    session_id = request.cookies.get("admin_session")
    if session_id:
        db.execute_query(
            "DELETE FROM admin_sessions WHERE id = ?",
            (session_id,),
        )
    response.delete_cookie("admin_session")
