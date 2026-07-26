/* HH Agent — логика интерфейса. Работает поверх window.pywebview.api (см. ui_app.py)
 * или поверх заглушки ui/mock.js, если открыто в обычном браузере с ?mock=1. */
const $ = id => document.getElementById(id);
const api = () => window.pywebview.api;

const state = {
  settings: null,
  stats: null,
  running: false,
  startedAt: null,
  setup: null,
  logs: [],          // {time, level, text, important}
  filter: "all",
  logAll: false,      // "показать всё" снял ограничение в 300 строк
  now: { query: null, region: null, page: null, round: 0, vacancy: null, phase: null, phaseAt: 0 },
  saveTimer: null,
  timerInterval: null,
};

const esc = s => (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function fmtDuration(ms) {
  const s = Math.floor(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h} ч ${m} мин` : `${m} мин`;
}

/* ================= настройки: загрузка / сбор / сохранение ================= */

async function loadSettings() {
  const s = await api().get_settings();
  state.settings = s;

  $("resumeName").value = s.resume.target_name || "";
  $("resumeSummary").value = s.resume.summary || "";
  updateSummaryCount();

  renderQueryChips();
  setSwitch("titleOnlySwitch", !!s.search.title_only);
  updateQueriesInfo();

  $("maxPagesVal").textContent = s.search.max_pages_per_query;
  updateMaxPagesHint();
  $("pauseVal").textContent = s.schedule.cycle_pause_minutes + " мин";

  renderExperience();
  renderRegions();
  updateRegionsCount();

  $("providerSeg").querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.p === s.llm.provider));
  syncProviderFields();
  $("ollamaUrl").value = s.llm.ollama_url;
  $("openaiUrl").value = s.llm.openai_base_url;
  $("openaiModel").value = s.llm.openai_model || "";
  $("anthropicModel").value = s.llm.anthropic_model || "";
  if (s._secrets.tg_bot_token) $("tgToken").placeholder = "сохранён — оставьте пустым";
  if (s._secrets.anthropic_api_key) $("anthropicKey").placeholder = "сохранён — оставьте пустым";
  if (s._secrets.openai_api_key) $("openaiKey").placeholder = "сохранён — оставьте пустым";

  setSwitch("desktopSwitch", !!s.notifications.desktop_enabled);
  setSwitch("telegramSwitch", !!s.notifications.telegram_enabled);
  $("tgFields").style.display = s.notifications.telegram_enabled ? "" : "none";
  $("tgUserId").value = s.notifications.tg_user_id || "";
  renderEvents();
  setSwitch("keychainSwitch", !!(s.security && s.security.use_keychain));
  $("settingsPath").textContent = "";

  updateSidebarFooter();
  await refreshModels(s.llm.ollama_model);
  await refreshModelList();
}

function collect() {
  const regions = state._regions || [];
  return {
    resume: { target_name: $("resumeName").value.trim(), summary: $("resumeSummary").value },
    search: {
      queries: state._queries || [],
      title_only: hasClass("titleOnlySwitch", "on"),
      max_pages_per_query: parseInt($("maxPagesVal").textContent, 10) || 2,
      regions,
      experience: [...document.querySelectorAll(".exp:checked")].map(c => c.value),
    },
    schedule: { cycle_pause_minutes: parseInt($("pauseVal").textContent, 10) || 10 },
    llm: {
      provider: document.querySelector("#providerSeg button.active")?.dataset.p || "ollama",
      ollama_url: $("ollamaUrl").value.trim(),
      ollama_model: $("ollamaModel").value,
      openai_base_url: $("openaiUrl").value.trim(),
      openai_model: $("openaiModel").value.trim(),
      anthropic_model: $("anthropicModel").value.trim(),
    },
    notifications: {
      desktop_enabled: hasClass("desktopSwitch", "on"),
      telegram_enabled: hasClass("telegramSwitch", "on"),
      tg_user_id: $("tgUserId").value.trim(),
      events: Object.fromEntries([...document.querySelectorAll(".ev")].map(c => [c.dataset.ev, c.checked])),
    },
    security: { use_keychain: hasClass("keychainSwitch", "on") },
    _secrets: {
      tg_bot_token: $("tgToken").value.trim(),
      anthropic_api_key: $("anthropicKey").value.trim(),
      openai_api_key: $("openaiKey").value.trim(),
    },
  };
}

function scheduleSave() {
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(async () => {
    const r = await api().save_settings(collect());
    if (r.ok) {
      $("settingsPath").textContent = r.path;
      flashSaved();
    }
    updateSidebarFooter();
  }, 500);
}

function flashSaved() {
  const el = $("savedIndicator");
  el.style.display = "flex";
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.display = "none"; }, 2200);
}

function updateSidebarFooter() {
  const llm = collect().llm;
  const kind = { ollama: "Локальная модель", openai_compat: "OpenAI-совместимый сервер", anthropic: "Anthropic" }[llm.provider];
  $("providerKind").textContent = kind;
  $("providerName").textContent = llm.provider === "ollama" ? (llm.ollama_model || "—")
    : llm.provider === "anthropic" ? (llm.anthropic_model || "—") : (llm.openai_model || "—");
}

/* ================= мелкие виджеты: переключатели/чекбоксы ================= */

function hasClass(id, cls) { return $(id).classList.contains(cls); }
function setSwitch(id, on) { $(id).classList.toggle("on", !!on); }
function wireSwitch(id, onChange) {
  const el = $(id);
  el.tabIndex = 0; el.setAttribute("role", "switch");
  el.onclick = () => { el.classList.toggle("on"); onChange && onChange(el.classList.contains("on")); scheduleSave(); };
  el.onkeydown = e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); el.click(); } };
}

/* ================= резюме и поиск ================= */

function updateSummaryCount() {
  $("summaryCount").textContent = `${$("resumeSummary").value.length} / 2000`;
}

function renderQueryChips() {
  state._queries = state._queries || state.settings.search.queries.slice();
  const box = $("queryChips");
  box.innerHTML = state._queries.map((q, i) =>
    `<div class="chip">${esc(q)}<button data-i="${i}">${ICON.remove11}</button></div>`).join("")
    + `<button class="chip-add" id="btnAddQuery">${ICON.plus12} Добавить</button>`;
  box.querySelectorAll("button[data-i]").forEach(b => b.onclick = () => {
    state._queries.splice(+b.dataset.i, 1);
    renderQueryChips(); updateQueriesInfo(); scheduleSave();
  });
  $("btnAddQuery").onclick = () => {
    const v = prompt("Новый поисковый запрос, например «QA инженер»:");
    if (v && v.trim()) { state._queries.push(v.trim()); renderQueryChips(); updateQueriesInfo(); scheduleSave(); }
  };
}

function totalPages() {
  const q = (state._queries || []).length, r = (state._regions || []).length;
  const mp = parseInt($("maxPagesVal").textContent, 10) || 0;
  return q * r * mp;
}

function updateQueriesInfo() {
  const n = (state._queries || []).length;
  $("queriesInfo").textContent = `${n} запрос${n === 1 ? "" : n < 5 ? "а" : "ов"} · один круг ≈ ${totalPages()} страниц`;
}

/* ================= фильтры: степперы, опыт, регионы ================= */

const EXP_LABELS = [["noExperience", "Нет опыта"], ["between1And3", "1–3 года"], ["between3And6", "3–6 лет"], ["moreThan6", "Более 6 лет"]];

function renderExperience() {
  const chosen = new Set(state.settings.search.experience);
  $("expList").innerHTML = EXP_LABELS.map(([v, label]) => `
    <label class="check-line"><span class="check${chosen.has(v) ? " checked" : ""}" data-v="${v}">${ICON.checkTick11}</span><span class="t">${label}</span></label>
  `).join("");
  $("expList").querySelectorAll(".check").forEach(c => {
    c.onclick = () => { c.classList.toggle("checked"); scheduleSave(); };
    // отдаём чекбоксу класс .exp с value через искусственный <input>, чтобы collect() мог их найти
  });
  // виртуальные input.exp для совместимости с collect()
  let box = $("expInputs");
  if (!box) { box = document.createElement("div"); box.id = "expInputs"; box.style.display = "none"; document.body.appendChild(box); }
  box.innerHTML = EXP_LABELS.map(([v]) => `<input type="checkbox" class="exp" value="${v}">`).join("");
  const sync = () => {
    $("expList").querySelectorAll(".check").forEach((c, i) => {
      box.children[i].checked = c.classList.contains("checked");
    });
  };
  $("expList").querySelectorAll(".check").forEach(c => c.addEventListener("click", sync));
  sync();
}

function stepperWire(name, {min, max, step = 1, fmt}) {
  const el = document.querySelector(`[data-stepper="${name}"]`);
  el.querySelectorAll("button").forEach(b => b.onclick = () => {
    const valEl = $(name + "Val");
    let cur = parseInt(valEl.textContent, 10) || min;
    cur = Math.max(min, Math.min(max, cur + (+b.dataset.d) * step));
    valEl.textContent = fmt(cur);
    if (name === "maxPages") { updateMaxPagesHint(); updateQueriesInfo(); }
    scheduleSave();
  });
}

function updateMaxPagesHint() {
  $("maxPagesHint").textContent = `≈ ${totalPages()} страниц за круг`;
}

function regionBadge(params) {
  return /schedule=remote/.test(params) ? "Только удалённка" : "Любой график";
}

function renderRegions() {
  state._regions = state._regions || state.settings.search.regions.map(r => ({ ...r }));
  const box = $("regionCards");
  box.innerHTML = state._regions.map((r, i) => {
    const remote = /schedule=remote/.test(r.params);
    return `<div class="region-card">
      <span class="icon">${remote ? ICON.globe15 : ICON.pin15}</span>
      <div class="text"><div class="t">${esc(r.name)}</div>
        <div class="d">${remote ? `<span class="badge">Только удалённка</span>` : "Любой график"}</div></div>
      <div class="spacer"></div>
      <div class="actions">
        <button data-e="${i}">Изменить</button>
        <button class="rm" data-r="${i}">${ICON.remove11}</button>
      </div>
    </div>`;
  }).join("");
  box.querySelectorAll("[data-e]").forEach(b => b.onclick = () => {
    const i = +b.dataset.e, r = state._regions[i];
    const name = prompt("Название региона:", r.name);
    if (name === null) return;
    const params = prompt("Параметры ссылки hh.ru (например &area=1 или &area=113&schedule=remote):", r.params);
    if (params === null) return;
    state._regions[i] = { name: name.trim() || r.name, params: params.trim() };
    renderRegions(); updateRegionsCount(); updateQueriesInfo(); scheduleSave();
  });
  box.querySelectorAll("[data-r]").forEach(b => b.onclick = () => {
    if (state._regions.length <= 1) { alert("Должен остаться хотя бы один регион."); return; }
    state._regions.splice(+b.dataset.r, 1);
    renderRegions(); updateRegionsCount(); updateQueriesInfo(); scheduleSave();
  });
}

function updateRegionsCount() {
  const n = state._regions.length;
  $("regionsCount").textContent = `${n} регион${n === 1 ? "" : n < 5 ? "а" : "ов"}`;
}

/* ================= модель ================= */

function syncProviderFields() {
  const p = document.querySelector("#providerSeg button.active").dataset.p;
  document.querySelectorAll("[data-p]").forEach(el => el.style.display = el.dataset.p === p ? "" : "none");
  $("ollamaExtras").style.display = p === "ollama" ? "" : "none";
  $("providerStatusPill").style.display = "none";
  $("providerStatusMsg").textContent = "";
}

async function refreshModels(selected) {
  const sel = $("ollamaModel");
  const want = selected || sel.value;
  const r = await api().list_models();
  sel.innerHTML = (r.models || []).map(m => `<option${m === want ? " selected" : ""}>${esc(m)}</option>`).join("")
    || `<option value="${esc(want || "")}">${esc(want) || "модели не найдены"}</option>`;
}

async function refreshModelList() {
  const r = await api().list_models_detail();
  const box = $("modelList");
  if (!r.ok || !r.models.length) { box.innerHTML = ""; return; }
  box.innerHTML = r.models.map(m => `
    <div class="model-row">
      <span class="check-icon">${m.in_use ? ICON.checkTick11 : ""}</span>
      <div class="name${m.in_use ? "" : " dim"}">${esc(m.name)}</div>
      ${m.in_use ? `<div class="tag">используется</div>` : ""}
      <div class="spacer"></div>
      <div class="size">${m.size_gb} ГБ</div>
      ${m.in_use ? "" : `<button class="del" data-m="${esc(m.name)}">Удалить</button>`}
    </div>`).join("");
  box.querySelectorAll(".del").forEach(b => b.onclick = async () => {
    if (!confirm(`Удалить модель «${b.dataset.m}»?`)) return;
    b.disabled = true;
    const r2 = await api().delete_model(b.dataset.m);
    if (!r2.ok) alert("Не удалось удалить: " + r2.error);
    await refreshModelList(); await refreshModels();
  });
}

$("providerSeg").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  $("providerSeg").querySelectorAll("button").forEach(x => x.classList.remove("active"));
  b.classList.add("active"); syncProviderFields(); updateSidebarFooter(); scheduleSave();
});

$("btnCheck").onclick = async () => {
  $("providerStatusPill").style.display = "none";
  $("providerStatusMsg").textContent = "Проверяю…";
  await api().save_settings(collect());
  const r = await api().check_provider();
  if (r.ok) {
    $("providerStatusPill").style.display = "inline-flex";
    $("providerStatusPill").innerHTML = `${ICON.check12}Отвечает`;
    $("providerStatusMsg").textContent = "";
  } else {
    $("providerStatusPill").style.display = "none";
    $("providerStatusMsg").innerHTML = `<span style="color:var(--err)">${esc(r.message)}</span>`;
  }
  refreshSetup();
};
$("btnRefresh").onclick = () => refreshModels();
$("btnPull").onclick = async () => {
  const name = $("pullName").value.trim(); if (!name) return;
  $("pullBox").style.display = "flex";
  $("pullTitle").textContent = "Скачиваю " + name;
  $("pullStatus").textContent = "Начинаю загрузку…";
  $("pullMeta").textContent = ""; $("pullBar").style.width = "0";
  await api().pull_model(name);
};

/* ================= уведомления ================= */

const EVENTS = [["applied", "Отклик отправлен"], ["reply", "Ответ от работодателя"], ["captcha", "Нужна помощь с проверкой"], ["summary", "Итоги сеанса"], ["error", "Ошибки в работе"]];

function renderEvents() {
  const ev = state.settings.notifications.events || {};
  $("eventsList").innerHTML = EVENTS.map(([k, label]) => `
    <label class="check-line"><span class="ev check${ev[k] !== false ? " checked" : ""}" data-ev="${k}">${ICON.checkTick11}</span><span class="t">${label}</span></label>
  `).join("");
  $("eventsList").querySelectorAll(".check").forEach(c => c.onclick = () => { c.classList.toggle("checked"); scheduleSave(); });
}

wireSwitch("desktopSwitch");
wireSwitch("telegramSwitch", on => { $("tgFields").style.display = on ? "" : "none"; });
wireSwitch("keychainSwitch");
wireSwitch("titleOnlySwitch", () => updateQueriesInfo());

$("btnTestNotify").onclick = async () => {
  $("notifyStatus").textContent = "Отправляю…";
  await api().save_settings(collect());
  const r = await api().test_notification();
  $("notifyStatus").innerHTML = `<span class="pill ${r.ok ? "pill-ok" : ""}" style="${r.ok ? "" : "color:var(--err)"}">${esc(r.message)}</span>`;
};
$("btnOpenFolder").onclick = () => api().open_settings_folder();

/* ================= вкладки (сайдбар) ================= */

document.querySelectorAll(".nav-item").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".tab-page").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  $("tab-" + b.dataset.tab).classList.add("active");
}));

/* ================= готовность / состояние экрана «Работа» ================= */

const SETUP_ROWS = [
  ["browser", "Браузер для Playwright", "установлен"],
  ["ollama_running", "Ollama запущена", null],
  ["logged_in", "Вход в аккаунт hh.ru", "откроется окно браузера, код придёт как обычно"],
  ["resume", "Название резюме", "должно совпадать с заголовком на hh.ru"],
  ["summary", "Профиль для писем", "модель пишет письма строго по этому тексту"],
];

async function refreshSetup() {
  const s = await api().setup_status();
  if (s.error) return;
  state.setup = s;
  const okCount = SETUP_ROWS.filter(([k]) => s[k]).length;
  $("readyBadge").textContent = `${okCount} из ${SETUP_ROWS.length}`;
  $("readyBadge").className = "pill " + (okCount === SETUP_ROWS.length ? "pill-ok" : "pill-warn");

  $("checklist").innerHTML = SETUP_ROWS.map(([k, label, hint]) => {
    const ok = !!s[k];
    const icon = ok ? `<span class="icon ok">${ICON.circleOk16}</span>` : `<span class="icon warn">${ICON.circleWarn16}</span>`;
    let right = "";
    if (k === "ollama_running") right = `<span class="status mono">${ok ? "localhost:11434" : "не запущена"}</span>`;
    else if (!ok && (k === "resume" || k === "summary")) right = `<button class="action" data-goto="resume">Заполнить${ICON.chevronRight11}</button>`;
    else if (!ok && k === "logged_in") right = `<button class="btn btn-primary btn-small" id="btnLoginNow">Войти</button>`;
    else right = `<span class="status">${ok ? (hint === "установлен" ? "установлен" : "") : ""}</span>`;
    return `<div class="check-row">${icon}<div class="text"><div class="t">${label}</div>${(!ok && hint) ? `<div class="d">${hint}</div>` : ""}</div><div class="spacer"></div>${right}</div>`;
  }).join("");

  $("checklist").querySelectorAll("[data-goto]").forEach(b => b.onclick = () => document.querySelector('.nav-item[data-tab="resume"]').click());
  const loginBtn = $("btnLoginNow"); if (loginBtn) loginBtn.onclick = () => $("btnStart").click();

  updateWorkLayout();
}

function isReady() { return state.setup && SETUP_ROWS.every(([k]) => state.setup[k]); }

function updateWorkLayout() {
  const running = state.running;
  const ready = isReady();
  $("stateNotReady").style.display = (!running && !ready) ? "" : "none";
  $("stateIdle").style.display = (!running && ready) ? "flex" : "none";
  $("stateRunning").style.display = running ? "flex" : "none";

  const fSecond = $("funnelCardSecond");
  if (running) { fSecond.style.display = ""; fSecond.style.width = "288px"; }
  else if (ready) { fSecond.style.display = "none"; }
  else { fSecond.style.display = ""; fSecond.style.width = "340px"; }

  renderFunnel(fSecond, state.stats, !ready && !running /* dashes только пока не готово и не запущено */);
  if (ready && !running) renderFunnel($("funnelCardIdle"), state.stats, false);

  $("workSubtitle").textContent = running ? "Можно свернуть окно — агент продолжит и пришлёт уведомление"
    : ready ? "Настройки сохранены. Нажмите «Запустить», когда будете готовы." : "Осталось несколько шагов, потом можно запускать и уходить";

  const head = $("workHeadRight");
  if (running) {
    head.innerHTML = `<div style="display:flex;flex-direction:column;align-items:flex-end;gap:2px">
      <div style="font:700 26px/1 -apple-system,'SF Pro Display',system-ui,sans-serif;font-variant-numeric:tabular-nums;color:var(--ok)">${state.stats.applied}</div>
      <div style="font-size:11.5px;color:var(--dim)">отклика за сеанс</div></div>`;
  } else if (ready) {
    head.innerHTML = `<div style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--ok)">${ICON.logOk}Всё настроено</div>`;
  } else head.innerHTML = "";

  if (ready && !running) { $("heroApplied").textContent = state.stats.applied; $("heroNote").textContent = `Все письма ушли с профилем «${state.settings?.resume?.target_name || "—"}»`; }
}

