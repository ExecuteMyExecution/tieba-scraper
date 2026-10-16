const $ = (selector) => document.querySelector(selector);

const state = {
  page: 1,
  pageSize: 12,
  total: 0,
  polling: null,
  activeCrawlImageFilter: "all",
  activeCrawlForum: "",
  activeJobId: null,
  view: "grid",
  cardWidth: 220,
  ignoredAuthors: [],
  collectorCollapsed: false,
  lightboxImages: [],
  lightboxIndex: 0,
  lightboxTrigger: null,
  threadLoadId: 0,
  preferencesReady: false,
};

let filterTimer = null;
const PREFERENCES_KEY = "tieba-content-desk-preferences-v1";

function savePreferences() {
  if (!state.preferencesReady) return;
  try {
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify({
      query: $("#query").value,
      forum: $("#filter-forum").value,
      author: $("#filter-author").value,
      hasImages: $("#filter-images").value,
      sort: $("#filter-sort").value,
      pageSize: state.pageSize,
      view: state.view,
      cardWidth: state.cardWidth,
      ignoredAuthors: state.ignoredAuthors,
      collectorCollapsed: state.collectorCollapsed,
      activeJobId: state.activeJobId,
    }));
  } catch {
    // Private browsing or disabled storage should not break the page.
  }
}

function restorePreferences() {
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(PREFERENCES_KEY) || "{}");
  } catch {
    saved = {};
  }
  $("#query").value = typeof saved.query === "string" ? saved.query : "";
  $("#filter-author").value = typeof saved.author === "string" ? saved.author : "";
  if (["all", "yes", "no"].includes(saved.hasImages)) {
    $("#filter-images").value = saved.hasImages;
  }
  if (["newest", "oldest", "replies", "views"].includes(saved.sort)) {
    $("#filter-sort").value = saved.sort;
  }
  const pageSize = Number(saved.pageSize);
  if ([12, 24, 48, 96].includes(pageSize)) {
    state.pageSize = pageSize;
    $("#page-size").value = String(pageSize);
  }
  setCollectorCollapsed(Boolean(saved.collectorCollapsed));
  setView(saved.view === "list" ? "list" : "grid");
  setCardSize(Number.isFinite(Number(saved.cardWidth)) ? Number(saved.cardWidth) : 220);
  if (Array.isArray(saved.ignoredAuthors)) {
    state.ignoredAuthors = [...new Set(saved.ignoredAuthors
      .filter((name) => typeof name === "string")
      .map((name) => name.trim())
      .filter(Boolean))].slice(0, 100);
  }
  const activeJobId = Number(saved.activeJobId);
  state.activeJobId = Number.isInteger(activeJobId) && activeJobId > 0
    ? activeJobId : null;
  return typeof saved.forum === "string" ? saved.forum : "";
}

const escapeHtml = (value = "") => String(value)
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");

function formatNumber(value) {
  return new Intl.NumberFormat("zh-CN", { notation: value > 9999 ? "compact" : "standard", maximumFractionDigits: 1 }).format(value || 0);
}

function formatDate(timestamp) {
  if (!timestamp) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(timestamp * 1000));
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.style.background = error ? "#8c3c31" : "#17211c";
  element.classList.remove("hidden");
  setTimeout(() => element.classList.add("hidden"), 3200);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  let body;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((item) => item.msg).join("；") : body.detail;
    throw new Error(detail || `请求失败 (${response.status})`);
  }
  return body;
}

async function loadStats() {
  const stats = await api("/api/stats");
  $("#stat-total").textContent = formatNumber(stats.total);
  $("#stat-forums").textContent = formatNumber(stats.forums);
  $("#stat-images").textContent = formatNumber(stats.with_images);
}

async function loadForums(preferredForum = $("#filter-forum").value) {
  const select = $("#filter-forum");
  const data = await api("/api/forums");
  select.innerHTML = [
    '<option value="">全部贴吧</option>',
    ...data.items.map((item) =>
      `<option value="${escapeHtml(item.forum_name)}">${escapeHtml(item.forum_name)}吧（${item.thread_count}）</option>`
    ),
  ].join("");
  if ([...select.options].some((option) => option.value === preferredForum)) {
    select.value = preferredForum;
  }
}

function queryParams() {
  const params = new URLSearchParams({
    query: $("#query").value.trim(),
    forum: $("#filter-forum").value,
    author: $("#filter-author").value.trim(),
    has_images: $("#filter-images").value,
    sort: $("#filter-sort").value,
    page: state.page,
    page_size: state.pageSize,
  });
  state.ignoredAuthors.forEach((author) => params.append("exclude_author", author));
  return params;
}

