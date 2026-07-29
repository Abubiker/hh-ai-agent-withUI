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
  setSwitch("requireLetterSwitch", s.search.require_letter !== false);
  $("exclusions").value = s.search.exclusions || "";

  $("maxPagesVal").textContent = s.search.max_pages_per_query || "до конца";
  $("pauseVal").textContent = s.schedule.cycle_pause_minutes + " мин";

  renderExperience();
  renderRegions();
  updateRegionsCount();
  renderWorkRegionChips();

  // Считаем страницы ПОСЛЕ того, как заданы и лимит страниц, и регионы:
  // иначе в подсказке оказывалось «≈ 0 страниц».
  updateMaxPagesHint();
  updateQueriesInfo();

  $("providerSeg").querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.p === s.llm.provider));
  syncProviderFields();
  $("ollamaUrl").value = s.llm.ollama_url;
  $("openaiUrl").value = s.llm.openai_base_url;
  $("openaiModel").value = s.llm.openai_model || "";
  $("anthropicModel").value = s.llm.anthropic_model || "";
  // Пресет сервиса подсвечиваем, если адрес совпал с известным
  $("openaiPreset").value =
    [...$("openaiPreset").options].some(o => o.value === s.llm.openai_base_url)
      ? s.llm.openai_base_url : "";
  // Модель Anthropic: известная — выбираем в списке, иначе режим ручного ввода
  const known = [...$("anthropicPreset").options].map(o => o.value).filter(Boolean);
  const isKnown = known.includes(s.llm.anthropic_model);
  $("anthropicPreset").value = isKnown ? s.llm.anthropic_model : "";
  $("anthropicManualWrap").style.display = isKnown ? "none" : "";
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

  syncOpenaiPicker();
  // Список тянем сразу, если сервис готов отвечать: иначе пользователь видит
  // пустой селект и не понимает, что нужно нажать «Обновить».
  if (s.llm.provider === "openai_compat") await loadOpenaiModels({ quiet: true });
  resetModelDirty();
}