function renderFunnel(container, stats, dashes) {
  if (!container) return;
  const s = stats || {};
  const total = (s.viewed || 0) + (s.hard_skipped || 0);
  const rows = [
    ["Просмотрено", total, "fill", false],
    ["Прошли фильтр", s.viewed || 0, "fill", false],
    ["Одобрены ИИ", s.ai_pass || 0, "accent", false],
    ["Письма", s.letters || 0, "accent-strong", false],
    ["Отклики", s.applied || 0, "ok", true],
  ];
  const pct = v => total > 0 ? Math.max(v > 0 ? 4 : 0, Math.min(100, (v / total) * 100)) : 0;
  const body = rows.map(([label, val, cls, strong]) => `
    <div class="funnel-row">
      <div class="label${strong ? " strong" : ""}">${label}</div>
      <div class="track"><div class="fill ${dashes ? "" : cls}" style="width:${dashes ? 0 : pct(val)}%"></div></div>
      <div class="num${strong ? " ok" : ""}">${dashes ? "—" : val}</div>
    </div>`).join("");
  const breakdown = `<div class="funnel-breakdown">
    <div class="stat"><b>${dashes ? "—" : (s.hard_skipped || 0)}</b><span>Отсеяно по названию</span></div>
    <div class="stat"><b>${dashes ? "—" : (s.ai_reject || 0)}</b><span>Отклонил ИИ</span></div>
    <div class="stat"><b>${dashes ? "—" : (s.already || 0)}</b><span>Уже был отклик</span></div>
    <div class="stat"><b>${dashes ? "—" : (s.skipped_page || 0)}</b><span>Не открылось</span></div>
    <div class="stat"><b>${dashes ? "—" : (s.apply_failed || 0)}</b><span>Не удалось</span></div>
  </div>`;
  container.innerHTML = `<div class="card-title-row"><div class="card-title">Воронка</div><div class="spacer"></div>
      ${dashes ? "" : `<div class="hint">из ${total} просмотренных до ${s.applied || 0} откликов</div>`}</div>
    <div style="display:flex;flex-direction:column;gap:10px${dashes ? ";opacity:.55" : ""}">${body}</div>
    ${dashes ? `<div class="spacer" style="flex:1"></div><div class="empty-tip"><div class="t">Тут появятся отклики</div><div class="d">За вечер агент обычно смотрит около 40 вакансий и отправляет 3–8 откликов.</div></div>`
      : `<div class="hairline"></div>${breakdown}`}`;
}

