import io
import json
import os
import base64
import uuid
from datetime import datetime, timedelta, timezone
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


config = load_config()
db = DB(config.database_path)
my_jira = MyJira(config.jira_server, config.jira_username, config.jira_password)
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
    return st.session_state.get("user_ip", "local")


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
    item = db.fetch_one("SELECT * FROM feedbacks WHERE id = ?", (feedback_id,))
    if not item:
        return None
    item["attachments"] = json_loads(item.get("attachments") or "[]")
    item["summary"] = item["content"][:120]
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
    cursor = db.execute_query(
        """
        INSERT INTO feedbacks (created_at, updated_at, sentiment, content, user_ip, attachments, status, jira_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            created_at,
            created_at,
            sentiment,
            content.strip(),
            get_user_ip(),
            "[]",
            "pending",
            None,
        ),
    )
    feedback_id = int(cursor.lastrowid)
    attachments = save_uploaded_files(files, feedback_id) if files else []
    db.execute_query(
        "UPDATE feedbacks SET attachments = ?, updated_at = ? WHERE id = ?",
        (json_dumps(attachments), now_iso(), feedback_id),
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



def list_feedbacks(filters: Dict[str, Any]) -> Dict[str, Any]:
    page = int(filters.get("page", 1))
    page_size = int(filters.get("page_size", 50))
    sentiment = filters.get("sentiment") or None
    status = filters.get("status") or None
    keyword = filters.get("q") or None
    from_time = parse_datetime(filters.get("from"))
    to_time = parse_datetime(filters.get("to"))

    conditions = []
    params: List[Any] = []
    if sentiment:
        conditions.append("sentiment = ?")
        params.append(sentiment)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if keyword:
        conditions.append("(content LIKE ? OR user_ip LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if from_time:
        conditions.append("created_at >= ?")
        params.append(from_time.isoformat())
    if to_time:
        conditions.append("created_at <= ?")
        params.append(to_time.isoformat())

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    log(f"where_clause={where_clause} params={params}")
    total = db.fetch_value(f"SELECT COUNT(*) FROM feedbacks {where_clause}", tuple(params))
    offset = (page - 1) * page_size
    rows = db.fetch_all(
        f"""
        SELECT id, created_at, sentiment, content, user_ip, status, jira_key, attachments, extra_content
        FROM feedbacks
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        tuple(params + [page_size, offset]),
    )
    for row in rows:
        row["attachments"] = json_loads(row.get("attachments") or "[]")
        # Extract text from HTML for summary
        content_html = row["content"] or ""
        try:
            summary_text = BeautifulSoup(content_html, "html.parser").get_text()
        except Exception:
            summary_text = content_html
        row["summary"] = summary_text[:50]
    return {"items": rows, "total": int(total or 0), "page": page, "page_size": page_size}


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


