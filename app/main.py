import io
import os
import csv
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .auth import (
    clear_session,
    create_captcha,
    create_session,
    get_client_ip,
    get_failure_count,
    record_login_failure,
    require_admin,
    reset_login_failures,
    verify_captcha,
)
from .config import load_config
from .db import execute_query, fetch_all, fetch_one, fetch_value, get_db_connection, init_db
from .jira_client import create_jira_issue, get_jira_status
from .utils import (
    build_attachment_dir,
    ensure_directory,
    format_recent_days,
    get_extension,
    json_dumps,
    json_loads,
    now_iso,
    parse_datetime,
    sanitize_filename,
    save_upload_file,
    validate_content,
    validate_files,
    validate_sentiment,
)


config = load_config()
connection = get_db_connection(config.database_path)
init_db(connection)
ensure_directory(config.attachments_root)

app = FastAPI()
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def log_event(
    category: str,
    message: str,
    metadata: Dict[str, Any],
    related_feedback_id: Optional[int] = None,
) -> None:
    """
    功能：写入日志记录到数据库。
    参数：category（日志类型）、message（摘要）、metadata（附加信息）、related_feedback_id（关联反馈 ID）。
    返回值：无。
    异常：sqlite3.Error 数据库执行错误。
    """
    execute_query(
        connection,
        """
        INSERT INTO logs (created_at, category, message, metadata, related_feedback_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (now_iso(), category, message, json_dumps(metadata), related_feedback_id),
    )


def scan_attachment(path: str) -> bool:
    """
    功能：进行附件病毒扫描占位校验。
    参数：path（文件路径）。
    返回值：是否通过扫描。
    异常：无显式异常。
    """
    return True


def parse_pagination(request: Request, default_size: int = 50) -> Dict[str, int]:
    """
    功能：解析分页参数并返回 page/page_size。
    参数：request（请求对象）、default_size（默认分页大小）。
    返回值：包含 page 与 page_size 的字典。
    异常：ValueError 分页参数非法。
    """
    page = int(request.query_params.get("page", "1"))
    page_size = int(request.query_params.get("page_size", str(default_size)))
    if page < 1:
        raise ValueError("page 必须大于等于 1")
    if page_size not in {20, 50, 100}:
        page_size = default_size
    return {"page": page, "page_size": page_size}


def check_rate_limit(user_ip: str) -> None:
    """
    功能：校验 10 分钟内同一 IP 的提交次数。
    参数：user_ip（IP 地址）。
    返回值：无。
    异常：HTTPException 超过限制。
    """
    since = (datetime.now() - timedelta(minutes=10)).isoformat()
    count = fetch_value(
        connection,
        """
        SELECT COUNT(*) FROM feedbacks
        WHERE user_ip = ? AND created_at >= ?
        """,
        (user_ip, since),
    )
    if int(count or 0) >= 3:
        raise HTTPException(status_code=429, detail="10 分钟内最多提交 3 次")


def fetch_feedback(feedback_id: int) -> Dict[str, Any]:
    """
    功能：读取指定反馈详情。
    参数：feedback_id（反馈 ID）。
    返回值：反馈数据字典。
    异常：HTTPException 反馈不存在。
    """
    row = fetch_one(connection, "SELECT * FROM feedbacks WHERE id = ?", (feedback_id,))
    if not row:
        raise HTTPException(status_code=404, detail="反馈不存在")
    row["attachments"] = json_loads(row["attachments"])
    return row


def delete_feedback_files(attachments: List[Dict[str, Any]]) -> None:
    """
    功能：删除反馈相关的附件文件。
    参数：attachments（附件列表）。
    返回值：无。
    异常：OSError 文件删除失败。
    """
    for item in attachments:
        path = item.get("path")
        if path and os.path.exists(path):
            os.remove(path)


@app.get("/")
async def serve_index() -> FileResponse:
    """
    功能：返回前台页面入口文件。
    参数：无。
    返回值：FileResponse。
    异常：FileNotFoundError 文件不存在。
    """
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/admin")
async def serve_admin() -> FileResponse:
    """
    功能：返回后台页面入口文件。
    参数：无。
    返回值：FileResponse。
    异常：FileNotFoundError 文件不存在。
    """
    return FileResponse(os.path.join(WEB_DIR, "admin.html"))


@app.post("/api/feedbacks")
async def create_feedback(request: Request) -> JSONResponse:
    """
    功能：提交用户反馈并保存附件。
    参数：request（请求对象）。
    返回值：JSONResponse，包含反馈 ID。
    异常：HTTPException 参数校验或上传失败。
    """
    user_ip = get_client_ip(request)
    check_rate_limit(user_ip)
    content_type = request.headers.get("content-type", "")
    sentiment = ""
    content = ""
    files: List[Any] = []
    if "multipart/form-data" in content_type:
        form = await request.form()
        sentiment = str(form.get("sentiment") or "")
        content = str(form.get("content") or "")
        files = [item for item in form.getlist("attachments") if hasattr(item, "filename")]
    else:
        data = await request.json()
        sentiment = str(data.get("sentiment") or "")
        content = str(data.get("content") or "")
    try:
        validate_sentiment(sentiment)
        validate_content(content)
        validate_files(files)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    created_at = now_iso()
    cursor = execute_query(
        connection,
        """
        INSERT INTO feedbacks (
            created_at, updated_at, sentiment, content, user_ip, attachments, status, jira_key
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (created_at, created_at, sentiment, content, user_ip, "[]", "pending", None),
    )
    feedback_id = cursor.lastrowid

    attachments_info: List[Dict[str, Any]] = []
    if files:
        attachment_dir = build_attachment_dir(config.attachments_root, feedback_id)
        ensure_directory(attachment_dir)
        for upload in files:
            extension = get_extension(upload.filename or "")
            safe_name = sanitize_filename(upload.filename or f"file.{extension}")
            target_path = os.path.join(attachment_dir, safe_name)
            try:
                info = save_upload_file(upload, target_path, 20 * 1024 * 1024)
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            if not scan_attachment(target_path):
                raise HTTPException(status_code=400, detail="附件扫描未通过")
            attachments_info.append(info)

    execute_query(
        connection,
        """
        UPDATE feedbacks SET attachments = ?, updated_at = ?
        WHERE id = ?
        """,
        (json_dumps(attachments_info), now_iso(), feedback_id),
    )
    log_event("run", "用户提交反馈", {"sentiment": sentiment}, feedback_id)
    return JSONResponse({"id": feedback_id})


@app.get("/api/feedbacks")
async def list_feedbacks(request: Request) -> JSONResponse:
    """
    功能：按条件查询反馈列表。
    参数：request（请求对象）。
    返回值：JSONResponse，包含列表与分页信息。
    异常：HTTPException 权限或参数错误。
    """
    require_admin(request, connection)
    try:
        pagination = parse_pagination(request)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    sentiment = request.query_params.get("sentiment")
    status = request.query_params.get("status")
    keyword = request.query_params.get("q")
    from_time = parse_datetime(request.query_params.get("from"))
    to_time = parse_datetime(request.query_params.get("to"))

    conditions = []
    params: List[Any] = []
    if sentiment:
        conditions.append("sentiment = ?")
        params.append(sentiment)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if keyword:
        conditions.append("content LIKE ?")
        params.append(f"%{keyword}%")
    if from_time:
        conditions.append("created_at >= ?")
        params.append(from_time.isoformat())
    if to_time:
        conditions.append("created_at <= ?")
        params.append(to_time.isoformat())
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    total = fetch_value(
        connection,
        f"SELECT COUNT(*) FROM feedbacks {where_clause}",
        tuple(params),
    )
    offset = (pagination["page"] - 1) * pagination["page_size"]
    items = fetch_all(
        connection,
        f"""
        SELECT id, created_at, sentiment, content, user_ip, status, jira_key
        FROM feedbacks
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        tuple(params + [pagination["page_size"], offset]),
    )
    for item in items:
        item["summary"] = item["content"][:100]
    return JSONResponse(
        {
            "items": items,
            "total": int(total or 0),
            "page": pagination["page"],
            "page_size": pagination["page_size"],
        }
    )


@app.get("/api/feedbacks/{feedback_id}")
async def get_feedback_detail(request: Request, feedback_id: int) -> JSONResponse:
    """
    功能：获取反馈详情。
    参数：request（请求对象）、feedback_id（反馈 ID）。
    返回值：JSONResponse，包含反馈详情。
    异常：HTTPException 反馈不存在或权限错误。
    """
    require_admin(request, connection)
    feedback = fetch_feedback(feedback_id)
    return JSONResponse(feedback)


@app.put("/api/feedbacks/{feedback_id}/review")
async def review_feedback(request: Request, feedback_id: int) -> JSONResponse:
    """
    功能：更新反馈评审状态。
    参数：request（请求对象）、feedback_id（反馈 ID）。
    返回值：JSONResponse，包含更新后的反馈。
    异常：HTTPException 状态非法或权限错误。
    """
    reviewer = require_admin(request, connection)
    data = await request.json()
    status = str(data.get("status") or "")
    note = str(data.get("note") or "")
    if status not in {"pending", "accepted", "rejected", "followup"}:
        raise HTTPException(status_code=400, detail="状态非法")
    execute_query(
        connection,
        "UPDATE feedbacks SET status = ?, updated_at = ? WHERE id = ?",
        (status, now_iso(), feedback_id),
    )
    log_event(
        "analysis",
        "反馈评审状态更新",
        {"status": status, "note": note, "reviewer": reviewer},
        feedback_id,
    )
    feedback = fetch_feedback(feedback_id)
    return JSONResponse(feedback)


@app.delete("/api/feedbacks/{feedback_id}")
async def delete_feedback(request: Request, feedback_id: int) -> JSONResponse:
    """
    功能：删除反馈及其附件。
    参数：request（请求对象）、feedback_id（反馈 ID）。
    返回值：JSONResponse，包含删除结果。
    异常：HTTPException 反馈不存在或权限错误。
    """
    operator = require_admin(request, connection)
    feedback = fetch_feedback(feedback_id)
    delete_feedback_files(feedback.get("attachments", []))
    execute_query(connection, "DELETE FROM feedbacks WHERE id = ?", (feedback_id,))
    log_event("analysis", "反馈删除", {"operator": operator}, feedback_id)
    return JSONResponse({"deleted": True})


@app.post("/api/feedbacks/{feedback_id}/jira")
async def create_feedback_jira(request: Request, feedback_id: int) -> JSONResponse:
    """
    功能：为反馈创建 Jira 问题。
    参数：request（请求对象）、feedback_id（反馈 ID）。
    返回值：JSONResponse，包含 jira_key 与 url。
    异常：HTTPException 配置缺失或创建失败。
    """
    operator = require_admin(request, connection)
    feedback = fetch_feedback(feedback_id)
    data = await request.json()
    project_key = str(data.get("project_key") or config.jira_project_key or "")
    issue_type = str(data.get("issue_type") or "")
    priority = str(data.get("priority") or "")
    if not (config.jira_base_url and config.jira_auth_token):
        raise HTTPException(status_code=400, detail="Jira 配置缺失")
    if not (project_key and issue_type and priority):
        raise HTTPException(status_code=400, detail="Jira 参数缺失")
    summary = f"用户建议反馈 #{feedback_id}"
    description = (
        f"情感：{feedback['sentiment']}\n"
        f"时间：{feedback['created_at']}\n"
        f"IP：{feedback['user_ip']}\n"
        f"内容：\n{feedback['content']}\n"
    )
    try:
        jira_key, jira_url = create_jira_issue(
            config.jira_base_url,
            config.jira_auth_token,
            project_key,
            issue_type,
            priority,
            summary,
            description,
        )
    except Exception as error:
        log_event("error", "Jira 创建失败", {"error": str(error)}, feedback_id)
        raise HTTPException(status_code=502, detail="Jira 创建失败") from error
    execute_query(
        connection,
        "UPDATE feedbacks SET jira_key = ?, updated_at = ? WHERE id = ?",
        (jira_key, now_iso(), feedback_id),
    )
    log_event(
        "analysis",
        "Jira 创建成功",
        {"jira_key": jira_key, "operator": operator},
        feedback_id,
    )
    return JSONResponse({"jira_key": jira_key, "url": jira_url})


@app.get("/api/jira/{jira_key}")
async def get_jira_info(request: Request, jira_key: str) -> JSONResponse:
    """
    功能：获取 Jira 状态信息。
    参数：request（请求对象）、jira_key（问题 Key）。
    返回值：JSONResponse，包含状态与链接。
    异常：HTTPException 配置缺失或查询失败。
    """
    require_admin(request, connection)
    if not (config.jira_base_url and config.jira_auth_token):
        raise HTTPException(status_code=400, detail="Jira 配置缺失")
    try:
        status, url = get_jira_status(
            config.jira_base_url,
            config.jira_auth_token,
            jira_key,
        )
    except Exception as error:
        log_event("error", "Jira 状态查询失败", {"error": str(error), "jira_key": jira_key})
        raise HTTPException(status_code=502, detail="Jira 查询失败") from error
    return JSONResponse({"status": status, "url": url})


@app.get("/api/stats/feedbacks")
async def stats_feedbacks(request: Request) -> JSONResponse:
    """
    功能：获取反馈统计数据。
    参数：request（请求对象）。
    返回值：JSONResponse，包含统计数据。
    异常：HTTPException 权限错误。
    """
    require_admin(request, connection)
    total = fetch_value(connection, "SELECT COUNT(*) FROM feedbacks")
    like_count = fetch_value(
        connection, "SELECT COUNT(*) FROM feedbacks WHERE sentiment = 'like'"
    )
    dislike_count = fetch_value(
        connection, "SELECT COUNT(*) FROM feedbacks WHERE sentiment = 'dislike'"
    )
    days = format_recent_days(7)
    recent_7d = []
    for day in days:
        next_day = (
            datetime.fromisoformat(day) + timedelta(days=1)
        ).date().isoformat()
        count = fetch_value(
            connection,
            """
            SELECT COUNT(*) FROM feedbacks
            WHERE created_at >= ? AND created_at < ?
            """,
            (f"{day}T00:00:00+00:00", f"{next_day}T00:00:00+00:00"),
        )
        recent_7d.append({"date": day, "count": int(count or 0)})
    return JSONResponse(
        {
            "total": int(total or 0),
            "like": int(like_count or 0),
            "dislike": int(dislike_count or 0),
            "recent_7d": recent_7d,
        }
    )


@app.get("/api/logs")
async def list_logs(request: Request) -> Response:
    """
    功能：查询日志列表或导出日志。
    参数：request（请求对象）。
    返回值：JSONResponse 或 StreamingResponse。
    异常：HTTPException 权限或参数错误。
    """
    require_admin(request, connection)
    try:
        pagination = parse_pagination(request, default_size=50)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    category = request.query_params.get("category")
    keyword = request.query_params.get("q")
    from_time = parse_datetime(request.query_params.get("from"))
    to_time = parse_datetime(request.query_params.get("to"))
    export_format = request.query_params.get("format")

    conditions = []
    params: List[Any] = []
    if category:
        conditions.append("category = ?")
        params.append(category)
    if keyword:
        conditions.append("message LIKE ?")
        params.append(f"%{keyword}%")
    if from_time:
        conditions.append("created_at >= ?")
        params.append(from_time.isoformat())
    if to_time:
        conditions.append("created_at <= ?")
        params.append(to_time.isoformat())
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    if export_format in {"csv", "json"}:
        items = fetch_all(
            connection,
            f"""
            SELECT id, created_at, category, message, metadata, related_feedback_id
            FROM logs
            {where_clause}
            ORDER BY created_at DESC
            """,
            tuple(params),
        )
        if export_format == "json":
            return JSONResponse({"items": items})
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "created_at", "category", "message", "metadata", "related_feedback_id"])
        for item in items:
            writer.writerow(
                [
                    item["id"],
                    item["created_at"],
                    item["category"],
                    item["message"],
                    item["metadata"],
                    item["related_feedback_id"],
                ]
            )
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=logs.csv"},
        )

    total = fetch_value(
        connection,
        f"SELECT COUNT(*) FROM logs {where_clause}",
        tuple(params),
    )
    offset = (pagination["page"] - 1) * pagination["page_size"]
    items = fetch_all(
        connection,
        f"""
        SELECT id, created_at, category, message, metadata, related_feedback_id
        FROM logs
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        tuple(params + [pagination["page_size"], offset]),
    )
    return JSONResponse(
        {
            "items": items,
            "total": int(total or 0),
            "page": pagination["page"],
            "page_size": pagination["page_size"],
        }
    )


@app.get("/api/feedbacks/export")
async def export_feedbacks(request: Request) -> Response:
    """
    功能：导出反馈数据为 CSV 或 JSON。
    参数：request（请求对象）。
    返回值：StreamingResponse 或 JSONResponse。
    异常：HTTPException 权限或参数错误。
    """
    require_admin(request, connection)
    export_format = request.query_params.get("format", "csv")
    from_time = parse_datetime(request.query_params.get("from"))
    to_time = parse_datetime(request.query_params.get("to"))

    conditions = []
    params: List[Any] = []
    if from_time:
        conditions.append("created_at >= ?")
        params.append(from_time.isoformat())
    if to_time:
        conditions.append("created_at <= ?")
        params.append(to_time.isoformat())
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    total = fetch_value(
        connection,
        f"SELECT COUNT(*) FROM feedbacks {where_clause}",
        tuple(params),
    )
    if int(total or 0) > 50000:
        raise HTTPException(status_code=400, detail="单次导出最多 5 万条")

    items = fetch_all(
        connection,
        f"""
        SELECT id, created_at, sentiment, content, user_ip, attachments, status, jira_key
        FROM feedbacks
        {where_clause}
        ORDER BY created_at DESC
        """,
        tuple(params),
    )
    if export_format == "json":
        return JSONResponse({"items": items})
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["id", "created_at", "sentiment", "content", "user_ip", "attachments", "status", "jira_key"]
    )
    for item in items:
        writer.writerow(
            [
                item["id"],
                item["created_at"],
                item["sentiment"],
                item["content"],
                item["user_ip"],
                item["attachments"],
                item["status"],
                item["jira_key"],
            ]
        )
    output.seek(0)
    log_event("job", "导出反馈数据", {"format": export_format, "count": len(items)})
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=feedbacks.csv"},
    )