/* ================= журнал ================= */

const IMPORTANT_RE = /Отклик отправлен|Вакансия подходит|Итоги работы|Круг закончен|Капч|VPN/i;
const LOG_PATTERNS = [
  [/^✅|отправлен/i, "logOk", "ok"],
  [/^❌|отклонил/i, "logReject", "dim"],
  [/^⏩|^⏭️|пропуска/i, "logSkip", "dim"],
  [/^🔒|^⚠️/i, "logWarn", "warn"],
  [/^👁️|открыва/i, "logViewing", "dim"],
];

function classifyLog(text, level) {
  if (level === "error") return { icon: "logError", cls: "error" };
  if (level === "warn") return { icon: "logWarn", cls: "warn" };
  for (const [re, icon, cls] of LOG_PATTERNS) if (re.test(text)) return { icon, cls: cls === "ok" ? "important" : "" };
  return { icon: "logViewing", cls: "" };
}

function addLog(text, level = "info") {
  const time = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  const important = IMPORTANT_RE.test(text) || level !== "info";
  state.logs.push({ time, level, text, important });
  if (state.logs.length > 4000) state.logs.shift();
  renderLogAppend(state.logs[state.logs.length - 1]);
  updateNowFromLog(text);
}

function passesFilter(entry) {
  if (state.filter === "error") return entry.level === "error";
  if (state.filter === "important") return entry.important;
  return true;
}

