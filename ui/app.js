/* AbuHH — логика интерфейса. Работает поверх window.pywebview.api (см. ui_app.py)
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

const DEFAULT_SECRET_PLACEHOLDER = "оставьте пустым — не изменится";

/** Хвост ключа в placeholder (не в value — контракт «пустое поле = не
 * менять» остаётся правдой без всякого sentinel-значения). Один вызов на
 * все три поля вместо трёх голых if — раньше без else подсказка не
 * сбрасывалась, и после переключения на пустой сервис поле молча
 * продолжало выглядеть заполненным чужим ключом. */
function renderSecretHints(s) {
  const hints = (s && s._secret_hints) || {};
  for (const [elId, key] of [["tgToken", "tg_bot_token"],
                              ["anthropicKey", "anthropic_api_key"],
                              ["openaiKey", "openai_api_key"]]) {
    const el = $(elId);
    const hint = hints[key];
    el.placeholder = hint || DEFAULT_SECRET_PLACEHOLDER;
    el.classList.toggle("has-secret", !!hint);
  }
}

async function loadSettings() {
  const s = await api().get_settings();
  state.settings = s;

  await renderSiteSeg();

  $("resumeName").value = s.resume.target_name || "";
  $("resumeSummary").value = s.resume.summary || "";
  updateSummaryCount();
  $("screeningSalary").value = s.screening?.salary_expectation || "";
  $("screeningAvailability").value = s.screening?.availability || "";
  $("screeningWorkFormat").value = s.screening?.work_format || "";
  setSwitch("screeningRelocationSwitch", !!s.screening?.relocation_ready);
  await renderLetterStyles();
  setSwitch("letterReviewSwitch", s.letters.review_enabled !== false);

  renderQueryChips();
  setSwitch("titleOnlySwitch", !!s.search.title_only);
  setSwitch("requireLetterSwitch", s.search.require_letter !== false);
  $("exclusions").value = s.search.exclusions || "";

  $("maxPagesVal").textContent = s.search.max_pages_per_query || "до конца";
  $("pauseVal").textContent = s.schedule.cycle_pause_minutes
    ? s.schedule.cycle_pause_minutes + " мин" : "без паузы";

  $("themeSeg").querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.theme === s.ui.theme));
  applyTheme(s.ui.theme);

  renderExperience();
  renderRegions();
  updateRegionsCount();
  renderWorkRegionChips();

  // Считаем страницы ПОСЛЕ того, как заданы и лимит страниц, и регионы:
  // иначе в подсказке оказывалось «≈ 0 страниц».
  updateMaxPagesHint();
  updateQueriesInfo();
  resetFrDirty();

  $("providerSeg").querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.p === s.llm.provider));
  syncProviderFields();
  $("ollamaUrl").value = s.llm.ollama_url;
  $("openaiUrl").value = s.llm.openai_base_url;
  $("openaiModel").value = s.llm.openai_model || "";
  $("anthropicModel").value = s.llm.anthropic_model || "";
  await renderOpenaiPresets();
  // Пресет сервиса подсвечиваем, если адрес совпал с известным
  $("openaiPreset").value =
    [...$("openaiPreset").options].some(o => o.value === s.llm.openai_base_url)
      ? s.llm.openai_base_url : "";
  // Модель Anthropic: известная — выбираем в списке, иначе режим ручного ввода
  const known = [...$("anthropicPreset").options].map(o => o.value).filter(Boolean);
  const isKnown = known.includes(s.llm.anthropic_model);
  $("anthropicPreset").value = isKnown ? s.llm.anthropic_model : "";
  $("anthropicManualWrap").style.display = isKnown ? "none" : "";
  renderSecretHints(s);

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
    screening: {
      salary_expectation: $("screeningSalary").value.trim(),
      availability: $("screeningAvailability").value.trim(),
      work_format: $("screeningWorkFormat").value.trim(),
      relocation_ready: hasClass("screeningRelocationSwitch", "on"),
    },
    letters: { style: activeLetterStyle(), review_enabled: hasClass("letterReviewSwitch", "on") },
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

// Вкладка «Модель» сохраняется своей явной кнопкой (см. markModelDirty/
// btnModelSave), а не общим автосохранением. collect() всегда читает её
// поля из живого DOM, поэтому обычный автосейв с ЛЮБОЙ другой вкладки
// (или даже кнопки на этой же вкладке типа «Отправить тестовое») тайком
// утаскивал бы в файл недописанный ключ или непереключённого провайдера,
// хотя пользователь ещё не нажал «Сохранить». Здесь эти поля вырезаются.
function collectWithoutModel() {
  const data = collect();
  delete data.llm;
  delete data._secrets.openai_api_key;
  delete data._secrets.anthropic_api_key;
  return data;
}

function scheduleSave() {
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(async () => {
    const r = await api().save_settings(collectWithoutModel());
    if (r.ok) {
      $("settingsPath").textContent = r.path;
      flashSaved();
    }
  }, 500);
}

function applyTheme(theme) {
  // "system" — без атрибута, поведение как раньше (только @media
  // prefers-color-scheme, см. styles.css). light/dark — явный оверрайд.
  if (theme === "light" || theme === "dark") {
    document.documentElement.dataset.theme = theme;
  } else {
    delete document.documentElement.dataset.theme;
  }
}

function scheduleTempoSave() {
  // Отдельно от scheduleSave(): та шлёт весь collect(), включая ещё не
  // сохранённые правки резюме/списка запросов (см. collectWithoutModel).
  // Темп (степперы) шлём частично — только эти два поля, чтобы не утащить
  // в файл чужой черновик с вкладки «Фильтры/Резюме».
  clearTimeout(state.tempoSaveTimer);
  state.tempoSaveTimer = setTimeout(async () => {
    const maxPages = /^\d+$/.test($("maxPagesVal").textContent.trim())
      ? parseInt($("maxPagesVal").textContent, 10) : 0;
    const pause = /^\d+$/.test($("pauseVal").textContent.trim())
      ? parseInt($("pauseVal").textContent, 10) : 0;
    const r = await api().save_settings({
      search: {max_pages_per_query: maxPages},
      schedule: {cycle_pause_minutes: pause},
    });
    if (r.ok) { $("settingsPath").textContent = r.path; flashSaved(); }
  }, 500);
}

function flashSaved() {
  const el = $("savedIndicator");
  el.style.display = "flex";
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.display = "none"; }, 2200);
}

