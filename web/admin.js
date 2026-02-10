const loginSection = document.getElementById("login-section");
const adminSection = document.getElementById("admin-section");
const loginButton = document.getElementById("login-button");
const loginMessage = document.getElementById("login-message");
const captchaSection = document.getElementById("captcha-section");
const captchaText = document.getElementById("captcha-text");
const logoutButton = document.getElementById("logout-button");

const feedbackList = document.getElementById("feedback-list");
const logList = document.getElementById("log-list");

let captchaId = "";
const jiraStatusMap = new Map();

/**
 * 功能：展示登录提示信息。
 * 参数：message（提示文本）、isError（是否错误）。
 * 返回值：无。
 * 异常：无显式异常。
 */
function showLoginMessage(message, isError = false) {
  loginMessage.textContent = message;
  loginMessage.style.color = isError ? "#b91c1c" : "#0f172a";
}

/**
 * 功能：切换登录与后台界面。
 * 参数：loggedIn（是否已登录）。
 * 返回值：无。
 * 异常：无显式异常。
 */
function toggleSections(loggedIn) {
  loginSection.classList.toggle("hidden", loggedIn);
  adminSection.classList.toggle("hidden", !loggedIn);
}

/**
 * 功能：发起 JSON 请求。
 * 参数：url（地址）、options（请求参数）。
 * 返回值：Promise<Response>。
 * 异常：Error 网络异常。
 */
async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  return response;
}

/**
 * 功能：查询当前登录状态。
 * 参数：无。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function checkLogin() {
  const response = await fetchJson("/api/admin/me");
  if (response.ok) {
    toggleSections(true);
    await loadDashboard();
  }
}

/**
 * 功能：登录管理员账户。
 * 参数：无。
 * 返回值：Promise<void>。
 * 异常：Error 登录失败。
 */
async function loginAdmin() {
  const username = document.getElementById("login-username").value.trim();
  const password = document.getElementById("login-password").value.trim();
  const captchaCode = document.getElementById("login-captcha").value.trim();
  const payload = { username, password };
  if (captchaSection.classList.contains("hidden") === false) {
    payload.captcha_id = captchaId;
    payload.captcha_code = captchaCode;
  }
  const response = await fetchJson("/api/admin/login", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const error = await response.json();
    const detail = error.detail || "登录失败";
    if (detail.need_captcha) {
      captchaSection.classList.remove("hidden");
      captchaText.textContent = detail.captcha_text;
      captchaId = detail.captcha_id;
      showLoginMessage("请输入验证码后重试", true);
      return;
    }
    showLoginMessage(typeof detail === "string" ? detail : "登录失败", true);
    return;
  }
  showLoginMessage("登录成功");
  captchaSection.classList.add("hidden");
  captchaText.textContent = "";
  captchaId = "";
  toggleSections(true);
  await loadDashboard();
}

/**
 * 功能：退出登录。
 * 参数：无。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function logoutAdmin() {
  await fetchJson("/api/admin/logout", { method: "POST" });
  toggleSections(false);
}

/**
 * 功能：加载统计信息。
 * 参数：无。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function loadStats() {
  const response = await fetchJson("/api/stats/feedbacks");
  if (!response.ok) {
    return;
  }
  const data = await response.json();
  const statsContainer = document.getElementById("stats");
  const cards = [
    { label: "提交总数", value: data.total },
    { label: "喜欢", value: data.like },
    { label: "不喜欢", value: data.dislike },
  ];
  statsContainer.innerHTML = cards.map(renderStatsCard).join("");
}

/**
 * 功能：渲染统计卡片。
 * 参数：card（统计项）。
 * 返回值：HTML 字符串。
 * 异常：无显式异常。
 */
function renderStatsCard(card) {
  return `<div class="card"><div>${card.label}</div><strong>${card.value}</strong></div>`;
}

/**
 * 功能：构建反馈筛选参数。
 * 参数：无。
 * 返回值：URLSearchParams 实例。
 * 异常：无显式异常。
 */
function buildFeedbackQuery() {
  const params = new URLSearchParams();
  const from = document.getElementById("filter-from").value;
  const to = document.getElementById("filter-to").value;
  const sentiment = document.getElementById("filter-sentiment").value;
  const status = document.getElementById("filter-status").value;
  const keyword = document.getElementById("filter-keyword").value;
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  if (sentiment) params.set("sentiment", sentiment);
  if (status) params.set("status", status);
  if (keyword) params.set("q", keyword);
  params.set("page", "1");
  params.set("page_size", "50");
  return params;
}