function renderLogAppend(entry) {
  const box = $("log");
  $("logEmpty").style.display = "none";
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const { icon, cls } = classifyLog(entry.text, entry.level);
  const div = document.createElement("div");
  div.className = "log-line" + (cls ? " " + cls : "") + (passesFilter(entry) ? "" : " hidden-by-filter");
  div.dataset.important = entry.important ? "1" : "0";
  div.dataset.level = entry.level;
  div.innerHTML = `<div class="time">${entry.time}</div><span class="icon ${entry.level === "error" ? "err" : entry.level === "warn" ? "warn" : cls === "important" ? "ok" : "dim"}">${ICON[icon]}</span><div class="text">${esc(entry.text)}</div>`;
  box.appendChild(div);
  while (box.children.length > 800) box.removeChild(box.firstChild);
  if (atBottom) box.scrollTop = box.scrollHeight;
  updateLogFooter();
  $("logCount").textContent = state.logs.length ? `${state.logs.length} строк${state.logs.length === 1 ? "а" : ""} за сеанс` : "";
}

function reflowLogFilter() {
  $("log").querySelectorAll(".log-line").forEach((el, i) => {
    const entry = state.logs[i];
    if (!entry) return;
    el.classList.toggle("hidden-by-filter", !passesFilter(entry));
  });
  updateLogFooter();
}

