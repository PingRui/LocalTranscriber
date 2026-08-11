(() => {
  const SETTINGS_KEY = "localtranscriber.settings.v2";
  const PROCESSING_STATUSES = new Set(["等待中", "模型加载中", "转写中", "已暂停", "大模型校订中"]);
  const COMPLETED_STATUSES = new Set(["已完成", "校订完成", "已有结果"]);

  const state = {
    files: [],
    history: [],
    running: false,
    paused: false,
    scanning: false,
    statusText: "请添加需要转写的文件",
    progress: 0,
    logs: [],
    resultDirs: {},
    outputPath: "",
    sourceUrls: {},
    historyFilter: "all",
    historyQuery: "",
    currentSource: "",
    currentMaterial: "accurate",
    currentViewMode: "preview",
    currentContent: "",
  };

  const elements = {};
  const byId = (id) => document.getElementById(id);

  function cacheElements() {
    [
      "navUpload", "navContent", "recentList", "settingsButton", "uploadView", "contentView",
      "dropzone", "selectFilesButton", "selectFolderButton", "queueList", "queueEmpty", "fileCount",
      "addMoreButton", "clearButton", "runProgress", "statusText", "progressText", "progressFill",
      "pauseButton", "cancelButton", "startButton", "historyView", "detailView", "historySearch",
      "historyFilters", "historyList", "backToHistoryButton", "detailTitle", "detailStatus",
      "detailProgress", "detailProgressStatus", "detailProgressText", "detailProgressFill", "materialTabs",
      "viewSwitch", "markdownPreview", "markdownSource", "copyResultButton", "openResultButton",
      "settingsOverlay", "closeSettingsButton", "saveSettingsButton", "languageSelect", "deviceSelect",
      "llmRepair", "llmKeyPanel", "deepseekApiKey", "pathControl", "outputPath", "chooseOutputButton",
      "modelStatus", "logHint", "logOutput", "modelSelect", "contextMode", "skipExisting",
      "autoStartFolder", "promptInput", "toastRegion",
    ].forEach((id) => { elements[id] = byId(id); });
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function statusClass(status) {
    if (COMPLETED_STATUSES.has(status)) return "success";
    if (PROCESSING_STATUSES.has(status)) return "processing";
    if (status === "失败") return "error";
    if (["已取消"].includes(status)) return "warning";
    return "";
  }

  function refreshIcons() {
    if (window.lucide?.createIcons) {
      window.lucide.createIcons({ attrs: { "stroke-width": 2 } });
    }
  }

  function formatDate(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).format(date);
  }

  function currentHistoryItem() {
    return state.history.find((item) => item.path === state.currentSource)
      || state.files.find((item) => item.path === state.currentSource)
      || null;
  }

  function renderQueue() {
    const locked = state.running || state.scanning;
    const known = new Set(state.files.map((file) => file.path));
    Object.keys(state.sourceUrls).forEach((path) => {
      if (!known.has(path)) delete state.sourceUrls[path];
    });

    elements.fileCount.textContent = `${state.files.length} 个`;
    elements.queueEmpty.classList.toggle("hidden", state.files.length > 0);
    elements.clearButton.disabled = locked || state.files.length === 0;
    elements.addMoreButton.disabled = locked;

    const rows = state.files.map((file) => {
      const progress = Math.max(0, Math.min(100, Number(file.progress || 0)));
      const url = state.sourceUrls[file.path] || file.source_url || "";
      const isAudio = [".wav", ".mp3", ".m4a", ".aac", ".flac"].some((suffix) => file.name.toLowerCase().endsWith(suffix));
      const progressBar = PROCESSING_STATUSES.has(file.status)
        ? `<div class="mini-progress"><span style="width:${Math.max(2, progress)}%"></span></div>`
        : "";
      const inlineSource = url ? "" : `
        <details class="inline-source-details">
          <summary><i data-lucide="link" class="lucide-icon source-link-icon" aria-hidden="true"></i><span>补充来源（可选）</span></summary>
          <div class="inline-source-popover">
            <input data-action="source-url" type="url" value="" placeholder="粘贴 YouTube、Bilibili、抖音等视频页面地址" ${locked ? "disabled" : ""}>
          </div>
        </details>`;
      const expandedSource = url ? `
        <details class="source-details" open>
          <summary><i data-lucide="chevron-right" class="lucide-icon source-chevron" aria-hidden="true"></i><span>补充视频来源（可选）</span></summary>
          <div class="source-control">
            <i data-lucide="link" class="lucide-icon source-link-icon" aria-hidden="true"></i>
            <input data-action="source-url" type="url" value="${escapeHtml(url)}" placeholder="粘贴 YouTube、Bilibili、抖音等视频页面地址" ${locked ? "disabled" : ""}>
          </div>
        </details>` : "";
      return `
        <article class="queue-item ${PROCESSING_STATUSES.has(file.status) ? "running" : ""} ${url ? "has-source" : ""}" data-path="${escapeHtml(file.path)}">
          <div class="queue-item-main">
            <i data-lucide="${isAudio ? "mic" : "video"}" class="lucide-icon file-type-icon" aria-hidden="true"></i>
            <div class="file-copy">
              <div class="file-name" title="${escapeHtml(file.path)}">${escapeHtml(file.name)}</div>
              <div class="file-meta">${escapeHtml(file.size_label || file.folder || "")}</div>
            </div>
            ${progressBar}
            ${inlineSource}
            <span class="status-pill ${statusClass(file.status)}">${escapeHtml(file.status || "等待中")}</span>
            <button class="remove-file-button" data-action="remove" type="button" aria-label="移除 ${escapeHtml(file.name)}" ${locked ? "disabled" : ""}><i data-lucide="trash-2" class="lucide-icon icon-16" aria-hidden="true"></i></button>
          </div>
          ${expandedSource}
        </article>`;
    }).join("");

    elements.queueList.querySelectorAll(".queue-item").forEach((item) => item.remove());
    if (rows) elements.queueList.insertAdjacentHTML("beforeend", rows);
    refreshIcons();
    renderRunControls();
  }

  function renderRunControls() {
    const locked = state.running || state.scanning;
    const hasFiles = state.files.length > 0;
    const showProgress = locked || state.progress > 0;
    const progress = Math.max(0, Math.min(100, Number(state.progress || 0)));

    elements.selectFilesButton.disabled = locked;
    elements.selectFolderButton.disabled = locked;
    elements.startButton.disabled = locked || !hasFiles;
    elements.startButton.classList.toggle("hidden", state.running || state.scanning);
    elements.startButton.textContent = hasFiles ? `开始任务（${state.files.length}）` : "开始任务";
    elements.pauseButton.classList.toggle("hidden", !state.running);
    elements.cancelButton.classList.toggle("hidden", !state.running);
    elements.pauseButton.textContent = state.paused ? "继续" : "暂停";
    elements.runProgress.classList.toggle("hidden", !showProgress);
    elements.statusText.textContent = state.statusText;
    elements.progressText.textContent = state.scanning ? "扫描中" : state.paused ? "已暂停" : `${Math.round(progress)}%`;
    elements.progressFill.style.width = `${progress}%`;
  }

  function historyMatchesFilter(item) {
    if (state.historyFilter === "processing") return PROCESSING_STATUSES.has(item.status);
    if (state.historyFilter === "completed") return COMPLETED_STATUSES.has(item.status);
    if (state.historyFilter === "failed") return item.status === "失败";
    return true;
  }

  function renderHistory() {
    const query = state.historyQuery.trim().toLowerCase();
    const items = state.history.filter((item) => {
      const matchesSearch = !query || `${item.name} ${item.path}`.toLowerCase().includes(query);
      return matchesSearch && historyMatchesFilter(item);
    });

    elements.historyList.innerHTML = items.length ? items.map((item) => {
      const progress = Math.max(0, Math.min(100, Number(item.progress || 0)));
      const isAudio = String(item.media_type || "").toLowerCase() === "audio";
      const progressBlock = PROCESSING_STATUSES.has(item.status)
        ? `<div class="history-progress"><span class="status-pill ${statusClass(item.status)}">${escapeHtml(item.status)}</span><div class="mini-progress"><span style="width:${Math.max(2, progress)}%"></span></div></div>`
        : `<div class="history-progress"><span class="status-pill ${statusClass(item.status)}">${escapeHtml(item.status || "等待中")}</span></div>`;
      return `
        <button class="history-item" data-source="${escapeHtml(item.path)}" type="button">
          <span class="history-main">
            <i data-lucide="${isAudio ? "mic" : "video"}" class="lucide-icon file-type-icon" aria-hidden="true"></i>
            <span class="history-copy">
              <span class="history-name">${escapeHtml(item.name)}</span>
              <span class="history-meta">${escapeHtml(item.size_label || item.folder || "")}</span>
            </span>
          </span>
          ${progressBlock}
          <span class="history-date">${escapeHtml(formatDate(item.updated_at || item.created_at))}</span>
          <span class="history-arrow">›</span>
        </button>`;
    }).join("") : '<div class="content-empty">没有符合条件的记录</div>';

    elements.recentList.innerHTML = state.history.length ? state.history.slice(0, 8).map((item) => `
      <button class="recent-item" data-source="${escapeHtml(item.path)}" type="button">
        <i data-lucide="${item.media_type === "audio" ? "mic" : "video"}" class="lucide-icon recent-media-icon" aria-hidden="true"></i>
        <span>${escapeHtml(item.name)}</span>
      </button>`).join("") : '<p class="recent-empty">暂无记录</p>';
    refreshIcons();
  }

  function renderDetail() {
    const item = currentHistoryItem();
    if (!item) return;
    const progress = Math.max(0, Math.min(100, Number(item.progress || 0)));
    elements.detailTitle.textContent = item.name;
    elements.detailStatus.textContent = item.status || "等待中";
    elements.detailStatus.className = `status-pill ${statusClass(item.status)}`;
    const processing = PROCESSING_STATUSES.has(item.status);
    elements.detailProgress.classList.toggle("hidden", !processing);
    elements.detailProgressStatus.textContent = item.status || state.statusText;
    elements.detailProgressText.textContent = `${Math.round(progress)}%`;
    elements.detailProgressFill.style.width = `${progress}%`;
  }

  function renderLogs() {
    if (!state.logs.length) {
      elements.logOutput.textContent = "等待开始转写…";
      elements.logHint.textContent = "暂无日志";
    } else {
      elements.logOutput.textContent = state.logs.join("\n");
      elements.logHint.textContent = `最近 ${state.logs.length} 条`;
      elements.logOutput.scrollTop = elements.logOutput.scrollHeight;
    }
    elements.modelStatus.textContent = state.modelStatus || "模型待加载";
  }

  function applySnapshot(snapshot) {
    if (!snapshot) return;
    state.files = snapshot.files || [];
    state.history = snapshot.history || state.history;
    state.running = Boolean(snapshot.running);
    state.paused = Boolean(snapshot.paused);
    state.scanning = Boolean(snapshot.scanning);
    state.statusText = snapshot.status_text || state.statusText;
    state.progress = Number(snapshot.progress || 0);
    state.logs = snapshot.logs || [];
    state.resultDirs = snapshot.result_dirs || {};
    state.modelStatus = snapshot.model_status || "模型待加载";
    if (snapshot.default_model) elements.modelSelect.value = snapshot.default_model;
    if (snapshot.default_device && !localStorage.getItem(SETTINGS_KEY)) {
      elements.deviceSelect.value = snapshot.default_device;
    }
    if (typeof snapshot.output_path === "string" && snapshot.output_path) {
      state.outputPath = snapshot.output_path;
      elements.outputPath.value = snapshot.output_path;
    }
    state.files.forEach((file) => {
      if (!(file.path in state.sourceUrls) && file.source_url) state.sourceUrls[file.path] = file.source_url;
    });
    renderQueue();
    renderHistory();
    renderDetail();
    renderLogs();
  }

  function showToast(message, type = "info") {
    if (!message) return;
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.textContent = message;
    elements.toastRegion.appendChild(toast);
    window.setTimeout(() => toast.remove(), type === "error" ? 6000 : 3600);
  }

  function selectedOutputMode() {
    return document.querySelector('input[name="outputMode"]:checked')?.value || "source";
  }

  function getSettings() {
    const sourceUrls = {};
    Object.entries(state.sourceUrls).forEach(([path, url]) => {
      const normalized = String(url || "").trim();
      if (normalized) sourceUrls[path] = normalized;
    });
    return {
      model: elements.modelSelect.value,
      language: elements.languageSelect.value,
      device: elements.deviceSelect.value,
      output_mode: selectedOutputMode(),
      output_path: elements.outputPath.value.trim(),
      skip_existing: elements.skipExisting.checked,
      auto_start_folder: false,
      prompt: elements.promptInput.value.trim(),
      source_urls: sourceUrls,
      context_mode: elements.contextMode.value,
      llm_repair: elements.llmRepair.checked,
      deepseek_api_key: elements.deepseekApiKey.value.trim(),
    };
  }

  function saveLocalSettings() {
    const safeSettings = {
      language: elements.languageSelect.value,
      device: elements.deviceSelect.value,
      output_mode: selectedOutputMode(),
      output_path: elements.outputPath.value.trim(),
      llm_repair: elements.llmRepair.checked,
    };
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(safeSettings));
  }

  function loadLocalSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
      if (saved.language) elements.languageSelect.value = saved.language;
      if (saved.device) elements.deviceSelect.value = saved.device;
      elements.llmRepair.checked = Boolean(saved.llm_repair);
      elements.llmKeyPanel.classList.toggle("hidden", !elements.llmRepair.checked);
      if (saved.output_path) elements.outputPath.value = saved.output_path;
      const outputRadio = document.querySelector(`input[name="outputMode"][value="${saved.output_mode || "source"}"]`);
      if (outputRadio) outputRadio.checked = true;
      elements.pathControl.classList.toggle("hidden", selectedOutputMode() !== "custom");
    } catch (_error) {
      localStorage.removeItem(SETTINGS_KEY);
    }
  }

  async function callApi(method, ...args) {
    try {
      const response = await window.pywebview.api[method](...args);
      if (response?.snapshot) applySnapshot(response.snapshot);
      if (response?.error) showToast(response.error, "error");
      if (response?.message) showToast(response.message, response.ok === false ? "error" : "info");
      return response;
    } catch (error) {
      showToast(`操作失败：${error}`, "error");
      return null;
    }
  }

  function showUpload() {
    elements.uploadView.classList.remove("hidden");
    elements.contentView.classList.add("hidden");
    elements.navUpload.classList.add("active");
    elements.navContent.classList.remove("active");
  }

  function showHistory() {
    elements.uploadView.classList.add("hidden");
    elements.contentView.classList.remove("hidden");
    elements.historyView.classList.remove("hidden");
    elements.detailView.classList.add("hidden");
    elements.navUpload.classList.remove("active");
    elements.navContent.classList.add("active");
    renderHistory();
  }

  async function showDetail(source, load = true) {
    state.currentSource = source;
    elements.uploadView.classList.add("hidden");
    elements.contentView.classList.remove("hidden");
    elements.historyView.classList.add("hidden");
    elements.detailView.classList.remove("hidden");
    elements.navUpload.classList.remove("active");
    elements.navContent.classList.add("active");
    renderDetail();
    if (load) await loadResult();
  }

  function inlineMarkdown(value) {
    return escapeHtml(value)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`(.+?)`/g, "<code>$1</code>");
  }

  function markdownToHtml(markdown) {
    if (!markdown.trim()) return '<div class="content-empty">暂时没有可显示的内容</div>';
    const lines = markdown.replace(/^\uFEFF/, "").split(/\r?\n/);
    const result = [];
    let listOpen = false;
    let frontmatter = lines[0]?.trim() === "---";
    lines.forEach((line, index) => {
      if (frontmatter) {
        if (index > 0 && line.trim() === "---") frontmatter = false;
        return;
      }
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      const list = line.match(/^[-*]\s+(.+)$/);
      if (!list && listOpen) { result.push("</ul>"); listOpen = false; }
      if (heading) {
        const level = heading[1].length;
        result.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      } else if (list) {
        if (!listOpen) { result.push("<ul>"); listOpen = true; }
        result.push(`<li>${inlineMarkdown(list[1])}</li>`);
      } else if (line.trim()) {
        result.push(`<p>${inlineMarkdown(line)}</p>`);
      }
    });
    if (listOpen) result.push("</ul>");
    return result.join("");
  }

  function renderResultContent() {
    elements.markdownSource.textContent = state.currentContent;
    elements.markdownPreview.innerHTML = markdownToHtml(state.currentContent);
    const preview = state.currentViewMode === "preview";
    elements.markdownPreview.classList.toggle("hidden", !preview);
    elements.markdownSource.classList.toggle("hidden", preview);
  }

  async function loadResult() {
    if (!state.currentSource) return;
    state.currentContent = "";
    elements.markdownPreview.innerHTML = '<div class="content-empty">正在读取内容…</div>';
    elements.markdownSource.textContent = "";
    const response = await callApi("read_result", state.currentSource, getSettings(), state.currentMaterial);
    if (response?.ok && typeof response.content === "string") {
      state.currentContent = response.content;
      renderResultContent();
    } else if (response?.pending) {
      elements.markdownPreview.innerHTML = '<div class="content-empty">转写完成后即可查看内容</div>';
    } else if (response?.unavailable) {
      elements.markdownPreview.innerHTML = `<div class="content-empty">${escapeHtml(response.reason || "本次任务没有生成该内容")}</div>`;
    } else if (response?.ok === false) {
      elements.markdownPreview.innerHTML = '<div class="content-empty">该任务还没有生成对应内容</div>';
    }
  }

  async function chooseFiles() { await callApi("choose_files"); }
  async function chooseFolder() { await callApi("choose_folder", false, getSettings()); }

  function bindEvents() {
    elements.navUpload.addEventListener("click", showUpload);
    elements.navContent.addEventListener("click", showHistory);
    elements.selectFilesButton.addEventListener("click", chooseFiles);
    elements.addMoreButton.addEventListener("click", chooseFiles);
    elements.selectFolderButton.addEventListener("click", chooseFolder);
    elements.clearButton.addEventListener("click", () => callApi("clear_files"));

    elements.dropzone.addEventListener("dragover", (event) => {
      event.preventDefault();
      elements.dropzone.classList.add("dragging");
    });
    elements.dropzone.addEventListener("dragleave", () => elements.dropzone.classList.remove("dragging"));
    elements.dropzone.addEventListener("drop", async (event) => {
      event.preventDefault();
      elements.dropzone.classList.remove("dragging");
      const paths = Array.from(event.dataTransfer?.files || []).map((file) => file.path).filter(Boolean);
      if (paths.length) await callApi("add_files", paths);
      else showToast("请使用“选择文件”或“选择文件夹”添加本地媒体");
    });

    elements.queueList.addEventListener("click", async (event) => {
      const button = event.target.closest('[data-action="remove"]');
      if (!button) return;
      const row = button.closest("[data-path]");
      if (row) await callApi("remove_files", [row.dataset.path]);
    });
    elements.queueList.addEventListener("input", (event) => {
      if (event.target.dataset.action !== "source-url") return;
      const row = event.target.closest("[data-path]");
      if (row) state.sourceUrls[row.dataset.path] = event.target.value;
    });

    elements.startButton.addEventListener("click", async () => {
      const firstSource = state.files[0]?.path || "";
      const response = await callApi("start_transcription", getSettings());
      if (response?.ok && firstSource) {
        elements.deepseekApiKey.value = "";
        await showDetail(firstSource, false);
      }
    });
    elements.pauseButton.addEventListener("click", () => callApi(state.paused ? "resume_transcription" : "pause_transcription"));
    elements.cancelButton.addEventListener("click", () => callApi("cancel_transcription"));

    elements.historySearch.addEventListener("input", () => {
      state.historyQuery = elements.historySearch.value;
      renderHistory();
    });
    elements.historyFilters.addEventListener("click", (event) => {
      const button = event.target.closest("[data-filter]");
      if (!button) return;
      state.historyFilter = button.dataset.filter;
      elements.historyFilters.querySelectorAll("[data-filter]").forEach((item) => item.classList.toggle("active", item === button));
      renderHistory();
    });
    elements.historyList.addEventListener("click", (event) => {
      const item = event.target.closest("[data-source]");
      if (item) showDetail(item.dataset.source);
    });
    elements.recentList.addEventListener("click", (event) => {
      const item = event.target.closest("[data-source]");
      if (item) showDetail(item.dataset.source);
    });
    elements.backToHistoryButton.addEventListener("click", showHistory);

    elements.materialTabs.addEventListener("click", (event) => {
      const tab = event.target.closest("[data-material]");
      if (!tab) return;
      state.currentMaterial = tab.dataset.material;
      elements.materialTabs.querySelectorAll("[data-material]").forEach((item) => item.classList.toggle("active", item === tab));
      loadResult();
    });
    elements.viewSwitch.addEventListener("click", (event) => {
      const button = event.target.closest("[data-mode]");
      if (!button) return;
      state.currentViewMode = button.dataset.mode;
      elements.viewSwitch.querySelectorAll("[data-mode]").forEach((item) => item.classList.toggle("active", item === button));
      renderResultContent();
    });
    elements.copyResultButton.addEventListener("click", async () => {
      if (!state.currentContent) return showToast("当前没有可复制的内容");
      await navigator.clipboard.writeText(state.currentContent);
      showToast("已复制 Markdown 内容");
    });
    elements.openResultButton.addEventListener("click", () => {
      if (state.currentSource) callApi("open_result", state.currentSource, getSettings());
    });

    elements.settingsButton.addEventListener("click", () => elements.settingsOverlay.classList.remove("hidden"));
    elements.closeSettingsButton.addEventListener("click", () => elements.settingsOverlay.classList.add("hidden"));
    elements.settingsOverlay.addEventListener("click", (event) => {
      if (event.target === elements.settingsOverlay) elements.settingsOverlay.classList.add("hidden");
    });
    elements.saveSettingsButton.addEventListener("click", () => {
      saveLocalSettings();
      elements.settingsOverlay.classList.add("hidden");
      showToast("设置已保存");
    });
    elements.llmRepair.addEventListener("change", () => elements.llmKeyPanel.classList.toggle("hidden", !elements.llmRepair.checked));
    document.querySelectorAll('input[name="outputMode"]').forEach((input) => {
      input.addEventListener("change", () => elements.pathControl.classList.toggle("hidden", selectedOutputMode() !== "custom"));
    });
    elements.chooseOutputButton.addEventListener("click", async () => {
      const response = await callApi("choose_output");
      if (response?.path) elements.outputPath.value = response.path;
    });
  }

  function receive(event) {
    if (!event) return;
    if (event.snapshot) applySnapshot(event.snapshot);
    if (event.message) showToast(event.message, event.level === "error" ? "error" : "info");
    if (["file_done", "file_skipped", "llm_repair_done"].includes(event.type) && event.source === state.currentSource) {
      loadResult();
    }
  }

  async function initialize() {
    cacheElements();
    loadLocalSettings();
    bindEvents();
    const response = await callApi("bootstrap");
    if (response?.snapshot) applySnapshot(response.snapshot);
  }

  window.LocalTranscriber = { receive };
  refreshIcons();
  window.addEventListener("pywebviewready", initialize);
})();