function updateSidebarFooter() {
  // Из СОХРАНЁННОГО состояния, а не из живых полей: вкладка «Модель»
  // сохраняется явной кнопкой, и подпись в сайдбаре не должна дёргаться
  // при простом переключении вкладок/провайдера до нажатия «Сохранить».
  const llm = (state.settings && state.settings.llm) || {};
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

/* ================= Фильтры/Резюме: сохранение по кнопке ================= */
// В отличие от остальных вкладок (автосохранение с задержкой), здесь правки
// не должны улетать в файл сами по себе — слишком легко случайно испортить
// профиль или список запросов и не заметить. Регионы (тумблер вкл/выкл и
// модалка добавления) и выбор сайта — исключение: они и так уже требуют
// явного действия и общие с быстрым переключением на вкладке «Работа»,
// поэтому остаются мгновенными, как раньше.

function frSnapshot() {
  // max_pages/pause сюда не входят — они сохраняются отдельно и сразу,
  // см. scheduleTempoSave().
  return JSON.stringify({
    resume: { target_name: $("resumeName").value.trim(), summary: $("resumeSummary").value },
    screening: {
      salary_expectation: $("screeningSalary").value.trim(),
      availability: $("screeningAvailability").value.trim(),
      work_format: $("screeningWorkFormat").value.trim(),
      relocation_ready: hasClass("screeningRelocationSwitch", "on"),
    },
    style: activeLetterStyle(),
    reviewEnabled: hasClass("letterReviewSwitch", "on"),
    queries: state._queries || [],
    title_only: hasClass("titleOnlySwitch", "on"),
    require_letter: hasClass("requireLetterSwitch", "on"),
    exclusions: $("exclusions").value,
    experience: [...document.querySelectorAll("#expList .check.checked")].map(c => c.dataset.v),
  });
}

function markFrDirty() {
  const dirty = frSnapshot() !== state._frSaved;
  for (const tab of ["filters", "resume"]) {
    $(`btn${tab[0].toUpperCase()}${tab.slice(1)}Save`).disabled = !dirty;
    $(`btn${tab[0].toUpperCase()}${tab.slice(1)}Reset`).disabled = !dirty;
    $(`${tab}DirtyHint`).textContent = dirty ? "Есть несохранённые изменения" : "Изменений нет";
    $(`${tab}DirtyHint`).style.color = dirty ? "var(--warn)" : "";
  }
}

function resetFrDirty() {
  state._frSaved = frSnapshot();
  markFrDirty();
}

async function saveFr() {
  const r = await api().save_settings(collectWithoutModel());
  if (!r.ok) { toast("err", "Не удалось сохранить", r.error || ""); return; }
  $("settingsPath").textContent = r.path;
  flashSaved();
  toast("ok", "Сохранено");
  resetFrDirty();
  refreshSetup();
}

async function resetFr() {
  await loadSettings();   // перечитывает всё с диска, включая Фильтры/Резюме
}

$("btnFiltersSave").onclick = saveFr;
$("btnFiltersReset").onclick = resetFr;
$("btnResumeSave").onclick = saveFr;
$("btnResumeReset").onclick = resetFr;

/* ================= мелкие виджеты: переключатели/чекбоксы ================= */

function hasClass(id, cls) { return $(id).classList.contains(cls); }
function setSwitch(id, on) { $(id).classList.toggle("on", !!on); }
function wireSwitch(id, onChange, { noAutosave = false } = {}) {
  const el = $(id);
  el.tabIndex = 0; el.setAttribute("role", "switch");
  el.onclick = () => {
    el.classList.toggle("on");
    onChange && onChange(el.classList.contains("on"));
    // Фильтры/Резюме сохраняются кнопкой — см. markFrDirty().
    if (noAutosave) markFrDirty(); else scheduleSave();
  };
  el.onkeydown = e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); el.click(); } };
}

/* ================= резюме и поиск ================= */

function updateSummaryCount() {
  $("summaryCount").textContent = `${$("resumeSummary").value.length} / 2000`;
}

let letterStylesCache = null;   // [{id, name, desc}] — нейминг живёт в ai_analyzer.py, не здесь

async function loadLetterStyles() {
  if (letterStylesCache) return letterStylesCache;
  letterStylesCache = (await api().get_letter_styles()).styles;
  return letterStylesCache;
}

let openaiPresetsCache = null;   // [{name, url, note}] — список живёт в llm_providers.py, не здесь

/** Опции селекта «Сервис» рисуем из бэкенда, чтобы список не разъезжался
 * с llm_providers.OPENAI_PRESETS (там же его использует мастер первого
 * запуска). HTML оставляет только «Свой адрес» как первую опцию. */
async function renderOpenaiPresets() {
  if (!openaiPresetsCache) openaiPresetsCache = (await api().get_openai_presets()).presets;
  const sel = $("openaiPreset");
  [...sel.querySelectorAll("option[value]:not([value=''])")].forEach(o => o.remove());
  for (const p of openaiPresetsCache) {
    const o = document.createElement("option");
    o.value = p.url;
    o.textContent = p.name;
    o.title = p.note;
    sel.appendChild(o);
  }
}

function activeLetterStyle() {
  return (state.settings.letters && state.settings.letters.style) || "business";
}

/** Ровно один стиль активен — не мультивыбор. Визуально те же переключатели,
 * что и остальные тумблеры на этой вкладке, но по клику включается ТОЛЬКО
 * выбранный, остальные гасятся — как радиогруппа. */
async function renderLetterStyles() {
  const box = $("letterStyles");
  if (!box) return;
  const styles = await loadLetterStyles();
  const active = activeLetterStyle();
  box.innerHTML = styles.map(s => `
    <div class="switch-row">
      <div class="switch${s.id === active ? " on" : ""}" data-style="${s.id}" title="Выбрать стиль «${esc(s.name)}»"><div class="knob"></div></div>
      <div class="text"><div class="t">${esc(s.name)}</div><div class="d">${esc(s.desc)}</div></div>
    </div>`).join("");
  box.querySelectorAll("[data-style]").forEach(el => {
    el.tabIndex = 0; el.setAttribute("role", "radio");
    el.onclick = () => {
      if (el.dataset.style === activeLetterStyle()) return;  // уже выбран
      state.settings.letters = { style: el.dataset.style };
      renderLetterStyles();
      markFrDirty();
    };
    el.onkeydown = e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); el.click(); } };
  });
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
    renderQueryChips(); updateQueriesInfo(); markFrDirty();
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
      markFrDirty();
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
  const r = siteRegionIndices().filter(i => state._regions[i].enabled !== false).length;
  const mp = parseInt($("maxPagesVal").textContent, 10) || 0;
  return q * r * mp;
}

function updateQueriesInfo() {
  const n = (state._queries || []).length;
  $("queriesInfo").textContent = `${n} запрос${n === 1 ? "" : n < 5 ? "а" : "ов"} · один круг ≈ ${totalPages()} страниц`;
}

/* ================= сайт поиска (hh.ru / hh.kz / rabota.by / ...) ================= */

let sitesCache = null;   // {sites, active} — грузится один раз, сбрасывается при смене сайта

async function loadSites() {
  if (sitesCache) return sitesCache;
  sitesCache = await api().get_sites();
  return sitesCache;
}

function activeSiteId() {
  return (state.settings && state.settings.site && state.settings.site.active) || "hh.ru";
}

async function renderSiteSeg(containerId = "siteSeg") {
  // Один и тот же рендерер обслуживает и вкладку «Фильтры» (#siteSeg), и
  // шаг 1 мастера первого запуска (#onboardSiteSeg) — до входа выбор сайта
  // больше нигде не показать (мастер модальный, без выхода до входа).
  const seg = $(containerId);
  if (!seg) return;
  const data = await loadSites();
  const active = activeSiteId();
  seg.innerHTML = data.sites.map(s =>
    `<button data-s="${s.id}" class="${s.id === active ? "active" : ""}">${esc(s.name)}</button>`).join("");
  seg.querySelectorAll("button").forEach(b => b.onclick = async () => {
    if (b.classList.contains("active")) return;
    const r = await api().set_active_site(b.dataset.s);
    if (!r.ok) { toast("err", "Не удалось сменить сайт", r.error); return; }
    // Регионы и справочник другой страны — сбрасываем кэш, дальше всё
    // перечитается из настроек и модалка регионов подтянет свежий список.
    areasCache = null;
    sitesCache = null;
    await loadSettings();
    renderSiteSeg("onboardSiteSeg");
    updateOnboardLoginTitle();
    refreshSetup();
  });
}

function updateOnboardLoginTitle() {
  const el = $("onboardLoginTitle");
  if (!el) return;
  const site = sitesCache && sitesCache.sites.find(s => s.id === activeSiteId());
  el.textContent = "Вход в " + (site ? site.name : "hh.ru");
}

/** Показывает кнопку установки, только пока Camoufox (единственный
 * поддерживаемый браузер) ещё не стоит. Вызывается из refreshSetup() — до
 * первого ответа setup_status() (state.setup ещё null) строка остаётся
 * скрытой, это нормально: она появится сама, как только придёт статус. */