function updateLogFooter() {
  const hidden = state.logs.filter(e => !passesFilter(e)).length;
  const footer = $("logFooter");
  if (state.filter !== "all" && hidden > 0) {
    footer.style.display = "flex";
    $("hiddenCount").textContent = state.filter === "error"
      ? `${state.logs.filter(e => e.level === "error").length} ошибки из ${state.logs.length} строк`
      : `Скрыто ${hidden} технических строк`;
  } else footer.style.display = "none";
}

$("logFilter").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  state.filter = b.dataset.f;
  $("logFilter").querySelectorAll("button").forEach(x => x.classList.remove("active", "err-tab"));
  b.classList.add("active", state.filter === "error" ? "err-tab" : "");
  reflowLogFilter();
});
$("logShowAll").onclick = () => { state.filter = "all"; $("logFilter").querySelectorAll("button").forEach(x => x.classList.toggle("active", x.dataset.f === "all")); reflowLogFilter(); };

/* ================= «Сейчас делаю» — выводим из текста журнала ================= */

function updateNowFromLog(text) {
  let m;
  if ((m = text.match(/^🔍 Поиск по запросу: (.+)/))) { state.now.query = m[1]; state.now.page = 1; }
  else if ((m = text.match(/^📍 Режим: (.+)/))) { state.now.region = m[1]; }
  else if ((m = text.match(/парсим страницу (\d+)/i))) { state.now.page = +m[1]; }
  else if ((m = text.match(/^👁️ Открываем вакансию: (.+)/))) { state.now.vacancy = m[1]; state.now.phase = "viewing"; state.now.phaseAt = Date.now(); }
  else if ((m = text.match(/^✨ Вакансия подходит: (.+)/))) { state.now.vacancy = m[1]; state.now.phase = "approved"; state.now.phaseAt = Date.now(); }
  else if ((m = text.match(/^✍️ Пишу сопроводительное — (.+)/))) { state.now.vacancy = m[1]; state.now.phase = "writing"; state.now.phaseAt = Date.now(); }
  else if ((m = text.match(/^✅ Отклик отправлен: (.+)/))) { state.now.vacancy = m[1]; state.now.phase = "applied"; state.now.phaseAt = Date.now(); setTimeout(() => { if (state.now.phase === "applied") clearNow(); }, 2500); }
  else if (/^❌ ИИ отклонил|^⏩|^⏭️/.test(text)) { clearNow(); }
  else if (/^😴 Круг закончен/.test(text)) { state.now.round++; state.now.query = null; state.now.page = null; clearNow(); }
  renderNow();
}
function clearNow() { state.now.phase = null; state.now.vacancy = null; renderNow(); }

