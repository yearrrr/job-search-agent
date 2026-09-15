(() => {
  "use strict";
  const id = () => crypto.randomUUID();
  const host = document.querySelector("[data-run-id]");
  let polling = false;
  async function submit(form) {
    const data = new FormData(form);
    const controls = [...form.elements].map(el => [el, el.disabled]);
    controls.forEach(([el]) => el.disabled = true);
    try {
      const response = await fetch(form.action, {method: "POST", body: data, headers: {Accept: "application/json"}});
      let result;
      try { result = await response.json(); } catch { throw new Error("服务未返回有效结果，请保留输入后重试。"); }
      if (!response.ok) throw new Error(result.message || "保存失败，请保留输入后重试。");
      return result;
    } finally { controls.forEach(([el, disabled]) => el.disabled = disabled); }
  }
  document.addEventListener("input", event => {
    const form = event.target.closest("form[data-inline]");
    if (form && form.elements.operation_id) form.elements.operation_id.value = id();
  });
  document.addEventListener("submit", async event => {
    const form = event.target.closest("form[data-inline]");
    if (!form) return;
    event.preventDefault();
    if (form.dataset.busy) return;
    form.dataset.busy = "1";
    const message = form.querySelector(".inline-message");
    if (message) message.textContent = "正在保存…";
    try {
      const result = await submit(form);
      if (message) message.textContent = result.message || "已保存。";
      form.elements.operation_id.value = id();
      const type = form.dataset.inline;
      if (["status","fields"].includes(type)) {
        document.querySelectorAll("form[data-inline='status'] input[name=revision],form[data-inline='fields'] input[name=revision]").forEach(el => el.value = result.revision);
      }
      if (type === "status") {
        document.querySelector("#job-status-label").textContent = result.label;
        const history = document.querySelector("#status-history");
        history.replaceChildren();
        for (const item of result.history) {
          const p = document.createElement("p"); p.className = "hint";
          p.textContent = new Date(item.time).toLocaleString("zh-CN") + " · " + item.old + " → " + item.new;
          history.append(p);
        }
        const choice = document.querySelector("#preparation-choice");
        choice.hidden = !result.ask_preparation;
        if (result.ask_preparation) choice.scrollIntoView({behavior:"smooth",block:"nearest"});
      }
      if (type === "jd-confirm") {
        const link = form.querySelector(".next-job");
        link.href = result.url; link.hidden = false;
        [...form.elements].forEach(el => el.disabled = true);
        link.scrollIntoView({behavior:"smooth",block:"nearest"});
      }
      if (type === "prepare") window.location.assign(result.url);
      if (type === "resume" && host) {
        const fragment = await fetch("/v2/runs/" + host.dataset.runId + "/content");
        if (fragment.ok) document.querySelector("#run-content").innerHTML = await fragment.text();
        startPolling();
      }
    } catch (error) { if (message) message.textContent = error.message; }
    finally { delete form.dataset.busy; }
  });
  document.querySelector("#skip-preparation")?.addEventListener("click", () => {
    document.querySelector("#preparation-choice").hidden = true;
  });
  if (!host) return;
  const base = "/v2/runs/" + host.dataset.runId;
  async function poll() {
    try {
      const response = await fetch(base + "/status");
      if (!response.ok) throw new Error();
      const state = await response.json();
      const usage = document.querySelector("#run-usage");
      if (usage) usage.textContent = "模型 " + state.model_calls + " 次 · 工具 " + state.tool_calls + " 次 · 缓存复用 " + state.cache_hits + " 次 · 已知用量 " + state.known_tokens + " Token" + (state.unknown_usage ? " · 部分用量未知" : "");
      const message = document.querySelector("#run-message");
      if (message) message.textContent = state.message || "正在处理，完成后结果会自动显示。";
      const rejected = document.querySelector("#run-tool-errors");
      if (rejected) {
        rejected.hidden = !state.tool_errors;
        rejected.textContent = "已拦截 " + state.tool_errors + " 次无效工具请求，记录已保留；纠正后才允许继续读取资料。";
      }
      if (["completed","failed"].includes(state.status)) {
        const fragment = await fetch(base + "/content");
        if (!fragment.ok) throw new Error();
        document.querySelector("#run-content").innerHTML = await fragment.text();
        host.dataset.runStatus = state.status;
        polling = false;
        return;
      }
    } catch {
      const message = document.querySelector("#run-message");
      if (message) message.textContent = "暂时无法读取进度，正在重连；已有输入和记录保留。";
    }
    setTimeout(poll, 1800);
  }
  function startPolling() {
    if (polling) return;
    polling = true;
    setTimeout(poll, 500);
  }
  if (["queued","processing"].includes(host.dataset.runStatus)) startPolling();
})();