function updateCamoufoxInstallRow() {
  const row = $("camoufoxInstallRow");
  if (!row) return;
  row.style.display = state.setup && !state.setup.browser ? "" : "none";
}

$("btnInstallCamoufox").onclick = async () => {
  $("btnInstallCamoufox").disabled = true;
  $("camoufoxInstallHint").textContent = "Устанавливаю…";
  await api().install_camoufox();
};

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
    markFrDirty();
  });
}

function stepperWire(name, {min, max, step = 1, fmt, autosave = false}) {
  const el = document.querySelector(`[data-stepper="${name}"]`);
  el.querySelectorAll("button").forEach(b => b.onclick = () => {
    const valEl = $(name + "Val");
    // «до конца» — это ноль, обычный parseInt на нём даёт NaN.
    let cur = /^\d+$/.test(valEl.textContent.trim())
      ? parseInt(valEl.textContent, 10) : 0;
    cur = Math.max(min, Math.min(max, cur + (+b.dataset.d) * step));
    valEl.textContent = fmt(cur);
    if (name === "maxPages") { updateMaxPagesHint(); updateQueriesInfo(); }
    // Темп работы — не профиль/резюме, случайно не испортишь, поэтому
    // сохраняем сразу, а не ждём отдельной кнопки «Сохранить» (см. пояснение
    // у frSnapshot про то, что именно требует ручного сохранения).
    if (autosave) scheduleTempoSave(); else markFrDirty();
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

/** Индексы в state._regions, принадлежащие активному сайту. Регионы без
 * ключа "site" (сохранённые до появления мультидоменности) считаются
 * hh.ru — так у существующих пользователей ничего не пропадает. */
function siteRegionIndices() {
  const site = activeSiteId();
  return (state._regions || [])
    .map((r, i) => i)
    .filter(i => (state._regions[i].site || "hh.ru") === site);
}

function enabledCount() {
  return siteRegionIndices().filter(i => state._regions[i].enabled !== false).length;
}

/** Включает/выключает регион. Последний включённый регион ЭТОГО сайта
 * выключить нельзя — агенту нужен хотя бы один, иначе искать негде
 * (см. active_regions в settings.py). */
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
  const indices = siteRegionIndices();
  const single = indices.length <= 1;
  if (!indices.length) {
    box.innerHTML = `<div class="hint">Для этого сайта пока нет регионов — добавьте хотя бы один.</div>`;
  } else {
    box.innerHTML = indices.map(i => {
      const r = state._regions[i];
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
  }
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
  box.innerHTML = siteRegionIndices().map(i => {
    const r = state._regions[i];
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
  // index === undefined — добавление новых (можно сразу несколько регионов
  // и несколько графиков — на все их сочетания заведутся отдельные записи).
  // index задан — редактирование ОДНОЙ существующей записи, там выбор
  // всегда одиночный: "стать четырьмя записями" при редактировании одной
  // не должно.
  const editing = index !== undefined ? state._regions[index] : null;
  const multi = index === undefined;
  state._regionModal = {
    index, multi,
    areaIds: new Map(),     // id -> name, выбранные регионы
    schedules: new Set(),   // выбранные графики; пусто = "любой график"
  };
  if (editing) {
    const areaId = (/area=(\d+)/.exec(editing.params || "") || [])[1];
    if (areaId) state._regionModal.areaIds.set(areaId, editing.name.replace(/\s*\(.*\)$/, ""));
    const sched = scheduleOf(editing.params);
    if (sched) state._regionModal.schedules.add(sched);
  }
  $("regionModalTitle").textContent = editing ? "Изменить регион" : "Добавить регионы";
  $("areaSearch").value = editing ? editing.name.replace(/\s*\(.*\)$/, "") : "";
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
    } else {
      // Пусто — не значит "нечего показать": по клику до ввода текста
      // должен быть виден список верхнеуровневых регионов страны, а не
      // пустая область без подсказки, что тут вообще можно выбрать.
      filterAreas($("areaSearch").value);
    }
  });
  setTimeout(() => $("areaSearch").focus(), 60);
}

/** Регионы верхнего уровня страны — прямые "дети" корневого узла
 * (Москва, области, республики и т.п.), а не весь плоский список: у
 * одной только России в справочнике 15000+ записей, показать их все
 * списком нечитаемо и бессмысленно. Появляются, пока пользователь ещё
 * ничего не напечатал. */
function topLevelAreas() {
  const areas = areasCache?.areas || [];
  const root = areas.find(a => a.parent === "");
  if (!root) return areas.slice(0, 60);
  return areas.filter(a => a.parent === root.name);
}

function filterAreas(q) {
  const query = (q || "").trim().toLowerCase();
  const box = $("areaResults");
  if (!areasCache?.areas?.length) { box.innerHTML = ""; return; }
  const hits = query
    ? areasCache.areas.filter(a => a.name.toLowerCase().includes(query)).slice(0, 30)
    : topLevelAreas();
  const m = state._regionModal;
  box.innerHTML = hits.map(a =>
    `<div class="area-hit${m.areaIds.has(a.id) ? " active" : ""}" data-id="${a.id}" data-name="${esc(a.name)}">
      <span>${esc(a.name)}</span>${a.parent ? `<span class="parent">${esc(a.parent)}</span>` : ""}
    </div>`).join("");
  box.querySelectorAll(".area-hit").forEach(el => el.onclick = () => {
    const id = el.dataset.id, name = el.dataset.name;
    if (m.multi) {
      if (m.areaIds.has(id)) m.areaIds.delete(id); else m.areaIds.set(id, name);
    } else {
      // Одиночный выбор при редактировании: новый клик заменяет прежний.
      m.areaIds = new Map([[id, name]]);
      $("areaSearch").value = name;
    }
    filterAreas($("areaSearch").value);
    updateRegionPreview();
  });
}

function renderScheduleChips() {
  const m = state._regionModal;
  const scheds = areasCache?.schedules ||
    Object.entries(SCHEDULE_LABELS).map(([id, name]) => ({ id, name }));
  $("scheduleChips").innerHTML = scheds.map(s => {
    const active = s.id === "" ? m.schedules.size === 0 : m.schedules.has(s.id);
    return `<button class="sched-chip${active ? " active" : ""}" data-id="${s.id}">${esc(s.name)}</button>`;
  }).join("");
  $("scheduleChips").querySelectorAll("button").forEach(b => b.onclick = () => {
    const id = b.dataset.id;
    if (!m.multi) {
      m.schedules = id ? new Set([id]) : new Set();
    } else if (id === "") {
      m.schedules = new Set();   // "Любой график" несовместим с конкретными — сбрасывает их
    } else {
      m.schedules.delete("");
      if (m.schedules.has(id)) m.schedules.delete(id); else m.schedules.add(id);
    }
    renderScheduleChips();
    updateRegionPreview();
  });
}

/** Все сочетания выбранных регионов × выбранных графиков — каждое станет
 * своей записью в «Где искать» (агент ходит по ним отдельными проходами,
 * см. active_regions в settings.py). Пустой набор графиков — "любой". */
function regionModalResults() {
  const m = state._regionModal;
  const enabled = m.index !== undefined ? state._regions[m.index].enabled !== false : true;
  const site = m.index !== undefined ? (state._regions[m.index].site || "hh.ru") : activeSiteId();
  const manual = $("areaManual").style.display !== "none";
  if (manual) {
    const name = $("manualName").value.trim();
    const params = $("manualParams").value.trim();
    return name && params ? [{ name, params, enabled, site }] : [];
  }
  if (!m.areaIds.size) return [];
  const schedIds = m.schedules.size ? [...m.schedules] : [""];
  const out = [];
  for (const [areaId, areaName] of m.areaIds) {
    for (const sid of schedIds) {
      const schedTag = sid ? ` (${SCHEDULE_LABELS[sid] || sid})` : "";
      out.push({
        name: areaName + schedTag,
        params: `&area=${areaId}` + (sid ? `&schedule=${sid}` : ""),
        enabled, site,
      });
    }
  }
  return out;
}

function updateRegionPreview() {
  const m = state._regionModal;
  const box = $("regionPreview");
  const manual = $("areaManual").style.display !== "none";
  if (manual) {
    box.innerHTML = "";
    $("regionModalSave").disabled = !regionModalResults().length;
    return;
  }
  if (!m.areaIds.size) {
    box.innerHTML = `<span class="hint">выберите хотя бы один регион</span>`;
    $("regionModalSave").disabled = true;
    return;
  }
  // Чипы показывают ВЫБРАННЫЕ РЕГИОНЫ (не все сочетания с графиками —
  // при нескольких графиках их было бы неразборчиво много).
  box.innerHTML = [...m.areaIds.entries()].map(([id, name]) =>
    `<div class="chip">${esc(name)}<button data-rm="${id}" title="Убрать регион">${ICON.remove11}</button></div>`).join("");
  box.querySelectorAll("[data-rm]").forEach(b => b.onclick = () => {
    m.areaIds.delete(b.dataset.rm);
    filterAreas($("areaSearch").value);
    updateRegionPreview();
  });
  $("regionModalSave").disabled = false;
}

$("areaSearch").addEventListener("input", () => {
  // При редактировании новый текст поиска обнуляет прежний одиночный выбор
  // (ищем замену). При добавлении — нет: поиск не должен сбрасывать уже
  // отмеченные регионы.
  if (!state._regionModal.multi) state._regionModal.areaIds = new Map();
  filterAreas($("areaSearch").value);
  updateRegionPreview();
});
["manualName", "manualParams"].forEach(id =>
  $(id).addEventListener("input", updateRegionPreview));

$("regionModalSave").onclick = () => {
  const results = regionModalResults();
  if (!results.length) return;
  const i = state._regionModal.index;
  if (i !== undefined) {
    state._regions[i] = results[0];
  } else {
    // Дедуп по params в пределах текущего сайта — повторное сохранение той
    // же выборки (или пересечение с уже добавленным регионом) не плодит
    // дублей.
    const seen = new Set(state._regions
      .filter(r => (r.site || "hh.ru") === activeSiteId())
      .map(r => r.params));
    for (const r of results) {
      if (seen.has(r.params)) continue;
      state._regions.push(r);
      seen.add(r.params);
    }
  }
  $("regionModalBox").classList.remove("show");
  renderRegions(); updateRegionsCount(); updateQueriesInfo(); renderWorkRegionChips(); scheduleSave();
};
$("regionModalCancel").onclick = () => $("regionModalBox").classList.remove("show");
$("btnAddRegion").onclick = () => openRegionModal(undefined);

function updateRegionsCount() {
  const n = siteRegionIndices().length;
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
  // list_models() отдаёт список ТЕКУЩЕГО активного провайдера (это нужно
  // кнопке «Проверить» и т.п.), а этот селект — конкретно про Ollama. Пока
  // активен другой провайдер, list_models() вернул бы его модели, они
  // осели бы здесь без пометки selected, браузер выбрал бы первую из
  // списка — и следующее любое сохранение настроек утащило бы чужое имя
  // в ollama_model. list_models_detail() всегда именно про Ollama.
  const r = await api().list_models_detail();
  const names = (r.models || []).map(m => m.name);
  sel.innerHTML = names.map(m => `<option${m === want ? " selected" : ""}>${esc(m)}</option>`).join("")
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
  // Сайдбар читает state.settings, а не живые поля — держим их в курсе,
  // иначе следующее markModelDirty() ложно решило бы, что есть несохранённое.
  state.settings.llm = collect().llm;
  resetModelDirty();
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
    state.settings.llm = collect().llm;      // сайдбар читает state.settings, не поля
    resetModelDirty();                       // сохранили — помечать нечего
    updateSidebarFooter();
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

// Подпись «сохранён» и hasKey() ниже раньше смотрели на _secrets,
// привязанный к адресу, с которым открывалась вкладка — после переключения
// сервиса (Groq → Gemini) подпись врала, что ключ уже есть, и молча
// разблокировала «Загрузить список моделей» на чужом ключе. Спрашиваем
// подсказку ЗАНОВО для нового адреса, до сохранения.
async function refreshOpenaiKeyHint() {
  const url = $("openaiUrl").value.trim();
  if (!url) return;
  const { hint } = await api().get_openai_key_hint(url);
  state.settings._secret_hints = state.settings._secret_hints || {};
  state.settings._secret_hints.openai_api_key = hint;
  state.settings._secrets.openai_api_key = !!hint;
  renderSecretHints(state.settings);
  syncOpenaiPicker();
}

// OpenAI-совместимые сервисы: пресет заполняет базовый адрес
$("openaiPreset").onchange = () => {
  const url = $("openaiPreset").value;
  if (url) { $("openaiUrl").value = url; }
  markModelDirty();
  syncOpenaiPicker();
  refreshOpenaiKeyHint();
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
$("openaiUrl").addEventListener("input", () => {
  syncOpenaiPicker();
  clearTimeout(state._urlHintTimer);
  state._urlHintTimer = setTimeout(refreshOpenaiKeyHint, 400);
});
$("openaiModel").addEventListener("input", () => { syncOpenaiPicker(); });

// Anthropic: список известных моделей + возможность ввести своё имя
$("anthropicPreset").onchange = () => {
  const v = $("anthropicPreset").value;
  $("anthropicManualWrap").style.display = v ? "none" : "";
  // Вкладка «Модель» сохраняется явной кнопкой — этот выбор был единственным
  // местом на вкладке, которое ещё сохраняло само, в обход «Сохранить».
  if (v) { $("anthropicModel").value = v; markModelDirty(); }
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
wireSwitch("titleOnlySwitch", () => updateQueriesInfo(), { noAutosave: true });
wireSwitch("requireLetterSwitch", null, { noAutosave: true });
wireSwitch("letterReviewSwitch", null, { noAutosave: true });
wireSwitch("screeningRelocationSwitch", null, { noAutosave: true });

// Степперы «Страниц на запрос» и «Пауза между проверками».
// Функция была написана, но не вызвана — кнопки +/− не работали вовсе.
stepperWire("maxPages", { min: 0, max: 10, fmt: v => v === 0 ? "до конца" : String(v), autosave: true });
stepperWire("pause", { min: 0, max: 120, step: 5, fmt: v => v === 0 ? "без паузы" : v + " мин", autosave: true });

// Тема — как темп: применяем и сохраняем сразу по клику, без кнопки
// «Сохранить» (не профиль/резюме, случайно не испортишь).
$("themeSeg").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  $("themeSeg").querySelectorAll("button").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  applyTheme(b.dataset.theme);
  api().save_settings({ ui: { theme: b.dataset.theme } });
});

// Автосохранение всех полей ввода. Раньше слушателей не было совсем:
// правки резюме, адресов и выбор модели в селекте молча терялись.
document.querySelectorAll("input, textarea, select").forEach(el => {
  if (["captchaInput", "pullName", "newQueryInput", "areaSearch",
       "manualName", "manualParams", "openaiModelFilter",
       "openaiPreset", "anthropicPreset"].includes(el.id)) return;
  // Вкладка «Модель» сохраняется кнопкой — см. markModelDirty().
  if (el.closest("#tab-model")) return;
  // Чат не относится к настройкам вообще — у него свой обработчик отправки.
  if (el.closest("#tab-chat")) return;
  // Фильтры/Резюме — тоже кнопкой, см. markFrDirty() ниже.
  if (el.closest("#tab-filters") || el.closest("#tab-resume")) return;
  el.addEventListener("change", scheduleSave);
  // #tgToken — исключение: на каждое нажатие клавиши scheduleSave() слал бы
  // ещё недописанный токен в collectWithoutModel() (которая специально НЕ
  // вырезает tg_bot_token — это единственный путь его сохранения), и после
  // закрытия окна на полуслове secrets.json оставался с обрезанным токеном.
  // change (уход фокуса/Enter) сохраняет только целиком введённое значение.
  if (el.id === "tgToken") return;
  if (el.tagName === "TEXTAREA" || ["text", "password", "number"].includes(el.type))
    el.addEventListener("input", scheduleSave);
});
$("resumeSummary").addEventListener("input", updateSummaryCount);

// Резюме (название, профиль) и Фильтры (причины отклонить) — те же поля
// ввода, но здесь правки только помечают вкладку как несохранённую.
document.querySelectorAll("#tab-resume input, #tab-resume textarea, "
  + "#tab-filters input, #tab-filters textarea").forEach(el => {
  el.addEventListener("change", markFrDirty);
  if (el.tagName === "TEXTAREA" || ["text", "password", "number"].includes(el.type))
    el.addEventListener("input", markFrDirty);
});

// Смена модели в селекте обновляет и список моделей, и сайдбар
$("ollamaModel").addEventListener("change", () => useModel($("ollamaModel").value));

$("btnTestNotify").onclick = async () => {
  $("notifyStatus").textContent = "Отправляю…";
  await api().save_settings(collectWithoutModel());
  const r = await api().test_notification();
  $("notifyStatus").innerHTML = `<span class="pill ${r.ok ? "pill-ok" : ""}" style="${r.ok ? "" : "color:var(--err)"}">${esc(r.message)}</span>`;
};
$("btnOpenFolder").onclick = () => api().open_settings_folder();
$("btnOpenLogsFolder").onclick = () => api().open_logs_folder();

/* ================= вкладка «Чат» ================= */

state.chatSending = false;
let pendingChatImage = null;   // data URL прикреплённого скриншота или null

// ⟦letter⟧...⟦/letter⟧ оборачивает quick_apply.py вокруг сгенерированного
// сопроводительного письма (см. quick_apply.py) — клик по нему копирует текст.
const CHAT_LETTER_RE = /⟦letter⟧([\s\S]*?)⟦\/letter⟧/g;
const CHAT_URL_RE = /https?:\/\/\S+/g;

function formatChatText(text) {
  if (!text) return "";
  // "\u0001<index>\u0002" — управляющие символы как маркер места вставки;
  // они не встречаются в обычном тексте, поэтому не путаются с числами/суммами.
  const placeholders = [];
  const stash = html => {
    const token = "\u0001" + placeholders.length + "\u0002";
    placeholders.push(html);
    return token;
  };

  let working = text.replace(CHAT_LETTER_RE, (_, letter) =>
    stash(`<span class="letter-copy" data-copy="${esc(letter)}">${esc(letter).replace(/\n/g, "<br>")}</span>`));

  working = working.replace(CHAT_URL_RE, url => {
    // Не захватываем пунктуацию на конце ссылки (точку/запятую/скобку после URL).
    const m = url.match(/^(.*?)([).,;:!?]*)$/);
    const clean = m[1], trail = m[2];
    return stash(`<a href="#" class="chat-link" data-url="${esc(clean)}">${esc(clean)}</a>`) + trail;
  });

  return esc(working).replace(/\n/g, "<br>")
    .replace(/\u0001(\d+)\u0002/g, (_, i) => placeholders[Number(i)]);
}

function renderChatBubble(role, text, opts = {}) {
  $("chatEmpty").style.display = "none";
  const box = $("chatMessages");
  const div = document.createElement("div");
  div.className = "chat-bubble " + role + (opts.error ? " error" : "");
  const img = opts.image ? `<img class="chat-bubble-img" src="${esc(opts.image)}" alt="">` : "";
  // Картинка без подписи — пустой .text не рисуем, иначе пузырь просит место зря.
  const body = text ? `<div class="text">${formatChatText(text)}</div>` : "";
  const retry = opts.error
    ? `<button class="chat-retry" id="chatRetryBtn">Повторить</button>` : "";
  div.innerHTML = img + body + retry;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  if (opts.error) $("chatRetryBtn").onclick = retryChat;
  return div;
}

function renderChatHistory(turns) {
  // Восстанавливает переписку, сохранённую в agent.db (см. database.py:
  // chat_turns) — чат теперь переживает перезапуск приложения.
  if (!turns || !turns.length) return;
  for (const t of turns) {
    renderChatBubble(t.role === "assistant" ? "assistant" : "user", t.text, { image: t.image });
  }
}

$("chatMessages").addEventListener("click", async e => {
  const link = e.target.closest(".chat-link");
  if (link) {
    e.preventDefault();
    await api().open_url(link.dataset.url);
    return;
  }
  const letter = e.target.closest(".letter-copy");
  if (letter) {
    await api().copy_to_clipboard(letter.dataset.copy);
    letter.classList.add("copied");
    setTimeout(() => letter.classList.remove("copied"), 1200);
  }
});

function readImageFile(file) {
  if (!file || !file.type || !file.type.startsWith("image/")) return;
  const reader = new FileReader();
  reader.onload = () => {
    pendingChatImage = reader.result;
    $("chatAttachThumb").src = pendingChatImage;
    $("chatAttachPreview").style.display = "flex";
  };
  reader.readAsDataURL(file);
}

function clearAttachPreview() {
  pendingChatImage = null;
  $("chatAttachThumb").src = "";
  $("chatAttachPreview").style.display = "none";
}

$("btnChatAttach").onclick = () => $("chatImageInput").click();
$("chatImageInput").addEventListener("change", e => {
  const f = e.target.files[0];
  if (f) readImageFile(f);
  e.target.value = "";   // иначе повторный выбор того же файла не даст change
});
$("chatAttachRemove").onclick = clearAttachPreview;

// Вставка скриншота из буфера (Cmd+V) — самый частый способ поделиться
// скриншотом на Mac, наравне с кнопкой-пикером.
$("chatInput").addEventListener("paste", e => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    if (item.type && item.type.startsWith("image/")) {
      e.preventDefault();
      const file = item.getAsFile();
      if (file) readImageFile(file);
      break;
    }
  }
});

function setChatStatus(text) {
  let el = document.getElementById("chatStatusLine");
  if (!el) {
    el = document.createElement("div");
    el.id = "chatStatusLine";
    el.className = "chat-typing";
    el.innerHTML = `<span class="dot"></span><span id="chatStatusText"></span>`;
    $("chatMessages").appendChild(el);
  }
  $("chatStatusText").textContent = text;
  el.style.display = "flex";
  $("chatMessages").scrollTop = $("chatMessages").scrollHeight;
}
function clearChatStatus() {
  const el = document.getElementById("chatStatusLine");
  if (el) el.remove();
}

function setChatSending(on) {
  state.chatSending = on;
  $("btnChatSend").disabled = on;
}

async function sendChatMessage() {
  const input = $("chatInput");
  const text = input.value.trim();
  const image = pendingChatImage;
  if ((!text && !image) || state.chatSending) return;
  input.value = "";
  input.style.height = "auto";
  clearAttachPreview();
  renderChatBubble("user", text, { image });
  setChatSending(true);
  setChatStatus(image ? "Смотрю на скриншот…" : "Печатает…");
  const r = await api().send_chat_message(text, image || null);
  if (!r.ok) {
    clearChatStatus();
    setChatSending(false);
    renderChatBubble("assistant", r.error || "Не удалось отправить сообщение", { error: true });
  }
}

async function retryChat() {
  setChatSending(true);
  setChatStatus("Печатает…");
  await api().retry_last_chat_message();
}

$("btnChatSend").onclick = sendChatMessage;
$("chatInput").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChatMessage(); }
});
$("chatInput").addEventListener("input", () => {
  const el = $("chatInput");
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 140) + "px";
});
$("btnChatReset").onclick = async () => {
  await api().reset_chat();
  $("chatMessages").innerHTML = "";
  $("chatEmpty").style.display = "";
};