function collect() {
  const regions = state._regions || [];
  return {
    resume: { target_name: $("resumeName").value.trim(), summary: $("resumeSummary").value },
    search: {
      queries: state._queries || [],
      title_only: hasClass("titleOnlySwitch", "on"),
      require_letter: hasClass("requireLetterSwitch", "on"),
      // Пустую строку сохраняем как есть: настройки подставят список
      // по умолчанию, иначе классификатор пропускал бы вообще всё.
      exclusions: $("exclusions").value,
      // Пустое значение «до конца» сохраняем нулём — так его понимает агент.
      max_pages_per_query: /^\d+$/.test($("maxPagesVal").textContent.trim())
        ? parseInt($("maxPagesVal").textContent, 10) : 0,
      regions,
      experience: [...document.querySelectorAll("#expList .check.checked")].map(c => c.dataset.v),
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
      events: Object.fromEntries([...document.querySelectorAll("#eventsList .check")]
        .map(c => [c.dataset.ev, c.classList.contains("checked")])),
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
  // Для облачных подписываем сервис по адресу: «OpenAI-совместимый сервер»
  // ничего не говорит, когда сервисов пять и переключаешься между ними.
  const preset = [...$("openaiPreset").options].find(o => o.value && o.value === llm.openai_base_url);
  const kind = llm.provider === "ollama" ? "Локальная модель"
    : llm.provider === "anthropic" ? "Anthropic"
    : (preset ? preset.textContent : "OpenAI-совместимый сервер");
  const name = llm.provider === "ollama" ? llm.ollama_model
    : llm.provider === "anthropic" ? llm.anthropic_model : llm.openai_model;
  $("providerKind").textContent = kind;
  $("providerName").textContent = name || "модель не выбрана";
  $("providerName").style.color = name ? "" : "var(--warn)";
  $("providerName").title = name || "";
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
  // prompt() в нативном WKWebView не работает, поэтому добавление — инлайн:
  // кнопка «Добавить» превращается в поле ввода прямо в ряду чипов.
  box.innerHTML = state._queries.map((q, i) =>
    `<div class="chip">${esc(q)}<button data-i="${i}" title="Убрать запрос">${ICON.remove11}</button></div>`).join("")
    + `<button class="chip-add" id="btnAddQuery">${ICON.plus12} Добавить</button>`
    + `<input id="newQueryInput" class="chip-input" placeholder="Например: QA инженер" style="display:none">`;
  box.querySelectorAll("button[data-i]").forEach(b => b.onclick = () => {
    state._queries.splice(+b.dataset.i, 1);
    renderQueryChips(); updateQueriesInfo(); scheduleSave();
  });
  const input = $("newQueryInput");
  $("btnAddQuery").onclick = () => {
    $("btnAddQuery").style.display = "none";
    input.style.display = "";
    input.focus();
  };
  const commit = () => {
    const v = input.value.trim();
    if (v && !state._queries.includes(v)) {
      state._queries.push(v);
      scheduleSave();
    }
    renderQueryChips(); updateQueriesInfo();
  };
  input.addEventListener("keydown", e => {
    if (e.key === "Enter") commit();
    if (e.key === "Escape") renderQueryChips();
  });
  input.addEventListener("blur", commit);
}

function totalPages() {
  const q = (state._queries || []).length;
  const r = (state._regions || []).filter(x => x.enabled !== false).length;
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
    <div class="check-line"><span class="check${chosen.has(v) ? " checked" : ""}" data-v="${v}">${ICON.checkTick11}</span><span class="t">${label}</span></div>
  `).join("");
  // кликабельна вся строка, не только квадратик
  $("expList").querySelectorAll(".check-line").forEach(line => line.onclick = () => {
    line.querySelector(".check").classList.toggle("checked");
    scheduleSave();
  });
}

function stepperWire(name, {min, max, step = 1, fmt}) {
  const el = document.querySelector(`[data-stepper="${name}"]`);
  el.querySelectorAll("button").forEach(b => b.onclick = () => {
    const valEl = $(name + "Val");
    // «до конца» — это ноль, обычный parseInt на нём даёт NaN.
    let cur = /^\d+$/.test(valEl.textContent.trim())
      ? parseInt(valEl.textContent, 10) : 0;
    cur = Math.max(min, Math.min(max, cur + (+b.dataset.d) * step));
    valEl.textContent = fmt(cur);
    if (name === "maxPages") { updateMaxPagesHint(); updateQueriesInfo(); }
    scheduleSave();
  });
}

function updateMaxPagesHint() {
  const lim = /^\d+$/.test($("maxPagesVal").textContent.trim())
    ? parseInt($("maxPagesVal").textContent, 10) : 0;
  $("maxPagesHint").textContent = lim
    ? `≈ ${totalPages()} страниц за проверку`
    : "Агент листает, пока встречает новые вакансии. Останавливают его "
      + "кнопка или лимит времени сеанса.";
}

function regionBadge(params) {
  return /schedule=remote/.test(params) ? "Только удалённка" : "Любой график";
}

const SCHEDULE_LABELS = {
  "": "Любой график", remote: "Только удалёнка", fullDay: "Полный день",
  flexible: "Гибкий график", shift: "Сменный график",
};

function scheduleOf(params) {
  const m = /schedule=([a-zA-Z]+)/.exec(params || "");
  return m ? m[1] : "";
}

function enabledCount() {
  return (state._regions || []).filter(r => r.enabled !== false).length;
}

/** Включает/выключает регион. Последний включённый выключить нельзя —
 * агенту нужен хотя бы один, иначе искать негде (см. active_regions в hh_client). */
function toggleRegion(i) {
  const r = state._regions[i];
  const turningOff = r.enabled !== false;
  if (turningOff && enabledCount() <= 1) return false;
  r.enabled = !turningOff;
  scheduleSave();
  return true;
}

function renderRegions() {
  state._regions = state._regions || state.settings.search.regions.map(r => ({ enabled: true, ...r }));
  const box = $("regionCards");
  const single = state._regions.length <= 1;
  box.innerHTML = state._regions.map((r, i) => {
    const sched = scheduleOf(r.params);
    const on = r.enabled !== false;
    return `<div class="region-card${on ? "" : " disabled"}">
      <div class="switch${on ? " on" : ""}" data-toggle="${i}" title="${on ? "Выключить" : "Включить"} регион"><div class="knob"></div></div>
      <span class="icon">${sched === "remote" ? ICON.globe15 : ICON.pin15}</span>
      <div class="text"><div class="t">${esc(r.name)}</div>
        <div class="d">${sched ? `<span class="badge">${SCHEDULE_LABELS[sched] || sched}</span>` : "Любой график"}</div></div>
      <div class="spacer"></div>
      <div class="actions">
        <button data-e="${i}">Изменить</button>
        ${single ? "" : `<button class="rm" data-r="${i}" title="Убрать регион">${ICON.remove11}</button>`}
      </div>
    </div>`;
  }).join("");
  box.querySelectorAll("[data-toggle]").forEach(el => el.onclick = () => {
    toggleRegion(+el.dataset.toggle);
    renderRegions(); updateRegionsCount(); updateQueriesInfo(); renderWorkRegionChips();
  });
  box.querySelectorAll("[data-e]").forEach(b => b.onclick = () => openRegionModal(+b.dataset.e));
  box.querySelectorAll("[data-r]").forEach(b => b.onclick = () => {
    state._regions.splice(+b.dataset.r, 1);
    renderRegions(); updateRegionsCount(); updateQueriesInfo(); renderWorkRegionChips(); scheduleSave();
  });
}

/** Компактная строка регионов на экране «Работа» — переключение без похода
 * во вкладку «Фильтры». Использует те же данные, что и карточки в Фильтрах. */
function renderWorkRegionChips() {
  const box = $("workRegionChips");
  if (!box) return;
  state._regions = state._regions || state.settings.search.regions.map(r => ({ enabled: true, ...r }));
  box.innerHTML = state._regions.map((r, i) => {
    const on = r.enabled !== false;
    return `<button class="region-chip${on ? " on" : ""}" data-wt="${i}" title="${on ? "Выключить" : "Включить"} регион">
      ${scheduleOf(r.params) === "remote" ? ICON.globe15 : ICON.pin15}${esc(r.name)}
    </button>`;
  }).join("") + `<button class="chip-add" id="btnWorkAddRegion">${ICON.plus12} Регион</button>`;
  box.querySelectorAll("[data-wt]").forEach(b => b.onclick = () => {
    toggleRegion(+b.dataset.wt);
    renderWorkRegionChips(); renderRegions(); updateRegionsCount(); updateQueriesInfo();
  });
  const addBtn = $("btnWorkAddRegion");
  if (addBtn) addBtn.onclick = () => openRegionModal(undefined);
}

/* ---------- модалка региона (со справочником hh.ru) ---------- */

let areasCache = null;   // {areas, source, schedules} — грузится один раз

async function loadAreas() {
  if (areasCache) return areasCache;
  areasCache = await api().get_areas();
  return areasCache;
}

function openRegionModal(index) {
  // index === undefined — добавление нового
  const editing = index !== undefined ? state._regions[index] : null;
  state._regionModal = {
    index,
    name: editing ? editing.name.replace(/\s*\(.*\)$/, "") : "",
    areaId: (/area=(\d+)/.exec(editing?.params || "") || [])[1] || "",
    schedule: scheduleOf(editing?.params),
  };
  $("regionModalTitle").textContent = editing ? "Изменить регион" : "Добавить регион";
  $("areaSearch").value = state._regionModal.name;
  $("areaResults").innerHTML = "";
  $("regionModalBox").classList.add("show");
  renderScheduleChips();
  updateRegionPreview();

  loadAreas().then(r => {
    const manual = !r.areas || !r.areas.length;
    $("areaManual").style.display = manual ? "" : "none";
    $("areaSearchWrap").style.display = manual ? "none" : "";
    if (manual) {
      $("manualName").value = editing ? editing.name : "";
      $("manualParams").value = editing ? editing.params : "";
    } else if (state._regionModal.name) {
      filterAreas(state._regionModal.name);
    }
  });
  setTimeout(() => $("areaSearch").focus(), 60);
}

function filterAreas(q) {
  const query = (q || "").trim().toLowerCase();
  const box = $("areaResults");
  if (!query || !areasCache?.areas?.length) { box.innerHTML = ""; return; }
  const hits = areasCache.areas.filter(a => a.name.toLowerCase().includes(query)).slice(0, 20);
  box.innerHTML = hits.map(a =>
    `<div class="area-hit${a.id === state._regionModal.areaId ? " active" : ""}" data-id="${a.id}" data-name="${esc(a.name)}">
      <span>${esc(a.name)}</span>${a.parent ? `<span class="parent">${esc(a.parent)}</span>` : ""}
    </div>`).join("");
  box.querySelectorAll(".area-hit").forEach(el => el.onclick = () => {
    state._regionModal.areaId = el.dataset.id;
    state._regionModal.name = el.dataset.name;
    $("areaSearch").value = el.dataset.name;
    filterAreas(el.dataset.name);
    updateRegionPreview();
  });
}

function renderScheduleChips() {
  const scheds = areasCache?.schedules ||
    Object.entries(SCHEDULE_LABELS).map(([id, name]) => ({ id, name }));
  $("scheduleChips").innerHTML = scheds.map(s =>
    `<button class="sched-chip${s.id === state._regionModal.schedule ? " active" : ""}" data-id="${s.id}">${esc(s.name)}</button>`).join("");
  $("scheduleChips").querySelectorAll("button").forEach(b => b.onclick = () => {
    state._regionModal.schedule = b.dataset.id;
    renderScheduleChips();
    updateRegionPreview();
  });
}

function regionModalResult() {
  const m = state._regionModal;
  // При редактировании сохраняем текущее enabled; у нового региона — включён сразу.
  const enabled = m.index !== undefined ? state._regions[m.index].enabled !== false : true;
  const manual = $("areaManual").style.display !== "none";
  if (manual) {
    const name = $("manualName").value.trim();
    const params = $("manualParams").value.trim();
    return name && params ? { name, params, enabled } : null;
  }
  if (!m.areaId) return null;
  const schedTag = m.schedule ? ` (${SCHEDULE_LABELS[m.schedule] || m.schedule})` : "";
  return {
    name: m.name + schedTag,
    params: `&area=${m.areaId}` + (m.schedule ? `&schedule=${m.schedule}` : ""),
    enabled,
  };
}

function updateRegionPreview() {
  const r = regionModalResult();
  $("regionPreview").textContent = r ? r.params : "выберите регион из списка";
  $("regionModalSave").disabled = !r;
}

$("areaSearch").addEventListener("input", () => {
  state._regionModal.areaId = "";  // текст меняли — прежний выбор недействителен
  filterAreas($("areaSearch").value);
  updateRegionPreview();
});
["manualName", "manualParams"].forEach(id =>
  $(id).addEventListener("input", updateRegionPreview));

$("regionModalSave").onclick = () => {
  const r = regionModalResult();
  if (!r) return;
  const i = state._regionModal.index;
  if (i !== undefined) state._regions[i] = r;
  else state._regions.push(r);
  $("regionModalBox").classList.remove("show");
  renderRegions(); updateRegionsCount(); updateQueriesInfo(); renderWorkRegionChips(); scheduleSave();
};
$("regionModalCancel").onclick = () => $("regionModalBox").classList.remove("show");
$("btnAddRegion").onclick = () => openRegionModal(undefined);

function updateRegionsCount() {
  const n = state._regions.length;
  $("regionsCount").textContent = `${n} регион${n === 1 ? "" : n < 5 ? "а" : "ов"}`;
}

/* ================= модель ================= */

function syncProviderFields() {
  const p = document.querySelector("#providerSeg button.active").dataset.p;
  // ВАЖНО: только блоки настроек. Кнопки самого переключателя тоже имеют
  // data-p, и общий селектор скрывал их — выбрав облачного провайдера,
  // вернуться к Ollama было нельзя.
  document.querySelectorAll(".card [data-p], .card[data-p]").forEach(el => {
    if (el.closest("#providerSeg")) return;
    el.style.display = el.dataset.p === p ? "" : "none";
  });
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
    <div class="model-row" data-row="${esc(m.name)}">
      <span class="check-icon">${m.in_use ? ICON.checkTick11 : ""}</span>
      <div class="name${m.in_use ? "" : " dim"}">${esc(m.name)}</div>
      ${m.in_use ? `<div class="tag">используется</div>` : ""}
      <div class="spacer"></div>
      <div class="size">${m.size_gb} ГБ</div>
      ${m.in_use ? "" : `<button class="use" data-m="${esc(m.name)}">Использовать</button>
      <button class="del" data-m="${esc(m.name)}">Удалить</button>`}
    </div>`).join("");

  box.querySelectorAll(".use").forEach(b => b.onclick = () => useModel(b.dataset.m));

  // confirm() в нативном WKWebView не работает — подтверждение двухшаговое:
  // первый клик меняет кнопку на «Точно удалить?», второй (за 3 секунды) удаляет.
  box.querySelectorAll(".del").forEach(b => b.onclick = async () => {
    if (!b.classList.contains("confirm")) {
      b.classList.add("confirm");
      b.textContent = "Точно удалить?";
      b._t = setTimeout(() => { b.classList.remove("confirm"); b.textContent = "Удалить"; }, 3000);
      return;
    }
    clearTimeout(b._t);
    b.disabled = true; b.textContent = "Удаляю…";
    const r2 = await api().delete_model(b.dataset.m);
    if (!r2.ok) {
      const row = box.querySelector(`[data-row="${CSS.escape(b.dataset.m)}"]`);
      if (row) row.insertAdjacentHTML("afterend",
        `<div class="model-row"><div class="name dim" style="color:var(--err)">Не удалось удалить: ${esc(r2.error || "")}</div></div>`);
    }
    await refreshModelList(); await refreshModels();
  });
}

async function useModel(name) {
  // Смена активной модели: сразу сохраняем (без debounce) и обновляем всё,
  // что показывает текущую модель — селект, список, подпись в сайдбаре.
  $("ollamaModel").innerHTML = `<option selected>${esc(name)}</option>`;
  await api().save_settings(collect());
  flashSaved();
  updateSidebarFooter();
  await refreshModels(name);
  await refreshModelList();
}

$("providerSeg").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  $("providerSeg").querySelectorAll("button").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  syncProviderFields();
  syncOpenaiPicker();
  markModelDirty();
});