/**
 * 功能：渲染反馈列表表格。
 * 参数：items（反馈列表）。
 * 返回值：无。
 * 异常：无显式异常。
 */
function renderFeedbackTable(items) {
  if (!items.length) {
    feedbackList.innerHTML = "<div class='result'>暂无数据</div>";
    return;
  }
  const rows = items.map(renderFeedbackRow).join("");
  feedbackList.innerHTML = `
    <table class="table">
      <thead>
        <tr>
          <th>ID</th>
          <th>时间</th>
          <th>IP</th>
          <th>情感</th>
          <th>摘要</th>
          <th>状态</th>
          <th>Jira</th>
          <th>Jira 状态</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

/**
 * 功能：渲染单条反馈行。
 * 参数：item（反馈数据）。
 * 返回值：HTML 字符串。
 * 异常：无显式异常。
 */
function renderFeedbackRow(item) {
  const jiraStatus = item.jira_key ? (jiraStatusMap.get(item.jira_key) || "未同步") : "-";
  const jiraLink = item.jira_key
    ? `<a href="/api/jira/${item.jira_key}" target="_blank">${item.jira_key}</a>`
    : "-";
  const syncButton = item.jira_key
    ? `<button data-action="jira-status" data-id="${item.id}" data-key="${item.jira_key}">同步状态</button>`
    : "";
  return `
    <tr>
      <td>${item.id}</td>
      <td>${item.created_at}</td>
      <td>${item.user_ip}</td>
      <td>${item.sentiment}</td>
      <td>${item.summary}</td>
      <td>${item.status}</td>
      <td>${jiraLink}</td>
      <td>${jiraStatus}</td>
      <td>
        <button data-action="accept" data-id="${item.id}">采纳</button>
        <button data-action="reject" data-id="${item.id}">拒绝</button>
        <button data-action="followup" data-id="${item.id}">跟进</button>
        <button data-action="jira" data-id="${item.id}">创建 Jira</button>
        ${syncButton}
        <button data-action="delete" data-id="${item.id}">删除</button>
      </td>
    </tr>
  `;
}

/**
 * 功能：加载反馈列表。
 * 参数：无。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function loadFeedbacks() {
  const params = buildFeedbackQuery();
  const response = await fetchJson(`/api/feedbacks?${params.toString()}`);
  if (!response.ok) {
    feedbackList.innerHTML = "<div class='result'>加载失败</div>";
    return;
  }
  const data = await response.json();
  renderFeedbackTable(data.items || []);
}

/**
 * 功能：更新反馈评审状态。
 * 参数：id（反馈 ID）、status（目标状态）。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function updateReviewStatus(id, status) {
  await fetchJson(`/api/feedbacks/${id}/review`, {
    method: "PUT",
    body: JSON.stringify({ status }),
  });
  await loadFeedbacks();
}

/**
 * 功能：创建 Jira 问题。
 * 参数：id（反馈 ID）。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function createJira(id) {
  const projectKey = prompt("项目键", "");
  const issueType = prompt("问题类型", "Task");
  const priority = prompt("优先级", "Medium");
  const response = await fetchJson(`/api/feedbacks/${id}/jira`, {
    method: "POST",
    body: JSON.stringify({
      project_key: projectKey,
      issue_type: issueType,
      priority: priority,
    }),
  });
  const payload = await response.json();
  if (!response.ok) {
    alert(payload.detail || "创建失败");
    return;
  }
  alert(`已创建 Jira：${payload.jira_key}`);
  await loadFeedbacks();
}

/**
 * 功能：同步 Jira 状态并更新展示。
 * 参数：jiraKey（Jira Key）。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function syncJiraStatus(jiraKey) {
  const response = await fetchJson(`/api/jira/${jiraKey}`);
  const payload = await response.json();
  if (!response.ok) {
    alert(payload.detail || "同步失败");
    return;
  }
  jiraStatusMap.set(jiraKey, payload.status || "未知");
  await loadFeedbacks();
}

/**
 * 功能：删除反馈。
 * 参数：id（反馈 ID）。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function deleteFeedback(id) {
  if (!confirm("确认删除该反馈？")) {
    return;
  }
  await fetchJson(`/api/feedbacks/${id}`, { method: "DELETE" });
  await loadFeedbacks();
}

/**
 * 功能：加载日志列表。
 * 参数：无。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function loadLogs() {
  const params = buildLogQuery();
  const response = await fetchJson(`/api/logs?${params.toString()}`);
  if (!response.ok) {
    logList.innerHTML = "<div class='result'>加载失败</div>";
    return;
  }
  const data = await response.json();
  if (!data.items.length) {
    logList.innerHTML = "<div class='result'>暂无数据</div>";
    return;
  }
  const rows = data.items.map(renderLogRow).join("");
  logList.innerHTML = `
    <table class="table">
      <thead>
        <tr>
          <th>时间</th>
          <th>类型</th>
          <th>摘要</th>
          <th>关联反馈</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

/**
 * 功能：构建日志筛选参数。
 * 参数：无。
 * 返回值：URLSearchParams 实例。
 * 异常：无显式异常。
 */
function buildLogQuery() {
  const params = new URLSearchParams();
  const from = document.getElementById("log-from").value;
  const to = document.getElementById("log-to").value;
  const category = document.getElementById("log-category").value;
  const keyword = document.getElementById("log-keyword").value;
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  if (category) params.set("category", category);
  if (keyword) params.set("q", keyword);
  params.set("page", "1");
  params.set("page_size", "50");
  return params;
}

/**
 * 功能：渲染单条日志行。
 * 参数：item（日志数据）。
 * 返回值：HTML 字符串。
 * 异常：无显式异常。
 */
function renderLogRow(item) {
  return `
    <tr>
      <td>${item.created_at}</td>
      <td>${item.category}</td>
      <td>${item.message}</td>
      <td>${item.related_feedback_id || "-"}</td>
    </tr>
  `;
}

/**
 * 功能：加载仪表盘数据。
 * 参数：无。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function loadDashboard() {
  await loadStats();
  await loadFeedbacks();
  await loadLogs();
}

/**
 * 功能：处理反馈筛选按钮点击。
 * 参数：无。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function handleFilterClick() {
  await loadFeedbacks();
}

/**
 * 功能：处理日志筛选按钮点击。
 * 参数：无。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function handleLogFilterClick() {
  await loadLogs();
}

/**
 * 功能：处理导出反馈 CSV。
 * 参数：无。
 * 返回值：无。
 * 异常：无显式异常。
 */
function handleExportCsv() {
  const params = buildFeedbackQuery();
  params.set("format", "csv");
  window.open(`/api/feedbacks/export?${params.toString()}`, "_blank");
}

/**
 * 功能：处理导出反馈 JSON。
 * 参数：无。
 * 返回值：无。
 * 异常：无显式异常。
 */
function handleExportJson() {
  const params = buildFeedbackQuery();
  params.set("format", "json");
  window.open(`/api/feedbacks/export?${params.toString()}`, "_blank");
}

/**
 * 功能：处理导出日志 CSV。
 * 参数：无。
 * 返回值：无。
 * 异常：无显式异常。
 */
function handleLogExportCsv() {
  const params = buildLogQuery();
  params.set("format", "csv");
  window.open(`/api/logs?${params.toString()}`, "_blank");
}

/**
 * 功能：处理导出日志 JSON。
 * 参数：无。
 * 返回值：无。
 * 异常：无显式异常。
 */
function handleLogExportJson() {
  const params = buildLogQuery();
  params.set("format", "json");
  window.open(`/api/logs?${params.toString()}`, "_blank");
}

/**
 * 功能：处理反馈列表操作。
 * 参数：event（点击事件）。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function handleFeedbackAction(event) {
  const action = event.target.getAttribute("data-action");
  const id = event.target.getAttribute("data-id");
  const jiraKey = event.target.getAttribute("data-key");
  if (!action || !id) {
    return;
  }
  if (action === "accept") {
    await updateReviewStatus(id, "accepted");
  }
  if (action === "reject") {
    await updateReviewStatus(id, "rejected");
  }
  if (action === "followup") {
    await updateReviewStatus(id, "followup");
  }
  if (action === "jira") {
    await createJira(id);
  }
  if (action === "jira-status" && jiraKey) {
    await syncJiraStatus(jiraKey);
  }
  if (action === "delete") {
    await deleteFeedback(id);
  }
}

loginButton.addEventListener("click", loginAdmin);
logoutButton.addEventListener("click", logoutAdmin);
document.getElementById("filter-button").addEventListener("click", handleFilterClick);
document.getElementById("log-filter-button").addEventListener("click", handleLogFilterClick);
document.getElementById("export-csv").addEventListener("click", handleExportCsv);
document.getElementById("export-json").addEventListener("click", handleExportJson);
document.getElementById("log-export-csv").addEventListener("click", handleLogExportCsv);
document.getElementById("log-export-json").addEventListener("click", handleLogExportJson);
feedbackList.addEventListener("click", handleFeedbackAction);

checkLogin();