const STEP_ORDER = ["viewing", "approved", "writing", "applied"];
const STEP_LABEL = { viewing: "Открыл страницу", approved: "Одобрено ИИ", writing: "Письмо", applied: "Отклик" };

function renderNow() {
  const n = state.now;
  const chips = [];
  if (n.query) chips.push(n.query);
  if (n.region) chips.push(n.region);
  if (n.page) chips.push(`Страница ${n.page} из ${state.settings?.search?.max_pages_per_query || "?"}`);
  chips.push(`Круг ${n.round + 1}`);
  $("nowChips").innerHTML = chips.map(c => `<div class="now-chip">${esc(c)}</div>`).join("");

  const box = $("nowBox");
  if (!n.phase || !n.vacancy) {
    box.innerHTML = `<div class="empty-now">${ICON.dot}Жду следующую вакансию…</div>`;
    return;
  }
  const idx = STEP_ORDER.indexOf(n.phase);
  const stepsHtml = STEP_ORDER.map((s, i) => {
    const done = i < idx, current = i === idx, pending = i > idx;
    return `<div class="step ${done ? "done" : current ? "current" : "pending"}">
      <span class="ring">${done ? ICON.checkTick11 : ""}</span>${STEP_LABEL[s]}
    </div>${i < STEP_ORDER.length - 1 ? '<div class="step-sep"></div>' : ""}`;
  }).join("");

  const titleWord = { viewing: "Открываю", approved: "Смотрю дальше", writing: "Пишу сопроводительное", applied: "Отклик отправлен" }[n.phase];
  let timerHtml = "";
  if (n.phase === "writing") {
    const elapsed = (Date.now() - n.phaseAt) / 1000;
    timerHtml = `<div class="timer">${elapsed.toFixed(1)} / ~12.6 с</div>`;
  }
  const spinIcon = n.phase === "applied" ? `<span class="icon ok" style="flex:none">${ICON.logOk}</span>` : `<span class="spin">${ICON.spinner15}</span>`;

  box.innerHTML = `
    <div class="now-box-line">${spinIcon}<div class="title">${titleWord} — ${esc(n.vacancy)}</div>${timerHtml}</div>
    ${n.phase === "writing" ? `<div class="progress-thin"><div class="fill" id="writeProgress" style="width:0"></div></div>` : ""}
    <div class="steps">${stepsHtml}</div>`;
}