function renderIgnoredAuthors() {
  const container = $("#ignored-authors");
  const authors = state.ignoredAuthors;
  container.classList.toggle("hidden", authors.length === 0);
  if (!authors.length) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = `<span>不看 ${authors.length} 位作者</span><div class="ignored-author-list">${authors.map((author) =>
    `<button type="button" data-unignore-author="${escapeHtml(author)}" title="恢复查看 ${escapeHtml(author)} 的帖子">${escapeHtml(author)} <b>×</b></button>`
  ).join("")}</div>`;
}

function ignoreAuthor(author) {
  const normalized = String(author || "").trim();
  if (!normalized || state.ignoredAuthors.includes(normalized)) return;
  if (state.ignoredAuthors.length >= 100) {
    toast("最多可屏蔽 100 位作者", true);
    return;
  }
  state.ignoredAuthors.push(normalized);
  renderIgnoredAuthors();
  state.page = 1;
  savePreferences();
  loadThreads();
  toast(`已不看“${normalized}”的帖子`);
}

function unignoreAuthor(author) {
  state.ignoredAuthors = state.ignoredAuthors.filter((name) => name !== author);
  renderIgnoredAuthors();
  state.page = 1;
  savePreferences();
  loadThreads();
}

function renderCard(item) {
  const gallery = item.images || [];
  const authorName = String(item.author_name || "").trim();
  const galleryData = escapeHtml(JSON.stringify(gallery));
  const images = gallery.slice(0, 5).map((src, index) =>
    `<button class="image-preview" type="button" data-gallery="${galleryData}" data-index="${index}" aria-label="查看第 ${index + 1} 张大图"><img src="${escapeHtml(src)}" alt="帖子图片" loading="lazy" referrerpolicy="no-referrer"></button>`
  ).join("");
  const ignoreAuthorButton = authorName
    ? `<button class="ignore-author-button" type="button" data-ignore-author="${escapeHtml(authorName)}" title="不再显示 ${escapeHtml(authorName)} 的帖子">不看此作者</button>`
    : "";
  return `<article class="content-card">
    <div class="card-top">
      <span class="forum-pill">${escapeHtml(item.forum_name)}吧</span>
      ${item.has_images ? '<span class="image-pill">含图片</span>' : ''}
      <span class="card-date">${formatDate(item.created_at)}</span>
    </div>
    <h3>${escapeHtml(item.title || "无标题")}</h3>
    <p>${escapeHtml(item.content || "（该主题没有可展示的文字内容）")}</p>
    ${images ? `<div class="image-strip">${images}</div>` : ""}
    <div class="card-footer">
      <div class="card-meta">
        <span>作者 <strong>${escapeHtml(authorName || "未知")}</strong></span>
        <span>回复 <strong>${formatNumber(item.reply_count)}</strong></span>
        <span>浏览 <strong>${formatNumber(item.view_count)}</strong></span>
      </div>
      <div class="card-actions">${ignoreAuthorButton}<a href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">查看原帖 ↗</a></div>
    </div>
  </article>`;
}

function renderLightbox() {
  const total = state.lightboxImages.length;
  if (!total) return;
  $("#lightbox-image").src = state.lightboxImages[state.lightboxIndex];
  $("#lightbox-counter").textContent = `${state.lightboxIndex + 1} / ${total}`;
  $("#lightbox-prev").disabled = total < 2;
  $("#lightbox-next").disabled = total < 2;
}

function openLightbox(images, index, trigger) {
  state.lightboxImages = images;
  state.lightboxIndex = index;
  state.lightboxTrigger = trigger;
  renderLightbox();
  $("#lightbox").classList.remove("hidden");
  document.body.classList.add("lightbox-open");
  $("#lightbox-close").focus();
}

function closeLightbox() {
  $("#lightbox").classList.add("hidden");
  document.body.classList.remove("lightbox-open");
  $("#lightbox-image").removeAttribute("src");
  if (state.lightboxTrigger) state.lightboxTrigger.focus();
}

function moveLightbox(step) {
  const total = state.lightboxImages.length;
  if (total < 2) return;
  state.lightboxIndex = (state.lightboxIndex + step + total) % total;
  renderLightbox();
}