/* ================= всплывашка о результате ================= */

let toastTimer = null;
function toast(kind, title, text = "", action = null) {
  // kind: "ok" | "err" | "wait". Ожидание не гасим по таймеру — его сменит итог.
  // action: {label, onClick} — необязательная кнопка в тосте (например,
  // «Я вошёл — сохранить сейчас» на ожидании входа в hh).
  const el = $("toast");
  el.className = "toast show " + kind;
  $("toastTitle").textContent = title;
  $("toastText").textContent = text;
  $("toastText").style.display = text ? "" : "none";
  const actionBtn = $("toastAction");
  if (action) {
    actionBtn.textContent = action.label;
    actionBtn.style.display = "";
    actionBtn.disabled = false;
    actionBtn.onclick = action.onClick;
  } else {
    actionBtn.style.display = "none";
    actionBtn.onclick = null;
  }
  clearTimeout(toastTimer);
  // Успех читается за секунду, ошибку нужно успеть прочитать и скопировать.
  if (kind === "ok") toastTimer = setTimeout(hideToast, 4000);
}
function hideToast() {
  clearTimeout(toastTimer);
  $("toast").classList.remove("show");
  $("toastAction").style.display = "none";
  $("toastAction").onclick = null;
}


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
  // Сайдбар сюда не трогаем: он должен показывать сохранённое состояние,
  // а эта функция вызывается на каждое движение по вкладке, включая
  // переключение провайдера ещё до нажатия «Сохранить».
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
  renderSecretHints(state.settings);
  resetModelDirty();
  updateSidebarFooter();
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
  const site = (s && s.site) || "hh.ru";
  return [
    ["browser", "Браузер (Camoufox)", "установлен"],
    modelRow,
    ["logged_in", `Вход в аккаунт ${site}`, "откроется окно браузера, код придёт как обычно"],
    ["resume", "Название резюме", `должно совпадать с заголовком на ${site}`],
    ["summary", "Профиль для писем", "модель пишет письма строго по этому тексту"],
  ];
}

