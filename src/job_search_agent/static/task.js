// 只轮询状态，不执行任务；暂停时停止更新，保留用户正在输入的内容。
const taskPanel = document.querySelector("[data-task-poll]");
if (taskPanel) {
  const poll = async () => {
    try {
      const response = await fetch("/tasks/" + taskPanel.dataset.taskPoll + "/status", {cache: "no-store"});
      if (!response.ok) throw new Error("status unavailable");
      const state = await response.json();
      if (!["queued", "processing"].includes(state.status) || state.error_code) {
        window.location.reload();
        return;
      }
      const modelCount = taskPanel.querySelector("[data-model-count]");
      const toolCount = taskPanel.querySelector("[data-tool-count]");
      if (modelCount) modelCount.textContent = modelCount.textContent.replace(/模型请求 \d+/, "模型请求 " + state.model_calls);
      if (toolCount) toolCount.textContent = toolCount.textContent.replace(/工具尝试 \d+/, "工具尝试 " + state.tool_calls);
      setTimeout(poll, 2500);
    } catch {
      const message = taskPanel.querySelector("[aria-live]");
      if (message) message.textContent = "暂时连接不到本地服务。任务已保留；重启服务后刷新页面继续。";
    }
  };
  setTimeout(poll, 1500);
}
