"use strict";
const $ = id => document.getElementById(id);
const api = "/api/v1/book-launch";
let sample, session, providers = [], busy = false, lastApprovalHash = "", blueprintDirty = false;
const STATUS = {researching:"市场研究中",awaiting_topic:"等待选择方向",planning:"构建设定中",awaiting_approval:"等待审阅设定",created:"已开书",failed:"需要处理",cancelled:"已取消，可恢复",interrupted:"已中断，可恢复"};
async function request(url, method = "GET", body) {
  const r = await fetch(url, {method, headers: {"Content-Type": "application/json"}, body: body === undefined ? undefined : JSON.stringify(body)});
  const value = await r.json();
  if (!r.ok) {
    const detail = value.detail;
    const message = typeof detail === "string" ? detail : Array.isArray(detail)
      ? detail.map(item => `${(item.loc || []).filter(v => v !== "body").join(" / ")}：${item.msg || "输入不符合要求"}`).join("；")
      : detail?.message || "请求失败，请检查服务日志。";
    throw new Error(message);
  }
  return value;
}
function notice(message) { $("notice").textContent = message; $("notice").hidden = !message; }
function stable(value) {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().map(k => [k, stable(value[k])]));
  return value;
}
async function action(fn) {
  if (busy) return;
  busy = true;
  try { notice(""); await fn(); } catch (error) { notice(error.message); } finally { busy = false; }
}
function showSample(value) {
  sample = value;
  $("sample-info").textContent = `${value.synthetic ? "合成演示" : "来源资料"} · ${value.books.length} 条 · ${value.coverage}`;
  $("sample-view").textContent = JSON.stringify(value, null, 2);
  updateModeNote();
}
function updateModeNote() {
  const demo = $("mode").value === "demo";
  $("mode-note").textContent = demo
    ? `当前为离线演示：选题是固定示例，不调用 SCNet。${sample && !sample.synthetic ? "已载入真实榜单；如需模型分析，请切换至真实模型。" : ""}`
    : "当前为真实模型：开始研究会调用所选服务并消耗用量；需要真实来源资料。";
  $("create").textContent = demo ? "开始离线演示" : "调用真实模型研究";
}
function providerChanged() {
  const p = providers.find(p => p.id === $("provider").value);
  $("base-wrap").hidden = $("key-wrap").hidden = !p?.input_required;
  $("model").value = p?.default_model || "";
  $("models").replaceChildren(...(p?.models || []).map(model => { const opt = document.createElement("option"); opt.value = model; return opt; }));
  $("key-note").textContent = p?.input_required ? "密钥不会进入 localStorage 或规划文件；服务重启后需重新输入。" : `真实模式从服务端 ${p?.key_env || ""} 读取 Key。${p?.configured ? "已配置" : "尚未配置"}`;
}
function text(tag, value) { const el = document.createElement(tag); el.textContent = value; return el; }
function render(value) {
  session = value.session;
  const s = session;
  $("status").textContent = `${s.config.mode === "demo" ? "离线演示 · 固定示例" : `真实模型 · ${s.config.model}`} · 状态：${STATUS[s.status] || s.status}${s.error ? " · " + s.error : ""}`;
  const selectedOption = [...$("sessions").options].find(o => o.value === s.id);
  if (selectedOption) selectedOption.textContent = `${s.config.mode} · ${s.blueprint?.title || s.config.brief.slice(0,20)} · ${STATUS[s.status]} · ${s.id.slice(0,6)}`;
  const usage = value.budget.run;
  $("usage").textContent = `${s.config.mode === "demo" ? "演示无 HTTP 调用" : "规划预算"} · ${usage.calls} 次 HTTP · 已计入 ${usage.charged_tokens} tokens · usage 未知 ${usage.unknown_calls} 次`;
  $("events").textContent = JSON.stringify(value.events, null, 2);
  $("resume").disabled = !["failed", "cancelled", "interrupted"].includes(s.status);
  $("cancel").disabled = ["created", "cancelled"].includes(s.status);
  $("update-budget").disabled = ["researching", "planning", "created"].includes(s.status);
  $("fork").disabled = !s.research;
  const stage = {researching:"research",awaiting_topic:"topic",planning:"blueprint",awaiting_approval:"approval",created:"chapters"}[s.status];
  document.querySelectorAll("[data-step]").forEach(el => el.classList.toggle("active", el.dataset.step === stage));
  $("findings").replaceChildren();
  if (s.research) {
    for (const finding of s.research.findings) $("findings").append(text("p", `${finding.observation}；推断：${finding.hypothesis} [${finding.source_ids.join(", ")}]`));
    $("findings").append(text("p", [...s.config.sample.limitations, ...s.research.limitations].join("；")));
  }
  $("topics").replaceChildren();
  for (const topic of s.research?.topics || []) {
    const card = document.createElement("article"); card.className = "topic-card";
    card.append(text("h3", `${topic.id} · ${topic.title} · ${topic.protagonist}`));
    for (const key of ["logline", "reader_promise", "differentiation", "opening_hook"]) card.append(text("p", topic[key]));
    card.append(text("p", `风险：${topic.risks.join("；")}；资料：${topic.source_ids.join(", ")}`));
    const button = text("button", s.selected_topic === topic.id ? "已确认此方向" : "确认此方向"); button.className = "button outline";
    button.disabled = s.status !== "awaiting_topic";
    button.onclick = () => action(async () => { await request(`${api}/${s.id}/select`, "POST", {topic_id:topic.id, research_hash:s.research_hash}); await refresh(); });
    card.append(button); $("topics").append(card);
  }
  $("plan").hidden = !s.blueprint;
  if (s.blueprint) {
    $("plan-title").textContent = s.blueprint.title; $("premise").textContent = s.blueprint.public_premise;
    if (lastApprovalHash !== s.blueprint_hash || !blueprintDirty) {
      $("blueprint").value = JSON.stringify(s.blueprint, null, 2);
      blueprintDirty = false;
    }
    $("blueprint").readOnly = s.status !== "awaiting_approval"; $("save-blueprint").disabled = s.status !== "awaiting_approval";
    $("hash").textContent = `设定 hash：${s.blueprint_hash}`;
    if (lastApprovalHash !== s.blueprint_hash) { $("approved").checked = false; lastApprovalHash = s.blueprint_hash; }
    $("approve").disabled = s.status !== "awaiting_approval";
  }
  $("continue").hidden = s.status !== "created";
  $("start").disabled = !!s.studio_run_id;
  $("studio-link").hidden = !s.studio_run_id;
  if (s.studio_run_id) $("studio-link").href = `/studio?run=${encodeURIComponent(s.studio_run_id)}`;
}
async function refresh() { if (session) render(await request(`${api}/${session.id}`)); }
async function list(selected) {
  const rows = await request(api); $("sessions").replaceChildren(text("option", "选择规划记录"));
  $("sessions").firstChild.value = "";
  for (const s of rows) { const opt = text("option", `${s.config.mode} · ${s.blueprint?.title || s.config.brief.slice(0, 20)} · ${s.status} · ${s.id.slice(0, 6)}`); opt.value = s.id; $("sessions").append(opt); }
  if (selected) $("sessions").value = selected;
}
async function choose(id) {
  if (!id) return;
  if (session?.id !== id) {
    blueprintDirty = false; lastApprovalHash = "";
    $("approved").checked = false; $("reviewer").value = "";
  }
  render(await request(`${api}/${id}`)); localStorage.setItem("novel-launch-id", id);
  showSample(session.config.sample);
  const cfg = session.config;
  $("mode").value = cfg.mode; $("brief").value = cfg.brief; $("provider").value = cfg.provider;
  providerChanged(); $("model").value = cfg.model; $("base").value = cfg.custom_base_url || "";
  $("budget").value = cfg.token_budget; $("calls").value = cfg.max_calls; $("output").value = cfg.max_output_tokens;
  $("capabilities").value = cfg.runtime_capabilities ? JSON.stringify(cfg.runtime_capabilities) : "";
  updateModeNote();
}
$("provider").onchange = providerChanged;
$("mode").onchange = () => { updateModeNote(); notice(""); };
$("blueprint").oninput = () => { blueprintDirty = true; };
$("sample-demo").onclick = () => action(async () => showSample(await request(`${api}/demo-sample`)));
$("collect").onclick = () => action(async () => { notice("正在读取公开榜单，最多两页、每页十条……"); showSample(await request(`${api}/sources/fanqie`, "POST", {categories:[258,257], rank_type:2, limit:10})); notice("采集完成；请先查看来源记录和采集日期。"); });
$("source-file").onchange = () => action(async () => { const f = $("source-file").files[0]; if (f) showSample(await request(`${api}/sources/import`, "POST", {text:await f.text(), format:f.name.endsWith(".csv") ? "csv" : "json"})); });
$("create").onclick = () => action(async () => {
  if (!sample) throw new Error("请先载入或采集资料");
  if ($("mode").value === "live" && sample.synthetic) throw new Error("当前是离线演示资料。请先成功采集番茄榜单，或导入真实 JSON / CSV 资料，再开始真实模型研究。");
  if ($("mode").value === "demo" && !sample.synthetic) throw new Error("已载入真实榜单，但当前仍是离线演示，选题为固定示例。请将“模式”切换为“真实模型”，或载入演示资料。");
  const config = {brief:$("brief").value, mode:$("mode").value, provider:$("provider").value, model:$("model").value,
    sample, max_calls:+$("calls").value, token_budget:+$("budget").value, max_output_tokens:+$("output").value};
  if (config.provider === "custom_input") { config.custom_base_url = $("base").value; config.api_key = $("key").value; }
  if ($("capabilities").value.trim()) {
    config.runtime_capabilities = JSON.parse($("capabilities").value);
    if (config.mode === "live" && !config.runtime_capabilities[config.model])
      throw new Error(`模型能力配置不包含当前模型 ${config.model}。请改用对应模型名称，或清空能力配置。`);
  }
  const s = await request(api, "POST", config); await list(s.id); await choose(s.id);
});
$("sessions").onchange = () => action(() => choose($("sessions").value));
$("refresh").onclick = () => action(async () => { await list(session?.id); await refresh(); });
$("cancel").onclick = () => action(async () => { await request(`${api}/${session.id}/cancel`, "POST"); await refresh(); });
$("resume").onclick = () => action(async () => { await request(`${api}/${session.id}/resume`, "POST", $("key").value ? {api_key:$("key").value} : undefined); await refresh(); });
$("update-budget").onclick = () => action(async () => { await request(`${api}/${session.id}/budget`, "POST", {max_calls:+$("calls").value, token_budget:+$("budget").value}); await refresh(); notice("预算已更新，已计入的历史用量保留；点击恢复继续。"); });
$("fork").onclick = () => action(async () => {
  const data = {provider:$("provider").value, model:$("model").value, max_calls:+$("calls").value,
    token_budget:+$("budget").value, max_output_tokens:+$("output").value};
  if (data.provider === "custom_input") { data.custom_base_url = $("base").value; data.api_key = $("key").value; }
  if ($("capabilities").value.trim()) data.runtime_capabilities = JSON.parse($("capabilities").value);
  const s = await request(`${api}/${session.id}/fork-research`, "POST", data);
  await list(s.id); await choose(s.id); notice("已复用原市场分析，请选择方向继续；这是独立预算的新规划。");
});
$("approve").onclick = () => action(async () => {
  if (!$("approved").checked || !$("reviewer").value.trim()) throw new Error("请填写审阅人并确认完整设定");
  if (JSON.stringify(stable(JSON.parse($("blueprint").value))) !== JSON.stringify(stable(session.blueprint))) throw new Error("设定有未保存修改，请先点击“保存设定修订”");
  await request(`${api}/${session.id}/approve`, "POST", {blueprint_hash:session.blueprint_hash, reviewed_by:$("reviewer").value, approved:true}); await refresh();
});
$("save-blueprint").onclick = () => action(async () => {
  const blueprint = JSON.parse($("blueprint").value);
  await request(`${api}/${session.id}/revise-blueprint`, "POST", {source_hash:session.blueprint_hash, blueprint});
  await refresh(); notice("设定已另存并重新校验，请审阅新版本后确认。");
});
$("start").onclick = () => action(async () => {
  const cfg = session.config;
  const payload = {novel_id:session.novel_id, mode:cfg.mode, provider:cfg.provider, model:cfg.model, custom_base_url:cfg.custom_base_url,
    runtime_capabilities:cfg.runtime_capabilities, max_output_tokens:cfg.max_output_tokens,
    chapter_count:+$("chapter-count").value, target_chars:+$("target").value,
    chapter_tokens:+$("chapter-tokens").value, run_tokens:+$("run-tokens").value, max_calls:+$("chapter-calls").value};
  if (cfg.provider === "custom_input") payload.api_key = $("key").value;
  const run = await request(`${api}/${session.id}/chapters`, "POST", payload);
  await refresh(); notice("章节任务已开始；点击“在章节工坊查看”，逐章审阅。");
});
action(async () => {
  const boot = await request("/api/v1/studio/bootstrap"); providers = boot.providers;
  for (const p of providers) { const opt = text("option", p.label); opt.value = p.id; $("provider").append(opt); }
  providerChanged(); await list();
  const id = localStorage.getItem("novel-launch-id");
  if (id && [...$("sessions").options].some(o => o.value === id)) { $("sessions").value = id; await choose(id); }
  else showSample(await request(`${api}/demo-sample`));
});
setInterval(() => { if (session && ["researching", "planning"].includes(session.status) && !busy) action(refresh); }, 2000);
