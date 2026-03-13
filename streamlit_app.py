import io
import json
import os
import base64
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

import streamlit as st
from streamlit_quill import st_quill
from streamlit_autorefresh import st_autorefresh
from bs4 import BeautifulSoup
from streamlit.runtime.scriptrunner_utils.script_run_context import get_script_run_ctx


from app.auth import (
    create_captcha,
    get_failure_count,
    record_login_failure,
    reset_login_failures,
    verify_captcha,
)
from app.config import load_config
from app.db import DB
from app.jira_client import MyJira
from catch_jira_data_to_mysql_app import run_once as run_jira_sync
from app.mysql_client import MySQLClient
from app.utils import (
    build_attachment_dir,
    ensure_directory,
    format_recent_days,
    get_extension,
    json_dumps,
    json_loads,
    now_iso,
    parse_datetime,
    sanitize_filename,
    validate_content,
    validate_sentiment,
)
from app.logger import log
import pandas as pd
import altair as alt


config = load_config()
db = DB(config.database_path)
my_jira = MyJira(config.jira_server, config.jira_username, config.jira_password)
mysql_client = MySQLClient(
    host=config.mysql_host,
    port=config.mysql_port,
    user=config.mysql_user,
    password=config.mysql_password,
    database=config.mysql_database,
    table=config.mysql_table,
)
ensure_directory(config.attachments_root)


class PastedFile:
    def __init__(self, buffer, name, type):
        self.name = name
        self.type = type
        self._buffer = buffer
        self.size = buffer.getbuffer().nbytes
    
    def getbuffer(self):
        return self._buffer.getbuffer()


def extract_images_from_html(html_content: str) -> Tuple[str, List[Any]]:
    """
    功能：从 HTML 内容中提取 Base64 图片并转换为文件对象。
    参数：html_content（HTML 内容）。
    返回值：清理后的 HTML（目前保持原样）和提取的文件列表。
    """
    if not html_content:
        return html_content, []
    
    soup = BeautifulSoup(html_content, "html.parser")
    images = []
    
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src.startswith("data:image/"):
            try:
                header, data = src.split(",", 1)
                mime_type = header.split(";")[0].split(":")[1]
                extension = mime_type.split("/")[-1]
                
                img_data = base64.b64decode(data)
                img_buffer = io.BytesIO(img_data)
                
                filename = f"embedded_image_{uuid.uuid4().hex[:8]}.{extension}"
                
                file_obj = PastedFile(img_buffer, filename, mime_type)
                images.append(file_obj)
            except Exception as e:
                log(f"Failed to extract image: {e}")
                continue

    return html_content, images

def get_user_ip() -> str:
    headers = st.context.headers or {}
    forwarded = headers.get("X-Forwarded-For") or headers.get("x-forwarded-for")
    log(f"forwarded:{forwarded}")
    real_ip = headers.get("X-Real-IP") or headers.get("x-real-ip")
    log(f"real_ip:{real_ip}")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    elif real_ip:
        ip = real_ip.strip()
    else:
        ip = st.session_state.get("user_ip", "local")
    st.session_state["user_ip"] = ip
    return ip


def log_event(category: str, message: str, metadata: Dict[str, Any], related_feedback_id: Optional[int] = None) -> None:
    db.execute_query(
        """
        INSERT INTO logs (created_at, category, message, metadata, related_feedback_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            category,
            message,
            json_dumps(metadata),
            related_feedback_id,
        ),
    )


def check_rate_limit(user_ip: str) -> None:
    since = datetime.now() - timedelta(seconds=10)
    count = db.fetch_value(
        "SELECT COUNT(*) FROM feedbacks WHERE user_ip = ? AND created_at >= ?",
        (user_ip, since.isoformat()),
    )
    if int(count or 0) >= 3:
        raise ValueError("10 秒内最多提交 3 次，请稍后再试")


def save_uploaded_files(files: List[Any], feedback_id: int) -> List[Dict[str, Any]]:
    attachment_dir = build_attachment_dir(config.attachments_root, feedback_id)
    ensure_directory(attachment_dir)
    saved_files = []
    for file in files:
        extension = get_extension(file.name)
        # filename = sanitize_filename(file.name)
        filename = file.name
        target_path = os.path.join(attachment_dir, filename)
        payload = file.getbuffer()
        size = len(payload)
        with open(target_path, "wb") as handler:
            handler.write(payload)
        saved_files.append(
            {
                "name": filename,
                "size": size,
                "path": target_path,
                "type": file.type or "application/octet-stream",
                "extension": extension,
            }
        )
    return saved_files


def fetch_feedback(feedback_id: int) -> Optional[Dict[str, Any]]:
    target_database = config.mysql_database
    target_table = config.mysql_feedback_table
    try:
        columns = mysql_client.fetchall(
            f"SHOW COLUMNS FROM `{target_database}`.`{target_table}`"
        )
    except Exception:
        return None
    column_names = [row[0] for row in columns] if columns else []
    if not column_names:
        return None
    row = mysql_client.fetchone(
        f"SELECT * FROM `{target_database}`.`{target_table}` WHERE feedback_id = %s",
        (feedback_id,),
    )
    if not row:
        return None
    item = dict(zip(column_names, row))
    raw_attachments = item.get("attachments")
    if raw_attachments:
        try:
            item["attachments"] = json_loads(raw_attachments)
        except Exception:
            item["attachments"] = []
    else:
        item["attachments"] = []
    raw_extra = item.get("extra")
    if raw_extra:
        try:
            extra_payload = json_loads(raw_extra)
            if isinstance(extra_payload, dict):
                jira_key = extra_payload.get("jira_key")
                if jira_key:
                    item["jira_key"] = jira_key
        except Exception:
            pass
    content = item.get("feedback_suggestion") or item.get("content") or ""
    item["summary"] = str(content)[:120]
    return item


def validate_feedback_inputs(sentiment: str, content: str, files: List[Any]) -> Optional[str]:
    """
    功能：校验反馈表单输入是否合法。
    参数：sentiment（情感）、content（内容）、files（附件列表）。
    返回值：错误信息或 None。
    异常：无显式异常。
    """
    if sentiment not in {"like", "dislike"}:
        return "请选择喜欢或不喜欢"
    
    # Handle HTML content from Quill
    has_text = False
    has_img = False
    content_text = ""
    
    if content:
        soup = BeautifulSoup(content, "html.parser")
        content_text = soup.get_text().strip()
        has_text = len(content_text) >= 1
        has_img = bool(soup.find("img"))
    
    if not (has_text or has_img):
        return "建议内容不能为空（请输入文字或粘贴图片）"
        
    if len(content_text) > 2000:
        return "建议内容文字长度不能超过 2000 字"
        
    if len(files) > 10:
        return "最多允许上传 10 个附件（含粘贴图片）"
    allowed_extensions = {"png", "jpg", "jpeg", "webp", "pdf", "docx", "txt", "log", "zip", "rar"}
    for file in files:
        extension = file.name.rsplit(".", 1)[-1].lower() if "." in file.name else ""
        if extension not in allowed_extensions:
            return "附件类型不被允许"
        if file.size > 20 * 1024 * 1024:
            return "单个附件大小不能超过 20MB"
    return None


def post_feedback(sentiment: str, content: str, files: List[Any], retries: int = 3) -> Tuple[bool, str]:
    """
    功能：提交反馈数据并进行超时重试。
    参数：sentiment（情感）、content（内容）、files（附件列表）、retries（最大重试次数）。
    返回值：是否成功与提示信息。
    异常：requests.RequestException 网络异常。
    """
    try:
    #     validate_sentiment(sentiment)
    #     validate_content(content)
        check_rate_limit(get_user_ip())
    except ValueError as error:
        return False, str(error)

    created_at = now_iso()
    log(f"提交反馈：时间={created_at},情感={sentiment}, 内容={content.strip()}, IP={get_user_ip()}")
    feedback_id = mysql_client.insert_feedback(
        sentiment,
        suggestion=content.strip(),
        extra={"status": "pending", "attachments": []},
        ip=get_user_ip(),
        database=config.mysql_database,
        table=config.mysql_feedback_table,
    )
    attachments = save_uploaded_files(files, feedback_id) if files else []
    if attachments:
        extra_payload = {"status": "pending", "attachments": attachments}
        mysql_client.execute(
            f"UPDATE `{config.mysql_database}`.`{config.mysql_feedback_table}` SET extra = %s WHERE feedback_id = %s",
            (json_dumps(extra_payload), feedback_id),
            commit=True,
        )
    log_event(
        "run",
        "提交反馈",
        {"sentiment": sentiment, "attachment_count": len(attachments)},
        related_feedback_id=feedback_id,
    )
    return True, f"感谢您的反馈，您的使用是对我们最大的支持，已收到反馈，编号：{feedback_id}"


def render_frontend() -> None:
    """
    功能：渲染前台提交界面。
    参数：无。
    返回值：无。
    异常：无显式异常。
    """
    if "form_id" not in st.session_state:
        st.session_state.form_id = 0
    
    st.header("LOG 分析反馈收集站")
    
    # Check for success state
    if st.session_state.get("submission_success"):
        st.success(st.session_state.submission_message)
        if st.button("提交下一条反馈"):
            del st.session_state.submission_success
            del st.session_state.submission_message
            st.session_state.form_id += 1
            st.rerun()
        return
    
    form_key_suffix = st.session_state.form_id

    with st.form(key=f"feedback_form_{form_key_suffix}"):
        sentiment = st.radio("请对当前的 LOG 分析内容进行评价：喜欢/不喜欢", options=["like", "dislike"], format_func=lambda v: "喜欢👍" if v == "like" else "不喜欢👎", key=f"fe_sentiment_{form_key_suffix}", horizontal=True)
        
        # 使用 Quill 编辑器替换原生 Text Area 以支持图片粘贴
        # content = st.text_area("建议内容", max_chars=2000, height=200, key=f"fe_content_{form_key_suffix}")
        st.write("喜欢/不喜欢的建议内容（方便我们针对问题进行改进，欢迎吐槽！！🦻）")
        content = st_quill(
            placeholder="请输入建议内容（支持直接粘贴图片）",
            html=True,
            toolbar=[
                ["bold", "italic", "underline"],
                [{"list": "ordered"}, {"list": "bullet"}],
                ["clean"],
            ],
            key=f"fe_content_{form_key_suffix}"
        )
        
        files = st.file_uploader(
            "附件上传（最多 3 个，每个 ≤20MB）",
            accept_multiple_files=True,
            type=["png", "jpg", "jpeg", "webp", "pdf", "docx", "txt", "log", "zip", "rar"],
            key=f"fe_files_{form_key_suffix}"
        )

        submitted = st.form_submit_button("提交")

    if submitted:
        # Extract images from Quill content
        content, embedded_images = extract_images_from_html(content)
        
        final_files = list(files) if files else []
        final_files.extend(embedded_images)
    
        error = validate_feedback_inputs(sentiment, content, final_files)
        log(f"upload files={final_files}")
        if error:
            st.error(error)
        else:
            ok, message = post_feedback(sentiment, content, final_files)
            if ok:
                st.session_state.submission_success = True
                st.session_state.submission_message = message
                st.rerun()
            else:
                st.error(message)



def list_feedbacks(
    filters: Dict[str, Any],
    database: Optional[str] = None,
    table: Optional[str] = None,
) -> Dict[str, Any]:
    page = int(filters.get("page", 1))
    page_size = int(filters.get("page_size", 50))
    sentiment = filters.get("sentiment") or None
    status = filters.get("status") or None
    keyword = filters.get("q") or None
    from_time = parse_datetime(filters.get("from"))
    to_time = parse_datetime(filters.get("to"))

    target_database = database or config.mysql_database
    target_table = table or config.mysql_feedback_table

    conditions = []
    params: List[Any] = []
    if sentiment:
        conditions.append("feedback = %s")
        params.append(sentiment)
    if keyword:
        conditions.append("(feedback_suggestion LIKE %s OR extra LIKE %s)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if from_time:
        conditions.append("create_time >= %s")
        params.append(from_time.isoformat(sep=" "))
    if to_time:
        conditions.append("create_time <= %s")
        params.append(to_time.isoformat(sep=" "))

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    log(f"where_clause={where_clause} params={params} status={status}")
    total_row = mysql_client.fetchone(
        f"SELECT COUNT(*) FROM `{target_database}`.`{target_table}` {where_clause}",
        tuple(params),
    )
    total = int(total_row[0] or 0) if total_row else 0
    offset = (page - 1) * page_size
    rows = mysql_client.fetchall(
        f"""
        SELECT feedback_id, create_time, feedback, feedback_suggestion, ip, extra
        FROM `{target_database}`.`{target_table}`
        {where_clause}
        ORDER BY create_time DESC
        LIMIT %s OFFSET %s
        """,
        tuple(params + [page_size, offset]),
    )
    items = []
    for row in rows:
        feedback_id, create_time, feedback, feedback_suggestion, ip, extra = row
        log(f"feedback_id={feedback_id} create_time={create_time} feedback={feedback} feedback_suggestion={feedback_suggestion} ip={ip} extra={extra}")
        content_html = feedback_suggestion or ""
        try:
            summary_text = BeautifulSoup(content_html, "html.parser").get_text()
        except Exception:
            summary_text = content_html
        extra_content = extra or ""
        status_value = "pending"
        jira_key_value = None
        note_value = ""
        if extra_content:
            try:
                extra_payload = json_loads(extra_content)
                if isinstance(extra_payload, dict):
                    status_value = extra_payload.get("status") or status_value
                    jira_key_value = extra_payload.get("jira_key") or jira_key_value
                    note_value = extra_payload.get("note") or note_value
                    extra_content = json_dumps(extra_payload)
            except Exception:
                pass
        items.append(
            {
                "id": feedback_id,
                "create_at": create_time,
                "sentiment": feedback,
                "content": content_html,
                "content_text": summary_text,
                "user_ip": ip,
                "status": status_value,
                "jira_key": jira_key_value,
                "note": note_value,
                "attachments": [],
                "extra_content": extra_content,
                "summary": summary_text[:50],
            }
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def list_logs(filters: Dict[str, Any]) -> Dict[str, Any]:
    page = int(filters.get("page", 1))
    page_size = int(filters.get("page_size", 50))
    category = filters.get("category") or None
    keyword = filters.get("q") or None
    from_time = parse_datetime(filters.get("from"))
    to_time = parse_datetime(filters.get("to"))

    conditions = []
    params: List[Any] = []
    if category:
        conditions.append("category = ?")
        params.append(category)
    if keyword:
        conditions.append("(message LIKE ? OR metadata LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if from_time:
        conditions.append("created_at >= ?")
        params.append(from_time.isoformat())
    if to_time:
        conditions.append("created_at <= ?")
        params.append(to_time.isoformat())

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    total = db.fetch_value(f"SELECT COUNT(*) FROM logs {where_clause}", tuple(params))
    offset = (page - 1) * page_size
    rows = db.fetch_all(
        f"""
        SELECT id, created_at, category, message, metadata, related_feedback_id
        FROM logs
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        tuple(params + [page_size, offset]),
    )
    for row in rows:
        row["metadata"] = json_loads(row.get("metadata") or "{}")
    return {"items": rows, "total": int(total or 0), "page": page, "page_size": page_size}


