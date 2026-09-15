(() => {
  "use strict";
  const order = ["basic", "education", "skill", "experience", "project", "award", "publication", "other"];
  const newId = () => crypto.randomUUID();
  function dirtyEditor() {
    const confirm = document.querySelector("form[data-resume='confirm']");
    if (!confirm) return;
    confirm.querySelector("button").disabled = true;
    confirm.elements.reviewed.checked = false;
    confirm.querySelector(".inline-message").textContent = "正文已修改，请先保存草稿。";
    confirm.querySelector(".resume-next").hidden = true;
  }
  document.addEventListener("input", event => {
    const form = event.target.closest("form[data-resume]");
    if (!form) return;
    form.elements.operation_id.value = newId();
    if (form.dataset.resume === "edit") dirtyEditor();
  });
  document.addEventListener("click", event => {
    const button = event.target.closest("button[data-move]");
    if (!button) return;
    const row = button.closest("[data-fact-id]");
    if (button.dataset.move === "up" && row.previousElementSibling) row.previousElementSibling.before(row);
    if (button.dataset.move === "down" && row.nextElementSibling) row.nextElementSibling.after(row);
    row.closest("form").elements.operation_id.value = newId();
    button.focus();
  });
  document.addEventListener("submit", async event => {
    const form = event.target.closest("form[data-resume]");
    if (!form) return;
    event.preventDefault();
    if (form.dataset.busy) return;
    form.dataset.busy = "1";
    const type = form.dataset.resume;
    const message = form.querySelector(".inline-message");
    const data = new FormData(form);
    if (type === "selection") {
      const selected = order.flatMap(category => [...form.querySelectorAll('[data-selection-group="' + category + '"] [data-select-fact]:checked')].map(el => el.value));
      if (selected.length < 1 || selected.length > 40) {
        message.textContent = "请选择 1–40 条经历。"; delete form.dataset.busy; return;
      }
      data.set("selection", JSON.stringify(selected));
    }
    if (type === "edit") {
      const blocks = [...form.querySelectorAll("[data-block-id]")].map(row => ({fact_id: row.dataset.blockId, heading: row.querySelector("[data-heading]").value, text: row.querySelector("[data-text]").value}));
      data.set("content", JSON.stringify({blocks}));
    }
    const controls = [...form.elements].map(el => [el, el.disabled]);
    controls.forEach(([el]) => el.disabled = true);
    message.textContent = "正在处理…";
    try {
      const response = await fetch(form.action, {method: "POST", body: data, headers: {Accept: "application/json"}});
      let result;
      try { result = await response.json(); } catch { throw new Error("服务暂未返回有效结果，输入已保留，请重试。"); }
      if (!response.ok) throw new Error(result.message || "操作未完成，请重试。");
      message.textContent = result.message || "已提交。";
      form.elements.operation_id.value = newId();
      if (type === "start" || type === "selection") window.location.assign(result.url);
      if (type === "edit") {
        form.elements.revision.value = result.revision;
        const confirm = document.querySelector("form[data-resume='confirm']");
        confirm.elements.revision.value = result.revision;
        confirm.elements.operation_id.value = newId();
        confirm.querySelector("button").disabled = false;
        confirm.querySelector(".inline-message").textContent = "已保存，请核对并确认。";
      }
      if (type === "confirm") {
        const next = form.querySelector(".resume-next");
        next.href = result.url; next.hidden = false;
        next.scrollIntoView({behavior: "smooth", block: "nearest"});
      }
    } catch (error) { message.textContent = error.message; }
    finally {
      controls.forEach(([el, disabled]) => el.disabled = disabled);
      delete form.dataset.busy;
    }
  });
})();