async function loadThreads() {
  const loadId = ++state.threadLoadId;
  const list = $("#content-list");
  list.style.opacity = ".55";
  try {
    const data = await api(`/api/threads?${queryParams()}`);
    if (loadId !== state.threadLoadId) return;
    state.total = data.total;
    const totalPages = Math.max(1, Math.ceil(data.total / state.pageSize));
    if (data.total > 0 && state.page > totalPages) {
      state.page = totalPages;
      await loadThreads();
      return;
    }
    list.innerHTML = data.items.map(renderCard).join("");
    $("#empty-state").classList.toggle("hidden", data.items.length > 0);
    $("#result-summary").textContent = data.job_id
      ? `共找到 ${data.total} 条内容 · 任务 #${data.job_id}`
      : `共找到 ${data.total} 条内容 · ${data.forum_count || 0} 个贴吧`;
    $("#pagination").classList.toggle("hidden", data.total === 0);
    $("#page-select").innerHTML = Array.from(
      { length: totalPages },
      (_, index) => `<option value="${index + 1}"${index + 1 === state.page ? " selected" : ""}>${index + 1}</option>`,
    ).join("");
    $("#page-info").textContent = `/ ${totalPages} 页`;
    const firstItem = (state.page - 1) * state.pageSize + 1;
    const lastItem = Math.min(state.page * state.pageSize, data.total);
    $("#page-range").textContent = `第 ${firstItem}–${lastItem} 条，共 ${data.total} 条`;
    $("#prev-page").disabled = state.page <= 1;
    $("#next-page").disabled = state.page >= totalPages;
  } catch (error) {
    if (loadId === state.threadLoadId) toast(error.message, true);
  } finally {
    if (loadId === state.threadLoadId) list.style.opacity = "1";
  }
}

async function pollJob(jobId) {
  clearInterval(state.polling);
  state.polling = null;
  state.activeJobId = Number(jobId);
  savePreferences();
  const box = $("#job-box");
  box.classList.remove("hidden");
  $("#cancel-job-button").classList.remove("hidden");
  $("#cancel-job-button").disabled = false;
  $("#cancel-job-button").textContent = "取消任务";
  const tick = async () => {
    try {
      const job = await api(`/api/jobs/${jobId}`);
      const labels = { pending: "等待中", running: "采集中", completed: "已完成", partial: "部分完成", failed: "失败", cancelled: "已取消" };
      $("#job-status").textContent = labels[job.status] || job.status;
      $("#job-percent").textContent = `${job.progress}%`;
      $("#job-progress").style.width = `${job.progress}%`;
      $("#job-message").textContent = job.message;
      $("#job-metrics").textContent = [
        `首楼缓存 ${job.detail_cache_count || 0} 条`,
        `图片缓存 ${job.image_cache_count || 0} 张`,
        `图片失败 ${job.image_failed_count || 0} 张`,
        `自动重试 ${job.retry_count || 0} 次`,
        `跳过 ${job.skipped_count || 0} 条`,
        `未完成 ${job.failed_count || 0} 条`,
      ].join(" · ");
      if (["completed", "partial", "failed", "cancelled"].includes(job.status)) {
        clearInterval(state.polling);
        state.polling = null;
        state.activeJobId = null;
        savePreferences();
        $("#cancel-job-button").classList.add("hidden");
        $("#crawl-button").disabled = false;
        $("#crawl-button span").textContent = "开始采集";
        if (["completed", "partial"].includes(job.status)) {
          toast(
            job.status === "partial"
              ? `采集部分完成：保存 ${job.saved_count} 条，主题失败 ${job.failed_count} 条，图片失败 ${job.image_failed_count || 0} 张`
              : `采集完成，保存了 ${job.saved_count} 条内容`,
            job.status === "partial",
          );
          const viewFilters = { all: "all", with_images: "yes", without_images: "no", portrait_images: "yes" };
          $("#filter-images").value = viewFilters[state.activeCrawlImageFilter] || "all";
          await loadForums();
          $("#filter-forum").value = state.activeCrawlForum;
          savePreferences();
          state.page = 1;
          await Promise.all([loadThreads(), loadStats()]);
        } else if (job.status === "cancelled") {
          toast("采集已取消，原有资料库未被覆盖");
        } else {
          toast(job.message, true);
        }
      }
    } catch (error) {
      clearInterval(state.polling);
      state.polling = null;
      $("#crawl-button").disabled = false;
      toast(error.message, true);
    }
  };
  await tick();
  if (state.polling === null && $("#crawl-button").disabled) {
    state.polling = setInterval(tick, 2500);
  }
}

$("#pages").addEventListener("input", (event) => {
  $("#pages-output").textContent = `${event.target.value} 页`;
});