def stats_feedbacks(
    database: Optional[str] = None,
    table: Optional[str] = None,
) -> Dict[str, Any]:
    target_database = database or config.mysql_database
    target_table = table or config.mysql_feedback_table

    total_row = mysql_client.fetchone(
        f"SELECT COUNT(*) FROM `{target_database}`.`{target_table}`"
    )
    like_row = mysql_client.fetchone(
        f"SELECT COUNT(*) FROM `{target_database}`.`{target_table}` WHERE feedback = 'like'"
    )
    dislike_row = mysql_client.fetchone(
        f"SELECT COUNT(*) FROM `{target_database}`.`{target_table}` WHERE feedback = 'dislike'"
    )
    suggest_row = mysql_client.fetchone(
        f"""
        SELECT COUNT(*) FROM `{target_database}`.`{target_table}`
        WHERE feedback_suggestion IS NOT NULL AND feedback_suggestion <> ''
        """
    )

    total = int(total_row[0] or 0) if total_row else 0
    like_count = int(like_row[0] or 0) if like_row else 0
    dislike_count = int(dislike_row[0] or 0) if dislike_row else 0
    suggest_count = int(suggest_row[0] or 0) if suggest_row else 0

    return {
        "total": total,
        "like": like_count,
        "dislike": dislike_count,
        "suggest": suggest_count,
    }


def stats_feedbacks_range(
    days_range: int = 7,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    database: Optional[str] = None,
    table: Optional[str] = None,
) -> Dict[str, Any]:
    if start_date or end_date:
        if not start_date:
            start_date = end_date
        if not end_date:
            end_date = start_date
        if start_date and end_date and start_date > end_date:
            start_date, end_date = end_date, start_date
        days = []
        current_day = start_date
        while current_day and end_date and current_day <= end_date:
            days.append(current_day.isoformat())
            current_day += timedelta(days=1)
    else:
        days = format_recent_days(days_range)
    daily_map: Dict[str, Dict[str, Any]] = {}
    for day in days:
        daily_map[day] = {
            "date": day,
            "count": 0,
            "like": 0,
            "dislike": 0,
            "suggest": 0,
            "pending": 0,
            "accepted": 0,
            "rejected": 0,
            "followup": 0,
            "done": 0,
            "jira_key_count": 0,
        }

    target_database = database or config.mysql_database
    target_table = table or config.mysql_feedback_table
    if days:
        from_value = f"{days[0]}T00:00:00"
        to_value = f"{days[-1]}T23:59:59.999999"
    else:
        from_value = None
        to_value = None

    all_items: List[Dict[str, Any]] = []
    page = 1
    page_size = 1000
    while True:
        filters: Dict[str, Any] = {"page": page, "page_size": page_size}
        if from_value:
            filters["from"] = from_value
        if to_value:
            filters["to"] = to_value
        data = list_feedbacks(filters, database=target_database, table=target_table)
        items = data.get("items", [])
        all_items.extend(items)
        total = int(data.get("total", 0) or 0)
        if page * page_size >= total:
            break
        page += 1

    detail_rows: List[Dict[str, Any]] = []
    for item in all_items:
        create_at = item.get("create_at")
        if isinstance(create_at, datetime):
            create_dt = create_at
        else:
            create_dt = parse_datetime(str(create_at)) if create_at else None
        if not create_dt:
            continue
        day = create_dt.date().isoformat()
        if day not in daily_map:
            continue
        daily_entry = daily_map[day]
        daily_entry["count"] += 1
        sentiment = item.get("sentiment")
        if sentiment == "like":
            daily_entry["like"] += 1
        if sentiment == "dislike":
            daily_entry["dislike"] += 1
        if item.get("content"):
            daily_entry["suggest"] += 1
        status_value = item.get("status") or "pending"
        if status_value not in {"pending", "accepted", "rejected", "followup", "done"}:
            status_value = "pending"
        daily_entry[status_value] += 1
        if item.get("jira_key"):
            daily_entry["jira_key_count"] += 1
        jira_key = item.get("jira_key")
        jira_link = f"https://jira.amlogic.com/browse/{jira_key}" if jira_key else "-"
        detail_rows.append(
            {
                "日期": create_at,
                "建议内容": item.get("content_text") or "",
                "工单链接": jira_link,
                "状态": status_value,
                "备注": item.get("note") or "",
            }
        )

    daily = [daily_map[day] for day in days]

    range_total = sum(item["count"] for item in daily)
    range_like = sum(item["like"] for item in daily)
    range_dislike = sum(item["dislike"] for item in daily)
    range_suggest = sum(item["suggest"] for item in daily)
    # range_pending = sum(item["pending"] for item in daily)
    # range_accepted = sum(item["accepted"] for item in daily)
    # range_rejected = sum(item["rejected"] for item in daily)
    # range_followup = sum(item["followup"] for item in daily)
    # range_done = sum(item["done"] for item in daily)

    return {
        "range_total": range_total,
        "range_like": range_like,
        "range_dislike": range_dislike,
        "range_suggest": range_suggest,
        # "range_pending": range_pending,
        # "range_accepted": range_accepted,
        # "range_rejected": range_rejected,
        # "range_followup": range_followup,
        # "range_done": range_done,
        "recent_trend": daily,
        "detail_rows": detail_rows,
    }