$("btnCheck").onclick = async () => {
  const btn = $("btnCheck");
  btn.disabled = true;                       // запрос не мгновенный — второй клик не нужен
  $("providerStatusPill").style.display = "none";
  $("providerStatusMsg").textContent = "";
  toast("wait", "Отправляю запрос к модели…");
  try {
    await api().save_settings(collect());    // проверяем то, что видит пользователь
    resetModelDirty();                       // сохранили — помечать нечего
    const r = await api().check_provider();
    if (r.ok) {
      toast("ok", "Подключение работает", r.message);
      $("providerStatusPill").style.display = "inline-flex";
      $("providerStatusPill").innerHTML = `${ICON.check12}Отвечает`;
    } else {
      toast("err", "Подключиться не удалось", r.message);
      $("providerStatusMsg").innerHTML = `<span style="color:var(--err)">${esc(r.message)}</span>`;
    }
  } catch (e) {
    toast("err", "Проверка сорвалась", String(e));
  } finally {
    btn.disabled = false;
    refreshSetup();
  }
};
$("btnRefresh").onclick = () => refreshModels();

/* ---------- выбор модели у облачных провайдеров ---------- */

// OpenAI-совместимые сервисы: пресет заполняет базовый адрес
$("openaiPreset").onchange = () => {
  const url = $("openaiPreset").value;
  if (url) { $("openaiUrl").value = url; }
  markModelDirty();
  syncOpenaiPicker();
};

