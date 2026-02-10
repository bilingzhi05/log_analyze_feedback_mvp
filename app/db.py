import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

class Database:
    def __init__(self, database_path: str) -> None:
        self.connection = get_db_connection(database_path)
        init_db(self.connection)
def get_db_connection(database_path: str) -> sqlite3.Connection:
    """
    功能：创建并返回 SQLite 数据库连接。
    参数：database_path（数据库文件路径）。
    返回值：sqlite3.Connection 实例。
    异常：sqlite3.Error 数据库连接错误。
    """
    connection = sqlite3.connect(database_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def init_db(connection: sqlite3.Connection) -> None:
    """
    功能：初始化数据库表结构与索引。
    参数：connection（数据库连接）。
    返回值：无。
    异常：sqlite3.Error 数据库执行错误。
    """
    cursor = connection.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS feedbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            sentiment TEXT NOT NULL,
            content TEXT NOT NULL,
            user_ip TEXT NOT NULL,
            attachments TEXT NOT NULL,
            status TEXT NOT NULL,
            jira_key TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            category TEXT NOT NULL,
            message TEXT NOT NULL,
            metadata TEXT NOT NULL,
            related_feedback_id INTEGER
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_sessions (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            user_ip TEXT NOT NULL,
            failed_count INTEGER NOT NULL,
            last_failed_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_captchas (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedbacks_created_at ON feedbacks(created_at)"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedbacks_status ON feedbacks(status)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_feedbacks_sentiment ON feedbacks(sentiment)"
    )
    connection.commit()


def execute_query(
    connection: sqlite3.Connection,
    sql: str,
    params: Tuple[Any, ...] = (),
) -> sqlite3.Cursor:
    """
    功能：执行写入类 SQL 并返回游标。
    参数：connection（数据库连接）、sql（SQL 语句）、params（参数元组）。
    返回值：sqlite3.Cursor。
    异常：sqlite3.Error 数据库执行错误。
    """
    cursor = connection.cursor()
    cursor.execute(sql, params)
    connection.commit()
    return cursor


def fetch_all(
    connection: sqlite3.Connection,
    sql: str,
    params: Tuple[Any, ...] = (),
) -> List[Dict[str, Any]]:
    """
    功能：执行查询并返回结果列表。
    参数：connection（数据库连接）、sql（SQL 语句）、params（参数元组）。
    返回值：列表形式的字典结果。
    异常：sqlite3.Error 数据库执行错误。
    """
    cursor = connection.cursor()
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    return [dict(row) for row in rows]


def fetch_one(
    connection: sqlite3.Connection,
    sql: str,
    params: Tuple[Any, ...] = (),
) -> Optional[Dict[str, Any]]:
    """
    功能：执行查询并返回单条结果。
    参数：connection（数据库连接）、sql（SQL 语句）、params（参数元组）。
    返回值：单条字典结果或 None。
    异常：sqlite3.Error 数据库执行错误。
    """
    cursor = connection.cursor()
    cursor.execute(sql, params)
    row = cursor.fetchone()
    return dict(row) if row else None


def fetch_value(
    connection: sqlite3.Connection,
    sql: str,
    params: Tuple[Any, ...] = (),
) -> Any:
    """
    功能：执行查询并返回单个值。
    参数：connection（数据库连接）、sql（SQL 语句）、params（参数元组）。
    返回值：查询结果的第一个字段值。
    异常：sqlite3.Error 数据库执行错误。
    """
    cursor = connection.cursor()
    cursor.execute(sql, params)
    row = cursor.fetchone()
    return row[0] if row else None
