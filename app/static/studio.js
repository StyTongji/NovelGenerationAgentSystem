"use strict";

(() => {
  const API = "/api/v1/studio";
  const $ = id => document.getElementById(id);
  const STATUS = {running:"正在创作", awaiting_review:"等待作者审阅", blocked:"需要处理", cancelled:"已暂停 · 可恢复", interrupted:"运行中断 · 可恢复", completed:"全部完成", queued:"等待生成", committed:"已正式提交",archived:"记录已结束"};
  const STAGES = {director:"章节导演", scene_plan:"分场景规划", draft:"场景写作", continuity:"连续性检查", repair:"定向修复", memory:"记忆抽取与验证", package:"候选预览", literary_review:"文学评审",review_assistant:"Agent 辅助填写",review:"作者审阅", approval:"作者审阅", commit:"正式提交", finalize:"正式提交", completed:"所有章节完成",archived:"记录已结束"};
  const SCORE_LABELS = {readability:"可读性", character:"人物塑造", dialogue:"对话区分", continuity:"连贯性", progression:"剧情推进"};
  const OP_LABELS = {create_event:"新增事件",character_state_update:"人物状态",character_knowledge_update:"人物知识",relationship_update:"人物关系",item_update:"物品变更",story_fact_create:"新增事实",plot_thread_update:"剧情线推进",foreshadowing_update:"伏笔变更",plot_debt_update:"剧情债务",propose_entity:"新实体"};
  const PAYLOADS = {create_event:"event",character_state_update:"character_state",character_knowledge_update:"knowledge",relationship_update:"relationship",item_update:"item",story_fact_create:"story_fact",plot_thread_update:"plot_thread",foreshadowing_update:"foreshadowing",plot_debt_update:"plot_debt",propose_entity:"proposed_entity"};
  const state = {bootstrap:null, novelId:"", run:null, chapterNumber:null, detail:null, detailKey:"", events:[], lastSequence:0, stream:null, poll:null, syncTimer:null, refreshInFlight:false, refreshAgain:false, busy:false, edits:new Map(), operationReviews:new Map(), world:null, selectedNode:null, view:"manuscript",assistedReviewHash:"",pendingAssistance:null};
  let toastTimer;

  function element(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }
  function option(value, label) { const el = element("option", "", label); el.value = value; return el; }
  function lines(value) { return value.split(/\r?\n/).map(line => line.trim()).filter(Boolean); }
  function stageLabel(stage) { return STAGES[stage] || (/^scene_\d+$/.test(stage || "") ? `场景 ${stage.split("_")[1]} 写作` : stage || "等待开始"); }
  function mappedStage(stage) { return stage === "scene_plan" || /^scene_\d+$/.test(stage || "") ? "draft" : ({review:"approval",commit:"finalize",review_assistant:"literary_review"}[stage] || stage); }
  function statusTone(status) { return ["completed", "committed"].includes(status) ? "good" : ["blocked"].includes(status) ? "bad" : ["awaiting_review","running"].includes(status) ? "warm" : "neutral"; }
  function badge(el, text, tone="neutral") { el.textContent = text; el.className = `pill ${tone}`; }
  function errorText(body, fallback) {
    const detail = body?.detail ?? body;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map(item => `${(item.loc || []).filter(s => s !== "body").join(" / ")}：${item.msg || "输入不符合要求"}`).join("；");
    return detail?.message || fallback;
  }
  async function api(path, data, method = data === undefined ? "GET" : "POST") {
    const response = await fetch(API + path, {method, headers:data === undefined ? {} : {"Content-Type":"application/json"}, body:data === undefined ? undefined : JSON.stringify(data)});
    let body;
    try { body = await response.json(); } catch { body = null; }
    if (!response.ok) throw new Error(errorText(body, `请求失败（HTTP ${response.status}），请检查服务端运行日志。`));
    return body;
  }
  function showError(error, target="alert") {
    const text = error instanceof Error ? error.message : String(error);
    if (target === "alert") { $("alert-message").textContent = text; $("alert").hidden = false; }
    else { $(target).textContent = text; $(target).hidden = false; }
  }
  function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4500); }
  async function action(button, work, errorTarget="alert") {
    if (state.busy) return;
    state.busy = true;
    $(errorTarget).hidden = true;
    const original = button.textContent;
    button.disabled = true; button.textContent = "处理中…";
    try { await work(); } catch (error) { showError(error, errorTarget); }
    finally { state.busy = false; button.textContent = original; button.disabled = false; renderRunControls(); updateApproval(); }
  }
  function selectedNovel() { return state.bootstrap?.novels.find(n => n.id === state.novelId); }
  function updateRun(run) {
    state.run = run;
    const index = state.bootstrap.runs.findIndex(r => r.id === run.id);
    if (index >= 0) state.bootstrap.runs[index] = run;
    else state.bootstrap.runs.unshift(run);
    renderRunOptions();
    renderRun();
  }
  function renderNovelOptions() {
    $("novel-select").replaceChildren(option("", "选择一本小说"));
    for (const novel of state.bootstrap.novels) $("novel-select").append(option(novel.id, novel.title));
    $("novel-select").value = state.novelId;
  }
  function renderRunOptions() {
    const runs = state.bootstrap.runs.filter(run => run.novel_id === state.novelId);
    $("run-select").replaceChildren(option("", runs.length ? "选择创作记录" : "暂无创作记录"));
    for (const run of runs) {
      const date = new Date(run.created_at).toLocaleDateString("zh-CN", {month:"2-digit",day:"2-digit"});
      $("run-select").append(option(run.id, `${date} · ${run.mode === "demo" ? "离线演示" : "真实模型"} · ${STATUS[run.status] || run.status}`));
    }
    $("run-select").value = state.run?.id || "";
  }
  async function selectNovel(id) {
    state.novelId = id; state.world = null;
    const novel = selectedNovel();
    $("book-name").textContent = novel?.title || "新的故事，新的世界";
    $("workspace-heading").textContent = novel ? `${novel.title}，未完待续。` : "让故事，接着发生。";
    $("workspace-subtitle").textContent = novel?.synopsis || "从一个念头出发，让规划、写作与记忆协同，为每一章留下可靠的来路。";
    renderNovelOptions();
    const run = state.bootstrap.runs.find(r => r.novel_id === id && !["completed", "cancelled", "archived"].includes(r.status)) || state.bootstrap.runs.find(r => r.novel_id === id);
    await selectRun(run?.id || "");
    if (id) await refreshWorld();
    else renderWorld();
  }
  function closeStream() {
    if (state.stream) { state.stream.close(); state.stream = null; }
    clearInterval(state.poll); state.poll = null;
    clearTimeout(state.syncTimer); state.syncTimer = null;
  }
  async function selectRun(id) {
    closeStream();
    state.run = null; state.chapterNumber = null; state.detail = null; state.detailKey = "";
    state.events = []; state.lastSequence = 0; state.edits.clear(); state.operationReviews.clear();
    state.assistedReviewHash="";state.pendingAssistance=null;
    state.budget=null;$("budget-panel").hidden=true;
    $("alert").hidden = true;
    renderChapter();
    if (id) { const run = await api(`/runs/${encodeURIComponent(id)}`); state.chapterNumber = run.current_chapter; updateRun(run); await loadChapter(); await loadBudget(); connectEvents(); }
    else { renderRunOptions(); renderRun(); renderChapter(); }
    renderEvents(); renderGraph();
  }
  function renderRunControls() {
    const run = state.run;
    $("configure").textContent = run?.status === "completed" ? "继续创作下一章" : "＋ 开始创作";
    $("cancel").disabled = state.busy || !run || ["completed","cancelled","interrupted","archived"].includes(run.status);
    if(!state.busy)$("cancel").textContent=run?.status==="running"?"暂停生成":"暂停此记录";
    $("finish-run").disabled=state.busy||!run||run.status==="archived";
    $("record-help").hidden=!run;
    $("resume").hidden = !run || !["cancelled","interrupted","blocked"].includes(run.status);
    $("resume").disabled = state.busy;
    const partial=state.detail?.partial||{};
    $("export").disabled = !run || !(run.chapters.some(c => c.characters > 0) || partial.draft?.full_text || partial.repair?.draft?.full_text || Object.keys(partial).some(k=>/^scene_\d+$/.test(k)));
    $("start-run").disabled = state.busy;
    $("edit-budget").disabled=state.busy||!run||["running","completed","archived"].includes(run.status);
  }
  function budgetText(budget) {
    const n=value=>Number(value).toLocaleString();
    return `第 ${budget.chapter_number} 章：已计入 ${n(budget.chapter.charged_tokens)} / ${n(budget.limits.chapter_tokens)} tokens，其中已知实际 ${n(budget.chapter.known_tokens)}、未知用量预留 ${n(budget.chapter.unknown_reserved_tokens)}（${budget.chapter.unknown_calls} 次）；请求 ${budget.chapter.calls} / ${budget.limits.max_calls} 次。整次任务：${n(budget.run.charged_tokens)} / ${n(budget.limits.run_tokens)} tokens。下一次调用还需预留输入＋最大输出额度。`;
  }
  async function loadBudget() {
    const run=state.run;if(!run||run.mode!=="live"){$("budget-panel").hidden=true;return;}
    const budget=await api(`/runs/${run.id}/budget`);if(state.run?.id!==run.id)return;
    state.budget=budget;$("budget-summary").textContent=budgetText(budget);$("budget-panel").hidden=false;
  }
  function renderRun() {
    const run = state.run;
    const completed = run?.chapters.filter(c => c.status === "committed").length || 0;
    $("metric-chapters").replaceChildren(document.createTextNode(run ? String(completed).padStart(2,"0") : "—"), element("small", "", ` / ${run ? String(run.total_chapters).padStart(2,"0") : "—"}`));
    const chars = run?.chapters.reduce((sum,c) => sum + (c.characters || 0), 0) || 0;
    $("metric-characters").replaceChildren(document.createTextNode(run ? chars.toLocaleString() : "—"), element("small", "", " 字"));
    $("metric-stage").textContent = run ? stageLabel(run.active_stage) : "等待开始";
    $("metric-status").textContent = run ? STATUS[run.status] || run.status : "尚未运行";
    badge($("mode-badge"), run ? run.mode === "demo" ? "离线脚本演示 · 非模型生成" : `${run.config?.model || "真实模型"}` : "本地工作区", run?.mode === "demo" ? "warm" : "neutral");
    badge($("live-label"), run ? STATUS[run.status] || run.status : "等待运行", statusTone(run?.status));
    $("chapter-nav-count").textContent = String(run?.total_chapters || 0).padStart(2,"0");
    $("chapter-list").replaceChildren();
    for (const chapter of run?.chapters || []) {
      const button = element("button", `chapter-link${chapter.number === state.chapterNumber ? " active" : ""}`);
      button.type = "button"; button.setAttribute("aria-current", chapter.number === state.chapterNumber ? "page" : "false");
      const label = element("span");
      label.append(element("span", "chapter-name", chapter.title || `第 ${chapter.number} 章`), element("span", "chapter-state", STATUS[chapter.status] || chapter.status));
      button.append(element("span", "chapter-number", String(chapter.number).padStart(2,"0")), label);
      button.addEventListener("click", () => { state.chapterNumber = chapter.number; state.detailKey = ""; renderRun(); loadChapter().catch(showError); });
      $("chapter-list").append(button);
    }
    if (!run) $("chapter-list").append(element("p", "muted small", "故事从第一章开始。"));
    if (run?.error) showError(run.error);
    renderRunControls(); renderGraph(); updateApproval();
  }
  async function refreshRun(forceChapter=false) {
    if (!state.run) return;
    if (state.refreshInFlight) { state.refreshAgain = true; return; }
    state.refreshInFlight = true;
    const id = state.run.id;
    try {
      const run = await api(`/runs/${id}`);
      if (state.run?.id !== id) return;
      const prior = state.run;
      const changed = prior.updated_at !== run.updated_at || prior.status !== run.status;
      updateRun(run);
      await loadBudget();
      if (changed || forceChapter) await loadChapter(forceChapter);
      if (prior.status !== run.status && ["awaiting_review","completed"].includes(run.status)) await refreshWorld();
    } finally {
      state.refreshInFlight = false;
      if (state.refreshAgain) { state.refreshAgain = false; refreshRun(true).catch(showError); }
    }
  }
  function queueRefresh() {
    if (state.syncTimer) return;
    state.syncTimer = setTimeout(() => { state.syncTimer = null; refreshRun().catch(showError); }, 180);
  }
  function connectEvents() {
    if (!state.run || state.stream) return;
    const id = state.run.id;
    const stream = new EventSource(`${API}/runs/${id}/events?after=${state.lastSequence}`);
    state.stream = stream;
    stream.onopen = () => { $("connection-dot").classList.add("online"); $("connection-label").textContent = "工作区已连接"; };
    stream.onmessage = event => {
      if (state.run?.id !== id) return;
      let data;
      try { data = JSON.parse(event.data); } catch { showError("收到了无法解析的运行事件，请刷新创作记录。"); return; }
      if (data.type === "idle") {
        stream.close(); if (state.stream === stream) state.stream = null;
        clearInterval(state.poll); state.poll = null;
        refreshRun(true).catch(showError); return;
      }
      if (data.sequence && data.sequence <= state.lastSequence) return;
      state.lastSequence = data.sequence || state.lastSequence;
      state.events.push(data);
      if (data.stage && data.status === "running") state.run.active_stage = data.stage;
      if (data.type === "error") showError(data.message || "任务运行失败，请查看运行时间线。");
      renderEvents(); renderGraph();
      $("metric-stage").textContent = stageLabel(state.run.active_stage);
      if (["status","stage","error"].includes(data.type)) queueRefresh();
    };
    stream.onerror = () => {
      stream.close(); if (state.stream === stream) state.stream = null;
      clearInterval(state.poll); state.poll = null;
      $("connection-dot").classList.remove("online"); $("connection-label").textContent = "事件连接已断开";
      refreshRun(true).catch(() => showError("与服务端的连接已断开。当前任务会停止并保留已完成阶段；恢复连接后刷新记录，再选择恢复创作。"));
    };
    if (state.run.status === "running") state.poll = setInterval(() => refreshRun().catch(showError), 4000);
  }
  async function loadChapter(force=false) {
    if (!state.run || !state.chapterNumber) { renderChapter(); return; }
    const runId = state.run.id, chapterNumber = state.chapterNumber;
    const progress = state.run.chapters.find(ch => ch.number === chapterNumber);
    const key = `${runId}/${chapterNumber}/${progress?.attempt_id || ""}/${progress?.status || ""}`;
    if (!force && state.detailKey === key && state.detail?.package) return;
    const detail = await api(`/runs/${runId}/chapters/${chapterNumber}`);
    if (state.run?.id !== runId || state.chapterNumber !== chapterNumber) return;
    const oldHash = state.detail?.package?.preview_package_hash;
    const oldAttempt = state.detail?.attempt_id;
    const changed = state.detailKey.split("/").slice(0,2).join("/") !== `${runId}/${chapterNumber}` || oldHash !== detail.package?.preview_package_hash || oldAttempt !== detail.attempt_id;
    state.detail = detail; state.detailKey = key;
    if (changed) {
      state.edits.clear(); state.operationReviews.clear();
      state.assistedReviewHash="";
      $("semantic-reviewed").checked = false; $("missing-memory").value = "";
      for (const select of $("quality-scores").querySelectorAll("select")) select.value = "";
    }
    // Polling a stable candidate must not reset in-progress human edits or review choices.
    if (changed || !detail.package || force) renderChapter();
    else { renderChecks(); renderAssistance(); updateApproval(); }
  }
  function editableCandidate() {
    return Boolean(state.detail?.package && state.run && state.run.current_chapter === state.chapterNumber && ["awaiting_review","blocked"].includes(state.run.status) && !state.detail.commit);
  }
  function editableDraft() {
    const draft = state.detail?.package?.draft || state.detail?.partial?.repair?.draft || state.detail?.partial?.draft;
    return Boolean(draft?.scenes?.length && state.run && state.run.current_chapter === state.chapterNumber && ["awaiting_review","blocked"].includes(state.run.status) && !state.detail?.commit);
  }
  function renderChapter() {
    const run = state.run, detail = state.detail;
    $("empty-state").hidden = Boolean(run);
    $("chapter-workspace").hidden = !run;
    if (!run) return;
    const progress = run.chapters.find(ch => ch.number === state.chapterNumber);
    const draft = detail?.package?.draft || detail?.partial?.repair?.draft || detail?.partial?.draft;
    const scenes = draft?.scenes || Object.entries(detail?.partial || {}).filter(([key,value]) => /^scene_\d+$/.test(key) && value.text).sort(([a],[b]) => Number(a.split("_")[1]) - Number(b.split("_")[1])).map(([,value]) => value);
    $("export").disabled = !run.chapters.some(c => c.characters > 0) && scenes.length === 0;
    $("chapter-kicker").textContent = `CHAPTER ${String(state.chapterNumber || run.current_chapter).padStart(2,"0")} · ${detail?.attempt_id ? `候选 ${detail.attempt_id}` : "等待生成"}`;
    $("chapter-title").textContent = draft?.title || progress?.title || `第 ${state.chapterNumber || run.current_chapter} 章`;
    badge($("chapter-badge"), STATUS[progress?.status] || "等待生成", statusTone(progress?.status));
    $("draft-note").textContent = detail?.commit ? "已审阅并正式提交" : draft ? `${(draft.full_text?.length || scenes.reduce((n,s) => n+s.text.length,0)).toLocaleString()} 字 · ${scenes.length} 个场景` : "已完成的场景会逐步显示";
    const content = $("draft-content"); content.replaceChildren();
    if (!scenes.length) content.append(element("p", "empty-note", run.status === "running" ? "Agent 正在规划与写作。\n你可以在「Agent 协作」中查看实时输出。" : progress?.error || "本章尚未形成草稿。开始或恢复创作后，内容会显示在这里。"));
    for (const [index,scene] of scenes.entries()) {
      const section = element("section", "scene");
      const heading = element("div", "scene-heading");
      const scenePlan = draft?.scene_plans?.find(plan => plan.scene_id === scene.scene_id);
      heading.append(element("span", "", `SCENE ${String(index+1).padStart(2,"0")}${scenePlan?.scene_goal ? ` · ${scenePlan.scene_goal}` : ""}`));
      const text = element("p", "scene-text", state.edits.get(scene.scene_id) ?? scene.text);
      if (editableDraft()) {
        const edit = element("button", "button outline compact", "编辑场景"); edit.type = "button";
        edit.addEventListener("click", () => {
          const existing = section.querySelector("textarea");
          if (existing) { existing.hidden = !existing.hidden; text.hidden = !existing.hidden; edit.textContent = existing.hidden ? "继续编辑" : "阅读视图"; return; }
          const textarea = element("textarea", "scene-editor"); textarea.value = state.edits.get(scene.scene_id) ?? scene.text;
          textarea.setAttribute("aria-label", `编辑第 ${index+1} 个场景`);
          textarea.addEventListener("input", () => { state.edits.set(scene.scene_id,textarea.value); text.textContent=textarea.value; $("revision-bar").hidden=false; updateApproval(); });
          section.append(textarea); text.hidden=true; edit.textContent="阅读视图"; textarea.focus();
        }); heading.append(edit);
      }
      section.append(heading,text); content.append(section);
    }
    $("revision-bar").hidden = !state.edits.size || !editableDraft();
    renderChecks(); renderMemory(); renderAssistance(); updateApproval();
  }
  function checkRow(label, value, notes) {
    const row = element("div", "check-row");
    const text = element("span", "", label);
    if (notes) text.append(element("small", "", notes));
    row.append(element("span", `check-mark${value === false ? " fail" : value == null ? " pending" : ""}`, value === true ? "✓" : value === false ? "!" : "○"), text);
    return row;
  }
  function renderChecks() {
    const pkg = state.detail?.package, partial = state.detail?.partial;
    const continuity = pkg?.continuity_report || partial?.repair?.report || partial?.continuity;
    const memory = pkg?.memory_result || partial?.memory;
    const checks = $("validation-checks"); checks.replaceChildren();
    const issues = report => report?.issues?.map(issue => issue.message || issue.description || issue.code || issue.category).filter(Boolean).join("；");
    checks.append(checkRow("连续性检查", continuity?.valid, issues(continuity)), checkRow("记忆证据与依赖校验", memory?.validation_report?.valid, issues(memory?.validation_report)), checkRow("状态变更预演", memory?.dry_run?.applicable, memory?.dry_run?.error || memory?.dry_run?.errors?.join("；")));
    if (state.detail?.commit) checks.append(checkRow("正式状态提交", true, `提交 ${state.detail.commit.commit_id} · ${state.detail.commit.operation_count} 条操作`));
    const quality = state.detail?.quality;
    $("quality-report").replaceChildren();
    if (quality) {
      const scores = Object.entries(quality.scores || {}).map(([key,value]) => `${SCORE_LABELS[key] || key} ${value}`).join(" · ");
      $("quality-report").append(element("p", "", `模型文学评审${scores ? `：${scores}` : "已完成"}。此处为辅助意见，人工评分仍需独立填写。`));
      if (quality.findings?.length) {
        const list = element("ul");
        for (const finding of quality.findings) list.append(element("li", "", `${finding.explanation || finding.description || ""}${finding.suggestion ? ` 建议：${finding.suggestion}` : ""}`));
        $("quality-report").append(list);
      }
    } else $("quality-report").append(element("p", "muted", state.run?.mode === "demo" ? "离线演示没有真实模型文学评审；辅助填写也是预设示例，仍需阅读核对。" : "尚无模型文学评审结果。"));
  }
  function entityName(id) {
    const entity = [...(state.world?.characters || []), ...(state.world?.items || []), ...(state.world?.plot_threads || [])].find(item => item.id === id);
    return entity?.name || entity?.title || id;
  }
  function operationSummary(op) {
    const payload = op[PAYLOADS[op.update_type]] || {};
    switch(op.update_type) {
      case "create_event": return payload.author_summary || payload.reader_safe_summary || payload.title;
      case "character_state_update": return `${entityName(payload.character_id)} · ${payload.field}：${payload.expected_old_value ?? "未指定"} → ${payload.proposed_new_value}`;
      case "character_knowledge_update": {
        const ref = payload.fact_ref || payload;
        const fact = state.world?.facts?.find(f => f.id === ref.fact_id);
        return `${entityName(payload.character_id)} · ${payload.status}：${fact?.statement || ref.temporary_fact_ref || ref.fact_id || payload.fact_statement}`;
      }
      case "relationship_update": return `${entityName(payload.source_character_id)} → ${entityName(payload.target_character_id)}：${payload.type}${payload.note ? `，${payload.note}` : ""}`;
      case "item_update": return `${entityName(payload.item_id || payload.temporary_item_ref)} · ${payload.change}${payload.new_owner_id ? ` → ${entityName(payload.new_owner_id)}` : ""}${payload.new_status ? `：${payload.new_status}` : ""}`;
      case "story_fact_create": return payload.statement;
      case "plot_thread_update": return `${entityName(payload.plot_thread_id)} · ${payload.action}：${payload.note || ""}`;
      case "foreshadowing_update": return `${payload.action}：${payload.setup || payload.note || payload.foreshadowing_id}`;
      case "plot_debt_update": return `${payload.action}：${payload.description || payload.plot_debt_id}`;
      case "propose_entity": return `${payload.entity_type}：${payload.proposed_name || payload.temporary_ref}`;
      default: return JSON.stringify(payload);
    }
  }
  function renderMemory() {
    const batch = state.detail?.package?.memory_result?.batch;
    const operations = batch?.operations || [];
    $("memory-count").textContent = `${operations.length} 条`;
    const target = $("memory-operations"); target.replaceChildren();
    if (!operations.length) target.append(element("p", "empty-note", batch ? "当前候选未提取任何记忆操作，不能直接通过人工验收。请检查是否漏提重要事件。" : "记忆抽取完成后，这里会展示每条变化及其正文证据。"));
    for (const op of operations) {
      const card = element("article", "memory-card");
      card.dataset.operationId = op.operation_id;
      const body = element("div"); const header = element("div", "memory-header");
      header.append(element("strong", "", OP_LABELS[op.update_type] || op.update_type), element("span", "memory-id", op.operation_id));
      body.append(header, element("p", "memory-summary", operationSummary(op)));
      const suggested=currentAssistance()?.operation_reviews.find(review=>review.operation_id===op.operation_id);
      if(suggested)body.append(element("p","small muted",`Agent 建议 · ${suggested.verdict}：${suggested.reason}`));
      for (const evidence of op.evidence || []) {
        const quote = element("blockquote", "evidence-quote", evidence.exact_quote);
        quote.append(element("small", "evidence-meta", `${evidence.evidence_id || "证据"} · ${evidence.scene_id} · ${evidence.start_char ?? "?"}–${evidence.end_char ?? "?"}`)); body.append(quote);
      }
      if (!op.evidence?.length) body.append(element("p", "small field-error", "尚无服务器解析后的正文证据，请检查校验报告。"));
      const details = element("details", "memory-details"); details.append(element("summary", "", "查看完整结构与引用"), element("pre", "", JSON.stringify(op,null,2))); body.append(details);
      const choice = element("div", "memory-choice"); const label = element("label", "", "你的判断");
      const select = element("select"); select.setAttribute("aria-label", `审阅记忆 ${op.operation_id}`);
      select.append(option("", "请选择判断"), option("correct","正确 · correct"), option("missing","有遗漏 · missing"), option("wrong","错误 · wrong"), option("over-extracted","过度抽取"));
      select.value = state.operationReviews.get(op.operation_id) || state.detail?.approval?.operation_reviews?.[op.operation_id] || "";
      select.disabled = !editableCandidate();
      select.addEventListener("change", () => { state.operationReviews.set(op.operation_id,select.value); updateApproval(); });
      label.append(select); choice.append(label); card.append(body,choice); target.append(card);
    }
    $("memory-editor").hidden = !editableCandidate();
    if (batch) {
      const wireBatch = JSON.parse(JSON.stringify(batch));
      if (wireBatch.contract_version === 3) for (const op of wireBatch.operations) op.evidence = [];
      $("memory-json").value = JSON.stringify(wireBatch,null,2);
    } else $("memory-json").value = "";
    $("memory-retry").hidden = !editableCandidate();
  }
  function approvalProblem() {
    const pkg = state.detail?.package;
    if (state.detail?.commit) return "这一章已提交，原始审阅记录已保存。";
    if (!editableCandidate()) return "草稿完成并暂停后，才能填写审阅并提交。";
    if (!pkg.continuity_report.valid || !pkg.memory_result.validation_report.valid || !pkg.memory_result.dry_run?.applicable) return "仍有检查未通过，请先修订正文或记忆。";
    if (state.edits.size) return "存在尚未保存的正文修改，请先保存修订并重新检查。";
    const operations = pkg.memory_result.batch.operations;
    if (!operations.length) return "记忆操作为空，请补充抽取后再审阅。";
    const pending = operations.filter(op => state.operationReviews.get(op.operation_id) !== "correct");
    const notes = lines($("missing-memory").value);
    if (pending.length || notes.length) {
      const labels = {missing:"有遗漏", wrong:"错误", "over-extracted":"过度抽取"};
      const issues = [];
      if (pending.length) {
        const names = pending.slice(0, 4).map(op => `${op.operation_id}（${labels[state.operationReviews.get(op.operation_id)] || "未判断"}）`).join("、");
        issues.push(`待处理 ${pending.length} 条：${names}${pending.length > 4 ? "等" : ""}`);
      }
      if (notes.length) issues.push(`待改问题 ${notes.length} 项`);
      return `本章共 ${operations.length} 条记忆（并非固定数量）；${issues.join("；")}。请先核对并处理。`;
    }
    const scores = [...$("quality-scores").querySelectorAll("select")];
    if (scores.some(select => !select.value)) return "请为五项文学质量分别评分。";
    if (scores.some(select => Number(select.value) < 3)) return "有评分低于 3 分，请先修订该问题。";
    if (!$("reviewed-by").value.trim()) return "请填写审阅人。";
    if (!$("semantic-reviewed").checked) return "核对完成后，请勾选审阅确认。";
    return "确认后将写入正式世界状态，并继续生成下一章。";
  }
  function updateApproval() {
    const problem = approvalProblem();
    $("approval-hint").textContent = problem;
    const pending = state.detail?.package?.memory_result?.batch?.operations?.some(op => state.operationReviews.get(op.operation_id) !== "correct");
    $("locate-review-issue").hidden = !pending || !editableCandidate();
    $("approve").disabled = state.busy || problem !== "确认后将写入正式世界状态，并继续生成下一章。";
    const disabled = !editableCandidate();
    for (const field of $("approval-form").querySelectorAll("input,textarea,select")) field.disabled = disabled;
    $("save-revision").disabled = state.busy || !state.edits.size || !editableDraft();
    $("save-memory").disabled = state.busy || !editableCandidate();
    $("memory-retry").disabled = state.busy || !editableCandidate();
    $("recheck-options").hidden = !(state.run?.mode === "live" && state.run.status === "blocked" && editableDraft());
    $("recheck-draft").disabled = state.busy || state.edits.size > 0;
    $("assist-review").disabled=state.busy||!editableCandidate()||state.edits.size>0;
    $("apply-review-assistance").hidden=!currentAssistance();
    $("apply-review-assistance").disabled=state.busy||!editableCandidate()||state.edits.size>0;
    $("repair-review-issues").hidden=!currentAssistance()||currentAssistance().source!=="model";
    $("repair-review-issues").disabled=state.busy||!editableCandidate()||state.edits.size>0;
  }
  function currentAssistance(){
    const report=state.detail?.assistance;
    return report&&report.attempt_id===state.detail.attempt_id&&report.preview_package_hash===state.detail.package?.preview_package_hash?report:null;
  }
  function applyAssistance(){
    const report=currentAssistance();if(!report||!editableCandidate()||state.edits.size)return;
    state.operationReviews=new Map(report.operation_reviews.map(review=>[review.operation_id,review.verdict]));
    for(const select of $("quality-scores").querySelectorAll("select"))select.value=String(report.quality_scores[select.dataset.score]);
    $("missing-memory").value=report.missing.join("\n");
    if(!$("reviewed-by").value.trim())$("reviewed-by").value="作者（Agent 辅助）";
    $("semantic-reviewed").checked=false;state.assistedReviewHash=report.report_hash;
    renderMemory();updateApproval();
    toast("Agent 已填写建议。请核对或修改，再勾选确认并提交。");
  }
  function renderAssistance(){
    const report=currentAssistance(),box=$("review-assistance-summary");box.replaceChildren();
    if(report)box.append(element("p","small muted",`${report.source==="offline_demo"?"离线预设建议 · 非真实模型审阅":"Agent 辅助建议"}：${report.summary}`));
    else box.append(element("p","small muted","可自己填写，也可让 Agent 建议逐条判断、漏提项和五项评分；最终提交由你确认。"));
    const pending=state.pendingAssistance;
    if(pending&&pending.runId===state.run?.id&&pending.chapter===state.chapterNumber&&report&&report.report_hash!==pending.previousHash&&editableCandidate()){
      state.pendingAssistance=null;applyAssistance();
    }else if(pending&&state.run?.status!=="running"&&state.run?.error)state.pendingAssistance=null;
  }
  function svgElement(tag, attributes={}, text) {
    const el = document.createElementNS("http://www.w3.org/2000/svg",tag);
    for (const [key,value] of Object.entries(attributes)) el.setAttribute(key,String(value));
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function renderGraph() {
    const graph = state.bootstrap?.graph;
    if (!graph) return;
    const nodes = graph.nodes || [], edges = graph.edges || [];
    const container = $("workflow-graph"); container.replaceChildren();
    const width = 930, rows = Math.ceil(nodes.length/3), height=rows*132 + 18;
    const svg = svgElement("svg",{viewBox:`0 0 ${width} ${height}`,role:"img","aria-label":"实际章节生成工作流，点击节点查看职责"});
    const defs = svgElement("defs"); const marker = svgElement("marker",{id:"workflow-arrow",viewBox:"0 0 10 10",refX:9,refY:5,markerWidth:5,markerHeight:5,orient:"auto-start-reverse"});
    marker.append(svgElement("path",{d:"M 0 0 L 10 5 L 0 10 z",fill:"#bac7ac"}));defs.append(marker);svg.append(defs);
    const points = new Map(nodes.map((node,index) => {const row=Math.floor(index/3), col=row%2 ? 2-index%3 : index%3; return [node.id,{x:col*310+17,y:row*132+21}];}));
    const relevant = state.events.filter(e => !e.chapter_number || e.chapter_number === (state.chapterNumber || state.run?.current_chapter));
    const complete = new Set(relevant.filter(e => e.status === "completed").map(e => mappedStage(e.stage)));
    const viewingCurrentChapter = !state.chapterNumber || state.chapterNumber === state.run?.current_chapter;
    const active = viewingCurrentChapter && (state.run?.status === "running" || state.run?.status === "awaiting_review") ? mappedStage(state.run.active_stage) : null;
    for (const edge of edges) {
      const from = points.get(edge.source), to = points.get(edge.target); if (!from || !to) continue;
      const same = Math.abs(from.y-to.y)<1;
      let path;
      if (same) { const right=to.x>from.x; const x1=from.x+(right?266:0),x2=to.x+(right?0:266);path=`M ${x1} ${from.y+37} L ${x2} ${to.y+37}`; }
      else { const x1=from.x+133,x2=to.x+133,y1=from.y+74,y2=to.y;path=`M ${x1} ${y1} C ${x1} ${y1+28}, ${x2} ${y2-28}, ${x2} ${y2}`; }
      const edgePath=svgElement("path",{d:path,class:`graph-edge${edge.target===active?" active":""}`,"marker-end":"url(#workflow-arrow)"});
      edgePath.append(svgElement("title",{},edge.label || `${edge.source} → ${edge.target}`)); svg.append(edgePath);
    }
    for (const [index,node] of nodes.entries()) {
      const p=points.get(node.id); const group=svgElement("g",{class:`graph-node${active===node.id?" active":complete.has(node.id)?" complete":""}`,tabindex:0,role:"button","aria-label":node.label,transform:`translate(${p.x} ${p.y})`});
      group.append(svgElement("rect",{width:266,height:74,rx:8}),svgElement("text",{x:18,y:27,class:"node-label"},node.label),svgElement("text",{x:18,y:49,class:"node-caption"},node.human?"由作者判断与批准":node.deterministic?"原子事务 · 写入正式状态":node.agent_ids?.length?node.agent_ids.join(" / "):"确定性流程节点"),svgElement("text",{x:240,y:27,class:"node-caption","text-anchor":"end"},String(index+1).padStart(2,"0")));
      const choose=()=>{state.selectedNode=node.id;const roles=(node.agent_ids||[]).map(id=>state.bootstrap.roles.find(r=>r.id===id)).filter(Boolean);$("node-description").textContent=`${node.label}：${roles.length?roles.map(r=>r.description||r.goal||r.label).join("；"):node.human?"核对草稿、记忆证据与文学质量，明确批准具体候选版本。":node.deterministic?"在一个数据库事务内保存章节、事件与快照；相同提交不会重复新增。":"整理本章候选，供作者审阅；不修改正式世界状态。"}`;};
      group.addEventListener("click",choose);group.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();choose();}});svg.append(group);
    }
    container.append(svg);
  }
  function renderRoles() {
    $("agent-count").textContent=state.bootstrap.roles.length;
    $("role-cards").replaceChildren(); $("role-models").replaceChildren();
    for (const [index,role] of state.bootstrap.roles.entries()) {
      const card=element("article","role-card");card.append(element("span","role-index",String(index+1).padStart(2,"0")),element("h3","",role.label||role.name||role.id),element("p","",role.description||role.goal||""));$("role-cards").append(card);
      const label=element("label","",role.label||role.id);const input=element("input");input.dataset.role=role.id;input.placeholder="使用默认模型";input.setAttribute("list","model-list");label.append(input);$("role-models").append(label);
    }
  }
  function renderEvents() {
    $("event-count").textContent=`${state.events.length} EVENTS`;
    const list=$("event-list"),atBottom=list.scrollHeight-list.scrollTop-list.clientHeight<60;
    const scrollTop=list.scrollTop;
    const expanded=new Set([...list.querySelectorAll("details[open]")].map(detail=>detail.dataset.sequence));
    list.replaceChildren();
    if (!state.events.length) {list.append(element("li","muted","创作开始后，运行事件会实时显示。"));return;}
    for(const event of state.events.slice(-200)) {
      const item=element("li");const time=element("time","",event.at?new Date(event.at).toLocaleTimeString("zh-CN",{hour12:false}):"—");
      const body=element("div",`event-message${event.type==="error"?" error":""}`);
      let message=event.message || `${event.agent||"API"} · ${event.status||"调用中"}`;
      if(event.type==="llm") {
        message=`${event.agent||"API"} · ${event.status||""} · ${event.requested_model||""}`;
        if(event.response_model) message+=event.response_model==="Auto"?" · 实际模型未披露":` → ${event.response_model}`;
        if(event.latency_ms!=null) message+=` · ${(event.latency_ms/1000).toFixed(1)} 秒`;
        if(event.usage) message+=` · tokens ${event.usage.total_tokens ?? "未知"}`;
      }
      body.append(element("span","",`${event.chapter_number?`第 ${event.chapter_number} 章 · `:""}${message}`));
      if(event.output || event.commit || event.type==="llm") {const details=element("details","memory-details");details.dataset.sequence=String(event.sequence);details.open=expanded.has(String(event.sequence));details.append(element("summary","",event.output?"查看 Agent 输出":"查看调用详情"),element("pre","",JSON.stringify(event.output||event.commit||event,null,2)));body.append(details);}
      item.append(time,body);list.append(item);
    }
    list.scrollTop=atBottom?list.scrollHeight:scrollTop;
  }
  async function refreshWorld() {
    if(!state.novelId) {state.world=null;renderWorld();return;}
    const id=state.novelId;const world=await api(`/novels/${id}/state`);
    if(id!==state.novelId)return;
    state.world=world;renderWorld();
    const novel=selectedNovel();if(novel)novel.current_chapter_number=world.current_chapter_number ?? world.novel?.current_chapter_number ?? novel.current_chapter_number;
  }
  function entityRow(title,description) {const row=element("div","entity-row");row.append(element("strong","",title),element("div","entity-meta",description));return row;}
  function worldCard(title,rows,empty) {const card=element("section","world-card");card.append(element("h3","",title));if(rows.length)card.append(...rows);else card.append(element("p","small muted",empty));return card;}
  function renderWorld() {
    const target=$("world-state");target.replaceChildren();const world=state.world;
    if(!world) {target.append(element("p","muted","选择一本小说后查看故事世界。"));return;}
    const chapter=world.current_chapter_number ?? world.novel?.current_chapter_number ?? 0;
    target.append(element("div","world-overview",`正式世界 · 已提交至第 ${chapter} 章 · ${(world.characters||[]).length} 位人物 · ${(world.items||[]).length} 件物品 · ${(world.facts||[]).length} 条事实`));
    const ownership=element("section","world-card");ownership.append(element("h3","","人物与物品归属"));
    const chars=world.characters||[],items=world.items||[];
    if(chars.length) {
      const height=Math.max(chars.length,items.length,1)*76+20;const svg=svgElement("svg",{viewBox:`0 0 850 ${height}`,role:"img","aria-label":"已提交的人物与物品归属关系图",style:"width:100%;min-width:480px"});
      for(const [i,item]of items.entries()) {const owner=chars.findIndex(c=>c.id===(item.owner_id||item.owner));if(owner>=0)svg.append(svgElement("path",{d:`M 290 ${owner*76+35} C 425 ${owner*76+35}, 425 ${i*76+35}, 560 ${i*76+35}`,fill:"none",stroke:"#c7d3ba","stroke-width":1.5}));}
      for(const [i,char]of chars.entries()){svg.append(svgElement("rect",{x:20,y:i*76+10,width:270,height:50,rx:7,fill:"#edf3e7",stroke:"#d8e4cc"}),svgElement("text",{x:38,y:i*76+40,fill:"#607c50","font-size":14},`${char.name}${char.is_protagonist?" · 主角":""}`));}
      for(const [i,item]of items.entries()){svg.append(svgElement("rect",{x:560,y:i*76+10,width:270,height:50,rx:7,fill:"#f8f1e3",stroke:"#e5dcc4"}),svgElement("text",{x:578,y:i*76+40,fill:"#9b885c","font-size":14},item.name));}
      const scroll=element("div");scroll.style.overflowX="auto";scroll.append(svg);ownership.append(scroll);
      if(!items.length)ownership.append(element("p","small muted","暂无已登记物品。角色状态会随着正式提交更新。"));
    } else ownership.append(element("p","small muted","尚未初始化人物。"));
    target.append(ownership);ownership.style.marginBottom="20px";
    const grid=element("div","world-grid");
    grid.append(worldCard("人物当前状态",chars.map(char=>{const s=char.current_state||char.state||{},location=s.location_name||s.location||s.location_id,realm=s.realm_name||s.realm||s.realm_id;return entityRow(char.name,[char.description,s.alive_status?`状态 ${s.alive_status}`:"",s.physical_condition?`身体 ${s.physical_condition}`:"",s.mental_condition?`心理 ${s.mental_condition}`:"",location?`地点 ${location}`:"",realm?`境界 ${realm}`:"",s.current_goals?.length?`目标 ${s.current_goals.join("；")}`:""].filter(Boolean).join(" · "));}),"暂无人物状态。"));
    grid.append(worldCard("剧情线与伏笔",(world.plot_threads||[]).map(thread=>entityRow(thread.title,`${thread.status||"进行中"}${thread.public_summary||thread.description?` · ${thread.public_summary||thread.description}`:""}${thread.last_advanced_chapter!=null?` · 最近推进：第 ${thread.last_advanced_chapter} 章`:""}`)),"暂无已登记剧情线。"));
    grid.append(worldCard("未完成的剧情债务",(world.plot_debts||world.open_plot_debts||[]).map(debt=>entityRow(debt.description||debt.title,`${debt.status||"未完成"}${debt.due_chapter?` · 第 ${debt.due_chapter} 章前处理`:""}`)),"暂无待处理剧情债务。"));
    grid.append(worldCard("世界规则",(world.constraints||[]).map(rule=>entityRow(rule.description||rule.statement||rule.content||rule.type,rule.severity||rule.constraint_type||"")),"尚无额外规则。"));
    const knowledge=worldCard("人物知识 · 按角色查看",[],"选择人物，查看其正式登记的知识。"),label=element("label","","观察角色"),select=element("select");select.append(option("","选择角色"));for(const char of chars)select.append(option(char.id,char.name));label.append(select);knowledge.append(label);const facts=element("div");knowledge.append(facts);
    select.addEventListener("change",()=>{facts.replaceChildren();const rows=(world.knowledge||[]).filter(k=>k.character_id===select.value);for(const k of rows){const fact=(world.facts||[]).find(f=>f.id===k.fact_id);facts.append(entityRow(fact?.statement||k.fact_id,`${k.status} · ${fact?.secrecy||"未标注"}`));}if(select.value&&!rows.length)facts.append(element("p","small muted","该角色暂无已登记知识。"));});grid.append(knowledge);
    grid.append(worldCard("已登记事实 · 作者视角",(world.facts||[]).map(fact=>entityRow(fact.statement,`可见性：${fact.secrecy||"未标注"}`)),"暂无事实。"));target.append(grid);
  }
  function setView(view) {
    state.view=view;
    for(const button of document.querySelectorAll("[data-view]")){const active=button.dataset.view===view;button.classList.toggle("active",active);button.setAttribute("aria-selected",String(active));button.tabIndex=active?0:-1;$("panel-"+button.dataset.view).hidden=!active;}
    if(view==="world")refreshWorld().catch(showError);
  }
  function updateProvider() {
    const provider=state.bootstrap.providers.find(p=>p.id===$("provider").value);
    $("model").value=provider?.default_model || "";$("model-list").replaceChildren();for(const model of provider?.models||[])$("model-list").append(option(typeof model==="string"?model:model.id,typeof model==="string"?model:model.id));
    $("provider-status").textContent=provider?.input_required?"填写自己的 API 地址、密钥与精确模型名称，即可使用该服务。":provider?`${provider.label || provider.id} · ${provider.base_url || ""} · ${provider.configured?`已配置 ${provider.key_env}，页面不会接触密钥。`:`请在服务端环境或 .env 中设置 ${provider.key_env}，然后重启服务。`}`:"尚无可用服务配置，可先运行离线演示。";
    updateMode();
  }
  function updateMode() {
    const live=document.querySelector('input[name="mode"]:checked').value==="live",custom=live&&$("provider").value==="custom_input";
    $("provider-options").hidden=!live;$("model").required=live;$("demo-note").hidden=live;
    $("custom-provider-fields").hidden=!custom;
    for(const id of ["custom-base-url","custom-api-key"]){$(id).required=custom;$(id).disabled=!custom;}
    for(const id of ["target-chars","direction","chapter-directions"])$(id).disabled=!live;
    $("new-budget-options").hidden=!live;
    $("output-token-option").hidden=!live;$("max-output-tokens").disabled=!live;
  }
  async function ensureCustomCredentials() {
    if(!state.run?.requires_api_key)return;
    const dialog=$("credential-dialog"),form=$("credential-form"),runId=state.run.id;
    $("credential-error").hidden=true;$("runtime-api-key").value="";dialog.returnValue="";
    await new Promise((resolve,reject)=>{
      dialog.addEventListener("close",()=>{
        $("runtime-api-key").value="";form.onsubmit=null;
        dialog.returnValue==="saved"?resolve():reject(new Error("已取消密钥输入。"));
      },{once:true});
      form.onsubmit=async event=>{
        event.preventDefault();$("save-credential").disabled=true;
        try {const run=await api(`/runs/${runId}/credentials`,{api_key:$("runtime-api-key").value.trim()});updateRun(run);dialog.close("saved");}
        catch(error){showError(error,"credential-error");}
        finally{$("save-credential").disabled=false;}
      };
      dialog.showModal();
    });
  }
  function openRunDialog() {
    if(!state.novelId){$("novel-dialog").showModal();return;}
    const conflicts=state.bootstrap.runs.filter(r=>r.novel_id===state.novelId&&!["completed","cancelled","archived"].includes(r.status));
    $("replace-run-notice").hidden=!conflicts.length;$("replace-existing").checked=false;
    $("replace-run-description").textContent=conflicts.length?`本书已有 ${conflicts.length} 条未结束记录。你可以返回继续，或勾选下方选项结束旧记录并开始新的创作。`:"";
    const last = Math.max(Number(selectedNovel()?.current_chapter_number || 0), ...state.bootstrap.runs.filter(r=>r.novel_id===state.novelId).flatMap(r=>r.chapters.filter(c=>c.status==="committed").map(c=>c.number)), 0);
    $("continuation-notice").hidden=!last;
    $("continuation-notice").textContent=last?`本书已提交至第 ${last} 章。新记录将从第 ${last+1} 章继续，继承已提交的故事状态；历史创作记录仍可在左侧切换。`:"";
    $("start-run").textContent="开始创作 →";
    $("run-form-error").hidden=true;$("run-dialog").showModal();
  }
  async function refreshBootstrap() {
    state.bootstrap=await api("/bootstrap");
    $("connection-dot").classList.add("online");$("connection-label").textContent="工作区已连接";
    renderRoles();renderGraph();$("provider").replaceChildren();
    for(const provider of state.bootstrap.providers)$("provider").append(option(provider.id,provider.label||provider.id));
    const preferred=state.bootstrap.providers.find(p=>p.is_default&&p.configured)||state.bootstrap.providers.find(p=>p.configured)||state.bootstrap.providers.find(p=>p.is_default);
    if(preferred)$("provider").value=preferred.id;
    updateProvider();updateMode();
    const linkedRun = !state.novelId && state.bootstrap.runs.find(r=>r.id===new URLSearchParams(location.search).get("run"));
    await selectNovel(state.novelId || linkedRun?.novel_id || state.bootstrap.novels[0]?.id || "");
    if(linkedRun && linkedRun.id!==state.run?.id)await selectRun(linkedRun.id);
  }

  // All approvals begin unselected, and are bound to the displayed server candidate.
  for(const [key,label]of Object.entries(SCORE_LABELS)){const row=element("label","",label),select=element("select");select.id=`quality-score-${key}`;row.htmlFor=select.id;select.setAttribute("aria-label",label);select.dataset.score=key;select.append(option("","未评分"));for(let i=1;i<=5;i++)select.append(option(String(i),`${i} 分${i===3?" · 合格":""}`));row.append(select);$("quality-scores").append(row);}
  const retry=element("button","button outline compact","重新抽取记忆");retry.id="memory-retry";retry.type="button";retry.hidden=true;$("memory-count").before(retry);
  const assistanceBox=element("div","review-assistance-controls");
  const assistButton=element("button","button outline compact","让 Agent 填写");assistButton.id="assist-review";assistButton.type="button";
  const assistOptions=element("details","role-model-details"), assistSummary=element("summary","","审阅模型与输出上限（选填）");
  const assistModelLabel=element("label","","审阅模型（留空使用当前角色模型）"),assistModel=element("input");assistModel.id="assist-model";assistModel.placeholder="例如 GLM-5.3-Flash";assistModelLabel.append(assistModel);
  const assistLimitLabel=element("label","","审阅单次最大输出 tokens"),assistLimit=element("select");assistLimit.id="assist-output";assistLimit.append(option("","沿用当前配置"),option("8192","8,192"),option("16384","16,384"),option("32768","32,768"));assistLimitLabel.append(assistLimit);assistOptions.append(assistSummary,assistModelLabel,assistLimitLabel);
  const applyButton=element("button","button outline compact","采用已保存建议");applyButton.id="apply-review-assistance";applyButton.type="button";applyButton.hidden=true;
  const repairButton=element("button","button outline compact","Agent 修订并重新审阅");repairButton.id="repair-review-issues";repairButton.type="button";repairButton.hidden=true;
  const assistanceSummary=element("div");assistanceSummary.id="review-assistance-summary";
  assistanceBox.append(assistButton,applyButton,repairButton,assistOptions,assistanceSummary);$("approval-form").before(assistanceBox);
  applyButton.addEventListener("click",applyAssistance);
  repairButton.addEventListener("click",()=>action(repairButton,async()=>{
    await ensureCustomCredentials();state.pendingAssistance={runId:state.run.id,chapter:state.chapterNumber,previousHash:currentAssistance()?.report_hash||""};
    const run=await api(`/runs/${state.run.id}/chapters/${state.chapterNumber}/repair-from-review`,{});updateRun(run);await loadChapter(true);connectEvents();toast("Agent 正在根据建议重新抽取记忆并复审；会消耗模型调用预算，原候选保留。");
  }));
  assistButton.addEventListener("click",()=>action(assistButton,async()=>{
    await ensureCustomCredentials();state.pendingAssistance={runId:state.run.id,chapter:state.chapterNumber,previousHash:currentAssistance()?.report_hash||""};
    const payload={model:$("assist-model").value.trim()};if($("assist-output").value)payload.max_output_tokens=Number($("assist-output").value);
    const run=await api(`/runs/${state.run.id}/chapters/${state.chapterNumber}/assist-review`,payload);updateRun(run);await loadChapter(true);connectEvents();toast(run.mode==="demo"?"正在展示离线预设的辅助填写。":"Agent 正在核对正文和记忆，并建议评分。");
  }));
  for(const button of document.querySelectorAll("[data-view]")){button.addEventListener("click",()=>setView(button.dataset.view));button.addEventListener("keydown",event=>{if(!["ArrowLeft","ArrowRight","Home","End"].includes(event.key))return;event.preventDefault();const tabs=[...document.querySelectorAll("[data-view]")],index=tabs.indexOf(button),next=event.key==="Home"?0:event.key==="End"?tabs.length-1:(index+(event.key==="ArrowRight"?1:-1)+tabs.length)%tabs.length;setView(tabs[next].dataset.view);tabs[next].focus();});}
  for(const button of document.querySelectorAll("[data-close]"))button.addEventListener("click",()=>$(button.dataset.close).close());
  $("run-dialog").addEventListener("close",()=>{$("custom-api-key").value="";});
  for(const id of ["new-novel","empty-create"])$(id).addEventListener("click",()=>{$("novel-form-error").hidden=true;$("novel-dialog").showModal();});
  $("configure").addEventListener("click",openRunDialog);
  $("budget-preset").addEventListener("change",()=>{
    const presets={standard:[150000,2000000,30],balanced:[300000,3000000,50],extended:[500000,5000000,80]};
    const values=presets[$("budget-preset").value];if(values)["chapter-tokens","run-tokens","max-calls"].forEach((id,index)=>$(id).value=values[index]);
  });
  for(const id of ["chapter-tokens","run-tokens","max-calls"])$(id).addEventListener("input",()=>$("budget-preset").value="custom");
  $("edit-budget").addEventListener("click",()=>action($("edit-budget"),async()=>{
    await loadBudget();const config=state.run.config;
    $("budget-current").textContent=budgetText(state.budget);$("edit-chapter-tokens").value=config.chapter_tokens;$("edit-run-tokens").value=config.run_tokens;$("edit-max-calls").value=config.max_calls;
    $("budget-form-error").hidden=true;$("budget-dialog").showModal();
  }));
  $("budget-form").addEventListener("submit",event=>{event.preventDefault();action($("save-budget"),async()=>{
    $("budget-form-error").hidden=true;
    const run=await api(`/runs/${state.run.id}/budget`,{chapter_tokens:Number($("edit-chapter-tokens").value),run_tokens:Number($("edit-run-tokens").value),max_calls:Number($("edit-max-calls").value)});
    updateRun(run);await loadBudget();$("budget-dialog").close();toast("预算已保存，历史用量未清空。可点击恢复创作继续。");
  },"budget-form-error");});
  $("dismiss-alert").addEventListener("click",()=>{$("alert").hidden=true;});
  $("novel-select").addEventListener("change",event=>selectNovel(event.target.value).catch(showError));
  $("run-select").addEventListener("change",event=>selectRun(event.target.value).catch(showError));
  $("refresh-runs").addEventListener("click",()=>action($("refresh-runs"),async()=>{state.bootstrap=await api("/bootstrap");renderNovelOptions();renderRunOptions();if(state.run)await refreshRun(true);else await selectNovel(state.novelId);if(state.run&&!state.stream)connectEvents();toast("创作记录已刷新。");}));
  $("provider").addEventListener("change",()=>{for(const input of $("role-models").querySelectorAll("input"))input.value="";updateProvider();});
  for(const radio of document.querySelectorAll('input[name="mode"]'))radio.addEventListener("change",updateMode);
  $("refresh-world").addEventListener("click",()=>action($("refresh-world"),refreshWorld));
  $("approval-form").addEventListener("input",updateApproval);
  $("approval-form").addEventListener("change",updateApproval);
  $("novel-form").addEventListener("submit",event=>{event.preventDefault();action($("create-novel"),async()=>{$("novel-form-error").hidden=true;const novel=await api("/novels",{title:$("novel-title").value.trim(),genre:$("novel-genre").value.trim(),synopsis:$("novel-synopsis").value.trim(),protagonist:$("novel-protagonist").value.trim(),companions:lines($("novel-companions").value),world_rules:lines($("novel-rules").value)});state.bootstrap.novels.unshift(novel);await selectNovel(novel.id);$("novel-dialog").close();toast("故事与世界已创建。现在可以开始连续章节创作。");openRunDialog();},"novel-form-error");});
  $("run-form").addEventListener("submit",event=>{event.preventDefault();action($("start-run"),async()=>{
    $("run-form-error").hidden=true;
    const roleModels={};for(const input of $("role-models").querySelectorAll("input"))if(input.value.trim())roleModels[input.dataset.role]=input.value.trim();
    const mode=document.querySelector('input[name="mode"]:checked').value,custom=mode==="live"&&$("provider").value==="custom_input";
    if(mode==="live"&&!custom&&!state.bootstrap.providers.find(p=>p.id===$("provider").value)?.configured)throw new Error("请先在服务器环境配置所选服务的 API Key，或选择自定义服务直接填写。");
    const payload={novel_id:state.novelId,mode,provider:mode==="live"?$("provider").value:"",model:mode==="live"?$("model").value.trim():"",role_models:mode==="live"?roleModels:{},chapter_count:Number($("chapter-count").value),target_chars:Number($("target-chars").value),direction:$("direction").value,chapter_directions:$("chapter-directions").value.trim()?$("chapter-directions").value.split(/\r?\n/).map(s=>s.trim()):[],replace_existing:$("replace-existing").checked};
    if(mode==="live")Object.assign(payload,{chapter_tokens:Number($("chapter-tokens").value),run_tokens:Number($("run-tokens").value),max_calls:Number($("max-calls").value)});
    if(mode==="live"&&$("max-output-tokens").value)payload.max_output_tokens=Number($("max-output-tokens").value);
    if(custom){payload.custom_base_url=$("custom-base-url").value.trim();payload.api_key=$("custom-api-key").value.trim();}
    const run=await api("/runs",payload);$("custom-api-key").value="";if(payload.replace_existing)state.bootstrap=await api("/bootstrap");updateRun(run);$("run-dialog").close();await selectRun(run.id);setView("workflow");toast(mode==="demo"?"离线脚本演示已启动；它不代表模型生成质量。":"真实模型创作已启动，调用记录会实时显示。");
  },"run-form-error");});
  $("cancel").addEventListener("click",()=>action($("cancel"),async()=>{const run=await api(`/runs/${state.run.id}/cancel`,{});updateRun(run);closeStream();await loadChapter(true);toast("运行已停止，已完成的阶段已保留。");}));
  $("finish-run").addEventListener("click",()=>action($("finish-run"),async()=>{
    const run=await api(`/runs/${state.run.id}/finish`,{});updateRun(run);closeStream();$("alert").hidden=true;await loadChapter(true);toast("此记录已结束，正文已保留。现在可以新建创作记录。");
  }));
  $("replace-existing").addEventListener("change",()=>{$("start-run").textContent=$("replace-existing").checked?"结束旧记录并新建 →":"开始创作 →";});
  $("resume").addEventListener("click",()=>action($("resume"),async()=>{$("alert").hidden=true;await ensureCustomCredentials();const run=await api(`/runs/${state.run.id}/resume`,{});updateRun(run);await loadChapter(true);connectEvents();toast("已从保存的阶段恢复创作。");}));
  $("recheck-draft").addEventListener("click",()=>action($("recheck-draft"),async()=>{await ensureCustomCredentials();const run=await api(`/runs/${state.run.id}/chapters/${state.chapterNumber}/recheck`,{max_output_tokens:Number($("recheck-output").value)});updateRun(run);await loadChapter(true);connectEvents();toast("已保留正文，正在重新检查并抽取记忆。");}));
  $("save-revision").addEventListener("click",()=>action($("save-revision"),async()=>{const replacements=Object.fromEntries(state.edits);if(Object.values(replacements).some(text=>!text.trim()))throw new Error("场景正文不能留空。");await ensureCustomCredentials();const run=await api(`/runs/${state.run.id}/chapters/${state.chapterNumber}/revise`,{replacements});state.edits.clear();updateRun(run);await loadChapter(true);connectEvents();toast("修订已保存为新候选，正在重新检查。");}));
  $("save-memory").addEventListener("click",()=>action($("save-memory"),async()=>{let batch;try{batch=JSON.parse($("memory-json").value);}catch{throw new Error("记忆 JSON 格式不正确，请检查括号、逗号与引号。");}const run=await api(`/runs/${state.run.id}/chapters/${state.chapterNumber}/memory`,{batch});updateRun(run);await loadChapter(true);connectEvents();toast("记忆修改已另存为新候选，正在重新校验。");}));
  retry.addEventListener("click",()=>action(retry,async()=>{await ensureCustomCredentials();const run=await api(`/runs/${state.run.id}/chapters/${state.chapterNumber}/memory-retry`,{});updateRun(run);await loadChapter(true);connectEvents();toast("正在对同一稿件重新抽取记忆。");}));
  $("approval-form").addEventListener("submit",event=>{event.preventDefault();if($("approve").disabled)return;action($("approve"),async()=>{
    if(state.chapterNumber<state.run.start_chapter+state.run.total_chapters-1)await ensureCustomCredentials();
    const detail=state.detail,scores={};for(const select of $("quality-scores").querySelectorAll("select"))scores[select.dataset.score]=Number(select.value);
    const run=await api(`/runs/${state.run.id}/chapters/${state.chapterNumber}/approve`,{attempt_id:detail.attempt_id,preview_package_hash:detail.package.preview_package_hash,reviewed_by:$("reviewed-by").value.trim(),operation_reviews:Object.fromEntries(state.operationReviews),missing:lines($("missing-memory").value),quality_scores:scores,semantic_review_complete:$("semantic-reviewed").checked,approved:true,assisted_review_hash:state.assistedReviewHash||null});
    const chapter=state.chapterNumber;const novel=selectedNovel();if(novel)novel.current_chapter_number=Math.max(novel.current_chapter_number||0,chapter);updateRun(run);await loadChapter(true);await refreshWorld();if(run.status==="running"){state.chapterNumber=run.current_chapter;state.detailKey="";await loadChapter();renderRun();connectEvents();}toast(`第 ${chapter} 章已正式提交${run.status==="completed"?"，可点击继续创作下一章。":"，正在继续下一章。"}`);
  });});
  $("locate-review-issue").addEventListener("click",()=>{
    const op=state.detail?.package?.memory_result?.batch?.operations?.find(item=>state.operationReviews.get(item.operation_id)!=="correct");
    const card=[...$("memory-operations").querySelectorAll("[data-operation-id]")].find(el=>el.dataset.operationId===op?.operation_id);
    if(card){card.scrollIntoView({behavior:"smooth",block:"center"});card.querySelector("select")?.focus({preventScroll:true});}
  });
  $("export").addEventListener("click",()=>{if(state.run){const anchor=element("a");anchor.href=`${API}/runs/${state.run.id}/export`;anchor.download=`${state.run.title}.txt`;document.body.append(anchor);anchor.click();anchor.remove();}});
  window.addEventListener("beforeunload",()=>closeStream());
  refreshBootstrap().catch(error=>{showError(error);$("connection-label").textContent="连接失败 · 请检查服务";$("connection-dot").classList.remove("online");});
})();