let openaiModels = [];   // полный список с последней загрузки

/* Список моделей доступен, только когда сервису есть что ответить: нужен
   адрес и, у всех облачных сервисов, ключ. Локальный сервер (LM Studio,
   llama.cpp) ключа не требует, поэтому смотрим не на сам факт ключа,
   а на то, локальный ли адрес. */
function needsKey(url) {
  return !/^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])/i.test(url || "");
}
function hasKey() {
  // Либо ключ введён прямо сейчас, либо уже сохранён для этого сервиса.
  return !!$("openaiKey").value.trim() || !!(state.settings?._secrets?.openai_api_key);
}

function syncOpenaiPicker() {
  const manual = hasClass("openaiManualSwitch", "on");
  const url = $("openaiUrl").value.trim();
  const ready = !!url && (!needsKey(url) || hasKey());
  const empty = !openaiModels.length;

  // Тумблер меняет одно поле на другое, места они занимают одинаково.
  $("openaiCombo").style.display = manual ? "none" : "";
  $("openaiModel").style.display = manual ? "" : "none";
  $("lblFromList").classList.toggle("on", !manual);
  $("lblManual").classList.toggle("on", manual);
  if (manual) closeCombo();

  // Блокируем, только когда выбирать действительно не из чего: список,
  // загруженный раньше, гасить нельзя — ключ после сохранения стирается
  // из поля, и живой список выглядел бы сломанным.
  $("openaiComboField").disabled = empty && !ready;
  $("btnLoadOpenaiModels").disabled = !ready;

  const cur = $("openaiModel").value.trim();
  $("openaiComboVal").textContent = cur || "Модель не выбрана";
  $("openaiComboVal").classList.toggle("empty", !cur);
  $("openaiModelCount").textContent = openaiModels.length ? `${openaiModels.length}` : "";

  $("openaiModelHint").innerHTML = manual
    ? "Название модели — как его пишет сам сервис, посимвольно."
    : !url ? "Сначала выберите сервис или впишите базовый адрес."
    : (!ready && empty) ? "Введите API-ключ — список моделей запрашивается у самого сервиса."
    : 'У OpenRouter бесплатные модели помечены суффиксом <span class="mono">:free</span> — наберите «free» в фильтре.';
}

