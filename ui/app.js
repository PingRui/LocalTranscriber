(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  const basename = (path) => String(path || "").split(/[\\/]/).filter(Boolean).pop() || path || "";
  const state = { snapshot: null, activeView: "generation", busy: new Set(), playerCitation: null };
  let toastSignature = "";
  let toastTime = 0;
  let uiBound = false;
  let bootPromise = null;

  function currentDesktopApi() {
    const api = window.pywebview?.api;
    return api && typeof api === "object" ? api : null;
  }

  async function waitForDesktopApi(timeoutMs = 5000) {
    const ready = currentDesktopApi();
    if (ready) return ready;
    const startedAt = Date.now();
    return new Promise((resolve, reject) => {
      let timer = 0;
      const finish = (api) => {
        window.clearInterval(timer);
        window.removeEventListener("pywebviewready", check);
        if (api) resolve(api);
        else reject(new Error("桌面服务尚未就绪，请关闭当前预览页，并从桌面快捷方式重新打开软件。"));
      };
      const check = () => {
        const api = currentDesktopApi();
        if (api) finish(api);
        else if (Date.now() - startedAt >= timeoutMs) finish(null);
      };
      window.addEventListener("pywebviewready", check);
      timer = window.setInterval(check, 100);
      check();
    });
  }

  function icons() {
    if (window.lucide) window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
  }

  async function callApi(method, ...args) {
    const api = await waitForDesktopApi();
    if (typeof api[method] !== "function") throw new Error(`桌面服务缺少 ${method} 功能，请重新打开软件。`);
    const result = await api[method](...args);
    if (result?.snapshot) applySnapshot(result.snapshot);
    if (result?.message) toast(result.message, "success");
    if (result?.ok === false) throw new Error(result.error || "操作失败");
    return result;
  }

  async function action(key, fn) {
    if (state.busy.has(key)) return;
    state.busy.add(key);
    renderBusy();
    try { return await fn(); }
    catch (error) { toast(error?.message || String(error), "error"); }
    finally { state.busy.delete(key); renderBusy(); }
  }

  function toast(message, type = "info") {
    if (!message) return;
    const signature = `${type}:${message}`;
    const now = Date.now();
    if (signature === toastSignature && now - toastTime < 1800) return;
    toastSignature = signature;
    toastTime = now;
    const item = document.createElement("div");
    item.className = `toast ${type}`;
    item.textContent = message;
    $("toastStack").appendChild(item);
    window.setTimeout(() => item.remove(), 3800);
  }

  function applySnapshot(snapshot) {
    state.snapshot = snapshot;
    renderAll();
  }

  function renderAll() {
    if (!state.snapshot) return;
    renderSpace();
    renderQueue();
    renderTask();
    renderSettings();
    renderChat();
    renderBusy();
    icons();
  }

  function renderSpace() {
    const { space, recent_spaces: recent = [], ai } = state.snapshot;
    $("spaceName").textContent = space.ready ? space.name : "选择知识空间";
    $("chatSpaceName").textContent = space.ready ? space.name : "尚未选择知识空间";
    $("knowledgeCountHint").textContent = `${space.knowledge_count || 0} 个知识点`;
    $("settingsStatusDot").classList.toggle("ready", Boolean(ai.verified));
    $("recentSpaces").innerHTML = recent.length ? recent.map((path) => `
      <div class="recent-item">
        <button class="recent-open" data-space-path="${escapeHtml(path)}">
          <strong>${escapeHtml(basename(path))}</strong><span>${escapeHtml(path)}</span>
        </button>
        <button class="recent-remove" data-remove-space="${escapeHtml(path)}" title="从最近列表移除，不删除磁盘文件" aria-label="移除 ${escapeHtml(basename(path))}">
          <i data-lucide="x"></i>
        </button>
      </div>`).join("") : `<div class="activity-empty">还没有使用过的目录</div>`;
    document.querySelectorAll("[data-space-path]").forEach((button) => {
      button.addEventListener("click", () => action("space", () => callApi("open_space", button.dataset.spacePath)));
    });
    document.querySelectorAll("[data-remove-space]").forEach((button) => {
      button.addEventListener("click", () => action("space", () => callApi("remove_recent_space", button.dataset.removeSpace)));
    });
  }

  function renderQueue() {
    const { queue, task, running, ai, space } = state.snapshot;
    const showTask = Boolean(task);
    $("taskPanel").classList.toggle("hidden", !showTask);
    $("queuePanel").classList.toggle("hidden", showTask || !queue.length);
    $("emptyGeneration").classList.toggle("hidden", showTask || Boolean(queue.length));
    $("queueSummary").textContent = `${queue.length} 个视频，开始前可以继续添加或移除`;
    $("queueList").innerHTML = queue.map((item) => `
      <div class="queue-item">
        <span class="file-icon"><i data-lucide="file-video-2"></i></span>
        <span class="queue-copy"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.source)}</span></span>
        <span class="queue-size">${escapeHtml(item.size || "")}</span>
        <button class="remove-queue" data-remove-source="${escapeHtml(item.source)}" title="移除"><i data-lucide="x"></i></button>
      </div>`).join("");
    document.querySelectorAll("[data-remove-source]").forEach((button) => {
      button.addEventListener("click", () => action("queue", () => callApi("remove_video", button.dataset.removeSource)));
    });
    const ready = Boolean(ai.verified);
    $("readinessCard").classList.toggle("ready", ready);
    $("readinessTitle").textContent = ready ? `AI 服务已验证 · ${ai.model}` : "AI 服务尚未配置";
    $("readinessText").textContent = ready ? "可进行专业词汇判断、可信校对和知识回答。" : "完成连接测试后才能开始。";
    $("openSettingsInline").classList.toggle("hidden", ready);
    $("startTaskButton").disabled = running || !queue.length || !ready || !space.ready;
  }

  function renderTask() {
    const { task, running, paused, activities = [] } = state.snapshot;
    if (!task) return;
    $("currentVideoName").textContent = task.current_video || "准备任务";
    $("taskStatusText").textContent = task.status_text || "";
    const videos = Array.isArray(task.videos) ? task.videos : [];
    const completedVideos = videos.filter((item) => item.status === "completed").length;
    const waitingVideos = videos.filter((item) => ["waiting", "interrupted"].includes(item.status)).length;
    $("taskQueueSummary").textContent = `本任务共 ${videos.length} 个视频 · 已完成 ${completedVideos} · 待处理 ${waitingVideos}`;
    const progress = Math.max(0, Math.min(100, Number(task.overall_progress || 0)));
    $("overallProgress").textContent = `${Math.round(progress)}%`;
    $("overallProgressBar").style.width = `${progress}%`;
    const statusLabels = {
      running: "正在处理",
      completed: "处理完成",
      failed: "处理失败",
      interrupted: "等待继续",
      needs_attention: "等待确认",
      cancelled: "任务已取消",
    };
    $("taskStatusLabel").textContent = paused ? "任务已暂停" : (statusLabels[task.status] || task.status || "准备任务");
    $("taskStages").innerHTML = (task.stages || []).map((stage) => {
      const icon = ["completed", "skipped"].includes(stage.status) ? "check" : stage.status === "failed" ? "x" : stage.status === "running" ? "loader-circle" : stage.status === "needs_confirmation" ? "circle-help" : "circle";
      return `<div class="stage-row ${escapeHtml(stage.status)}">
        <span class="stage-dot"><i data-lucide="${icon}"></i></span>
        <span class="stage-copy"><strong>${escapeHtml(stage.label)}</strong><span>${escapeHtml(stage.message || stageStatusText(stage.status))}</span></span>
        <span class="stage-percent">${stage.progress ? `${Math.round(stage.progress)}%` : ""}</span>
      </div>`;
    }).join("");
    $("activityList").innerHTML = activities.length ? activities.slice().reverse().map((item) => `
      <div class="activity-item ${escapeHtml(item.level)}"><time>${escapeHtml(item.time)}</time><span>${escapeHtml(item.text)}</span></div>`).join("") : `<div class="activity-empty">状态会在这里实时显示</div>`;

    const needsHotwords = task.current_stage === "confirm" && task.hotword_profile;
    $("hotwordConfirmCard").classList.toggle("hidden", !needsHotwords);
    if (needsHotwords) {
      const input = $("hotwordInput");
      const words = (task.hotword_profile.hotwords || []).join("，");
      if (input.dataset.profile !== words) { input.value = words; input.dataset.profile = words; }
      const category = task.hotword_profile.category || "未分类";
      const categoryInput = $("hotwordCategory");
      if (categoryInput.dataset.profile !== category) { categoryInput.value = category; categoryInput.dataset.profile = category; }
      $("hotwordReason").textContent = (task.hotword_profile.manual_reasons || []).join("；") || "自动判断条件不足，请确认后继续。";
    }
    const done = task.status === "completed";
    const resumable = ["failed", "interrupted", "cancelled"].includes(task.status);
    $("runningActions").classList.toggle("hidden", !running);
    $("failedActions").classList.toggle("hidden", !resumable);
    $("completedActions").classList.toggle("hidden", !done);
    $("pauseButton").classList.toggle("hidden", paused);
    $("resumeButton").classList.toggle("hidden", !paused);
    $("appendVideosButton").classList.toggle("hidden", !paused);
    $("appendFolderButton").classList.toggle("hidden", !paused);
  }

  function stageStatusText(status) {
    return ({ waiting: "等待执行", running: "正在执行", completed: "已完成", skipped: "已从断点复用", failed: "需要处理", needs_confirmation: "等待你的确认" })[status] || "";
  }

  function renderSettings() {
    const { ai, runtime } = state.snapshot;
    if (document.activeElement !== $("aiBaseUrl")) $("aiBaseUrl").value = ai.base_url || "";
    if (document.activeElement !== $("aiModel")) $("aiModel").value = ai.model || "";
    if (document.activeElement !== $("aiContextWindow")) $("aiContextWindow").value = ai.context_window || 128000;
    const badge = $("aiVerifiedBadge");
    badge.textContent = ai.verified ? "已验证" : "未验证";
    badge.classList.toggle("ready", Boolean(ai.verified));
    const modelSelect = $("runtimeModel");
    const modelOptions = Array.isArray(runtime.model_options) && runtime.model_options.length
      ? runtime.model_options
      : [{ value: "medium", label: "Medium" }, { value: "large-v3-turbo", label: "Large-v3 Turbo" }];
    const optionSignature = modelOptions.map((item) => `${item.value}:${item.label}`).join("|");
    if (modelSelect.dataset.options !== optionSignature) {
      modelSelect.innerHTML = modelOptions.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("");
      modelSelect.dataset.options = optionSignature;
    }
    const requestedModel = String(runtime.model || "");
    modelSelect.value = modelOptions.some((item) => item.value === requestedModel) ? requestedModel : modelOptions[0].value;
    $("runtimeDevice").value = runtime.device;
    $("runtimeLanguage").value = runtime.language;
    $("tempPolicy").value = "manual";
  }

  function renderChat() {
    const { chat, space } = state.snapshot;
    const messages = chat.messages || [];
    $("chatEmpty").classList.toggle("hidden", Boolean(messages.length));
    $("messageList").innerHTML = messages.map(renderMessage).join("") + (chat.running ? renderLoadingMessage() : "");
    $("sendKnowledgeButton").disabled = chat.running || !space.ready || !(space.knowledge_count > 0);
    document.querySelectorAll("[data-citation-index]").forEach((button) => {
      button.addEventListener("click", () => playCitation(Number(button.dataset.messageIndex), Number(button.dataset.citationIndex)));
    });
    document.querySelectorAll("[data-relink-video]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        action("relink", () => callApi("relink_missing_video", button.dataset.relinkVideo));
      });
    });
    const thread = $("chatThread");
    requestAnimationFrame(() => { thread.scrollTop = thread.scrollHeight; });
  }

  function renderMessage(message, messageIndex) {
    if (message.role === "user") return `<div class="message user"><div class="user-bubble">${escapeHtml(message.content)}</div></div>`;
    const citations = (message.citations || []).map((citation, citationIndex) => `
      <button class="citation" data-message-index="${messageIndex}" data-citation-index="${citationIndex}">
        <span class="citation-icon"><i data-lucide="play"></i></span>
        <span class="citation-copy"><strong>${escapeHtml(citation.title || "视频证据")}</strong><span>${escapeHtml(basename(citation.video_path))}</span></span>
        ${citation.video_available === false
          ? `<span class="text-button" data-relink-video="${escapeHtml(citation.video_id)}">重新关联</span>`
          : `<span class="citation-time">${formatTime(citation.evidence_start)}</span>`}
      </button>`).join("");
    return `<div class="message assistant"><div class="assistant-message">
      <span class="assistant-avatar">知</span>
      <div class="assistant-body"><div class="assistant-answer">${escapeHtml(message.content)}</div>
      ${citations ? `<div class="citation-list">${citations}</div>` : ""}
      <div class="answer-meta">${message.error ? "未完成检索" : `${(message.citations || []).length} 条可核对证据`}</div></div>
    </div></div>`;
  }

  function renderLoadingMessage() {
    return `<div class="message assistant"><div class="assistant-message"><span class="assistant-avatar">知</span><div class="assistant-body"><div class="assistant-answer loading">正在检索当前目录，并整理有证据的回答…</div></div></div></div>`;
  }

  function formatTime(seconds) {
    const value = Math.max(0, Math.round(Number(seconds || 0)));
    const h = Math.floor(value / 3600);
    const m = Math.floor((value % 3600) / 60);
    const s = value % 60;
    return h ? `${h}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}` : `${m}:${String(s).padStart(2,"0")}`;
  }

  async function playCitation(messageIndex, citationIndex) {
    const citation = state.snapshot?.chat?.messages?.[messageIndex]?.citations?.[citationIndex];
    if (!citation) return;
    if (citation.video_available === false) {
      await action("relink", () => callApi("relink_missing_video", citation.video_id));
      return;
    }
    await action("video", async () => {
      const result = await callApi("get_video_source", citation.video_path, citation.evidence_start || 0);
      if (!result?.uri) return;
      const video = $("videoPlayer");
      $("playerTitle").textContent = basename(citation.video_path);
      $("playerMeta").textContent = `${citation.title || "视频证据"} · ${formatTime(citation.evidence_start)} — ${formatTime(citation.evidence_end)}`;
      $("evidencePlayer").classList.remove("hidden");
      video.src = result.uri;
      video.onloadedmetadata = () => { video.currentTime = Number(result.start || 0); video.play().catch(() => {}); };
    });
  }

  function renderBusy() {
    const mapping = {
      space: ["spaceButton", "changeChatSpaceButton", "chooseSpacePromptButton"],
      videos: ["chooseVideosButton", "addVideosButton", "appendVideosButton"], folder: ["chooseVideoFolderButton", "addFolderButton", "appendFolderButton"],
      start: ["startTaskButton"], ai: ["testAiButton"], runtime: ["saveRuntimeButton"], chat: ["sendKnowledgeButton"]
    };
    Object.entries(mapping).forEach(([key, ids]) => ids.forEach((id) => { if ($(id)) $(id).disabled = state.busy.has(key); }));
  }

  function switchView(view) {
    state.activeView = view;
    $("knowledgeGenerationView").classList.toggle("active-view", view === "generation");
    $("knowledgeChatView").classList.toggle("active-view", view === "chat");
    $("navGenerate").classList.toggle("active", view === "generation");
    $("navChat").classList.toggle("active", view === "chat");
    if (view === "chat") setTimeout(() => $("knowledgeComposer").focus(), 50);
  }

  function chooseSpace() {
    if (!state.snapshot?.space?.ready) $("spacePrompt").classList.remove("hidden");
    else action("space", () => callApi("choose_space"));
  }

  function openSettings() {
    $("settingsModal").classList.remove("hidden");
    $("aiTestResult").classList.add("hidden");
    setTimeout(() => $("aiBaseUrl").focus(), 40);
  }

  function sendQuestion(prompt = "") {
    const input = $("knowledgeComposer");
    const question = String(prompt || input.value || "").trim();
    if (!question) return;
    if (!state.snapshot?.space?.ready) { chooseSpace(); return; }
    input.value = "";
    resizeComposer();
    action("chat", () => callApi("ask_knowledge", question));
  }

  function resizeComposer() {
    const input = $("knowledgeComposer");
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 130)}px`;
  }

  function bind() {
    $("navGenerate").addEventListener("click", () => switchView("generation"));
    $("navChat").addEventListener("click", () => switchView("chat"));
    $("goChatButton").addEventListener("click", () => switchView("chat"));
    $("spaceButton").addEventListener("click", chooseSpace);
    $("changeChatSpaceButton").addEventListener("click", () => action("space", () => callApi("choose_space")));
    $("chooseVideosButton").addEventListener("click", () => action("videos", () => callApi("choose_videos")));
    $("addVideosButton").addEventListener("click", () => action("videos", () => callApi("choose_videos")));
    $("appendVideosButton").addEventListener("click", () => action("videos", () => callApi("choose_videos")));
    $("chooseVideoFolderButton").addEventListener("click", () => action("folder", () => callApi("choose_video_folder")));
    $("addFolderButton").addEventListener("click", () => action("folder", () => callApi("choose_video_folder")));
    $("appendFolderButton").addEventListener("click", () => action("folder", () => callApi("choose_video_folder")));
    $("clearQueueButton").addEventListener("click", () => action("queue", () => callApi("clear_queue")));
    $("startTaskButton").addEventListener("click", () => {
      if (!state.snapshot?.space?.ready) { chooseSpace(); return; }
      action("start", () => callApi("start_knowledge_task"));
    });
    $("confirmHotwordsButton").addEventListener("click", () => action("hotwords", () => callApi("confirm_hotwords", $("hotwordInput").value, $("hotwordCategory").value)));
    $("pauseButton").addEventListener("click", () => action("task", () => callApi("pause_task")));
    $("resumeButton").addEventListener("click", () => action("task", () => callApi("resume_task")));
    $("cancelButton").addEventListener("click", () => {
      if (window.confirm("停止当前任务？已经完成的知识成果会保留。")) action("task", () => callApi("cancel_task"));
    });
    $("retryButton").addEventListener("click", () => action("task", () => callApi("continue_knowledge_task")));
    $("cleanupButton").addEventListener("click", () => {
      if (window.confirm("确认已验收结果，并删除本次任务的临时转录与校对文件？视频、索引和 Obsidian 知识库不会删除。")) action("cleanup", () => callApi("cleanup_task"));
    });
    $("settingsButton").addEventListener("click", openSettings);
    $("openSettingsInline").addEventListener("click", openSettings);
    $("closeSettingsButton").addEventListener("click", () => $("settingsModal").classList.add("hidden"));
    $("settingsModal").addEventListener("click", (event) => { if (event.target === $("settingsModal")) $("settingsModal").classList.add("hidden"); });
    $("testAiButton").addEventListener("click", () => action("ai", async () => {
      const resultBox = $("aiTestResult");
      resultBox.className = "inline-result";
      resultBox.textContent = "正在连接并发送最小测试请求…";
      const result = await callApi("test_and_save_ai", $("aiBaseUrl").value, $("aiModel").value, $("aiApiKey").value, $("aiContextWindow").value);
      $("aiApiKey").value = "";
      resultBox.className = "inline-result success";
      resultBox.textContent = result.message || "连接成功，设置已保存。";
    }));
    $("saveRuntimeButton").addEventListener("click", () => action("runtime", () => callApi("save_runtime_settings", $("runtimeModel").value, $("runtimeDevice").value, $("runtimeLanguage").value, $("tempPolicy").value)));
    $("toggleApiKey").addEventListener("click", () => { $("aiApiKey").type = $("aiApiKey").type === "password" ? "text" : "password"; });
    $("chooseSpacePromptButton").addEventListener("click", () => action("space", async () => { await callApi("choose_space"); $("spacePrompt").classList.add("hidden"); }));
    $("cancelSpacePromptButton").addEventListener("click", () => $("spacePrompt").classList.add("hidden"));
    $("sendKnowledgeButton").addEventListener("click", () => sendQuestion());
    $("knowledgeComposer").addEventListener("input", resizeComposer);
    $("knowledgeComposer").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendQuestion(); } });
    document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => sendQuestion(button.dataset.prompt)));
    $("clearChatButton").addEventListener("click", () => action("chat-clear", () => callApi("clear_chat")));
    $("closePlayerButton").addEventListener("click", () => { $("videoPlayer").pause(); $("videoPlayer").removeAttribute("src"); $("evidencePlayer").classList.add("hidden"); });
  }

  window.LocalTranscriber = {
    receive(payload) {
      if (payload?.snapshot) applySnapshot(payload.snapshot);
      if (payload?.message && ["task_done", "task_error"].includes(payload.type)) toast(payload.message, payload.level === "error" ? "error" : "success");
    }
  };

  async function boot() {
    if (bootPromise) return bootPromise;
    initializeUi();
    bootPromise = (async () => {
      try {
        const result = await callApi("bootstrap");
        if (!result?.snapshot) throw new Error("无法读取应用状态");
      } catch (error) {
        toast(error?.message || String(error), "error");
        window.setTimeout(() => { bootPromise = null; boot(); }, 1200);
      }
    })();
    return bootPromise;
  }

  function initializeUi() {
    if (uiBound) return;
    uiBound = true;
    bind();
    icons();
  }

  initializeUi();
  window.addEventListener("pywebviewready", boot, { once: true });
  if (currentDesktopApi()) boot();
})();