async function refreshSetup() {
  const s = await api().setup_status();
  if (s.error) return;
  state.setup = s;
  updateCamoufoxInstallRow();
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

  renderFunnelTab();

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

function renderFunnelTab() {
  // Воронка теперь на отдельной вкладке «Статистика» — не привязана к
  // running/idle состоянию вкладки «Работа», просто отражает state.stats.
  const s = state.stats || {};
  const empty = !(s.viewed || s.hard_skipped);
  renderFunnel($("funnelCard"), s, empty);
}

/* Кольцевая диаграмма воронки — по макету maket/HH Agent macOS v2.dc.html,
   экран "3a Статистика — кольцевая диаграмма воронки". Пять концентрических
   колец вместо горизонтальных полос: каждое кольцо — доля от total,
   радиусы/толщина/geometry перенесены из мокапа дословно (r=124..52,
   stroke-width=14, viewBox 280×280). Числа/цвета — не мокап, а живые данные
   через те же поля Stats, что раньше питали бары (см. git-историю функции). */
function renderFunnel(container, stats, dashes) {
  if (!container) return;
  const s = stats || {};
  const total = (s.viewed || 0) + (s.hard_skipped || 0);

  if (dashes || total === 0) {
    container.innerHTML = `<div class="empty-tip">
      <div class="t">Тут появится воронка</div>
      <div class="d">За вечер агент обычно смотрит около 40 вакансий и отправляет 3–8 откликов. Как только цикл пройдёт хотя бы раз — здесь будут кольца.</div>
    </div>`;
    return;
  }

  const rings = [
    ["Просмотрено", total, 124, "var(--faint)"],
    ["Прошли фильтр", s.viewed || 0, 106, "var(--dim)"],
    ["Одобрены ИИ", s.ai_pass || 0, 88, "color-mix(in srgb, var(--accent) 70%, var(--faint))"],
    ["Письма написаны", s.letters || 0, 70, "var(--accent)"],
    ["Отклики", s.applied || 0, 52, "var(--ok)"],
  ];
  // Как и у баров раньше: ненулевому значению — минимум 2% дуги, иначе
  // маленькие проценты (3% откликов) были бы не видны глазом на кольце.
  const frac = v => total > 0 ? Math.max(v > 0 ? 0.02 : 0, Math.min(1, v / total)) : 0;
  const pct = v => total > 0 ? Math.round((v / total) * 100) : 0;

  const circles = rings.map(([, val, r, color]) => {
    const c = 2 * Math.PI * r;
    const off = c * (1 - frac(val));
    return `<circle cx="140" cy="140" r="${r}" fill="none" stroke="var(--surf2)" stroke-width="14"/>`
      + `<circle cx="140" cy="140" r="${r}" fill="none" stroke="${color}" stroke-width="14" `
      + `stroke-linecap="round" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}" `
      + `style="transition:stroke-dashoffset 500ms ease-out"/>`;
  }).join("");

  const legend = rings.map(([label, val, , color], i) => {
    const last = i === rings.length - 1;
    return `<div style="display:flex;align-items:center;gap:12px">
      <div style="width:11px;height:11px;border-radius:50%;background:${color};flex:none"></div>
      <div style="font-size:13px;color:var(--text);flex:1">${esc(label)}</div>
      <div style="font:${last ? 700 : 600} 15px/1 -apple-system;font-variant-numeric:tabular-nums;color:${last ? "var(--ok)" : "var(--text)"}">${val}</div>
      <div style="width:44px;text-align:right;font-size:11.5px;color:${last ? "var(--ok)" : "var(--faint)"}">${pct(val)}%</div>
    </div>`;
  }).join("");

  const breakdownData = [
    ["Отсеяно по названию", s.hard_skipped || 0, "var(--faint)"],
    ["Отклонил ИИ", s.ai_reject || 0, "var(--warn)"],
    ["Уже был отклик", s.already || 0, "var(--faint)"],
    ["Не открылось", s.skipped_page || 0, "var(--faint)"],
    ["Не удалось", s.apply_failed || 0, "var(--err)"],
  ];
  const maxBreak = Math.max(1, ...breakdownData.map(([, v]) => v));
  const breakdown = breakdownData.map(([label, val, color]) => {
    const r = 27, c = 2 * Math.PI * r;
    const off = c * (1 - (val > 0 ? val / maxBreak : 0));
    return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:10px">
      <div style="position:relative;width:64px;height:64px">
        <svg width="64" height="64" viewBox="0 0 64 64" style="transform:rotate(-90deg)">
          <circle cx="32" cy="32" r="${r}" fill="none" stroke="var(--surf2)" stroke-width="7"/>
          ${val > 0 ? `<circle cx="32" cy="32" r="${r}" fill="none" stroke="${color}" stroke-width="7" stroke-linecap="round" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"/>` : ""}
        </svg>
        <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font:700 17px/1 -apple-system;font-variant-numeric:tabular-nums${val === 0 ? ";color:var(--faint)" : ""}">${val}</div>
      </div>
      <div style="font-size:11.5px;color:var(--dim);text-align:center;line-height:1.3">${esc(label).replace(" ", "<br>")}</div>
    </div>`;
  }).join("");

  container.innerHTML = `
    <div style="display:flex;gap:34px;align-items:center;flex:1;min-height:0">
      <div style="flex:none;position:relative;width:280px;height:280px">
        <svg width="280" height="280" viewBox="0 0 280 280" style="transform:rotate(-90deg)">${circles}</svg>
        <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center">
          <div style="font:700 26px/1 -apple-system,'SF Pro Display',system-ui,sans-serif;letter-spacing:-.02em;color:var(--ok);font-variant-numeric:tabular-nums">${s.applied || 0}</div>
          <div style="font-size:9.5px;color:var(--dim);margin-top:2px">${(s.applied || 0) === 1 ? "отклик" : "откликов"}</div>
          <div style="font-size:8.5px;color:var(--faint);margin-top:1px">из ${total}</div>
        </div>
      </div>
      <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:11px">
        ${legend}
        <div style="height:1px;background:var(--line2);margin-top:4px"></div>
        <div style="font-size:11.5px;line-height:1.5;color:var(--faint)">Каждое кольцо — доля от просмотренных вакансий. Чем ближе кольца друг к другу по длине дуги, тем меньше теряется на этом шаге.</div>
      </div>
    </div>
    <div class="hairline"></div>
    <div style="display:flex;gap:16px">${breakdown}</div>`;
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

function addLog(text, level = "info", origin = "") {
  const time = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  const important = IMPORTANT_RE.test(text) || level !== "info";
  state.logs.push({ time, level, text, important, origin });
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
  // origin — только у error/warn: у info он был бы почти на каждой из
  // 173 существующих print(), а полезен именно там, где надо локализовать
  // сбой. В обычную "line" его не подмешиваем НИКОГДА — LOG_PATTERNS и
  // updateNowFromLog разбирают текст регулярками с якорем ^.
  const originHtml = (entry.level === "error" || entry.level === "warn") && entry.origin
    ? `<span class="log-origin">${esc(entry.origin)}</span> ` : "";
  // Пробел перед текстом нужен именно в разметке: иначе при копировании
  // время слипается с сообщением («12:09Готов к работе»).
  div.innerHTML = `<div class="text"><span class="time">${entry.time}</span>` +
    `<span class="icon ${iconCls}">${ICON[icon]}</span> ${originHtml}${esc(entry.text)}</div>`;
  box.appendChild(div);
  while (box.children.length > 800) box.removeChild(box.firstChild);
  if (atBottom) box.scrollTop = box.scrollHeight;
  updateLogFooter();
  $("logCount").textContent = state.logs.length ? `${state.logs.length} строк${state.logs.length === 1 ? "а" : ""} за сеанс` : "";
}

// Фильтрует по признакам, записанным на сам DOM-узел (dataset.important/
// dataset.level в renderLogAppend), а не по индексу в state.logs: DOM
// обрезается на 800 узлах, а state.logs — на 4000, после первого обрезания
// индексы расходятся и фильтр начинает прятать не те строки.
function passesFilterEl(el) {
  if (state.filter === "error") return el.dataset.level === "error";
  if (state.filter === "important") return el.dataset.important === "1";
  return true;
}

function reflowLogFilter() {
  $("log").querySelectorAll(".log-line").forEach(el => {
    el.classList.toggle("hidden-by-filter", !passesFilterEl(el));
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
  // classList.add() бросает SyntaxError на пустой строке-токене — с
  // условным вторым аргументом ("" для не-error) это обрывало обработчик
  // ДО reflowLogFilter(): переключение на «Всё»/«Важное» не подсвечивало
  // кнопку и не снимало старый фильтр со строк.
  b.classList.add("active");
  if (state.filter === "error") b.classList.add("err-tab");
  reflowLogFilter();
});
$("logShowAll").onclick = () => { state.filter = "all"; $("logFilter").querySelectorAll("button").forEach(x => x.classList.toggle("active", x.dataset.f === "all")); reflowLogFilter(); };

$("btnCopyLog").onclick = async () => {
  // Копируем то, что сейчас показано фильтром — с временем, как в журнале.
  // Origin дописываем только для не-info строк — ради баг-репортов, не
  // ради дублирования сотен обычных строк текстом, который уже есть.
  const lines = state.logs.filter(passesFilter).map(e =>
    e.level !== "info" && e.origin ? `${e.time}  [${e.origin}] ${e.text}` : `${e.time}  ${e.text}`);
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

// Вынесено из-под кнопки — вызывается и напрямую с «Запустить», и из мастера
// первого запуска (последний шаг «Начать работу»), чтобы не дублировать
// проверку готовности и обработку ошибок в двух местах.
async function startAgent() {
  // Без названия резюме и профиля агент ищет вслепую (по дефолтным
  // запросам вроде "Тестировщик"/"QA") и пишет письма из пустого профиля.
  // CLI (main.py) это блокирует через ensure_configured(), в интерфейсе
  // такой проверки не было вовсе — можно было случайно запустить агента
  // до заполнения «Резюме и поиск» и не заметить. refreshSetup() —
  // не доверяем возможно устаревшему кэшу state.setup.
  await refreshSetup();
  if (!state.setup || !state.setup.resume || !state.setup.summary) {
    toast("err", "Сначала заполните резюме",
      "Название резюме и профиль для писем — на вкладке «Резюме и поиск». Без них агент ищет вслепую.");
    document.querySelector('.nav-item[data-tab="resume"]').click();
    return;
  }
  try {
    $("errorBanner").style.display = "none";
    await api().save_settings(collectWithoutModel());
    const r = await api().start_agent(+$("duration").value);
    if (r && !r.ok) showErrorBanner("Не удалось запустить", r.error || "неизвестная ошибка");
  } catch (e) {
    showErrorBanner("Ошибка при запуске", e && e.message ? e.message : String(e));
  }
}
$("btnStart").onclick = startAgent;
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
  if (event === "log") addLog(data.line, data.level, data.origin || "");
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
  else if (event === "setup_done") {
    // Кнопка установки Camoufox сама спрячется через updateCamoufoxInstallRow(),
    // если ставилось успешно; при неудаче строка остаётся видимой — тогда
    // кнопку нужно разблокировать явно, иначе "Устанавливаю…" зависнет навсегда.
    if (data && !data.ok) {
      $("btnInstallCamoufox").disabled = false;
      $("camoufoxInstallHint").textContent = "Не установлен — " + (data.message || "ошибка");
    }
    refreshSetup();
  }
  else if (event === "await_login") {
    // В мастере первого запуска (шаг «Вход») то же ожидание рендерится
    // прямо в шаге, а не тостом — тост здесь появился бы поверх и рядом с
    // уже видимой на экране кнопкой-подстраховкой, вышло бы дублирование.
    if ($("onboardingBox").classList.contains("show")) {
      if (data && data.site) {
        $("onboardLoginIdle").style.display = "none";
        $("onboardLoginError").style.display = "none";
        $("onboardLoginWait").style.display = "";
        $("onboardLoginWait").querySelector(".hint").textContent =
          `Ждём вход в аккаунт ${data.site} в открывшемся окне — как только он будет виден на странице, продолжим сами.`;
      }
      return;
    }
    if (data && data.site) {
      // Персистентный тост ("wait" не гасится сам) с кнопкой-подстраховкой:
      // авто-детект входа обычно справляется сам, но если разметку hh
      // поменяют и он не сработает, ждать 10 минут молча незачем.
      toast("wait", `Войдите в аккаунт ${data.site}`,
        "Как только вход будет виден на странице — сохраним сами. Если не сработает через минуту-две, нажмите кнопку.",
        { label: "Я вошёл — сохранить сейчас", onClick: async () => {
            $("toastAction").disabled = true;
            await api().confirm_login();
          } });
    } else {
      hideToast();
    }
  }
  else if (event === "wizard_login_done") {
    onboardLoginBusy = false;
    $("onboardLoginWait").style.display = "none";
    if (data && data.ok) {
      $("onboardLoginIdle").style.display = "";
      $("onboardLoginError").style.display = "none";
      showOnboardStep(3);
    } else {
      $("onboardLoginIdle").style.display = "";
      $("onboardLoginError").style.display = "";
      $("onboardLoginError").textContent = (data && data.error) || "Не удалось войти в аккаунт.";
    }
  }
  else if (event === "wizard_resumes_done") {
    $("onboardResumesLoading").style.display = "none";
    if (data && data.ok && data.resumes && data.resumes.length) {
      $("onboardResumesList").style.display = "";
      $("onboardResumesError").style.display = "none";
      renderOnboardResumes(data.resumes);
    } else {
      $("onboardResumesError").style.display = "";
      $("onboardResumesError").textContent = (data && data.error) || "Не удалось найти резюме автоматически.";
    }
  }
  else if (event === "wizard_profile_done") {
    $("onboardProfileLoading").style.display = "none";
    $("onboardProfileForm").style.display = "";
    if (data && data.ok) {
      $("onboardProfileErrorBox").style.display = "none";
      $("onboardResumeSummary").value = data.summary || "";
    } else {
      $("onboardProfileErrorBox").style.display = "";
      $("onboardProfileError").textContent = (data && data.error) || "Не удалось собрать профиль автоматически — впишите сами.";
    }
    updateOnboardSummaryCount();
    onboardValidateStep3();
  }
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
  else if (event === "chat_status") setChatStatus(data.text || "Печатает…");
  else if (event === "chat_reply") {
    clearChatStatus();
    setChatSending(false);
    renderChatBubble("assistant", data.text);
  }
  else if (event === "chat_error") {
    clearChatStatus();
    setChatSending(false);
    renderChatBubble("assistant", data.error || "Произошла ошибка", { error: true });
  }
};

/* ================= мастер первого запуска ================= */
// Полноэкранный гейт: пока вход + резюме + профиль не готовы, обычный
// интерфейс не показывается вовсе. Три шага — Вход / Резюме / Профиль —
// используют мосты wizard_login/wizard_list_resumes/wizard_condense_resume
// (ui_app.py) и события await_login (переиспользован из обычного флоу
// «Запустить», см. onAgentEvent выше) / wizard_login_done /
// wizard_resumes_done / wizard_profile_done.

const onboardState = { chosenResume: null };

function needsOnboarding() {
  return !state.setup || !state.setup.logged_in || !state.setup.resume || !state.setup.summary;
}

function onboardStartStep() {
  // Сайт (шаг 1) больше не сменить без нового входа — пропускаем его, если
  // уже залогинены.
  if (!state.setup.logged_in) return 1;
  if (!state.setup.resume || !state.setup.summary) return 3;
  return 4;
}

function showOnboardStep(n) {
  [1, 2, 3, 4].forEach(i => $("onboardStep" + i).style.display = i === n ? "" : "none");
  document.querySelectorAll(".onboard-dot").forEach(d => {
    const s = +d.dataset.step;
    d.classList.toggle("active", s === n);
    d.classList.toggle("done", s < n);
  });
  if (n === 1) renderSiteSeg("onboardSiteSeg");
  if (n === 2) updateOnboardLoginTitle();
  if (n === 3) enterOnboardResumeStep();
}

function openOnboarding() {
  $("onboardingBox").classList.add("show");
  showOnboardStep(onboardStartStep());
}

function enterOnboardResumeStep() {
  $("onboardResumesLoading").style.display = "";
  $("onboardResumesList").style.display = "none";
  $("onboardResumesError").style.display = "none";
  $("onboardManualResume").style.display = "none";
  $("onboardStep3Actions").style.display = "";
  api().wizard_list_resumes();
}

function renderOnboardResumes(resumes) {
  const box = $("onboardResumesList");
  box.innerHTML = resumes.map((r, i) =>
    `<div class="area-hit" data-i="${i}"><span>${esc(r.title)}</span></div>`).join("");
  box.querySelectorAll("[data-i]").forEach(el => el.onclick = () => onboardPickResume(resumes[+el.dataset.i]));
}

function onboardPickResume(r) {
  onboardState.chosenResume = r;
  showOnboardStep(4);
  $("onboardResumeName").value = r.title;
  $("onboardProfileForm").style.display = "none";
  $("onboardProfileErrorBox").style.display = "none";
  $("onboardProfileLoading").style.display = "";
  $("btnOnboardFinish").disabled = true;
  api().wizard_condense_resume(r.url);
}

function updateOnboardSummaryCount() {
  $("onboardSummaryCount").textContent = `${$("onboardResumeSummary").value.length} / 3000`;
}

function onboardValidateStep3() {
  const ok = $("onboardResumeName").value.trim() && $("onboardResumeSummary").value.trim();
  $("btnOnboardFinish").disabled = !ok;
}

async function finishOnboarding(name, summary) {
  await api().save_settings({ resume: { target_name: name, summary } });
  $("onboardingBox").classList.remove("show");
  // loadSettings(), а не только refreshSetup(): иначе поля на вкладке
  // «Резюме и поиск» остались бы пустыми в живом DOM, и следующий
  // collectWithoutModel() внутри startAgent() отправил бы их назад
  // пустыми, затерев то, что только что сохранил мастер.
  await loadSettings();
  await startAgent();
}

// Отдельный флаг, а не только скрытие кнопки через display:none: без него
// повторный клик (например, если onboardLoginIdle почему-то снова стала
// видимой, пока предыдущий вход ещё не завершился) открывал ВТОРОЙ браузер
// поверх первого — оба висели и ждали, окна множились. Бэкенд теперь тоже
// это отклоняет (AgentBridge._wizard_login_busy), но проверка на клике —
// более быстрая обратная связь.
$("btnOnboardSiteNext").onclick = () => showOnboardStep(2);

let onboardLoginBusy = false;
$("btnOnboardLogin").onclick = () => {
  if (onboardLoginBusy) return;
  onboardLoginBusy = true;
  $("onboardLoginIdle").style.display = "none";
  $("onboardLoginError").style.display = "none";
  $("onboardLoginWait").style.display = "";
  $("onboardLoginWait").querySelector(".hint").textContent = "Открываю браузер…";
  api().wizard_login();
};
$("btnOnboardConfirmLogin").onclick = async () => {
  $("btnOnboardConfirmLogin").disabled = true;
  await api().confirm_login();
};
$("btnOnboardManualResume").onclick = () => {
  $("onboardResumesLoading").style.display = "none";
  $("onboardResumesList").style.display = "none";
  $("onboardResumesError").style.display = "none";
  $("onboardStep3Actions").style.display = "none";
  $("onboardManualResume").style.display = "";
};
$("btnOnboardManualFinish").onclick = async () => {
  const name = $("onboardManualName").value.trim();
  const summary = $("onboardManualSummary").value.trim();
  if (!name || !summary) return;
  await finishOnboarding(name, summary);
};
$("btnOnboardRetryProfile").onclick = () => {
  if (!onboardState.chosenResume) return;
  $("onboardProfileErrorBox").style.display = "none";
  $("onboardProfileForm").style.display = "none";
  $("onboardProfileLoading").style.display = "";
  api().wizard_condense_resume(onboardState.chosenResume.url);
};
$("btnOnboardFinish").onclick = async () => {
  await finishOnboarding($("onboardResumeName").value.trim(), $("onboardResumeSummary").value.trim());
};
$("onboardResumeName").addEventListener("input", onboardValidateStep3);
$("onboardResumeSummary").addEventListener("input", () => { updateOnboardSummaryCount(); onboardValidateStep3(); });

/* ================= стартовая инициализация ================= */

window.addEventListener("pywebviewready", async () => {
  await loadSettings();
  const st = await api().get_state();
  state.stats = st.stats;
  await refreshSetup();
  setRunningUi(st.running, st.started_at ? st.started_at : null);
  addLog("Готов к работе.");
  renderChatHistory(await api().get_chat_history());
  if (needsOnboarding()) openOnboarding();
});

$("toastClose").onclick = hideToast;
document.addEventListener("keydown", e => { if (e.key === "Escape") hideToast(); });