function openCombo() {
  if ($("openaiComboField").disabled) return;
  $("openaiCombo").classList.add("open");
  renderOpenaiModels();
  $("openaiModelFilter").focus();
  $("openaiModelFilter").select();
}
function closeCombo() { $("openaiCombo").classList.remove("open"); }

$("openaiComboField").onclick = () =>
  $("openaiCombo").classList.contains("open") ? closeCombo() : openCombo();

// Клик мимо панели закрывает её — иначе она перекрывает кнопку «Сохранить».
document.addEventListener("click", e => {
  if (!e.target.closest("#openaiCombo")) closeCombo();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeCombo(); });

async function loadOpenaiModels({ quiet = false } = {}) {
  const url = $("openaiUrl").value.trim();
  if (!url || (needsKey(url) && !hasKey())) return;
  $("openaiComboFoot").textContent = "загружаю…";
  // Провайдер на стороне Python читает адрес и ключ из настроек, поэтому
  // перед запросом списка сохраняем то, что введено.
  await api().save_settings(collect());
  const r = await api().list_models();
  if (!r.ok || !r.models.length) {
    openaiModels = [];
    if (!quiet && r.error) toast("err", "Не удалось получить список моделей", r.error);
  } else {
    openaiModels = r.models;
  }
  renderOpenaiModels();
  syncOpenaiPicker();
}

$("btnLoadOpenaiModels").onclick = e => { e.stopPropagation(); loadOpenaiModels(); };

function renderOpenaiModels() {
  const q = $("openaiModelFilter").value.trim().toLowerCase();
  const hits = q ? openaiModels.filter(m => m.toLowerCase().includes(q)) : openaiModels;
  const cur = $("openaiModel").value.trim();
  const shown = hits.slice(0, 300);
  $("openaiComboList").innerHTML = shown.length
    ? shown.map(m => `<button type="button" class="combo-item${m === cur ? " sel" : ""}" data-m="${esc(m)}">${esc(m)}</button>`).join("")
    : `<div class="combo-empty">${openaiModels.length ? "Ничего не найдено" : "Список пуст"}</div>`;
  $("openaiComboFoot").textContent = q
    ? `${hits.length} из ${openaiModels.length}`
    : `${openaiModels.length} моделей`;
  $("openaiComboList").querySelectorAll(".combo-item").forEach(b => b.onclick = () => {
    $("openaiModel").value = b.dataset.m;
    closeCombo();
    syncOpenaiPicker();
    markModelDirty();
  });
}

$("openaiModelFilter").addEventListener("input", renderOpenaiModels);
$("openaiModelFilter").addEventListener("click", e => e.stopPropagation());

wireSwitch("openaiManualSwitch", () => { syncOpenaiPicker(); markModelDirty(); });
// По подписям тоже переключаем: попасть в сам тумблер сложнее, чем в слово.
$("lblFromList").onclick = () => { if (hasClass("openaiManualSwitch", "on")) $("openaiManualSwitch").click(); };
$("lblManual").onclick = () => { if (!hasClass("openaiManualSwitch", "on")) $("openaiManualSwitch").click(); };

// Ключ ввели — список уже можно спросить.
$("openaiKey").addEventListener("input", () => {
  syncOpenaiPicker();
  clearTimeout(state._keyTimer);
  state._keyTimer = setTimeout(() => loadOpenaiModels({ quiet: true }), 700);
});
$("openaiUrl").addEventListener("input", syncOpenaiPicker);
$("openaiModel").addEventListener("input", () => { syncOpenaiPicker(); });