def login_admin(username: str, password: str, captcha_id: str, captcha_code: str) -> Tuple[bool, str, Dict[str, Any]]:
    """
    功能：执行管理员登录并处理验证码。
    参数：username（账号）、password（密码）、captcha_id（验证码 ID）、captcha_code（验证码文本）。
    返回值：是否成功、提示信息、附加数据。
    异常：requests.RequestException 网络异常。
    """
    user_ip = get_user_ip()
    failure_count = get_failure_count(db, username, user_ip)
    if failure_count >= 3:
        if not captcha_id or not captcha_code:
            captcha_id, captcha_text = create_captcha(db)
            return False, "需要验证码", {"need_captcha": True, "captcha_id": captcha_id, "captcha_text": captcha_text}
        if not verify_captcha(db, captcha_id, captcha_code):
            captcha_id, captcha_text = create_captcha(db)
            return False, "验证码错误", {"need_captcha": True, "captcha_id": captcha_id, "captcha_text": captcha_text}

    if username != config.admin_username or password != config.admin_password:
        record_login_failure(db, username, user_ip)
        failure_count = get_failure_count(db, username, user_ip)
        if failure_count >= 3:
            captcha_id, captcha_text = create_captcha(db)
            return False, "需要验证码", {"need_captcha": True, "captcha_id": captcha_id, "captcha_text": captcha_text}
        return False, "账号或密码错误", {}

    reset_login_failures(db, username, user_ip)
    st.session_state.admin_logged_in = True
    st.session_state.admin_expires_at = (datetime.now() + timedelta(hours=24)).isoformat()
    return True, "登录成功", {}


def require_admin_session() -> bool:
    """
    功能：检查当前会话是否已登录。
    参数：无。
    返回值：是否已登录。
    异常：requests.RequestException 网络异常。
    """
    logged_in = st.session_state.get("admin_logged_in")
    expires_at = st.session_state.get("admin_expires_at")
    if not logged_in or not expires_at:
        return False
    if datetime.fromisoformat(expires_at) < datetime.now():
        st.session_state.admin_logged_in = False
        st.session_state.pop("admin_expires_at", None)
        return False
    return True


def fetch_json(path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any]:
    """
    功能：发送 GET 请求并解析 JSON。
    参数：path（接口路径）、params（查询参数）。
    返回值：是否成功与 JSON 数据或错误信息。
    异常：requests.RequestException 网络异常。
    """
    params = params or {}
    try:
        if path == "/api/feedbacks":
            return True, list_feedbacks(
                params,
                database=config.mysql_database,
                table=config.mysql_feedback_table,
            )
        if path == "/api/logs":
            return True, list_logs(params)
        if path == "/api/stats/feedbacks":
            return True, stats_feedbacks(
                database=config.mysql_database,
                table=config.mysql_feedback_table,
            )
        if path.startswith("/api/jira/"):
            jira_key = path.split("/api/jira/")[-1]
            status, _ = jira_get_status(config.jira_base_url, config.jira_auth_token, jira_key)
            return True, {"status": status}
    except ValueError as error:
        return False, str(error)
    return False, "请求失败"


def update_review_status(feedback_id: int, status: str, note: str) -> Tuple[bool, str]:
    """
    功能：更新反馈评审状态。
    参数：feedback_id（反馈 ID）、status（状态）、note（备注）。
    返回值：是否成功与提示信息。
    异常：requests.RequestException 网络异常。
    """
    if status not in {"pending", "accepted", "rejected", "followup", "done"}:
        return False, "状态不合法"
    target_database = config.mysql_database
    target_table = config.mysql_feedback_table
    extra_row = mysql_client.fetchone(
        f"SELECT extra FROM `{target_database}`.`{target_table}` WHERE feedback_id = %s",
        (feedback_id,),
    )
    if not extra_row:
        return False, "反馈不存在"

    raw_extra = extra_row[0] or ""
    extra_payload = {}
    if raw_extra:
        try:
            parsed = json_loads(raw_extra)
            if isinstance(parsed, dict):
                extra_payload = parsed
        except Exception:
            extra_payload = {}

    extra_payload["status"] = status
    extra_payload["note"] = note or ""

    mysql_client.execute(
        f"UPDATE `{target_database}`.`{target_table}` SET extra = %s WHERE feedback_id = %s",
        (json_dumps(extra_payload), feedback_id),
        commit=True,
    )
    log_event(
        "job",
        "更新评审状态",
        {"status": status, "note": note or ""},
        related_feedback_id=feedback_id,
    )
    return True, "状态已更新"


def add_jira_issue(feedback_id: int, jira_key: str) -> Tuple[bool, str]:
    """
    功能：为反馈创建 Jira 问题。
    参数：feedback_id（反馈 ID）、jira_key（项目键）。
    返回值：是否成功与提示信息。
    异常：requests.RequestException 网络异常。
    """
    item = fetch_feedback(feedback_id)
    if not item:
        return False, "反馈不存在"
    extra_row = mysql_client.fetchone(
        f"""
        SELECT extra FROM `{config.mysql_database}`.`{config.mysql_feedback_table}`
        WHERE feedback_id = %s
        """,
        (feedback_id,),
    )
    if not extra_row:
        return False, "反馈不存在"
    raw_extra = extra_row[0] or ""
    extra_payload = {}
    if raw_extra:
        try:
            parsed = json_loads(raw_extra)
            if isinstance(parsed, dict):
                extra_payload = parsed
        except Exception:
            extra_payload = {}
    extra_payload["jira_key"] = jira_key
    mysql_client.execute(
        f"""
        UPDATE `{config.mysql_database}`.`{config.mysql_feedback_table}`
        SET extra = %s
        WHERE feedback_id = %s
        """,
        (json_dumps(extra_payload), feedback_id),
        commit=True,
    )
    log_event(
        "job",
        "添加 Jira key",
        {"jira_key": jira_key},
        related_feedback_id=feedback_id,
    )
    return True, f"已添加 Jira：{jira_key}"

def remove_jira_issue(feedback_id: int) -> Tuple[bool, str]:
    """
    功能：为反馈创建 Jira 问题。
    参数：feedback_id（反馈 ID）。
    返回值：是否成功与提示信息。
    异常：requests.RequestException 网络异常。
    """
    item = fetch_feedback(feedback_id)
    if not item:
        return False, "反馈不存在"
    extra_row = mysql_client.fetchone(
        f"""
        SELECT extra FROM `{config.mysql_database}`.`{config.mysql_feedback_table}`
        WHERE feedback_id = %s
        """,
        (feedback_id,),
    )
    if not extra_row:
        return False, "反馈不存在"
    raw_extra = extra_row[0] or ""
    extra_payload = {}
    if raw_extra:
        try:
            parsed = json_loads(raw_extra)
            if isinstance(parsed, dict):
                extra_payload = parsed
        except Exception:
            extra_payload = {}
    extra_payload.pop("jira_key", None)
    mysql_client.execute(
        f"""
        UPDATE `{config.mysql_database}`.`{config.mysql_feedback_table}`
        SET extra = %s
        WHERE feedback_id = %s
        """,
        (json_dumps(extra_payload), feedback_id),
        commit=True,
    )
    log_event(
        "job",
        "删除 Jira key",
        {"jira_key": None},
        related_feedback_id=feedback_id,
    )
    return True, f"已删除 feedback_id Jira key"

def sync_jira_status(jira_key: str) -> Tuple[bool, str, Optional[str]]:
    """
    功能：同步 Jira 状态。
    参数：jira_key（问题 Key）。
    返回值：是否成功、提示信息、状态值。
    异常：requests.RequestException 网络异常。
    """
    if not jira_key or not str(jira_key).strip():
        return False, "Jira 编号为空", None
    status = my_jira.getJiraStatus(str(jira_key).strip())
    if status == "ERROR":
        return False, "Jira 编号不存在", None
    # log_event("analysis", "同步 Jira 状态", {"jira_key": jira_key, "status": status})
    return True, "同步成功", status