// таймер прогресс-бара письма тикает независимо от прихода новых строк лога
setInterval(() => {
  if (state.now.phase === "writing" && state.running) {
    const elapsed = (Date.now() - state.now.phaseAt) / 1000;
    const bar = $("writeProgress");
    if (bar) bar.style.width = Math.min(96, elapsed / 12.6 * 100) + "%";
    const timerEl = document.querySelector(".now-box-line .timer");
    if (timerEl) timerEl.textContent = `${elapsed.toFixed(1)} / ~12.6 с`;
  }
}, 300);

/* ================= заголовок: запуск/остановка/таймер ================= */

function setRunningUi(running, startedAt) {
  state.running = running;
  if (startedAt) state.startedAt = startedAt * 1000;
  $("btnStart").style.display = running ? "none" : "";
  $("btnStop").style.display = running ? "" : "none";
  $("duration").disabled = running;
  $("sessionLive").style.display = running ? "flex" : "none";
  $("logLiveTag").style.display = running ? "flex" : "none";

  $("statusDot").className = "dot" + (running ? " running" : "");
  $("statusLabel").textContent = running ? "Работает" : "Остановлен";
  $("statusSub").textContent = running ? `Круг ${state.now.round + 1}` : (isReady() ? "Готов к запуску" : "Нужна настройка");

  clearInterval(state.timerInterval);
  if (running) {
    state.timerInterval = setInterval(() => {
      if (!state.startedAt) return;
      $("sessionLiveText").textContent = "Работает " + fmtDuration(Date.now() - state.startedAt);
    }, 1000);
  } else {
    clearNow();
  }
  updateWorkLayout();
}