@app.post("/api/admin/login")
async def admin_login(request: Request) -> JSONResponse:
    """
    功能：管理员登录并建立会话。
    参数：request（请求对象）。
    返回值：JSONResponse，包含登录结果。
    异常：HTTPException 登录失败或验证码错误。
    """
    data = await request.json()
    username = str(data.get("username") or "")
    password = str(data.get("password") or "")
    captcha_id = str(data.get("captcha_id") or "")
    captcha_code = str(data.get("captcha_code") or "")
    user_ip = get_client_ip(request)
    need_captcha = get_failure_count(connection, username, user_ip) >= 3
    if need_captcha:
        if not (captcha_id and captcha_code and verify_captcha(connection, captcha_id, captcha_code)):
            new_captcha_id, captcha_text = create_captcha(connection)
            raise HTTPException(
                status_code=401,
                detail={"need_captcha": True, "captcha_id": new_captcha_id, "captcha_text": captcha_text},
            )

    if username != config.admin_username or password != config.admin_password:
        failures = record_login_failure(connection, username, user_ip)
        if failures >= 3:
            new_captcha_id, captcha_text = create_captcha(connection)
            raise HTTPException(
                status_code=401,
                detail={"need_captcha": True, "captcha_id": new_captcha_id, "captcha_text": captcha_text},
            )
        raise HTTPException(status_code=401, detail="账号或密码错误")

    reset_login_failures(connection, username, user_ip)
    session_id, expires_at = create_session(connection, username)
    response = JSONResponse({"ok": True, "expires_at": expires_at})
    response.set_cookie("admin_session", session_id, httponly=True, max_age=24 * 3600)
    log_event("run", "管理员登录", {"username": username})
    return response


@app.post("/api/admin/logout")
async def admin_logout(request: Request) -> JSONResponse:
    """
    功能：管理员退出登录。
    参数：request（请求对象）。
    返回值：JSONResponse。
    异常：HTTPException 权限错误。
    """
    response = JSONResponse({"ok": True})
    clear_session(response, connection, request)
    return response


@app.get("/api/admin/me")
async def admin_me(request: Request) -> JSONResponse:
    """
    功能：获取当前登录管理员信息。
    参数：request（请求对象）。
    返回值：JSONResponse，包含用户名。
    异常：HTTPException 权限错误。
    """
    username = require_admin(request, connection)
    return JSONResponse({"username": username})
