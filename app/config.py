import os
from dataclasses import dataclass


@dataclass
class AppConfig:
    database_path: str
    attachments_root: str
    admin_username: str
    admin_password: str
    jira_server: str
    jira_username: str
    jira_password: str


def load_config() -> AppConfig:
    """
    功能：加载应用配置并提供默认值。
    参数：无。
    返回值：AppConfig 配置对象。
    异常：可能抛出因环境变量异常导致的 ValueError。
    """
    return AppConfig(
        database_path=os.getenv(
            "APP_DATABASE_PATH",
            "data/app2.db",
        ),
        attachments_root=os.getenv(
            "APP_ATTACHMENTS_ROOT",
            "data/attachments",
        ),
        admin_username=os.getenv("APP_ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("APP_ADMIN_PASSWORD", "admin123"),
        jira_server=os.getenv("JIRA_BASE_URL", "https://jira.amlogic.com").rstrip("/"),
        jira_username=os.getenv("JIRA_USERNAME", "lingzhi.bi"),
        jira_password=os.getenv("JIRA_PASSWORD", "Qwer!23456"),
        # my_jira = MyJira("https://jira.amlogic.com", "lingzhi.bi", "Qwer!23456")
    )
