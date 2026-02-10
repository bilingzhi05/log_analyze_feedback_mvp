建议收集系统（MVP）功能规格

要求函数都要有中文注释，中文注释中要包含函数的功能、参数、返回值、异常等信息。
在/home/bj17300-049u/work/log_analyze_feedback_mvp 中创建文件和文件夹。
UI 使用streamlit实现功能

前台 UI（2.10）
- 表单字段
  - 喜欢/不喜欢：必填，单选，取值 like/dislike
  - 建议内容：必填，文本，1–2000 字
  - 附件上传：可选，支持图片（png/jpg/jpeg/webp）、文件（pdf/docx/txt）、压缩（zip/rar），单个文件≤20MB，最多3个附件
  - 用户 IP：系统自动采集，前端不可编辑
- 交互与校验
  - 必填校验与类型/大小校验；失败时展示明确错误提示
  - 提交成功后展示“已收到”确认与反馈编号
  - 保留一次重新编辑后重试能力；超时重试不超过3次
- 可用性与合规
  - 在提交入口旁展示隐私说明（采集 IP、附件用途）
  - 防刷：同一 IP 10 分钟内最多提交 3 次

后台 UI（2.13）
- 管理员登录
  - 账号/密码登录；3 次失败后验证码；会话 24h 过期
- 建议信息列表与统计
  - 统计：提交总数、like/dislike 数量、近7天趋势
  - 列表字段：反馈ID、时间、IP、情感、建议摘要（前100字）、状态、Jira Key
  - 筛选：时间范围（今天/日/周/月/年/自定义）、情感、状态、关键词
  - 分页：默认每页 50 条，可选 20/50/100；导出 CSV/JSON
- 人工评审与 Jira
  - 状态流转：待评估 → 已采纳/已拒绝/待跟进
  - 一键创建 Jira：必填项目键、问题类型、优先级；自动生成 Summary 与 Description（含用户建议与来源信息）
  - 展示 Jira 状态与链接；支持同步更新为“进行中/已完成”
  - 操作审计：记录评审、Jira 创建与状态变更时间与操作者
- 日志工具数据展示
  - 展示后台运行数据（任务执行、分析记录、错误）：时间、类型、摘要、关联反馈ID（可空）
  - 支持筛选与导出；用于与建议评审联动查看

API 与数据（2.28）
- 数据库（sqlite）
  - 表 feedbacks：
    - id INTEGER PK、created_at DATETIME、updated_at DATETIME
    - sentiment TEXT（like/dislike）
    - content TEXT、user_ip TEXT
    - attachments JSON（[{name, type, size, path}]）
    - status TEXT（pending/accepted/rejected/followup）
    - jira_key TEXT（可空）
  - 表 logs：
    - id INTEGER PK、created_at DATETIME
    - category TEXT（job/run/error/analysis）
    - message TEXT、metadata JSON、related_feedback_id INTEGER（可空）
- 接口定义
  - POST /api/feedbacks
    - 请求：{sentiment, content, attachments?}
    - 响应：{id}
  - GET /api/feedbacks
    - 查询参数：page, page_size, from, to, sentiment, status, q
    - 响应：{items:[], total, page, page_size}
  - GET /api/feedbacks/{id}
    - 响应：反馈详情（含附件与状态）
  - PUT /api/feedbacks/{id}/review
    - 请求：{status, note?}
    - 响应：更新后的反馈
  - POST /api/feedbacks/{id}/jira
    - 请求：{project_key, issue_type, priority}
    - 响应：{jira_key, url}
  - GET /api/jira/{jira_key}
    - 响应：{status, url}
  - GET /api/stats/feedbacks
    - 响应：{total, like, dislike, recent_7d:[{date,count}]}
  - GET /api/logs
    - 查询参数：page, page_size, from, to, category, q
    - 响应：{items:[], total}
  - GET /api/feedbacks/export
    - 查询参数：from, to, format=csv|json
- 约束与索引
  - 索引：feedbacks(created_at), feedbacks(status), feedbacks(sentiment)
  - 附件落盘路径结构：/data/attachments/{yyyy}/{MM}/{dd}/{id}/

联调与运维
- 联调范围：前台提交 → 后台查看/评审 → Jira 创建与状态同步 → 导出
- 测试用例：正常提交、上传校验失败、防刷限流、评审流程、Jira 创建失败回滚、导出大数据量分页
- 部署与配置：
  - 需要配置 Jira：base_url、project_key、auth_token（安全保管）
  - 导出限流：每次导出最多 5 万条，超过需分批
  - 观测：关键接口埋点与错误日志归档

非功能需求
- 安全：输入校验、SQL 参数化、XSS/CSRF 防护、附件病毒扫描（占位）
- 性能：列表分页默认 50，接口超时 5s，失败重试上限 3
- 合规：展示隐私说明并允许用户请求删除其反馈（后台入口）
