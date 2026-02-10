const form = document.getElementById("feedback-form");
const result = document.getElementById("result");
const resetButton = document.getElementById("reset-button");
const attachmentsInput = document.getElementById("attachments");

const allowedExtensions = ["png", "jpg", "jpeg", "webp", "pdf", "docx", "txt", "zip", "rar"];

/**
 * 功能：展示结果信息。
 * 参数：message（提示文本）、isError（是否错误）。
 * 返回值：无。
 * 异常：无显式异常。
 */
function showResult(message, isError = false) {
  result.textContent = message;
  result.style.color = isError ? "#b91c1c" : "#0f172a";
}

/**
 * 功能：校验附件类型与大小。
 * 参数：files（文件列表）。
 * 返回值：错误信息或空字符串。
 * 异常：无显式异常。
 */
function validateFiles(files) {
  if (files.length > 3) {
    return "最多允许上传 3 个附件";
  }
  for (const file of files) {
    const nameParts = file.name.split(".");
    const extension = nameParts.length > 1 ? nameParts.pop().toLowerCase() : "";
    if (!allowedExtensions.includes(extension)) {
      return "附件类型不被允许";
    }
    if (file.size > 20 * 1024 * 1024) {
      return "单个附件大小不能超过 20MB";
    }
  }
  return "";
}

/**
 * 功能：带超时的网络请求。
 * 参数：url（请求地址）、options（fetch 参数）、timeoutMs（超时毫秒）。
 * 返回值：Promise<Response>。
 * 异常：Error 超时或网络异常。
 */
function fetchWithTimeout(url, options, timeoutMs) {
  /**
   * 功能：Promise 执行器，封装超时逻辑。
   * 参数：resolve（成功回调）、reject（失败回调）。
   * 返回值：无。
   * 异常：Error 超时或网络异常。
   */
  function executor(resolve, reject) {
    /**
     * 功能：处理超时逻辑。
     * 参数：无。
     * 返回值：无。
     * 异常：Error 超时异常。
     */
    function handleTimeout() {
      reject(new Error("timeout"));
    }

    const timer = setTimeout(handleTimeout, timeoutMs);

    /**
     * 功能：处理请求成功。
     * 参数：response（响应对象）。
     * 返回值：无。
     * 异常：无显式异常。
     */
    function handleResolve(response) {
      clearTimeout(timer);
      resolve(response);
    }

    /**
     * 功能：处理请求失败。
     * 参数：error（错误对象）。
     * 返回值：无。
     * 异常：Error 网络异常。
     */
    function handleReject(error) {
      clearTimeout(timer);
      reject(error);
    }

    fetch(url, options).then(handleResolve).catch(handleReject);
  }

  return new Promise(executor);
}

/**
 * 功能：创建包含状态码的错误对象。
 * 参数：message（错误信息）、status（HTTP 状态码）。
 * 返回值：Error 对象。
 * 异常：无显式异常。
 */
function buildError(message, status) {
  const error = new Error(message);
  error.status = status;
  return error;
}

/**
 * 功能：提交反馈并进行失败重试。
 * 参数：formData（表单数据）、attempt（当前重试次数）。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function submitWithRetry(formData, attempt = 0) {
  try {
    const response = await fetchWithTimeout("/api/feedbacks", {
      method: "POST",
      body: formData,
    }, 5000);
    if (!response.ok) {
      const payload = await response.json();
      throw buildError(payload.detail || "提交失败", response.status);
    }
    const data = await response.json();
    showResult(`已收到，反馈编号：${data.id}`);
    form.reset();
    return;
  } catch (error) {
    if (error.message === "timeout" && attempt < 2) {
      await submitWithRetry(formData, attempt + 1);
      return;
    }
    showResult(error.message || "提交失败", true);
    throw error;
  }
}

/**
 * 功能：设置表单可用状态。
 * 参数：enabled（是否可用）。
 * 返回值：无。
 * 异常：无显式异常。
 */
function setFormEnabled(enabled) {
  for (const element of form.elements) {
    element.disabled = !enabled;
  }
  resetButton.disabled = !enabled;
}

/**
 * 功能：处理表单提交事件。
 * 参数：event（提交事件）。
 * 返回值：Promise<void>。
 * 异常：Error 请求失败。
 */
async function handleSubmit(event) {
  event.preventDefault();
  const files = attachmentsInput.files;
  const errorMessage = validateFiles(files);
  if (errorMessage) {
    showResult(errorMessage, true);
    return;
  }
  const formData = new FormData(form);
  setFormEnabled(false);
  showResult("提交中...");
  try {
    await submitWithRetry(formData, 0);
  } catch (error) {
    if (error.status === 429) {
      showResult("同一 IP 10 分钟内最多提交 3 次", true);
    }
  } finally {
    setFormEnabled(true);
  }
}

/**
 * 功能：处理表单重置事件。
 * 参数：无。
 * 返回值：无。
 * 异常：无显式异常。
 */
function handleReset() {
  form.reset();
  showResult("已重新编辑表单，可再次提交");
}

form.addEventListener("submit", handleSubmit);
resetButton.addEventListener("click", handleReset);
