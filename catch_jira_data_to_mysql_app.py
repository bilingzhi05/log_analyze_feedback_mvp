import time
import os
import json
from datetime import datetime
from typing import Dict, Any, List, Optional
from app.config import load_config
from app.jira_client import MyJira
from app.mysql_client import MySQLClient
from app.utils import parse_datetime
from app.logger import log


def ensure_table(client: MySQLClient, database: str, table: str) -> None:
    client.create_database(database)
    client.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{database}`.`{table}` (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            `key` VARCHAR(64) NOT NULL,
            attachment_time DATETIME NULL,
            label_time DATETIME NULL,
            priority_time DATETIME NULL,
            attachemt_delay_minutes DOUBLE NULL,
            priority_delay_minutes DOUBLE NULL,
            extra JSON NULL,
            UNIQUE KEY uniq_key (`key`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        commit=True,
    )


def upsert_row(client: MySQLClient, database: str, table: str, row: Dict[str, Any]) -> None:
    client.execute(
        f"""
        INSERT INTO `{database}`.`{table}`
        (`key`, attachment_time, label_time, priority_time, attachemt_delay_minutes, priority_delay_minutes, extra)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            attachment_time = VALUES(attachment_time),
            label_time = VALUES(label_time),
            priority_time = VALUES(priority_time),
            attachemt_delay_minutes = VALUES(attachemt_delay_minutes),
            priority_delay_minutes = VALUES(priority_delay_minutes),
            extra = VALUES(extra)
        """,
        params=(
            row.get("key"),
            row.get("attachment_time"),
            row.get("label_time"),
            row.get("priority_time"),
            row.get("attachemt_delay_minutes"),
            row.get("priority_delay_minutes"),
            row.get("extra"),
        ),
        commit=True,
    )


def fetch_existing_rows(
    client: MySQLClient,
    database: str,
    table: str,
    keys: List[str],
    chunk_size: int = 500,
) -> Dict[str, Dict[str, Any]]:
    if not keys:
        return {}
    existing: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i : i + chunk_size]
        placeholders = ",".join(["%s"] * len(chunk))
        rows = client.fetchall(
            f"""
            SELECT `key`, attachment_time, label_time, priority_time,
                   attachemt_delay_minutes, priority_delay_minutes, extra
            FROM `{database}`.`{table}`
            WHERE `key` IN ({placeholders})
            """,
            params=tuple(chunk),
        )
        for row in rows:
            existing[row[0]] = {
                "attachment_time": row[1],
                "label_time": row[2],
                "priority_time": row[3],
                "attachemt_delay_minutes": row[4],
                "priority_delay_minutes": row[5],
                "extra": row[6],
            }
    return existing


def build_rows(my_jira: MyJira, created_jql: str, label_name: str) -> List[Dict[str, Any]]:
    add_labels_time_items = my_jira.getLabelAppliedTimeWithSql(created_jql, label_name)
    label_time_by_key: Dict[str, datetime] = {}
    for item in add_labels_time_items:
        label_dt = parse_datetime(item.get("label_applied_time"))
        if not label_dt:
            continue
        issue_key = item.get("key")
        if issue_key:
            label_time_by_key[issue_key] = label_dt

    add_attachemt_time_items = my_jira.getEarliestAttachmentTimeWithSql(created_jql)
    attachemt_time_by_key: Dict[str, datetime] = {}
    for item in add_attachemt_time_items:
        attachemt_dt = parse_datetime(item.get("attachment_time"))
        if not attachemt_dt:
            continue
        issue_key = item.get("key")
        if issue_key:
            attachemt_time_by_key[issue_key] = attachemt_dt

    priority_time_items = my_jira.getPriorityHighFirstTimeWithSql(created_jql)
    priority_time_by_key: Dict[str, datetime] = {}
    for item in priority_time_items:
        priority_dt = parse_datetime(item.get("priority_high_time"))
        if not priority_dt:
            continue
        issue_key = item.get("key")
        if issue_key:
            priority_time_by_key[issue_key] = priority_dt

    rows: List[Dict[str, Any]] = []
    extra_json = json.dumps({}, ensure_ascii=False)
    for issue_key in sorted(set(attachemt_time_by_key) | set(label_time_by_key) | set(priority_time_by_key)):
        attachemt_dt = attachemt_time_by_key.get(issue_key)
        label_dt = label_time_by_key.get(issue_key)
        priority_dt = priority_time_by_key.get(issue_key)
        attachemt_delay_minutes: Optional[float] = None
        priority_delay_minutes: Optional[float] = None
        if attachemt_dt and label_dt:
            attachemt_delay_minutes = round((label_dt - attachemt_dt).total_seconds() / 60.0, 2)
            if attachemt_delay_minutes < 0:
                attachemt_delay_minutes = None
        if priority_dt and label_dt:
            priority_delay_minutes = round((label_dt - priority_dt).total_seconds() / 60.0, 2)
        rows.append(
            {
                "key": issue_key,
                "attachment_time": attachemt_dt,
                "label_time": label_dt,
                "priority_time": priority_dt,
                "attachemt_delay_minutes": attachemt_delay_minutes,
                "priority_delay_minutes": priority_delay_minutes,
                "extra": extra_json,
            }
        )
    return rows


def merge_missing_fields(new_row: Dict[str, Any], existing_row: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(new_row)
    for field in [
        "attachment_time",
        "label_time",
        "priority_time",
        "attachemt_delay_minutes",
        "priority_delay_minutes",
        "extra",
    ]:
        if existing_row.get(field) is not None and existing_row.get(field) != "":
            merged[field] = existing_row.get(field)
    return merged


def has_missing_fields(existing_row: Dict[str, Any]) -> bool:
    for field in [
        "attachment_time",
        "label_time",
        "priority_time",
        "attachemt_delay_minutes",
        "priority_delay_minutes",
        "extra",
    ]:
        if existing_row.get(field) is None or existing_row.get(field) == "":
            return True
    return False





def build_rows_for_keys(my_jira: MyJira, keys: List[str], label_name: str, chunk_size: int = 500) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not keys:
        return rows
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i : i + chunk_size]
        key_in = ",".join(chunk)
        jql = f"key in ({key_in})"
        rows.extend(build_rows(my_jira, jql, label_name))
    return rows


def run_once() -> None:
    config = load_config()
    my_jira = MyJira(config.jira_server, config.jira_username, config.jira_password)
    client = MySQLClient(
        host=config.mysql_host,
        port=config.mysql_port,
        user=config.mysql_user,
        password=config.mysql_password,
        database=config.mysql_database,
        table=config.mysql_table,
    )
    ensure_table(client, config.mysql_database, config.mysql_table)

    label_name = os.getenv("JIRA_LABEL_NAME", "SE-LN-LOG-2026")
    from_date = os.getenv("JIRA_CREATED_FROM_DATE", "2026-02-01")
    created_jql = os.getenv(
        "JIRA_CREATED_JQL",
        '(project in ("OTT projects") AND priority in (High, Highest) AND type in (Bug, Sub-bug) AND (status not in (Closed, Done, Resolved, Verified) OR labels = SE-LN-LOG-2026))',
    )
    created_jql += f' AND created >= "{from_date}"'

    log("开始抓取 Jira 数据并写入 MySQL")
    issue_keys = my_jira.get_issue_keys(created_jql)
    log(f"Jira 匹配到的 key 数量：{len(issue_keys)}")
    existing_by_key = fetch_existing_rows(
        client,
        config.mysql_database,
        config.mysql_table,
        issue_keys,
    )
    missing_keys = []
    for key in issue_keys:
        existing_row = existing_by_key.get(key)
        if not existing_row or has_missing_fields(existing_row):
            missing_keys.append(key)
    log(f"数据库中需要补全的 key 数量：{len(missing_keys)}")
    rows = build_rows_for_keys(my_jira, missing_keys, label_name)
    log(f"准备写入记录数：{len(rows)}")
    for row in rows:
        try:
            existing_row = existing_by_key.get(row.get("key"))
            if existing_row:
                row = merge_missing_fields(row, existing_row)
            upsert_row(client, config.mysql_database, config.mysql_table, row)
        except Exception as exc:
            log(f"写入失败 key={row.get('key')}: {exc}")
    log("写入完成")


def main() -> None:
    run_once()
    # interval_seconds = int(os.getenv("JIRA_SYNC_INTERVAL_SECONDS", "1800"))
    # while True:
    #     try:
    #         run_once()
    #     except Exception as exc:
    #         log(f"抓取任务异常: {exc}")
    #     time.sleep(max(600, interval_seconds))


if __name__ == "__main__":
    main()
