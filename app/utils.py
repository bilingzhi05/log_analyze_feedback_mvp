import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fastapi import UploadFile


ALLOWED_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "webp",
    "pdf",
    "docx",
    "txt",
    "zip",
    "rar",
}


def now_iso() -> str:
    """
    功能：生成当前 UTC 时间的 ISO 字符串。
    参数：无。
    返回值：ISO 时间字符串。
    异常：无显式异常。
    """
    return datetime.now().isoformat()


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """
    功能：解析 ISO 或日期字符串为 datetime。
    参数：value（时间字符串）。
    返回值：datetime 或 None。
    异常：ValueError 解析失败。
    """
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    if re.search(r"[+-]\d{4}$", normalized):
        normalized = f"{normalized[:-5]}{normalized[-5:-2]}:{normalized[-2:]}"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.fromisoformat(f"{normalized}T00:00:00+00:00")


def validate_sentiment(sentiment: str) -> None:
    """
    功能：校验情感字段是否合法。
    参数：sentiment（情感字段）。
    返回值：无。
    异常：ValueError 校验失败。
    """
    if sentiment not in {"like", "dislike"}:
        raise ValueError("情感字段必须为 like 或 dislike")


def validate_content(content: str) -> None:
    """
    功能：校验建议内容长度。
    参数：content（建议文本）。
    返回值：无。
    异常：ValueError 校验失败。
    """
    length = len(content.strip())
    if length < 1 or length > 2000:
        raise ValueError("建议内容长度需在 1 到 2000 字之间")


def sanitize_filename(filename: str) -> str:
    """
    功能：清理文件名以避免非法字符。
    参数：filename（原始文件名）。
    返回值：安全文件名。
    异常：无显式异常。
    """
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", filename).strip("._")
    return cleaned or f"file_{uuid.uuid4().hex}"


def get_extension(filename: str) -> str:
    """
    功能：获取文件扩展名并转小写。
    参数：filename（文件名）。
    返回值：扩展名字符串。
    异常：无显式异常。
    """
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def ensure_directory(path: str) -> None:
    """
    功能：确保目录存在。
    参数：path（目录路径）。
    返回值：无。
    异常：OSError 目录创建失败。
    """
    os.makedirs(path, exist_ok=True)


def build_attachment_dir(root: str, feedback_id: int) -> str:
    """
    功能：生成附件保存目录路径。
    参数：root（根目录）、feedback_id（反馈 ID）。
    返回值：附件目录路径。
    异常：无显式异常。
    """
    now = datetime.now()
    return os.path.join(
        root,
        now.strftime("%Y"),
        now.strftime("%m"),
        now.strftime("%d"),
        str(feedback_id),
    )


def validate_files(files: List[UploadFile]) -> None:
    """
    功能：校验上传文件数量与类型。
    参数：files（上传文件列表）。
    返回值：无。
    异常：ValueError 校验失败。
    """
    if len(files) > 3:
        raise ValueError("最多允许上传 3 个附件")
    for file in files:
        extension = get_extension(file.filename or "")
        if extension not in ALLOWED_EXTENSIONS:
            raise ValueError("附件类型不被允许")


def save_upload_file(
    upload: UploadFile,
    target_path: str,
    max_size_bytes: int,
) -> Dict[str, Any]:
    """
    功能：保存上传文件并校验大小。
    参数：upload（上传文件对象）、target_path（保存路径）、max_size_bytes（最大字节数）。
    返回值：包含文件信息的字典。
    异常：ValueError 超过大小限制或写入失败。
    """
    total_size = 0
    with open(target_path, "wb") as target_file:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > max_size_bytes:
                raise ValueError("单个附件大小不能超过 20MB")
            target_file.write(chunk)
    return {
        "name": upload.filename,
        "type": upload.content_type or "application/octet-stream",
        "size": total_size,
        "path": target_path,
    }


def json_dumps(data: Any) -> str:
    """
    功能：序列化数据为 JSON 字符串。
    参数：data（任意可序列化对象）。
    返回值：JSON 字符串。
    异常：TypeError 序列化失败。
    """
    return json.dumps(data, ensure_ascii=False)


def json_loads(data: str) -> Any:
    """
    功能：反序列化 JSON 字符串。
    参数：data（JSON 字符串）。
    返回值：反序列化对象。
    异常：json.JSONDecodeError 解析失败。
    """
    return json.loads(data)


def format_recent_days(days: int = 7) -> List[str]:
    """
    功能：生成近 N 天日期字符串列表。
    参数：days（天数）。
    返回值：日期字符串列表。
    异常：无显式异常。
    """
    today = datetime.now().date()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