def download_export(path: str, params: Dict[str, Any]) -> Tuple[bool, bytes, str]:
    """
    功能：下载导出文件。
    参数：path（接口路径）、params（查询参数）。
    返回值：是否成功、文件内容、文件名。
    异常：requests.RequestException 网络异常。
    """
    export_format = params.get("format", "csv")
    if path == "/api/feedbacks/export":
        data = list_feedbacks(
            {**params, "page": 1, "page_size": 50000},
            database=config.mysql_database,
            table=config.mysql_feedback_table,
        )
        rows = data.get("items", [])
        if export_format == "json":
            content = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
            return True, content, "feedbacks.json"
        csv_buffer = io.StringIO()
        headers = ["id", "create_at", "sentiment", "content", "user_ip", "status", "jira_key", "extra_content", "note"]
        csv_buffer.write(",".join(headers) + "\n")
        for row in rows:
            # 对内容处理，移除图片
            content_html = str(row.get("content", ""))
            try:
                soup = BeautifulSoup(content_html, "html.parser")
                for img in soup.find_all("img"):
                    img.decompose()
                content_text = soup.get_text()
            except Exception:
                content_text = content_html
            
            extra_content = str(row.get("extra_content", ""))
            note_text = ""
            if extra_content:
                try:
                    extra_payload = json_loads(extra_content)
                    if isinstance(extra_payload, dict):
                        note_text = str(extra_payload.get("note", ""))
                except Exception:
                    note_text = ""
            csv_buffer.write(
                ",".join(
                    [
                        str(row.get("id", "")),
                        str(row.get("create_at", "")),
                        str(row.get("sentiment", "")),
                        str(content_text).replace("\n", " "),
                        str(row.get("user_ip", "")),
                        str(row.get("status", "")),
                        str(row.get("jira_key", "")),
                        extra_content.replace("\n", " "),
                        note_text.replace("\n", " "),
                    ]
                )
                + "\n"
            )
        return True, csv_buffer.getvalue().encode("utf-8"), "feedbacks.csv"
    if path == "/api/logs":
        data = list_logs({**params, "page": 1, "page_size": 50000})
        rows = data.get("items", [])
        if export_format == "json":
            content = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
            return True, content, "logs.json"
        csv_buffer = io.StringIO()
        headers = ["id", "created_at", "category", "message", "metadata", "related_feedback_id"]
        csv_buffer.write(",".join(headers) + "\n")
        for row in rows:
            csv_buffer.write(
                ",".join(
                    [
                        str(row.get("id", "")),
                        str(row.get("created_at", "")),
                        str(row.get("category", "")),
                        str(row.get("message", "")).replace("\n", " "),
                        json.dumps(row.get("metadata", {}), ensure_ascii=False).replace("\n", " "),
                        str(row.get("related_feedback_id", "")),
                    ]
                )
                + "\n"
            )
        return True, csv_buffer.getvalue().encode("utf-8"), "logs.csv"
    return False, b"", "export"


def build_time_range(option: str) -> Tuple[Optional[str], Optional[str]]:
    """
    功能：根据时间范围选项生成查询起止日期。
    参数：option（选项名称）。
    返回值：from_date 与 to_date。
    异常：无显式异常。
    """
    today = datetime.now().date()
    if option == "今天":
        return today.isoformat(), today.isoformat()
    if option == "本周":
        start = today - timedelta(days=today.weekday())
        return start.isoformat(), today.isoformat()
    if option == "近7天":
        start = today - timedelta(days=6)
        return start.isoformat(), today.isoformat()
    if option == "本月":
        start = today.replace(day=1)
        return start.isoformat(), today.isoformat()
    if option == "本年":
        start = today.replace(month=1, day=1)
        return start.isoformat(), today.isoformat()
    return None, None


def render_feedback_list(filters: Dict[str, Any]) -> None:
    """
    功能：渲染反馈列表与评审操作。
    参数：filters（筛选条件）。
    返回值：无。
    异常：无显式异常。
    """
    data = list_feedbacks(
        filters,
        database=config.mysql_database,
        table=config.mysql_feedback_table,
    )
    items = data.get("items", [])
    if not items:
        st.info("暂无数据")
        return
    for item in items:
        jira_status_text = ""
        jira_display = "-"
        if item.get("jira_key"):
            ok_sync, _, status_text = sync_jira_status(item["jira_key"])
            if ok_sync and status_text:
                jira_status_text = status_text
                jira_display = f"{item['jira_key']}（{status_text}）"
            else:
                jira_display = item["jira_key"]
        expander_title = f"| {item['status']} 反馈 #{item['id']} | **{item['sentiment']}** | **{item['summary']}**"
        if jira_display:
            expander_title = f"{expander_title} | **{jira_display}** |"
        with st.expander(expander_title):
            # st.write(f"item:{item}")
            st.write(f"时间：{item['create_at']}")
            st.write(f"IP：{item['user_ip']}")
            st.write(f"情感：{item['sentiment']}")
            if item.get("jira_key"):
                jira_url = f"https://jira.amlogic.com/browse/{item['jira_key']}"
                st.markdown(f"Jira：{jira_url}")
            else:
                st.write("Jira：-")

            # if st.button("删除反馈", key=f"delete_{item['id']}"):
            #     db.execute_query("DELETE FROM feedbacks WHERE id = ?", (item['id'],))
            #     log_event("job", "删除反馈", {"feedback_id": item['id']})
            #     st.success("反馈已删除")
            #     st.rerun()
            #     return
            
            if not item.get("jira_key"):
                jira_key_input = st.text_input("Jira 编号", key=f"jira_key_{item['id']}")
                if st.button("添加 Jira号", key=f"add_jira_{item['id']}"):
                    jira_key_value = jira_key_input.strip()
                    if jira_key_value:
                        ok_sync, _, status_text = sync_jira_status(jira_key_value)
                        if ok_sync and status_text:
                            ok_jira, message = add_jira_issue(item["id"], jira_key_value)
                            if ok_jira:
                                st.success(f"{message}：状态：{status_text}")
                                st.rerun()
                            else:
                                st.error(message)
                        else:
                            st.error("请检查后输入有效的 Jira 编号")
                    else:
                        st.error("请输入有效的 Jira 编号")

            if item.get("jira_key") and st.button("删除 Jira号", key=f"del_jira_{item['id']}"):
                ok_del, msg = remove_jira_issue(item["id"])
                if ok_del:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
            st.markdown("---")
            st.markdown("### 详细内容")
            # Render HTML content safely
            st.markdown(item["content"], unsafe_allow_html=True)
            
            if item.get("attachments"):
                st.write("附件列表：")
                for att in item["attachments"]:
                    name = att.get("name") or "attachment"
                    path = att.get("path") or ""
                    size = att.get("size") or 0
                    if path and os.path.exists(path):
                        with open(path, "rb") as handler:
                            data = handler.read()
                        st.download_button(
                            label=f"下载 {name} ({size} bytes)",
                            data=data,
                            file_name=name,
                            key=f"download_{item['id']}_{name}",
                        )
                    else:
                        st.write(f"- {name} ({size} bytes) 文件不存在")
            
            st.markdown("---")
            status = st.selectbox(
                "状态流转",
                options=["pending", "accepted", "rejected", "followup", "done"],
                index=["pending", "accepted", "rejected", "followup", "done"].index(item["status"]),
                key=f"status_{item['id']}",
            )

            # 添加已有的内容
            note_key = f"note_{item['id']}"
            if note_key not in st.session_state:
                raw_extra = item.get("extra_content") or ""
                existing_note = ""
                if raw_extra:
                    try:
                        extra_payload = json_loads(raw_extra)
                        existing_note = str(extra_payload.get("note") or "") if isinstance(extra_payload, dict) else ""
                    except Exception:
                        existing_note = ""
                st.session_state[note_key] = existing_note
            note = st.text_input("评审备注", key=note_key)
            
            if st.button("更新状态", key=f"update_{item['id']}"):
                ok_status, message = update_review_status(item["id"], status, note)
                if ok_status:
                    st.success(message)
                    st.rerun()
                else:
                    st.error(message)
            # log(f"jira_key_input={jira_key_input}")



def render_logs(filters: Dict[str, Any]) -> None:
    """
    功能：渲染日志列表。
    参数：filters（筛选条件）。
    返回值：无。
    异常：无显式异常。
    """
    data = list_logs(filters)
    items = data.get("items", [])
    if not items:
        st.info("暂无日志记录")
        return
    st.table(items)