$("#crawl-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const startDate = $("#start-date").value;
  const endDate = $("#end-date").value;
  if (startDate && endDate && startDate > endDate) {
    toast("开始日期不能晚于结束日期", true);
    return;
  }
  const button = $("#crawl-button");
  button.disabled = true;
  $("#crawl-button span").textContent = "正在创建…";
  try {
    state.activeCrawlImageFilter = $("input[name='image_condition']:checked").value;
    state.activeCrawlForum = $("#forum-name").value.trim().replace(/吧$/, "").trim();
    const result = await api("/api/crawl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        forum_name: $("#forum-name").value,
        pages: Number($("#pages").value),
        first_floor_image_filter: state.activeCrawlImageFilter,
        start_date: startDate || null,
        end_date: endDate || null,
      }),
    });
    $("#crawl-button span").textContent = "采集进行中…";
    await pollJob(result.job_id);
  } catch (error) {
    button.disabled = false;
    $("#crawl-button span").textContent = "开始采集";
    toast(error.message, true);
  }
});

function scheduleFilter(delay = 0) {
  clearTimeout(filterTimer);
  savePreferences();
  filterTimer = setTimeout(() => {
    state.page = 1;
    loadThreads();
  }, delay);
}

$("#filter-form").addEventListener("submit", (event) => {
  event.preventDefault();
  scheduleFilter();
});
$("#query").addEventListener("input", () => scheduleFilter(300));
$("#filter-author").addEventListener("input", () => scheduleFilter(300));
["#filter-forum", "#filter-images", "#filter-sort"].forEach((selector) => {
  $(selector).addEventListener("change", () => scheduleFilter());
});
$("#filter-reset").addEventListener("click", () => {
  clearTimeout(filterTimer);
  $("#query").value = "";
  $("#filter-forum").value = "";
  $("#filter-author").value = "";
  $("#filter-images").value = "all";
  $("#filter-sort").value = "newest";
  state.page = 1;
  savePreferences();
  loadThreads();
});

function setCollectorCollapsed(collapsed) {
  state.collectorCollapsed = collapsed;
  $(".workspace").classList.toggle("collector-collapsed", state.collectorCollapsed);
  $(".collector-panel").classList.toggle("collapsed", state.collectorCollapsed);
  $("#collector-toggle").setAttribute("aria-expanded", String(!state.collectorCollapsed));
  $("#collector-toggle").setAttribute("title", state.collectorCollapsed ? "展开采集栏" : "收起采集栏");
  $("#collector-toggle span").textContent = state.collectorCollapsed ? "→" : "←";
  $("#collector-toggle b").textContent = state.collectorCollapsed ? "展开" : "收起";
  savePreferences();
}

$("#collector-toggle").addEventListener("click", () => {
  setCollectorCollapsed(!state.collectorCollapsed);
});
function setView(view) {
  state.view = view;
  $("#content-list").classList.toggle("grid-view", view === "grid");
  $("#content-list").classList.toggle("list-view", view === "list");
  $("#grid-view-button").classList.toggle("active", view === "grid");
  $("#list-view-button").classList.toggle("active", view === "list");
  $("#grid-view-button").setAttribute("aria-pressed", String(view === "grid"));
  $("#list-view-button").setAttribute("aria-pressed", String(view === "list"));
  $("#card-size-control").classList.toggle("hidden", view !== "grid");
  savePreferences();
}

function setCardSize(width) {
  const range = $("#card-size-range");
  const value = Math.min(Number(range.max), Math.max(Number(range.min), Number(width)));
  const previewWidth = Math.round(106 * value / 220);
  const previewHeight = Math.round(84 * value / 220);
  state.cardWidth = value;
  range.value = String(value);
  $("#card-size-output").textContent = `${value}px`;
  $("#card-size-smaller").disabled = value <= Number(range.min);
  $("#card-size-larger").disabled = value >= Number(range.max);
  $("#content-list").style.setProperty("--card-min-width", `${value}px`);
  $("#content-list").style.setProperty("--preview-width", `${previewWidth}px`);
  $("#content-list").style.setProperty("--preview-height", `${previewHeight}px`);
  savePreferences();
}

function scrollToCardTop() {
  requestAnimationFrame(() => {
    $("#content-list").scrollIntoView({ behavior: "instant", block: "start" });
  });
}

async function changePage(page) {
  state.page = page;
  await loadThreads();
  scrollToCardTop();
}