// Anthropic: список известных моделей + возможность ввести своё имя
$("anthropicPreset").onchange = () => {
  const v = $("anthropicPreset").value;
  $("anthropicManualWrap").style.display = v ? "none" : "";
  if (v) { $("anthropicModel").value = v; scheduleSave(); updateSidebarFooter(); }
  else $("anthropicModel").focus();
};
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
    <div class="check-line"><span class="check${ev[k] !== false ? " checked" : ""}" data-ev="${k}">${ICON.checkTick11}</span><span class="t">${label}</span></div>
  `).join("");
  $("eventsList").querySelectorAll(".check-line").forEach(line => line.onclick = () => {
    line.querySelector(".check").classList.toggle("checked");
    scheduleSave();
  });
}

wireSwitch("desktopSwitch");
wireSwitch("telegramSwitch", on => { $("tgFields").style.display = on ? "" : "none"; });
wireSwitch("keychainSwitch");
wireSwitch("titleOnlySwitch", () => updateQueriesInfo());
wireSwitch("requireLetterSwitch");

// Степперы «Страниц на запрос» и «Пауза между проверками».
// Функция была написана, но не вызвана — кнопки +/− не работали вовсе.
stepperWire("maxPages", { min: 0, max: 10, fmt: v => v === 0 ? "до конца" : String(v) });
stepperWire("pause", { min: 5, max: 120, step: 5, fmt: v => v + " мин" });

// Автосохранение всех полей ввода. Раньше слушателей не было совсем:
// правки резюме, адресов и выбор модели в селекте молча терялись.
document.querySelectorAll("input, textarea, select").forEach(el => {
  if (["captchaInput", "pullName", "newQueryInput", "areaSearch",
       "manualName", "manualParams", "openaiModelFilter",
       "openaiPreset", "anthropicPreset"].includes(el.id)) return;
  // Вкладка «Модель» сохраняется кнопкой — см. markModelDirty().
  if (el.closest("#tab-model")) return;
  el.addEventListener("change", scheduleSave);
  if (el.tagName === "TEXTAREA" || ["text", "password", "number"].includes(el.type))
    el.addEventListener("input", scheduleSave);
});
$("resumeSummary").addEventListener("input", updateSummaryCount);

// Смена модели в селекте обновляет и список моделей, и сайдбар
$("ollamaModel").addEventListener("change", () => useModel($("ollamaModel").value));

$("btnTestNotify").onclick = async () => {
  $("notifyStatus").textContent = "Отправляю…";
  await api().save_settings(collect());
  const r = await api().test_notification();
  $("notifyStatus").innerHTML = `<span class="pill ${r.ok ? "pill-ok" : ""}" style="${r.ok ? "" : "color:var(--err)"}">${esc(r.message)}</span>`;
};
$("btnOpenFolder").onclick = () => api().open_settings_folder();


/* ================= всплывашка о результате ================= */

let toastTimer = null;
function toast(kind, title, text = "") {
  // kind: "ok" | "err" | "wait". Ожидание не гасим по таймеру — его сменит итог.
  const el = $("toast");
  el.className = "toast show " + kind;
  $("toastTitle").textContent = title;
  $("toastText").textContent = text;
  $("toastText").style.display = text ? "" : "none";
  clearTimeout(toastTimer);
  // Успех читается за секунду, ошибку нужно успеть прочитать и скопировать.
  if (kind === "ok") toastTimer = setTimeout(hideToast, 4000);
}
function hideToast() { clearTimeout(toastTimer); $("toast").classList.remove("show"); }


/* ================= вкладка «Модель»: явное сохранение ================= */

/* Автосохранение здесь мешает: провайдер, адрес, ключ и модель меняют
   пачкой, а промежуточные состояния бессмысленны — сохранённый ключ без
   модели или адрес без ключа. Поэтому на этой вкладке сохраняем по кнопке. */

function modelSnapshot() {
  const c = collect();
  return JSON.stringify({
    llm: c.llm,
    manual: hasClass("openaiManualSwitch", "on"),
    // Ключи в снимок кладём как факт ввода, а не значением.
    keyTyped: !!($("openaiKey").value.trim() || $("anthropicKey").value.trim()),
  });
}

function markModelDirty() {
  const dirty = modelSnapshot() !== state._modelSaved;
  $("btnModelSave").disabled = !dirty;
  $("btnModelReset").disabled = !dirty;
  $("modelDirtyHint").textContent = dirty ? "Есть несохранённые изменения" : "Изменений нет";
  $("modelDirtyHint").style.color = dirty ? "var(--warn)" : "";
  updateSidebarFooter();
}

function resetModelDirty() {
  state._modelSaved = modelSnapshot();
  markModelDirty();
}

$("btnModelSave").onclick = async () => {
  const btn = $("btnModelSave");
  btn.disabled = true;
  const r = await api().save_settings(collect());
  if (!r.ok) { toast("err", "Не удалось сохранить", r.error || ""); btn.disabled = false; return; }
  // Ключ ушёл в хранилище — поле очищаем, дальше оно значит «не менять».
  $("openaiKey").value = ""; $("anthropicKey").value = "";
  state.settings = await api().get_settings();
  $("openaiKey").placeholder = state.settings._secrets.openai_api_key
    ? "сохранён — оставьте пустым" : "оставьте пустым — не изменится";
  resetModelDirty();
  flashSaved();
  toast("ok", "Настройки модели сохранены");
  refreshSetup();
};

$("btnModelReset").onclick = async () => {
  await loadSettings();
  resetModelDirty();
};

// Любое изменение на этой вкладке помечает её как несохранённую.
document.querySelectorAll("#tab-model input, #tab-model select").forEach(el => {
  if (["pullName", "openaiModelFilter"].includes(el.id)) return;
  el.addEventListener("change", markModelDirty);
  if (el.tagName === "TEXTAREA" || ["text", "password"].includes(el.type))
    el.addEventListener("input", markModelDirty);
});

/* ================= вкладки (сайдбар) ================= */

document.querySelectorAll(".nav-item").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".tab-page").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  $("tab-" + b.dataset.tab).classList.add("active");
}));

/* ================= готовность / состояние экрана «Работа» ================= */

// Строка про модель зависит от провайдера: облачной модели Ollama не нужна.
function setupRows(s) {
  const modelRow = (s && s.provider && s.provider !== "ollama")
    ? ["model_ready", "Облачная модель", "укажите ключ и модель на вкладке «Модель»"]
    : ["model_ready", "Ollama запущена", null];
  return [
    ["browser", "Браузер для Playwright", "установлен"],
    modelRow,
    ["logged_in", "Вход в аккаунт hh.ru", "откроется окно браузера, код придёт как обычно"],
    ["resume", "Название резюме", "должно совпадать с заголовком на hh.ru"],
    ["summary", "Профиль для писем", "модель пишет письма строго по этому тексту"],
  ];
}

async function refreshSetup() {
  const s = await api().setup_status();
  if (s.error) return;
  state.setup = s;
  const rows = setupRows(s);
  const okCount = rows.filter(([k]) => s[k]).length;
  $("readyBadge").textContent = `${okCount} из ${rows.length}`;
  $("readyBadge").className = "pill " + (okCount === rows.length ? "pill-ok" : "pill-warn");

  $("checklist").innerHTML = rows.map(([k, label, hint]) => {
    const ok = !!s[k];
    const icon = ok ? `<span class="icon ok">${ICON.circleOk16}</span>` : `<span class="icon warn">${ICON.circleWarn16}</span>`;
    let right = "";
    if (k === "model_ready" && s.provider === "ollama") {
      // Не установлена и не запущена — разные беды, и чинятся по-разному.
      right = ok ? `<span class="status mono">localhost:11434</span>`
        : s.ollama_installed
          ? `<button class="btn btn-primary btn-small" id="btnStartOllama">Запустить Ollama</button>`
          : `<button class="btn btn-primary btn-small" id="btnGetOllama">Скачать Ollama</button>`;
    }
    else if (k === "model_ready") right = ok ? `<span class="status">готова</span>`
      : `<button class="action" data-goto="model">Настроить${ICON.chevronRight11}</button>`;
    else if (!ok && (k === "resume" || k === "summary")) right = `<button class="action" data-goto="resume">Заполнить${ICON.chevronRight11}</button>`;
    else if (!ok && k === "logged_in") right = `<button class="btn btn-primary btn-small" id="btnLoginNow">Войти</button>`;
    else right = `<span class="status">${ok ? (hint === "установлен" ? "установлен" : "") : ""}</span>`;
    const sub = (k === "model_ready" && !ok && s.model_note) ? s.model_note : (!ok ? hint : "");
    return `<div class="check-row">${icon}<div class="text"><div class="t">${label}</div>${sub ? `<div class="d">${esc(sub)}</div>` : ""}</div><div class="spacer"></div>${right}</div>`;
  }).join("");

  $("checklist").querySelectorAll("[data-goto]").forEach(
    b => b.onclick = () => document.querySelector(`.nav-item[data-tab="${b.dataset.goto}"]`).click());
  const loginBtn = $("btnLoginNow"); if (loginBtn) loginBtn.onclick = () => $("btnStart").click();
  const getOllama = $("btnGetOllama");
  if (getOllama) getOllama.onclick = async () => {
    // Без /mac в адресе: сайт сам покажет сборку под систему пользователя.
    await api().open_url("https://ollama.com/download");
  };
  const startOllama = $("btnStartOllama");
  if (startOllama) startOllama.onclick = async () => {
    startOllama.textContent = "Запускаю…"; startOllama.disabled = true;
    await api().open_ollama_app();
    // Приложению нужно несколько секунд, чтобы поднять сервер на 11434.
    setTimeout(refreshSetup, 4000);
  };

  updateWorkLayout();
}

function isReady() { return !!state.setup && setupRows(state.setup).every(([k]) => state.setup[k]); }

function updateWorkLayout() {
  const running = state.running;
  const ready = isReady();
  $("stateNotReady").style.display = (!running && !ready) ? "" : "none";
  $("stateIdle").style.display = (!running && ready) ? "flex" : "none";
  $("stateRunning").style.display = running ? "flex" : "none";

  // Ширину задаём классом, а не пикселями в style: раньше при каждой смене
  // состояния сюда прописывалось то 288px, то 340px, и карточка заметно
  // дёргалась — а в узком окне ещё и не давала ряду перенестись.
  const fSecond = $("funnelCardSecond");
  fSecond.style.display = ready && !running ? "none" : "";
  fSecond.classList.toggle("narrow", running);

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

const IMPORTANT_RE = /Отклик отправлен|Вакансия подходит|Итоги работы|Проверка закончена|Новых вакансий не появилось|Капч|VPN/i;
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
  // Время и иконка ЛЕЖАТ ВНУТРИ .text как строчные элементы: если они блочные,
  // при выделении мышью браузер вставляет перенос и копия выглядит как
  // «12:07 \n сообщение». Висячий отступ для переносов даёт CSS (text-indent).
  const iconCls = entry.level === "error" ? "err"
    : entry.level === "warn" ? "warn" : cls === "important" ? "ok" : "dim";
  // Пробел перед текстом нужен именно в разметке: иначе при копировании
  // время слипается с сообщением («12:09Готов к работе»).
  div.innerHTML = `<div class="text"><span class="time">${entry.time}</span>` +
    `<span class="icon ${iconCls}">${ICON[icon]}</span> ${esc(entry.text)}</div>`;
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

$("btnCopyLog").onclick = async () => {
  // Копируем то, что сейчас показано фильтром — с временем, как в журнале
  const lines = state.logs.filter(passesFilter).map(e => `${e.time}  ${e.text}`);
  if (!lines.length) return;
  const btn = $("btnCopyLog");
  const r = await api().copy_to_clipboard(lines.join("\n"));
  btn.textContent = r && r.ok ? `Скопировано (${lines.length})` : "Не удалось";
  setTimeout(() => { btn.textContent = "Скопировать"; }, 2000);
};

/* ================= «Сейчас делаю» — выводим из текста журнала ================= */

function updateNowFromLog(text) {
  let m;
  if ((m = text.match(/^🔍 Поиск по запросу: (.+)/))) { state.now.query = m[1]; state.now.page = 1; }
  else if ((m = text.match(/^📍 Режим: (.+)/))) { state.now.region = m[1]; }
  else if ((m = text.match(/(?:парсим|смотрю) страницу (\d+)/i))) { state.now.page = +m[1]; }
  else if ((m = text.match(/^👁️ Открываем вакансию: (.+)/))) { state.now.vacancy = m[1]; state.now.phase = "viewing"; state.now.phaseAt = Date.now(); }
  else if ((m = text.match(/^✨ Вакансия подходит: (.+)/))) { state.now.vacancy = m[1]; state.now.phase = "approved"; state.now.phaseAt = Date.now(); }
  else if ((m = text.match(/^✍️ Пишу сопроводительное — (.+)/))) { state.now.vacancy = m[1]; state.now.phase = "writing"; state.now.phaseAt = Date.now(); }
  else if ((m = text.match(/^✅ Отклик отправлен: (.+)/))) { state.now.vacancy = m[1]; state.now.phase = "applied"; state.now.phaseAt = Date.now(); setTimeout(() => { if (state.now.phase === "applied") clearNow(); }, 2500); }
  else if (/^❌ ИИ отклонил|^⏩|^⏭️/.test(text)) { clearNow(); }
  else if (/^(Новых вакансий не появилось|Проверка закончена)/.test(text)) {
    state.now.round++; state.now.query = null; state.now.page = null; clearNow();
  }
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
  chips.push(`Проверка №${n.round + 1}`);
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
  const stopBtn = $("btnStop");
  stopBtn.style.display = running ? "" : "none";
  if (running) {  // сброс после предыдущей остановки
    stopBtn.disabled = false;
    stopBtn.innerHTML = `<i data-icon="stop11"></i> Остановить`;
    paintIcons(stopBtn);
  }
  $("duration").disabled = running;
  $("sessionLive").style.display = running ? "flex" : "none";
  $("logLiveTag").style.display = running ? "flex" : "none";

  $("statusDot").className = "dot" + (running ? " running" : "");
  $("statusLabel").textContent = running ? "Работает" : "Остановлен";
  $("statusSub").textContent = running ? `Проверка №${state.now.round + 1}` : (isReady() ? "Готов к запуску" : "Нужна настройка");

  clearInterval(state.timerInterval);
  if (running) {
    state.timerInterval = setInterval(() => {
      if (!state.startedAt) return;
      $("sessionLiveText").textContent = "Работает " + fmtDuration(Date.now() - state.startedAt);
    }, 1000);
  } else {
    clearNow();
    setPauseCountdown(null);
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
$("btnStop").onclick = () => {
  // Блокируем сразу: раньше при отсутствии мгновенной реакции пользователь
  // жал несколько раз, и лог засорялся повторными «Останавливаюсь…».
  const b = $("btnStop");
  b.disabled = true;
  b.innerHTML = `<i data-icon="stop11"></i> Останавливаюсь…`;
  paintIcons(b);
  api().stop_agent();
};
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

/* ================= пауза между проверками ================= */

let pauseTimer = null;

function setPauseCountdown(seconds) {
  clearInterval(pauseTimer);
  const note = $("pauseNote");
  if (!seconds) { note.innerHTML = ""; note.title = ""; return; }
  const until = Date.now() + seconds * 1000;
  note.title = "Пауза нужна: вакансии публикуются постепенно, а частые обходы повышают риск блокировки hh.ru";
  const tick = () => {
    const left = Math.max(0, until - Date.now());
    if (left <= 0) { clearInterval(pauseTimer); note.innerHTML = ""; return; }
    const m = Math.floor(left / 60000), s = Math.floor((left % 60000) / 1000);
    note.innerHTML = `${ICON.timer12} Следующая проверка через ${m}:${String(s).padStart(2, "0")}`;
  };
  tick();
  pauseTimer = setInterval(tick, 1000);
}

/* ================= события от Python ================= */

window.onAgentEvent = (event, data) => {
  if (event === "log") addLog(data.line, data.level);
  else if (event === "state") setRunningUi(data.running, data.started_at);
  else if (event === "stats") { state.stats = data; updateWorkLayout(); }
  else if (event === "pause") setPauseCountdown(data && data.seconds);
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

$("toastClose").onclick = hideToast;
document.addEventListener("keydown", e => { if (e.key === "Escape") hideToast(); });