$("btnStart").onclick = async () => {
  try {
    $("errorBanner").style.display = "none";
    await api().save_settings(collect());
    const r = await api().start_agent(+$("duration").value);
    if (r && !r.ok) showErrorBanner("Не удалось запустить", r.error || "неизвестная ошибка");
  } catch (e) {
    showErrorBanner("Ошибка при запуске", e && e.message ? e.message : String(e));
  }
};
$("btnStop").onclick = () => api().stop_agent();
$("btnRecheck").onclick = () => refreshSetup();

/* ================= баннер ошибки ================= */

function showErrorBanner(title, desc) {
  $("errorBannerTitle").textContent = title;
  $("errorBannerDesc").textContent = desc;
  $("errorBanner").style.display = "flex";
}
$("btnOpenOllama").onclick = () => api().open_ollama_app();
$("btnRecheckProvider").onclick = async () => {
  const r = await api().check_provider();
  if (r.ok) $("errorBanner").style.display = "none";
  else $("errorBannerDesc").textContent = r.message;
};
$("btnGoCloud").onclick = () => { document.querySelector('.nav-item[data-tab="model"]').click(); $("errorBanner").style.display = "none"; };

/* ================= капча ================= */

$("captchaSend").onclick = () => sendCaptcha($("captchaInput").value.trim());
$("captchaSkip").onclick = () => sendCaptcha("");
$("captchaInput").addEventListener("keydown", e => { if (e.key === "Enter") sendCaptcha($("captchaInput").value.trim()); });
function sendCaptcha(text) { $("captchaBox").classList.remove("show"); api().submit_captcha(text); }

/* ================= события от Python ================= */

window.onAgentEvent = (event, data) => {
  if (event === "log") addLog(data.line, data.level);
  else if (event === "state") setRunningUi(data.running, data.started_at);
  else if (event === "stats") { state.stats = data; updateWorkLayout(); }
  else if (event === "captcha") {
    $("captchaPrompt").textContent = data.prompt || "";
    if (data.image) { $("captchaImg").src = data.image; $("captchaImg").style.display = "block"; $("captchaPlaceholder").style.display = "none"; }
    else { $("captchaImg").style.display = "none"; $("captchaPlaceholder").style.display = "block"; }
    $("captchaInput").value = "";
    $("captchaBox").classList.add("show");
    setTimeout(() => $("captchaInput").focus(), 60);
  }
  else if (event === "captcha_close") $("captchaBox").classList.remove("show");
  else if (event === "setup_done") refreshSetup();
  else if (event === "pull_progress") {
    $("pullBox").style.display = "flex";
    $("pullTitle").textContent = data.status || "Скачиваю…";
    $("pullMeta").textContent = data.percent ? data.percent + "%" : "";
    $("pullBar").style.width = (data.percent || 0) + "%";
  }
  else if (event === "pull_done") {
    if (data.ok) { $("pullStatus").textContent = "Готово: " + data.model; $("pullBar").style.width = "100%"; refreshModels(); refreshModelList(); }
    else $("pullStatus").innerHTML = `<span style="color:var(--err)">Ошибка: ${esc(data.error)}</span>`;
  }
};

/* ================= стартовая инициализация ================= */

window.addEventListener("pywebviewready", async () => {
  await loadSettings();
  const st = await api().get_state();
  state.stats = st.stats;
  await refreshSetup();
  setRunningUi(st.running, st.started_at ? st.started_at : null);
  addLog("Готов к работе.");
});