def render_admin() -> None:
    """
    功能：渲染后台管理界面。
    参数：无。
    返回值：无。
    异常：无显式异常。
    """

    logged_in = require_admin_session()
    admin_tab = None
    if not logged_in:
        st.subheader("管理员登录")
        username = st.text_input("账号")
        password = st.text_input("密码", type="password")
        captcha_id = st.session_state.get("captcha_id", "")
        captcha_text = st.session_state.get("captcha_text", "")
        captcha_code = ""
        if captcha_text:
            st.warning(f"验证码：{captcha_text}")
            captcha_code = st.text_input("验证码")
        if st.button("登录"):
            ok, message, data = login_admin(username, password, captcha_id, captcha_code)
            if ok:
                st.success(message)
                st.session_state.pop("captcha_id", None)
                st.session_state.pop("captcha_text", None)
                st.rerun()
            else:
                if data.get("need_captcha"):
                    st.session_state.captcha_id = data.get("captcha_id", "")
                    st.session_state.captcha_text = data.get("captcha_text", "")
                    st.warning("请填写验证码后重试")
                else:
                    st.error(message)
        return
    admin_page = st.session_state.get("admin_page", "5000agent 反馈页")
    admin_tab = st.session_state.get("admin_tab", "反馈总表")
    with st.sidebar:
        st.header("后台管理")
        if st.button("退出登录"):
            st.session_state.admin_logged_in = False
            st.session_state.pop("admin_expires_at", None)
            st.rerun()
        if st.button("强制刷新"):
            # 保持当前页面与菜单选择不变
            st.session_state["admin_page"] = admin_page
            st.session_state["admin_tab"] = admin_tab
            if admin_tab == "标签统计":
                try:
                    run_jira_sync()
                    st.success("已触发 Jira 数据同步")
                except Exception as exc:
                    st.error(str(exc))
            else:
                st.rerun()
        st.markdown("---")
    
    admin_page = st.sidebar.selectbox(
        "管理页面",
        ["5000agent 反馈页", "5000agent 运行状态页"],
        key="admin_page",
    )
    if admin_page == "5000agent 反馈页":
        admin_tab = st.sidebar.radio("管理菜单", ["反馈总表", "反馈列表"], key="admin_tab")
    else:
        admin_tab = st.sidebar.radio("管理菜单", ["使用情况", "标签统计", "调试日志记录"], key="admin_tab")

   
    with st.sidebar:
        st.markdown("---")
        log_refresh_interval = None
        log_auto_refresh = st.checkbox("自动刷新", value=False, key="log_auto_refresh")
        if log_auto_refresh:
            log_refresh_interval = st.selectbox("刷新间隔(秒)", options=[5, 10, 30, 60, 120, 600], index=5, key="log_refresh_interval")
        if log_refresh_interval:     
            st_autorefresh(log_refresh_interval * 1000, key="log_autorefresh")
    if admin_tab == "反馈总表":
        st.subheader("反馈总表")

        # total_stats = stats_feedbacks(
        #     database=config.mysql_database,
        #     table=config.mysql_feedback_table,
        # )

        # st.markdown("全量统计")
        # total_cols, total_chart_col = st.columns([2, 1])
        # with total_cols:
        #     col1, col2, col3, col4 = st.columns(4)
        #     col1.metric("反馈总数", total_stats.get("total", 0))
        #     col2.metric("喜欢", total_stats.get("like", 0))
        #     col3.metric("不喜欢", total_stats.get("dislike", 0))
        #     col4.metric("建议", total_stats.get("suggest", 0))
        # with total_chart_col:
        #     st.markdown("反馈分布")
        #     summary_df = pd.DataFrame(
        #         [
        #             {"metric": "喜欢", "count": total_stats.get("like", 0), "metric_order": 0},
        #             {"metric": "不喜欢", "count": total_stats.get("dislike", 0), "metric_order": 1},
        #         ]
        #     )
        #     pie = alt.Chart(summary_df).mark_arc().encode(
        #         theta=alt.Theta("count", stack=True),
        #         color=alt.Color(
        #             "metric",
        #             scale=alt.Scale(range=["#66c2a5", "#fc8d62"]),
        #             legend=alt.Legend(title="类型"),
        #         ),
        #         order=alt.Order("metric_order"),
        #         tooltip=["metric", "count"],
        #     )
        #     st.altair_chart(pie, use_container_width=True)
        
        # st.markdown("统计时间范围")
        col_filters, col_content = st.columns([1, 7])
        with col_filters:
            st.subheader("过滤条件")
            range_option = st.selectbox(
                "统计时间范围",
                options=["今天", "本周", "近7天", "本月", "本年", "自定义"],
                index=1,
                key="feedback_stats_range_option",
            )
            range_from_date = None
            range_to_date = None
            if range_option == "自定义":
                range_from_date = st.date_input("开始日期", key="feedback_stats_from_date")
                range_to_date = st.date_input("结束日期", key="feedback_stats_to_date")
            else:
                from_value, to_value = build_time_range(range_option)
                if from_value:
                    range_from_date = datetime.fromisoformat(from_value).date()
                if to_value:
                    range_to_date = datetime.fromisoformat(to_value).date()

        with col_content:
            range_stats = stats_feedbacks_range(
                start_date=range_from_date,
                end_date=range_to_date,
                database=config.mysql_database,
                table=config.mysql_feedback_table,
            )
            range_title = f"{range_option}统计"
            if range_from_date and range_to_date:
                range_label = f"{range_from_date} ~ {range_to_date}"
            elif range_from_date and not range_to_date:
                range_label = f"{range_from_date} ~"
            elif range_to_date and not range_from_date:
                range_label = f"~ {range_to_date}"
            else:
                range_label = "全部"
            range_label = f"{range_label}统计"
            st.markdown(range_label)
            range_col1, range_col2, range_col3, range_col4 = st.columns(4)
            range_col1.metric("反馈总数", range_stats.get("range_total", 0))
            range_col2.metric("喜欢", range_stats.get("range_like", 0))
            range_col3.metric("不喜欢", range_stats.get("range_dislike", 0))
            range_col4.metric("建议", range_stats.get("range_suggest", 0))
            recent_data = range_stats.get("recent_trend", [])
            if recent_data:

                df = pd.DataFrame(recent_data)

                trend_col, pie_col = st.columns([2, 1])
                with trend_col:
                    line = alt.Chart(df).mark_line(point=True, color="#FF0000").encode(
                        x=alt.X("date", axis=alt.Axis(title="日期")),
                        y=alt.Y("count", axis=alt.Axis(title="每日反馈总数")),
                        tooltip=["date", "count", "like", "dislike", "suggest"]
                    )
                    line_text = alt.Chart(df).mark_text(dy=-10, color="#FF0000", fontSize=20).encode(
                        x=alt.X("date"),
                        y=alt.Y("count"),
                        text=alt.Text("count:Q"),
                    )

                    df_melted = df.melt(id_vars=["date"], value_vars=["like", "dislike"], var_name="type", value_name="value")
                    
                    bar = alt.Chart(df_melted).mark_bar().encode(
                        x=alt.X("date", axis=alt.Axis(title="日期")),
                        y=alt.Y("value", axis=alt.Axis(title="数量")),
                        color=alt.Color("type", legend=alt.Legend(title="类型")),
                        tooltip=["date", "type", "value"]
                    )

                    trend_chart = alt.layer(bar, line, line_text).resolve_scale(y='independent').properties(title="每日反馈趋势与情感分布")
                    st.altair_chart(trend_chart, use_container_width=True)
                with pie_col:
                    pie_source = pd.DataFrame(
                        [
                            {"type": "like", "value": range_stats.get("range_like", 0)},
                            {"type": "dislike", "value": range_stats.get("range_dislike", 0)},
                        ]
                    )
                    pie = alt.Chart(pie_source).mark_arc().encode(
                        theta=alt.Theta("value:Q"),
                        color=alt.Color("type:N", legend=alt.Legend(title="类型")),
                        tooltip=["type", "value"],
                    ).properties(title="情感占比")
                    pie_labels = alt.Chart(pie_source).transform_joinaggregate(
                        total="sum(value)"
                    ).transform_calculate(
                        percent="datum.value / datum.total"
                    ).mark_text(
                        radius=60,
                        align="center",
                        baseline="middle",
                        fontSize=20
                    ).encode(
                        theta=alt.Theta("value:Q", stack=True),
                        text=alt.Text("label:N"),
                    ).transform_calculate(
                        label="format(datum.value, 'd') + ' (' + format(datum.percent, '.0%') + ')'"
                    )
                    st.altair_chart(pie + pie_labels, use_container_width=True)

                compare_df = df.melt(
                    id_vars=["date"],
                    value_vars=["suggest", "jira_key_count"],
                    var_name="metric",
                    value_name="value",
                )
                compare_df["metric"] = compare_df["metric"].replace(
                    {"suggest": "建议数量", "jira_key_count": "工单数量"}
                )
                compare_line = alt.Chart(compare_df).mark_line(point=True).encode(
                    x=alt.X("date", axis=alt.Axis(title="日期")),
                    y=alt.Y("value", axis=alt.Axis(title="数量")),
                    color=alt.Color("metric", legend=alt.Legend(title="类型")),
                    tooltip=["date", "metric", "value"],
                )
                compare_text = alt.Chart(compare_df).mark_text(dy=-10, fontSize=20).encode(
                    x=alt.X("date"),
                    y=alt.Y("value"),
                    text=alt.Text("value:Q"),
                    color=alt.Color("metric", legend=None),
                )
                compare_chart = alt.layer(compare_line, compare_text).properties(title="建议与工单数量对比")
                st.altair_chart(compare_chart, use_container_width=True)
                
                table_rows = range_stats.get("detail_rows", [])
                st.subheader("建议与工单明细")
                if table_rows:
                    filtered_rows = [row for row in table_rows if (row.get("建议内容") or "").strip()]
                    if filtered_rows:
                        st.dataframe(pd.DataFrame(filtered_rows), use_container_width=True)
                    else:
                        st.info("暂无明细数据")
                else:
                    st.info("暂无明细数据")
            else:
                st.info(f"暂无{range_title}数据")

    elif admin_tab == "反馈列表":
        st.subheader("反馈列表")
        col_filters, col_content = st.columns([1, 7])
        
        with col_filters:
            st.subheader("过滤条件")
            range_option = st.selectbox(
                "时间范围",
                options=["全部", "今天", "本周", "近7天", "本月", "本年", "自定义"],
                index=2,
                key="feedback_range_option",
            )
            from_date = None
            to_date = None
            if range_option == "自定义":
                from_date = st.date_input("开始日期", key="feedback_from_date")
                to_date = st.date_input("结束日期", key="feedback_to_date")
            else:
                from_value, to_value = build_time_range(range_option)
                if from_value:
                    from_date = datetime.fromisoformat(from_value).date()
                if to_value:
                    to_date = datetime.fromisoformat(to_value).date()
            log(f"构建时间范围: from_date={from_date} to_date={to_date}")
            sentiment = st.selectbox("情感", options=["", "like", "dislike"], key="feedback_sentiment")
            status = st.selectbox(
                "状态",
                options=["", "pending", "accepted", "rejected", "followup"],
                key="feedback_status",
            )
            keyword = st.text_input("关键词", key="feedback_keyword")
            page_size = st.selectbox("每页条数", options=[10, 20, 50, 100], key="feedback_page_size")
            page = st.number_input("页码", min_value=1, step=1, value=1, key="feedback_page")

            filters: Dict[str, Any] = {"page": int(page), "page_size": page_size}
            if from_date:
                filters["from"] = from_date.isoformat()
            if to_date:
                filters["to"] = f"{to_date.isoformat()}T23:59:59.999999"
            if sentiment:
                filters["sentiment"] = sentiment
            if status:
                filters["status"] = status
            if keyword:
                filters["q"] = keyword

            export_filename = st.text_input("导出文件名 (可选，不含扩展名)", key="log_export_name")
            col_export_csv, col_export_json = st.columns(2)
            time_range = f"{from_date}-{to_date}"
            with col_export_csv:
                if st.button("导出 CSV"):
                    ok_export, content, filename = download_export("/api/feedbacks/export", {**filters, "format": "csv"})
                    if ok_export:
                        final_name = f"{export_filename}.csv" if export_filename else filename
                        final_name = final_name.split(".")[0]
                        final_name = f"{final_name}-{time_range}.csv"
                        st.download_button("下载 CSV", data=content, file_name=final_name, mime="text/csv")
                    else:
                        st.error("导出失败")
            with col_export_json:
                if st.button("导出 JSON"):
                    ok_export, content, filename = download_export("/api/feedbacks/export", {**filters, "format": "json"})
                    if ok_export:
                        final_name = f"{export_filename}.json" if export_filename else filename
                        final_name = final_name.split(".")[0]
                        final_name = f"{final_name}-{time_range}.json"
                        st.download_button("下载 JSON", data=content, file_name=final_name, mime="application/json")
                    else:
                        st.error("导出失败")

        with col_content:
            render_feedback_list(filters)

    elif admin_tab == "标签统计":
        col_filters, col_content = st.columns([1, 6]) 
        from_date = None
        to_date = None
        label_name = None
        created_jql = ""
        label_stats_df = None
        label_detail_df = None
        threshold_minutes = None
        with col_filters:
            st.subheader("过滤条件")
            range_option = st.selectbox("给Jira打Label的时间范围", options=["全部", "今天", "本周", "近7天", "本月", "本年", "自定义"], index=2, key="label_range_option")

            if range_option == "自定义":
                from_date = st.date_input("开始日期", key="label_from_date")
                to_date = st.date_input("结束日期", key="label_to_date")
            else:
                from_value, to_value = build_time_range(range_option)
                if from_value:
                    from_date = datetime.fromisoformat(from_value).date()
                if to_value:
                    to_date = datetime.fromisoformat(to_value).date()
            threshold_minutes = st.number_input("超时阈值(分钟)", min_value=1, value=240, step=10, key="label_threshold_minutes")
            # label_name = st.text_input("标签", value="SE-LN-LOG-2026", key="label_name")
            # created_jql = st.text_area("JQL_Filter", value="(project in (\"OTT projects\") AND priority in (High, Highest) AND type in (Bug, Sub-bug) AND (status not in (Closed, Done, Resolved, Verified) OR labels = SE-LN-LOG-2026))", height=120, key="created_jql")
            st.subheader("导出")
            export_filename = st.text_input("导出文件名 (可选，不含扩展名)", key="label_export_name")
        
        with col_content:
            if not config.mysql_database or not config.mysql_table:
                st.warning("请配置 MYSQL_DATABASE 和 MYSQL_TABLE")
            else:
                
                try:
                    rows = mysql_client.fetchall(
                        f"""
                        SELECT `key`, attachment_time, label_time, priority_time, attachemt_delay_minutes, priority_delay_minutes
                        FROM `{config.mysql_database}`.`{config.mysql_table}`
                        """
                    )
                except Exception as exc:
                    st.error(str(exc))
                    rows = []

                if not rows:
                    st.info("暂无数据")
                else:
                    now = datetime.now()
                    created_label_date_counts: Dict[Any, int] = {}
                    created_attachment_date_counts: Dict[Any, int] = {}
                    detail_rows = []
                    label_filtered_rows = []
                    no_label_overdue_rows = []

                    for row in rows:
                        issue_key = row[0]
                        attachemt_dt = row[1] if isinstance(row[1], datetime) else parse_datetime(str(row[1]) if row[1] else None)
                        label_dt = row[2] if isinstance(row[2], datetime) else parse_datetime(str(row[2]) if row[2] else None)
                        priority_dt = row[3] if isinstance(row[3], datetime) else parse_datetime(str(row[3]) if row[3] else None)
                        attachemt_delay_minutes = float(row[4]) if row[4] is not None else None
                        priority_delay_minutes = float(row[5]) if row[5] is not None else None

                        if attachemt_delay_minutes is None and attachemt_dt and label_dt:
                            attachemt_delay_minutes = round((label_dt - attachemt_dt).total_seconds() / 60.0, 2)
                        if priority_delay_minutes is None and priority_dt and label_dt:
                            priority_delay_minutes = round((label_dt - priority_dt).total_seconds() / 60.0, 2)

                        if attachemt_dt:
                            created_date = attachemt_dt.date()
                            if (not from_date or created_date >= from_date) and (not to_date or created_date <= to_date):
                                created_attachment_date_counts[created_date] = created_attachment_date_counts.get(created_date, 0) + 1

                        if label_dt:
                            label_date = label_dt.date()
                            if (not from_date or label_date >= from_date) and (not to_date or label_date <= to_date):
                                created_label_date_counts[label_date] = created_label_date_counts.get(label_date, 0) + 1
                                if (
                                    attachemt_delay_minutes is not None
                                    and priority_delay_minutes is not None
                                    and attachemt_delay_minutes >= threshold_minutes
                                    and priority_delay_minutes >= threshold_minutes
                                ):
                                    label_filtered_rows.append(
                                        {
                                            "key": issue_key,
                                            "attachment_time": attachemt_dt.isoformat() if attachemt_dt else "",
                                            "label_time": label_dt.isoformat() if label_dt else "",
                                            "priority_time": priority_dt.isoformat() if priority_dt else "",
                                            "attachemt_delay_minutes": attachemt_delay_minutes,
                                            "priority_delay_minutes": priority_delay_minutes,
                                        }
                                    )
                        elif attachemt_dt and priority_dt:
                            attachment_overdue = (now - attachemt_dt).total_seconds() / 60.0 >= threshold_minutes
                            priority_overdue = (now - priority_dt).total_seconds() / 60.0 >= threshold_minutes
                            if attachment_overdue and priority_overdue:
                                no_label_overdue_rows.append(
                                    {
                                        "key": issue_key,
                                        "attachment_time": attachemt_dt.isoformat() if attachemt_dt else "",
                                        "label_time": "",
                                        "priority_time": priority_dt.isoformat() if priority_dt else "",
                                        "attachemt_delay_minutes": attachemt_delay_minutes,
                                        "priority_delay_minutes": priority_delay_minutes,
                                    }
                                )

                        detail_rows.append(
                            {
                                "key": issue_key,
                                "attachment_time": attachemt_dt.isoformat() if attachemt_dt else "",
                                "label_time": label_dt.isoformat() if label_dt else "",
                                "priority_time": priority_dt.isoformat() if priority_dt else "",
                                "attachemt_delay_minutes": attachemt_delay_minutes,
                                "priority_delay_minutes": priority_delay_minutes,
                            }
                        )

                    metric_col1, metric_col2 = st.columns(2)
                    metric_col1.metric("创建 Attachment 数量", sum(created_attachment_date_counts.values()))
                    metric_col2.metric("创建 Label 数量", sum(created_label_date_counts.values()))

                    date_index = sorted(set(created_label_date_counts) | set(created_attachment_date_counts))
                    if date_index:
                        rows = []
                        for date_value in date_index:
                            date_label = date_value.isoformat()
                            rows.append(
                                {
                                    "日期": date_label,
                                    "Attachment数量": created_attachment_date_counts.get(date_value, 0),
                                    "Label数量": created_label_date_counts.get(date_value, 0),
                                }
                            )

                        df = pd.DataFrame(rows)
                        label_stats_df = df
                        label_detail_df = pd.DataFrame(detail_rows)
                        st.dataframe(df, use_container_width=True)

                        date_labels = [value.isoformat() for value in date_index]
                        counts_df = df.melt(
                            id_vars=["日期"],
                            value_vars=["Attachment数量", "Label数量"],
                            var_name="类型",
                            value_name="数量",
                        )
                        chart_counts = alt.Chart(counts_df).mark_bar().encode(
                            x=alt.X(
                                "日期",
                                sort=date_labels,
                                title="日期",
                            ),
                            xOffset=alt.XOffset("类型", sort=["Attachment数量", "Label数量"]),
                            y=alt.Y("数量:Q", title="数量", stack=None),
                            color=alt.Color("类型", title="类型"),
                            tooltip=["日期", "类型", "数量"],
                        ).properties(title="每日附件与标签数量")
                        st.altair_chart(chart_counts, use_container_width=True)

                        if not label_detail_df.empty:
                            st.subheader(f"超过{threshold_minutes}分钟后才打标签的Jira")
                            if label_filtered_rows:
                                st.dataframe(pd.DataFrame(label_filtered_rows), use_container_width=True)
                            else:
                                st.info(f"没有超过{threshold_minutes}分钟后才打标签的Jira")
                            st.subheader(f"超过{threshold_minutes}分钟还没有打标签的Jira")
                            if no_label_overdue_rows:
                                st.dataframe(pd.DataFrame(no_label_overdue_rows), use_container_width=True)
                            else:
                                st.info(f"没有超过{threshold_minutes}分钟的未打标签Jira")

                        if not label_detail_df.empty and "attachemt_delay_minutes" in label_detail_df.columns:
                            delay_df = label_detail_df.copy()
                            if not delay_df.empty:
                                st.subheader("附件&标签&优先级的总表")
                                st.dataframe(delay_df, use_container_width=True)
                                delay_long = delay_df.melt(
                                    id_vars=["key"],
                                    value_vars=["attachemt_delay_minutes", "priority_delay_minutes"],
                                    var_name="类型",
                                    value_name="耗时",
                                )
                                delay_long = delay_long.dropna(subset=["耗时"])
                                if not delay_long.empty:
                                    delay_long["类型"] = delay_long["类型"].replace(
                                        {
                                            "attachemt_delay_minutes": "附件->标签",
                                            "priority_delay_minutes": "优先级->标签",
                                        }
                                    )
                                    priority_order = (
                                        delay_df.dropna(subset=["priority_delay_minutes"])
                                        .sort_values("priority_delay_minutes", ascending=False)["key"]
                                        .tolist()
                                    )
                                    if not priority_order:
                                        priority_order = sorted(delay_df["key"].unique().tolist())
                                    chart_delay = alt.Chart(delay_long).mark_bar(opacity=0.5).encode(
                                        x=alt.X("key", title="Key", sort=priority_order),
                                        y=alt.Y(
                                            "耗时:Q",
                                            title="时间差(分钟)",
                                            stack=None,
                                            scale=alt.Scale(type="symlog", constant=1),
                                        ),
                                        color=alt.Color("类型", title="类型"),
                                        tooltip=["key", "类型", "耗时"],
                                    ).properties(title="标签耗时分布")
                                    threshold_line = alt.Chart(
                                        pd.DataFrame({"y": [threshold_minutes]}),
                                ).mark_rule(strokeDash=[6, 4]).encode(
                                    y="y:Q"
                                )
                                st.caption("说明：attachment_time/attachment_delay_minutes 为 None 表示没有附件，priority_delay_minutes 为 None表示没有打label。")
                                delay_chart = (chart_delay + threshold_line).properties(title="标签耗时分布")
                                st.altair_chart(delay_chart, use_container_width=True)


        with col_filters:
            time_range = f"{from_date}-{to_date}"
            if label_stats_df is not None and not label_stats_df.empty:
                final_name = f"{export_filename}.csv" if export_filename else "label_stats.csv"
                export_df = label_stats_df[["日期", "Attachment数量", "Label数量"]]
                csv_data = export_df.to_csv(index=False, header=True).encode("utf-8-sig")
                final_name = final_name.split(".")[0]
                final_name = f"{final_name}-{time_range}.csv"
                st.download_button("下载汇总 CSV", data=csv_data, file_name=final_name, mime="text/csv")
                
                if label_detail_df is not None and not label_detail_df.empty:
                    detail_name = f"{export_filename}_detail.csv" if export_filename else "label_stats_detail.csv"
                    detail_name = detail_name.split(".")[0]
                    detail_name = f"{detail_name}-{time_range}.csv"
                    detail_csv = label_detail_df.to_csv(index=False, header=True).encode("utf-8-sig")
                    st.download_button("下载明细 CSV", data=detail_csv, file_name=detail_name, mime="text/csv")
            else:
                st.info("暂无可导出数据")
            
    elif admin_tab == "调试日志记录":
        col_filters, col_content = st.columns([1, 4])
        
        with col_filters:
            st.subheader("日志记录")
            log_from = st.date_input("日志开始日期", key="log_from")
            log_to = st.date_input("日志结束日期", key="log_to")
            category = st.selectbox("类型", options=["", "job", "run", "error", "analysis"])
            log_keyword = st.text_input("日志关键词", key="log_keyword")

            log_filters: Dict[str, Any] = {"page": 1, "page_size": 50}
            if log_from:
                log_filters["from"] = log_from.isoformat()
            if log_to:
                log_filters["to"] = f"{log_to.isoformat()}T23:59:59.999999"
            if category:
                log_filters["category"] = category
            if log_keyword:
                log_filters["q"] = log_keyword
            
            export_filename = st.text_input("导出文件名 (可选，不含扩展名)", key="log_export_name")
            col_log_csv, col_log_json = st.columns(2)
            with col_log_csv:
                if st.button("导出日志 CSV"):
                    ok_export, content, filename = download_export("/api/logs", {**log_filters, "format": "csv"})
                    if ok_export:
                        final_name = f"{export_filename}-{log_from}-{log_to}.csv" if export_filename else filename
                        st.download_button("下载日志 CSV", data=content, file_name=final_name, mime="text/csv")
                    else:
                        st.error("导出失败")
            with col_log_json:
                if st.button("导出日志 JSON"):
                    ok_export, content, filename = download_export("/api/logs", {**log_filters, "format": "json"})
                    if ok_export:
                        final_name = f"{export_filename}.json" if export_filename else filename
                        st.download_button("下载日志 JSON", data=content, file_name=final_name, mime="application/json")
                    else:
                        st.error("导出失败")
        with col_content:
            render_logs(log_filters)

    elif admin_tab == "使用情况":
        st.subheader("分析问题量统计")
        col_filters, col_content = st.columns([1, 7])
        with col_filters:
            st.subheader("过滤条件")
            range_option = st.selectbox(
                "时间范围",
                options=["全部", "今天", "本周", "近7天", "本月", "本年", "自定义"],
                index=2,
                key="analysis_range_option",
            )
            from_date = None
            to_date = None
            if range_option == "自定义":
                from_date = st.date_input("开始日期", key="analysis_from_date")
                to_date = st.date_input("结束日期", key="analysis_to_date")
            else:
                from_value, to_value = build_time_range(range_option)
                if from_value:
                    from_date = datetime.fromisoformat(from_value).date()
                if to_value:
                    to_date = datetime.fromisoformat(to_value).date()

        with col_content:
            analysis_daily_counts = None
            access_daily_counts = None
            comment_daily_counts = None
            analysis_df = None
            access_df = None
            ip_breakdown_df = None
            ip_summary_df = None
            analysis_total = None
            access_total = None
            comment_total = None
            if from_date and to_date:
                range_label = f"{from_date} ~ {to_date}"
            elif from_date and not to_date:
                range_label = f"{from_date} ~"
            elif to_date and not from_date:
                range_label = f"~ {to_date}"
            else:
                range_label = "全部"
            if not config.mysql_database or not config.mysql_analysis_table:
                st.warning("请配置 MYSQL_DATABASE 和 MYSQL_ANALYSIS_TABLE")
            else:
                analysis_where_clauses = []
                analysis_params: List[Any] = []
                if from_date:
                    analysis_where_clauses.append("create_time >= %s")
                    analysis_params.append(from_date.isoformat())
                if to_date:
                    analysis_where_clauses.append("create_time <= %s")
                    analysis_params.append(f"{to_date.isoformat()}T23:59:59.999999")
                analysis_where_sql = (
                    f"WHERE {' AND '.join(analysis_where_clauses)}" if analysis_where_clauses else ""
                )

                try:
                    analysis_columns_rows = mysql_client.fetchall(
                        f"SHOW COLUMNS FROM `{config.mysql_database}`.`{config.mysql_analysis_table}`"
                    )
                    analysis_column_names = [row[0] for row in analysis_columns_rows]
                except Exception as exc:
                    st.error(str(exc))
                    analysis_column_names = []

                try:
                    analysis_rows = mysql_client.fetchall(
                        f"""
                        SELECT * FROM `{config.mysql_database}`.`{config.mysql_analysis_table}`
                        {analysis_where_sql}
                        ORDER BY create_time DESC
                        """,
                        params=tuple(analysis_params),
                    )
                except Exception as exc:
                    st.error(str(exc))
                    analysis_rows = []

                if analysis_rows and analysis_column_names:

                    analysis_df = pd.DataFrame(
                        [dict(zip(analysis_column_names, row)) for row in analysis_rows]
                    )


                    if "create_time" in analysis_df.columns:
                        analysis_df["create_time"] = analysis_df["create_time"].apply(
                            lambda value: value if isinstance(value, datetime) else parse_datetime(str(value))
                        )
                        if "extra" in analysis_df.columns:
                            def extract_add_comment_count(value):
                                if value is None:
                                    return 0
                                if isinstance(value, dict):
                                    raw = value.get("add_comment_count")
                                    return int(raw) if raw is not None else 0
                                try:
                                    parsed = json.loads(value)
                                except Exception:
                                    return 0
                                if isinstance(parsed, dict):
                                    raw = parsed.get("add_comment_count")
                                    return int(raw) if raw is not None else 0
                                return 0

                            analysis_df["add_comment_count"] = analysis_df["extra"].apply(
                                extract_add_comment_count
                            )
                        else:
                            analysis_df["add_comment_count"] = 0
                        analysis_daily_counts = (
                            analysis_df.dropna(subset=["create_time"])
                            .assign(date=analysis_df["create_time"].dt.date)
                            .groupby("date")
                            .size()
                            .reset_index(name="count")
                        )
                        if not analysis_daily_counts.empty:
                            analysis_daily_counts = analysis_daily_counts.sort_values("date")
                            analysis_total = int(analysis_daily_counts["count"].sum())
                            analysis_daily_counts["metric"] = "分析问题量"
                            comment_daily_counts = (
                                analysis_df.dropna(subset=["create_time"])
                                .assign(date=analysis_df["create_time"].dt.date)
                                .groupby("date")["add_comment_count"]
                                .sum()
                                .reset_index(name="count")
                            )
                            if not comment_daily_counts.empty:
                                comment_daily_counts = comment_daily_counts.sort_values("date")
                                comment_daily_counts["metric"] = "评论次数"
                                comment_total = int(comment_daily_counts["count"].sum())
                        else:
                            st.info("暂无分析问题量数据")
                else:
                    st.info("暂无分析问题量数据")
            
            if not config.mysql_database or not config.mysql_access_table:
                st.warning("请配置 MYSQL_DATABASE 和 MYSQL_ACCESS_TABLE")
            else:
                access_where_clauses = []
                access_params: List[Any] = []
                if from_date:
                    access_where_clauses.append("create_time >= %s")
                    access_params.append(from_date.isoformat())
                if to_date:
                    access_where_clauses.append("create_time <= %s")
                    access_params.append(f"{to_date.isoformat()}T23:59:59.999999")
                access_where_sql = (
                    f"WHERE {' AND '.join(access_where_clauses)}" if access_where_clauses else ""
                )

                try:
                    access_columns_rows = mysql_client.fetchall(
                        f"SHOW COLUMNS FROM `{config.mysql_database}`.`{config.mysql_access_table}`"
                    )
                    access_column_names = [row[0] for row in access_columns_rows]
                except Exception as exc:
                    st.error(str(exc))
                    access_column_names = []

                try:
                    access_rows = mysql_client.fetchall(
                        f"""
                        SELECT * FROM `{config.mysql_database}`.`{config.mysql_access_table}`
                        {access_where_sql}
                        ORDER BY create_time DESC
                        """,
                        params=tuple(access_params),
                    )
                except Exception as exc:
                    st.error(str(exc))
                    access_rows = []

                if access_rows and access_column_names:
                    access_df = pd.DataFrame(
                        [dict(zip(access_column_names, row)) for row in access_rows]
                    )

                    if "create_time" in access_df.columns:
                        access_df["create_time"] = access_df["create_time"].apply(
                            lambda value: value if isinstance(value, datetime) else parse_datetime(str(value))
                        )
                        if "ip" in access_df.columns:
                            end_date = to_date or datetime.now().date()
                            start_dt = (
                                datetime.combine(from_date, datetime.min.time()) if from_date else None
                            )
                            end_dt = datetime.combine(end_date, datetime.max.time())
                            try:
                                all_rows = mysql_client.fetchall(
                                    f"""
                                    SELECT ip, create_time FROM `{config.mysql_database}`.`{config.mysql_access_table}`
                                    WHERE create_time <= %s
                                    """,
                                    params=(end_dt.isoformat(),),
                                )
                            except Exception as exc:
                                st.error(str(exc))
                                all_rows = []
                            if all_rows:
                                access_all_df = pd.DataFrame(all_rows, columns=["ip", "create_time"])
                                access_all_df["create_time"] = access_all_df["create_time"].apply(
                                    lambda value: value if isinstance(value, datetime) else parse_datetime(str(value))
                                )
                                first_seen = (
                                    access_all_df.dropna(subset=["ip", "create_time"])
                                    .groupby("ip")["create_time"]
                                    .min()
                                )
                                if start_dt is None:
                                    min_seen = access_all_df["create_time"].min()
                                    start_dt = min_seen if isinstance(min_seen, datetime) else datetime.min
                                range_df = (
                                    access_df.dropna(subset=["ip", "create_time"])
                                    .loc[
                                        (access_df["create_time"] >= start_dt)
                                        & (access_df["create_time"] <= end_dt),
                                        ["ip", "create_time"],
                                    ]
                                    .copy()
                                )
                                if not range_df.empty:
                                    range_df["date"] = range_df["create_time"].dt.date
                                    first_seen_date = first_seen.dt.date
                                    daily_rows = []
                                    for date_value in sorted(range_df["date"].unique()):
                                        day_ips = (
                                            range_df.loc[range_df["date"] == date_value, "ip"]
                                            .dropna()
                                            .unique()
                                        )
                                        new_ips = []
                                        existing_ips = []
                                        for ip_value in day_ips:
                                            first_date = first_seen_date.get(ip_value)
                                            if first_date is None:
                                                continue
                                            if first_date < date_value:
                                                existing_ips.append(ip_value)
                                            else:
                                                new_ips.append(ip_value)
                                        daily_rows.append(
                                            {
                                                "date": str(date_value),
                                                "metric": "新增用户",
                                                "count": len(new_ips),
                                                "ips": "; ".join(sorted(new_ips)),
                                            }
                                        )
                                        daily_rows.append(
                                            {
                                                "date": str(date_value),
                                                "metric": "既有用户",
                                                "count": len(existing_ips),
                                                "ips": ",".join(sorted(existing_ips)),
                                            }
                                        )
                                    if daily_rows:
                                        ip_breakdown_df = pd.DataFrame(daily_rows)

                                        summary_df = (
                                            ip_breakdown_df.groupby("metric", as_index=False)["count"].sum()
                                        )
                                        total_count = int(summary_df["count"].sum())
                                        if total_count > 0:
                                            summary_df["percent"] = summary_df["count"] / total_count
                                        else:
                                            summary_df["percent"] = 0
                                        summary_df["label"] = (
                                            summary_df["count"].astype(str)
                                            + " ("
                                            + (summary_df["percent"] * 100).round(1).astype(str)
                                            + "%)"
                                        )
                                        summary_df["metric_order"] = summary_df["metric"].map(
                                            {"新增用户": 0, "既有用户": 1}
                                        ).fillna(999)
                                        ip_summary_df = summary_df
                        access_daily_counts = (
                            access_df.dropna(subset=["create_time"])
                            .assign(date=access_df["create_time"].dt.date)
                            .groupby("date")
                            .size()
                            .reset_index(name="count")
                        )
                        if not access_daily_counts.empty:
                            access_daily_counts = access_daily_counts.sort_values("date")
                            access_total = int(access_daily_counts["count"].sum())
                            access_daily_counts["metric"] = "访问量"
                        else:
                            st.info("暂无访问量数据")
                else:
                    st.info("暂无访问量数据")

            if analysis_total is not None or access_total is not None or comment_total is not None:
                st.markdown(f"{range_label}统计")
                metric_col1, metric_col2, metric_col3 = st.columns(3)
                with metric_col1:
                    if analysis_total is not None:
                        st.metric("分析问题量", analysis_total)
                with metric_col2:
                    if access_total is not None:
                        st.metric("访问量", access_total)
                with metric_col3:
                    if comment_total is not None:
                        st.metric("评论次数", comment_total)

            if (
                analysis_daily_counts is not None
                or access_daily_counts is not None
                or comment_daily_counts is not None
            ):

                series_frames = [
                    series
                    for series in (analysis_daily_counts, access_daily_counts, comment_daily_counts)
                    if series is not None
                ]
                date_values = pd.concat(
                    [series["date"] for series in series_frames if not series.empty],
                    ignore_index=True,
                )
                if not date_values.empty:
                    min_date = date_values.min()
                    max_date = date_values.max()
                    full_dates = pd.date_range(min_date, max_date, freq="D").date
                    filled_frames = []
                    for series in series_frames:
                        if series.empty:
                            continue
                        metric_value = series["metric"].iloc[0] if "metric" in series.columns else None
                        filled = (
                            series[["date", "count"]]
                            .set_index("date")
                            .reindex(full_dates, fill_value=0)
                            .rename_axis("date")
                            .reset_index()
                        )
                        if metric_value is not None:
                            filled["metric"] = metric_value
                        filled_frames.append(filled)
                    combined_df = (
                        pd.concat(filled_frames, ignore_index=True)
                        if filled_frames
                        else pd.DataFrame(columns=["date", "count", "metric"])
                    )
                else:
                    combined_df = pd.concat(series_frames, ignore_index=True)
                combined_df["date"] = combined_df["date"].astype(str)
                y_max = float(combined_df["count"].max() or 0)
                y_pad = max(1.0, y_max * 0.15)
                y_enc = alt.Y("count", title="数量", scale=alt.Scale(domain=[0, y_max + y_pad]))
                line = alt.Chart(combined_df).mark_line(point=True).encode(
                    x=alt.X("date", title="日期"),
                    y=y_enc,
                    color=alt.Color("metric:N", legend=alt.Legend(title="类型", orient="right")),
                    tooltip=["date", "count", "metric"],
                )
                line_text = alt.Chart(combined_df).mark_text(dy=-10, fontSize=20).encode(
                    x=alt.X("date"),
                    y=y_enc,
                    text=alt.Text("count:Q"),
                    color=alt.Color("metric:N", legend=None),
                )
                layered = alt.layer(line, line_text).resolve_scale(color="shared").properties(title="分析/访问/评论趋势")
                st.altair_chart(layered, use_container_width=True)

            if ip_breakdown_df is not None and not ip_breakdown_df.empty and ip_summary_df is not None:
                bar = alt.Chart(ip_breakdown_df).mark_bar().encode(
                    x=alt.X("date", title="日期"),
                    y=alt.Y("count", title="IP数量"),
                    color=alt.Color("metric", title="类型"),
                    tooltip=["date", "metric", "count"],
                ).properties(title="新增/既有用户每日变化")
                pie = alt.Chart(ip_summary_df).mark_arc().encode(
                    theta=alt.Theta("count", title="IP数量"),
                    color=alt.Color("metric", title="类型"),
                    order=alt.Order("metric_order"),
                    tooltip=["metric", "count", "percent"],
                )
                labels = alt.Chart(ip_summary_df).mark_text(
                    radius=60,
                    align="center",
                    baseline="middle",
                    fontSize=20,
                ).encode(
                    theta=alt.Theta("count", stack=True),
                    order=alt.Order("metric_order"),
                    text="label",
                )
                ip_pie = (pie + labels).properties(title="新增/既有用户占比")
                ip_col1, ip_col2 = st.columns([2, 1])
                with ip_col1:
                    st.altair_chart(bar, use_container_width=True)
                with ip_col2:
                    st.altair_chart(ip_pie, use_container_width=True)

            if analysis_df is not None and not analysis_df.empty:
                st.subheader("分析问题量明细")
                st.dataframe(analysis_df, use_container_width=True)

            if access_df is not None and not access_df.empty:
                st.subheader("访问量明细")
                st.dataframe(access_df, use_container_width=True)

            if ip_breakdown_df is not None and not ip_breakdown_df.empty:
                st.subheader("新增/既有用户明细")
                st.dataframe(ip_breakdown_df, use_container_width=True)

def render_app() -> None:
    """
    功能：渲染应用入口并切换前后台页面。
    参数：无。
    返回值：无。
    异常：无显式异常。
    """
    st.set_page_config(page_title="建议收集系统", layout="wide")
    page = get_requested_page()
    if page == "admin":
        render_admin()
    else:
        render_frontend()


def get_requested_page() -> str:
    page = None
    try:
        if hasattr(st, "query_params"):
            page = st.query_params.get("page")
            if isinstance(page, list):
                page = page[0] if page else None
    except Exception:
        page = None
    if not page:
        ctx = get_script_run_ctx(suppress_warning=True)
        if ctx and ctx.query_string:
            params = parse_qs(ctx.query_string)
            values = params.get("page")
            if values:
                page = values[0]
    return "admin" if str(page).lower() == "admin" else "feedback"


if __name__ == "__main__":
    render_app()

# cd /home/amlogic/FAE/AutoLog/lingzhi.bi/log_analyze_feedback_mvp && source /home/amlogic/FAE/AutoLog/lingzhi.bi/log_analyze_feedback_mvp/310venv/bin/activate && nohup streamlit run streamlit_app.py --server.port 8054 --server.headless true &
# 
