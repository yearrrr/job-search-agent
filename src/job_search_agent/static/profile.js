(() => {
  "use strict";
  const panel = document.querySelector("#parse-panel");
  const labels = {pending: "待确认", confirmed: "已确认", rejected: "已排除"};
  let saving = false, polling = 0, batchKey = "", batchPayload = "";
  const field = (form, name) => form.elements.namedItem(name);
  const operation = form => { field(form, "operation_id").value = crypto.randomUUID(); };
  async function request(url, data) {
    let response;
    try {
      response = await fetch(url, {
        method: data ? "POST" : "GET", credentials: "same-origin",
        headers: {"Accept": "application/json"}, ...(data ? {body: data} : {})
      });
    } catch {
      throw new Error("连接中断，输入已保留。请检查本地服务后重试。");
    }
    let result;
    try { result = await response.json(); } catch {
      throw new Error("服务未返回有效结果，输入已保留，请稍后重试。");
    }
    if (!response.ok) throw new Error(result.message || "保存失败，输入已保留。");
    return result;
  }
  function entry(form, status) {
    const result = {};
    for (const key of ["category", "title", "description", "period", "organization", "role", "review_notes"]) {
      result[key] = field(form, key).value;
    }
    result.technologies = field(form, "technologies").value.split(/[,，]/).map(x => x.trim()).filter(Boolean);
    return {id: form.closest("[data-fact]").dataset.fact,
      revision: Number(field(form, "revision").value), status, entry: result};
  }
  function categoryCounts() {
    document.querySelectorAll(".entry-category").forEach(section => {
      const n = section.querySelectorAll(".entry-card:not([hidden])").length;
      section.querySelector("[data-category-count]").textContent = n + " 条";
      const empty = section.querySelector(".category-empty");
      if (empty) empty.hidden = n > 0;
    });
  }
  async function save(forms, status, message, isBatch) {
    if (saving) { message.textContent = "正在保存，请稍候。"; return; }
    if (!forms.length) { message.textContent = "请先选中需要处理的经历。"; return; }
    if (forms.some(f => !f.reportValidity())) return;
    const items = forms.map(f => entry(f, status));
    const serialized = JSON.stringify(items);
    if (isBatch && batchPayload !== serialized) {
      batchPayload = serialized; batchKey = crypto.randomUUID();
    }
    const data = new FormData();
    data.set("csrf", field(forms[0], "csrf").value);
    data.set("operation_id", isBatch ? batchKey : field(forms[0], "operation_id").value);
    data.set("items", serialized);
    const url = forms[0].action;
    const controls = forms.flatMap(f => Array.from(f.elements));
    const disabled = controls.map(c => c.disabled);
    saving = true; controls.forEach(c => { c.disabled = true; });
    message.textContent = "正在保存…";
    try {
      const result = await request(url, data);
      for (const saved of result.entries) {
        const form = forms.find(f => f.closest("[data-fact]").dataset.fact === saved.id);
        const card = form.closest("[data-fact]");
        field(form, "revision").value = saved.revision;
        card.dataset.status = saved.status;
        card.dataset.legacy = "no";
        card.querySelector("[data-status-label]").textContent = labels[saved.status];
        card.querySelector("[data-select-entry]").checked = false;
        card.querySelector("[data-history-hint]").textContent = "已保存修订 " + saved.revision + "；重新打开本条可查看完整历史。";
        form.querySelector(".entry-message").textContent = labels[saved.status] + "，已保存。";
        delete form.dataset.dirty; operation(form);
        const destination = document.getElementById("category-" + field(form, "category").value);
        if (card.parentElement !== destination) destination.append(card);
      }
      for (const [statusKey, count] of Object.entries(result.counts)) {
        document.querySelectorAll('[data-count="' + statusKey + '"]').forEach(el => { el.textContent = count; });
      }
      categoryCounts();
      message.textContent = result.message;
      if (isBatch) { batchPayload = ""; batchKey = ""; }
    } catch (error) { message.textContent = error.message; }
    finally {
      controls.forEach((c, i) => { c.disabled = disabled[i]; });
      saving = false;
    }
  }
  async function refreshEntries() {
    const response = await fetch("/v2/documents/" + panel.dataset.document + "/entries", {credentials: "same-origin"});
    if (!response.ok) throw new Error("解析完成，但载入结果失败。可点击载入按钮重试。");
    const html = await response.text();
    // 同源 Jinja 页面；所有材料字段均由服务端自动转义。
    document.querySelector("#review-content").innerHTML = html;
    document.querySelector("#load-parsed").hidden = true;
  }
  async function poll(taskId, refresh, generation) {
    if (generation !== polling) return;
    const message = document.querySelector("#parse-message");
    try {
      const task = await request("/v2/profile-tasks/" + taskId);
      if (generation !== polling) return;
      document.querySelector("#parse-usage").textContent = "已登记调用 " + task.attempts.length
        + " 次 · 已知 Token " + task.known_tokens
        + (task.unknown_usage ? " · " + task.unknown_usage + " 次用量未知 / 尚未返回" : "");
      if (task.status === "completed") {
        message.textContent = "解析完成：提取 " + task.result.extracted + " 条，新增 "
          + task.result.added + " 条待确认经历。" + (task.result.notes || "");
        if (refresh) {
          if (saving || document.querySelector("[data-entry-form][data-dirty]")) {
            message.textContent += "检测到未保存编辑，请先保存，再载入新解析结果。";
            document.querySelector("#load-parsed").hidden = false;
          } else await refreshEntries();
        }
      } else if (task.status === "failed") {
        message.textContent = task.message;
      } else {
        message.textContent = task.status === "queued" ? "等待解析…" : "正在理解资料并整理完整经历…";
        setTimeout(() => poll(taskId, true, generation), 1600);
      }
    } catch (error) {
      message.textContent = error.message;
      document.querySelector("#load-parsed").hidden = false;
    }
  }
  document.addEventListener("input", event => {
    const form = event.target.closest("[data-entry-form], [data-preferences-form]");
    if (form) { form.dataset.dirty = "yes"; operation(form); }
  });
  document.addEventListener("change", event => {
    if (event.target.matches("[data-hide-legacy]")) {
      document.querySelectorAll('[data-legacy="yes"]').forEach(card => {
        card.hidden = event.target.checked && card.dataset.status === "pending";
        if (card.hidden) card.querySelector("[data-select-entry]").checked = false;
      });
      categoryCounts();
    }
  });
  document.addEventListener("submit", async event => {
    const form = event.target;
    if (form.matches("[data-entry-form]")) {
      event.preventDefault();
      await save([form], event.submitter?.value || "pending", form.querySelector(".entry-message"), false);
    } else if (form.matches("[data-parse-form]")) {
      event.preventDefault();
      const button = form.querySelector("button"), message = document.querySelector("#parse-message");
      button.disabled = true;
      try {
        const task = await request(form.action, new FormData(form));
        operation(form);
        panel.dataset.task = task.task_id;
        poll(task.task_id, true, ++polling);
      } catch (error) { message.textContent = error.message; }
      finally { button.disabled = false; }
    } else if (form.matches("[data-preferences-form]")) {
      event.preventDefault();
      if (form.dataset.saving) return;
      const data = new FormData(form), controls = Array.from(form.elements);
      controls.forEach(c => { c.disabled = true; }); form.dataset.saving = "yes";
      try {
        const result = await request(form.action, data);
        field(form, "revision").value = result.revision; operation(form);
        form.querySelector(".entry-message").textContent = result.message;
      } catch (error) { form.querySelector(".entry-message").textContent = error.message; }
      finally { controls.forEach(c => { c.disabled = false; }); delete form.dataset.saving; }
    }
  });
  document.addEventListener("click", async event => {
    if (event.target.closest("[data-select-pending]")) {
      document.querySelectorAll("[data-fact]").forEach(card => {
        card.querySelector("[data-select-entry]").checked = !card.hidden && card.dataset.status === "pending";
      });
    }
    const batch = event.target.closest("[data-batch]");
    if (batch) {
      const forms = Array.from(document.querySelectorAll("[data-select-entry]:checked"))
        .map(c => c.closest("[data-fact]").querySelector("form"));
      await save(forms, batch.dataset.batch, document.querySelector("#batch-message"), true);
    }
    if (event.target.id === "load-parsed" && !saving) {
      try { await refreshEntries(); } catch (error) { document.querySelector("#parse-message").textContent = error.message; }
    }
  });
  if (panel?.dataset.task) poll(panel.dataset.task, panel.dataset.active === "yes", ++polling);
})();