def stats_feedbacks(days_range: int = 7) -> Dict[str, Any]:
    total = db.fetch_value("SELECT COUNT(*) FROM feedbacks")
    like_count = db.fetch_value("SELECT COUNT(*) FROM feedbacks WHERE sentiment = 'like'")
    dislike_count = db.fetch_value("SELECT COUNT(*) FROM feedbacks WHERE sentiment = 'dislike'")
    days = format_recent_days(days_range)
    daily = []
    for day in days:
        count = db.fetch_value(
            "SELECT COUNT(*) FROM feedbacks WHERE created_at BETWEEN ? AND ?",
            (f"{day}T00:00:00+00:00", f"{day}T23:59:59+00:00"),
        )
        day_like = db.fetch_value(
            "SELECT COUNT(*) FROM feedbacks WHERE sentiment = 'like' AND created_at BETWEEN ? AND ?",
            (f"{day}T00:00:00+00:00", f"{day}T23:59:59+00:00"),
        )
        day_dislike = db.fetch_value(
            "SELECT COUNT(*) FROM feedbacks WHERE sentiment = 'dislike' AND created_at BETWEEN ? AND ?",
            (f"{day}T00:00:00+00:00", f"{day}T23:59:59+00:00"),
        )
        daily.append({
            "date": day,
            "count": int(count or 0),
            "like": int(day_like or 0),
            "dislike": int(day_dislike or 0),
        })
    return {"total": int(total or 0), "like": int(like_count or 0), "dislike": int(dislike_count or 0), "recent_trend": daily}


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
            return True, list_feedbacks(params)
        if path == "/api/logs":
            return True, list_logs(params)
        if path == "/api/stats/feedbacks":
            return True, stats_feedbacks()
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
    if status not in {"pending", "accepted", "rejected", "followup"}:
        return False, "状态不合法"
    item = fetch_feedback(feedback_id)
    if not item:
        return False, "反馈不存在"
    db.execute_query(
        "UPDATE feedbacks SET status = ?, extra_content = ?, updated_at = ? WHERE id = ?",
        (status, json_dumps({"note": note or ""}), now_iso(), feedback_id),
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
    
    db.execute_query(
        "UPDATE feedbacks SET jira_key = ?, updated_at = ? WHERE id = ?",
        (jira_key, now_iso(), feedback_id),
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
    
    db.execute_query(
        "UPDATE feedbacks SET jira_key = ?, updated_at = ? WHERE id = ?",
        (None, now_iso(), feedback_id),
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
        data = list_feedbacks({**params, "page": 1, "page_size": 50000})
        rows = data.get("items", [])
        if export_format == "json":
            content = json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
            return True, content, "feedbacks.json"
        csv_buffer = io.StringIO()
        headers = ["id", "created_at", "sentiment", "content", "user_ip", "status", "jira_key", "extra_content", "note"]
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
                        str(row.get("created_at", "")),
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
    data = list_feedbacks(filters)
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
            st.write(f"时间：{item['created_at']}")
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
                options=["pending", "accepted", "rejected", "followup"],
                index=["pending", "accepted", "rejected", "followup"].index(item["status"]),
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
    st.header("后台管理")
    logged_in = require_admin_session()
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

    if st.button("退出登录"):
        st.session_state.admin_logged_in = False
        st.session_state.pop("admin_expires_at", None)
        st.rerun()
    if st.button("强制刷新"):
        st.rerun()

    admin_tab = st.sidebar.radio("管理菜单", ["统计信息", "反馈列表","标签统计", "日志记录"], key="admin_tab")
   
    with st.sidebar:
        st.markdown("---")
        log_refresh_interval = None
        log_auto_refresh = st.checkbox("自动刷新", value=False, key="log_auto_refresh")
        if log_auto_refresh:
            log_refresh_interval = st.selectbox("刷新间隔(秒)", options=[5, 10, 30, 60, 120, 600], index=5, key="log_refresh_interval")
        if log_refresh_interval:     
            st_autorefresh(log_refresh_interval * 1000, key="log_autorefresh")
    if admin_tab == "统计信息":
        st.subheader("统计信息")
        days_range = st.number_input("统计天数", min_value=1, max_value=365, value=7)
        stats = stats_feedbacks(days_range)
        col1, col2, col3 = st.columns(3)
        col1.metric("提交总数", stats.get("total", 0))
        col2.metric("喜欢", stats.get("like", 0))
        col3.metric("不喜欢", stats.get("dislike", 0))

        recent_data = stats.get("recent_trend", [])
        if recent_data:
            import pandas as pd
            import altair as alt

            df = pd.DataFrame(recent_data)
            
            # 每日总数增长曲线 (Line Chart)
            line = alt.Chart(df).mark_line(point=True).encode(
                x=alt.X("date", axis=alt.Axis(title="日期")),
                y=alt.Y("count", axis=alt.Axis(title="每日提交总数")),
                tooltip=["date", "count", "like", "dislike"]
            )

            # 每日 Like/Dislike 堆叠柱状图 (Stacked Bar Chart)
            # 需要先转换数据格式为 long format 以便 altair 堆叠
            df_melted = df.melt(id_vars=["date"], value_vars=["like", "dislike"], var_name="type", value_name="value")
            
            bar = alt.Chart(df_melted).mark_bar().encode(
                x=alt.X("date", axis=alt.Axis(title="日期")),
                y=alt.Y("value", axis=alt.Axis(title="数量")),
                color=alt.Color("type", scale=alt.Scale(domain=["like", "dislike"], range=["#66c2a5", "#fc8d62"]), legend=alt.Legend(title="类型")),
                tooltip=["date", "type", "value"]
            )

            st.altair_chart(alt.layer(bar, line).resolve_scale(y='independent'), use_container_width=True)
            
            st.table(recent_data)
        else:
            st.info(f"暂无最近{days_range}天数据")

    elif admin_tab == "反馈列表":
        st.subheader("反馈列表")
        col_filters, col_content = st.columns([1, 4])
        
        with col_filters:
            st.subheader("过滤条件")
            range_option = st.selectbox(
                "时间范围",
                options=["全部", "今天", "近7天", "本月", "本年", "自定义"],
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
        col_filters, col_content = st.columns([1, 4]) 
        from_date = None
        to_date = None
        label_name = None
        created_jql = ""
        label_stats_df = None
        label_detail_df = None
        with col_filters:
            st.subheader("过滤条件")
            range_option = st.selectbox("Jira 创建时间范围", options=["全部", "今天", "近7天", "本月", "本年", "自定义"], index=2, key="label_range_option")

            if range_option == "自定义":
                from_date = st.date_input("开始日期", key="label_from_date")
                to_date = st.date_input("结束日期", key="label_to_date")
            else:
                from_value, to_value = build_time_range(range_option)
                if from_value:
                    from_date = datetime.fromisoformat(from_value).date()
                if to_value:
                    to_date = datetime.fromisoformat(to_value).date()
            label_name = st.text_input("标签", value="SE-LN-LOG-2026", key="label_name")
            created_jql = st.text_area("JQL_Filter", value="(project in (\"OTT projects\") AND priority in (High, Highest) AND type in (Bug, Sub-bug) AND (status not in (Closed, Done, Resolved, Verified) OR labels = SE-LN-LOG-2026))", height=120, key="created_jql")
            st.subheader("导出")
            export_filename = st.text_input("导出文件名 (可选，不含扩展名)", key="label_export_name")
        
        with col_content:
            if not label_name.strip():
                st.warning("请输入标签")
            else:
                # if from_date and from_date >= datetime.fromisoformat("2026-02-01").date():
                #     created_jql += f' AND created >= \"{from_date.isoformat()}\"'
                # else:
                #     created_jql += f' AND created >= \"2026-02-01\"'
                if from_date:
                    created_jql += f' AND created >= \"{from_date.isoformat()}\"'
                if to_date:
                    created_jql += f' AND created <= \"{to_date.isoformat()}\"'
                log(f"created_jql:{created_jql}")
                # 解析时间计算
                add_labels_time_items = my_jira.getLabelAppliedTimeWithSql(created_jql, label_name)
                add_labels_time_items_count = len(add_labels_time_items)
                log(f"add_labels_time_items_count:{add_labels_time_items_count}")
                created_label_date_counts: Dict[Any, int] = {}
                label_time_by_key: Dict[str, datetime] = {}
                for item in add_labels_time_items:
                    label_dt = parse_datetime(item.get("label_applied_time"))
                    if not label_dt:
                        continue
                    issue_key = item.get("key")
                    if issue_key:
                        label_time_by_key[issue_key] = label_dt
                    label_date = label_dt.date()
                    if from_date and label_date < from_date:
                        continue
                    if to_date and label_date > to_date:
                        continue
                    created_label_date_counts[label_date] = created_label_date_counts.get(label_date, 0) + 1

                add_attachemt_time_items = my_jira.getEarliestAttachmentTimeWithSql(created_jql)
                add_attachemt_time_items_count = len(add_attachemt_time_items)
                log(f"add_attachemt_time_items_count:{add_attachemt_time_items_count}")
                created_attachment_date_counts: Dict[Any, int] = {}
                attachemt_time_by_key: Dict[str, datetime] = {}
                for item in add_attachemt_time_items:
                    attachemt_dt = parse_datetime(item.get("attachment_time"))
                    if not attachemt_dt:
                        continue
                    issue_key = item.get("key")
                    if issue_key:
                        attachemt_time_by_key[issue_key] = attachemt_dt
                    created_date = attachemt_dt.date()
                    if from_date and created_date < from_date:
                        continue
                    if to_date and created_date > to_date:
                        continue
                    created_attachment_date_counts[created_date] = created_attachment_date_counts.get(created_date, 0) + 1

                priority_time_items = my_jira.getPriorityHighFirstTimeWithSql(created_jql)
                priority_time_items_count = len(priority_time_items)
                log(f"priority_time_items_count:{priority_time_items_count}")
                priority_time_by_key: Dict[str, datetime] = {}
                for item in priority_time_items:
                    priority_dt = parse_datetime(item.get("priority_high_time"))
                    if not priority_dt:
                        continue
                    issue_key = item.get("key")
                    if issue_key:
                        priority_time_by_key[issue_key] = priority_dt

                log(f"created_attachment_date_counts:{created_attachment_date_counts}")
                log(f"created_label_date_counts:{created_label_date_counts}")

                delay_minutes_by_date: Dict[Any, List[float]] = {}
                for issue_key, attachemt_dt in attachemt_time_by_key.items():
                    label_dt = label_time_by_key.get(issue_key)
                    if not label_dt:
                        continue
                    attachemt_delay_minutes = (label_dt - attachemt_dt).total_seconds() / 60.0
                    if attachemt_delay_minutes < 0:
                        continue
                    created_date = attachemt_dt.date()
                    if from_date and created_date < from_date:
                        continue
                    if to_date and created_date > to_date:
                        continue
                    delay_minutes_by_date.setdefault(created_date, []).append(attachemt_delay_minutes)

                import pandas as pd
                import altair as alt
                metric_col1, metric_col2 = st.columns(2)
                metric_col1.metric("创建 Attachment 数量", sum(created_attachment_date_counts.values()))
                metric_col2.metric("创建 Label 数量", sum(created_label_date_counts.values()))

                date_index = sorted(set(created_label_date_counts) | set(created_attachment_date_counts))
                if not date_index:
                    st.info("暂无数据")
                else:
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
                    detail_rows = []
                    for issue_key in sorted(set(attachemt_time_by_key) | set(label_time_by_key) | set(priority_time_by_key)):
                        attachemt_dt = attachemt_time_by_key.get(issue_key)
                        label_dt = label_time_by_key.get(issue_key)
                        priority_dt = priority_time_by_key.get(issue_key)
                        attachemt_delay_minutes = None
                        priority_delay_minutes = None
                        if attachemt_dt and label_dt:
                            attachemt_delay_minutes = round((label_dt - attachemt_dt).total_seconds() / 60.0, 2)
                            if attachemt_delay_minutes < 0:
                                attachemt_delay_minutes = None
                        if priority_dt and label_dt:
                            priority_delay_minutes = round((label_dt - priority_dt).total_seconds() / 60.0, 2)
                            # if priority_delay_minutes < 0:
                            #     priority_delay_minutes = None
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
                    )
                    st.altair_chart(chart_counts, use_container_width=True)

                    if not label_detail_df.empty and "attachemt_delay_minutes" in label_detail_df.columns:
                        delay_df = label_detail_df.dropna(subset=["attachemt_delay_minutes", "priority_delay_minutes"], how="all")
                        if not delay_df.empty:
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
                                )
                                threshold_line = alt.Chart(
                                    pd.DataFrame({"y": [120]}),
                                ).mark_rule(color="#FF0000", strokeDash=[6, 4]).encode(
                                    y="y:Q"
                                )
                                st.caption("说明：附件耗时为 None 表示没有附件， 红色虚线代表120分钟。")
                                st.altair_chart(chart_delay + threshold_line, use_container_width=True)


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
            
    elif admin_tab == "日志记录":
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

# cd /home/amlogic/FAE/AutoLog/lingzhi.bi/log_analyze_feedback_mvp && source /home/amlogic/FAE/AutoLog/lingzhi.bi/log_analyze_feedback_mvp/310venv/bin/activate && nohup streamlit run streamlit_app.py --server.port 8053 --server.headless true &
# 