$("#grid-view-button").addEventListener("click", () => setView("grid"));
$("#list-view-button").addEventListener("click", () => setView("list"));
$("#card-size-range").addEventListener("input", (event) => setCardSize(event.target.value));
$("#card-size-smaller").addEventListener("click", () => setCardSize(state.cardWidth - 20));
$("#card-size-larger").addEventListener("click", () => setCardSize(state.cardWidth + 20));
$("#content-list").addEventListener("click", (event) => {
  const ignoreButton = event.target.closest("[data-ignore-author]");
  if (ignoreButton) {
    ignoreAuthor(ignoreButton.dataset.ignoreAuthor);
    return;
  }
  const preview = event.target.closest(".image-preview");
  if (!preview) return;
  openLightbox(JSON.parse(preview.dataset.gallery), Number(preview.dataset.index), preview);
});
$("#ignored-authors").addEventListener("click", (event) => {
  const button = event.target.closest("[data-unignore-author]");
  if (button) unignoreAuthor(button.dataset.unignoreAuthor);
});
$("#lightbox-mask").addEventListener("click", closeLightbox);
$("#lightbox-close").addEventListener("click", closeLightbox);
$("#lightbox-prev").addEventListener("click", () => moveLightbox(-1));
$("#lightbox-next").addEventListener("click", () => moveLightbox(1));
$("#cancel-job-button").addEventListener("click", async () => {
  if (!state.activeJobId) return;
  const button = $("#cancel-job-button");
  button.disabled = true;
  button.textContent = "正在取消…";
  try {
    await api(`/api/jobs/${state.activeJobId}/cancel`, { method: "POST" });
    await pollJob(state.activeJobId);
  } catch (error) {
    button.disabled = false;
    button.textContent = "取消任务";
    toast(error.message, true);
  }
});
document.addEventListener("keydown", (event) => {
  const lightboxOpen = !$("#lightbox").classList.contains("hidden");
  if (lightboxOpen) {
    if (event.key === "Escape") closeLightbox();
    if (event.key === "ArrowLeft") moveLightbox(-1);
    if (event.key === "ArrowRight") moveLightbox(1);
    if (["Escape", "ArrowLeft", "ArrowRight"].includes(event.key)) {
      event.preventDefault();
    }
    return;
  }

  const target = event.target;
  const isEditing = target instanceof HTMLElement
    && (target.matches("input, textarea, select") || target.isContentEditable);
  if (isEditing || event.repeat || event.ctrlKey || event.metaKey || event.altKey) return;

  if (event.key === "ArrowLeft" && state.page > 1) {
    event.preventDefault();
    changePage(state.page - 1);
  } else if (event.key === "ArrowRight" && state.page * state.pageSize < state.total) {
    event.preventDefault();
    changePage(state.page + 1);
  } else if (["+", "="].includes(event.key) && state.view === "grid") {
    event.preventDefault();
    setCardSize(state.cardWidth + 20);
  } else if (["-", "_"].includes(event.key) && state.view === "grid") {
    event.preventDefault();
    setCardSize(state.cardWidth - 20);
  }
});
$("#refresh-button").addEventListener("click", async () => {
  await loadForums();
  await Promise.all([loadThreads(), loadStats()]);
});
$("#prev-page").addEventListener("click", () => { if (state.page > 1) changePage(state.page - 1); });
$("#next-page").addEventListener("click", () => { if (state.page * state.pageSize < state.total) changePage(state.page + 1); });
$("#page-select").addEventListener("change", (event) => {
  changePage(Number(event.target.value));
});
$("#page-size").addEventListener("change", async (event) => {
  state.pageSize = Number(event.target.value);
  savePreferences();
  await changePage(1);
});

async function initializePage() {
  const preferredForum = restorePreferences();
  renderIgnoredAuthors();
  await loadForums(preferredForum);
  state.preferencesReady = true;
  savePreferences();
  await Promise.all([loadStats(), loadThreads()]);
  await resumeRunningJob();
}

async function resumeRunningJob() {
  let activeJob = null;
  if (state.activeJobId) {
    try {
      const savedJob = await api(`/api/jobs/${state.activeJobId}`);
      if (["pending", "running"].includes(savedJob.status)) activeJob = savedJob;
    } catch {
      activeJob = null;
    }
  }
  if (!activeJob) {
    const jobs = await api("/api/jobs");
    activeJob = jobs.items.find((job) => ["pending", "running"].includes(job.status));
  }
  if (!activeJob) {
    state.activeJobId = null;
    savePreferences();
    return;
  }
  state.activeCrawlForum = activeJob.forum_name;
  state.activeCrawlImageFilter = activeJob.image_filter;
  $("#crawl-button").disabled = true;
  $("#crawl-button span").textContent = "采集进行中…";
  await pollJob(activeJob.id);
}

initializePage().catch((error) => toast(error.message, true));
