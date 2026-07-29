/* Заглушка данных для работы дизайнера.
 *
 * Подключается только если интерфейс открыт в обычном браузере (без Python).
 * В приложении этот файл не срабатывает: там window.pywebview существует.
 *
 * Как смотреть: открыть ui/index.html в Safari или Chrome двойным щелчком.
 * Тёмная тема переключается системной темой macOS.
 */
(function () {
  if (window.pywebview) return;  // запущено в приложении — не мешаем

  const settings = {
    search: {
      queries: ["Тестировщик", "QA", "Инженер по тестированию", "Специалист по тестированию"],
      title_only: true,
      max_pages_per_query: 2,
      regions: [
        { name: "Москва (любой график)", params: "&area=1", enabled: true },
        { name: "Вся Россия (только удаленка)", params: "&area=113&schedule=remote", enabled: true },
      ],
      experience: ["between1And3", "between3And6", "moreThan6"],
      require_letter: true,
    },
    resume: {
      target_name: "Тестировщик",
      summary: "Middle QA Engineer с коммерческим опытом более 3 лет в продуктовой и заказной разработке.\n\nСейчас работаю в текущем месте работы — low-code платформе автоматизации. Протестировал более 300 API-интеграций, настроил свыше 300 систем аутентификации, разработал 50+ автотестов на Playwright.\n\nСтек: JavaScript, Playwright, Postman, Git, Docker, PostgreSQL, REST, SOAP, XML.",
    },
    llm: {
      provider: "ollama",
      ollama_url: "http://localhost:11434",
      ollama_model: "gemma4:e4b-it-qat",
      openai_base_url: "http://localhost:1234/v1",
      openai_model: "",
      anthropic_model: "claude-sonnet-5",
    },
    notifications: {
      // Значение вымышленное: файл лежит в публичном репозитории, настоящий
      // id из него любой желающий смог бы написать владельцу в Telegram.
      desktop_enabled: true, telegram_enabled: true, tg_user_id: "100000001",
      events: { applied: true, reply: true, captcha: true, summary: true, error: true },
    },
    schedule: { cycle_pause_minutes: 10 },
    security: { use_keychain: false },
    _secrets: { tg_bot_token: true, anthropic_api_key: false, openai_api_key: false },
  };

  // Типичные цифры за сеанс
  const stats = { viewed: 40, hard_skipped: 28, ai_pass: 7, ai_reject: 5,
                  letters: 7, applied: 5, already: 2, skipped_page: 1, apply_failed: 0 };

  // Настоящие строки журнала — включая длинные названия вакансий и паузу
  // ~12 с на письме (после "Вакансия подходит" перед "Отклик отправлен")
  const LOG = [
    ["🔍 Поиск по запросу: Тестировщик", "info"],
    ["📍 Режим: Москва (любой график)", "info"],
    ["📄 Смотрю страницу 1 по запросу 'Тестировщик' (Москва (любой график))...", "info"],
    ["⏩ Пропускаем (Неподходящий грейд/профессия — 'руководитель'): Teamlead QA Engineer/ Руководитель команды тестирования в TravelTech", "info"],
    ["⏩ Пропускаем (Неподходящий грейд/профессия — '1с'): QA Engineer 1C / Тестировщик 1С", "info"],
    ["👁️ Открываем вакансию: Специалист по тестированию", "info"],
    ["✨ Вакансия подходит: Специалист по тестированию", "info"],
    ["✍️ Пишу сопроводительное — Специалист по тестированию", "info"],
    ["✅ Отклик отправлен: Специалист по тестированию", "info"],
    ["❌ ИИ отклонил: Инженер по нагрузочному тестированию (Performance QA)", "info"],
    ["🔒 HH показал проверку VPN — нажимаю «Я не использую VPN»...", "warn"],
    ["⏭️ Пропускаю (archived (по тексту страницы)): QA Engineer (Mobile)", "warn"],
    ["⚠️ Не нашёл кнопку отправки отклика: QA Engineer (ITSM)", "error"],
    ["Проверка закончена: новых вакансий 6, следующая в 16:41.", "info"],
  ];

  const ok = (extra) => Promise.resolve(Object.assign({ ok: true }, extra || {}));

  // ?mock=notready показывает экран первого запуска (ничего не настроено)
  const notReady = /notready/.test(location.search);
  const setup = notReady
    ? { browser: true, ollama_installed: true, ollama_running: true, logged_in: false, resume: false, summary: false }
    : { browser: true, ollama_installed: true, ollama_running: true, logged_in: true, resume: true, summary: true };

  const models = [
    { name: "gemma4:e4b-it-qat", size_gb: 3.1, in_use: true },
    { name: "gemma4:12b-it-qat", size_gb: 7.6, in_use: false },
  ];

  window.pywebview = {
    api: {
      get_settings: () => Promise.resolve(JSON.parse(JSON.stringify(settings))),
      save_settings: () => ok({ path: "~/Library/Application Support/HHAgent/settings.json" }),
      get_state: () => Promise.resolve({ running: false, stats: notReady ? Object.fromEntries(Object.keys(stats).map(k => [k, 0])) : stats }),
      setup_status: () => Promise.resolve(setup),
      list_models: () => {
        // Для облачного провайдера отдаём длинный список, как у OpenRouter,
        // чтобы проверялись фильтр и счётчик
        const p = document.querySelector("#providerSeg button.active")?.dataset.p;
        if (p === "openai_compat") return ok({ models: [
          "deepseek/deepseek-r1:free", "deepseek/deepseek-chat-v3:free",
          "meta-llama/llama-3.3-70b-instruct:free", "qwen/qwen-2.5-72b-instruct:free",
          "google/gemma-3-27b-it:free", "mistralai/mistral-nemo:free",
          "openai/gpt-4o-mini", "openai/gpt-4o", "anthropic/claude-sonnet-4.5",
          "google/gemini-2.0-flash-001", "meta-llama/llama-3.1-8b-instruct",
        ]});
        return ok({ models: models.map(m => m.name) });
      },
      copy_to_clipboard: () => ok(),
      list_models_detail: () => ok({ models }),
      delete_model: (name) => { const i = models.findIndex(m => m.name === name); if (i >= 0) models.splice(i, 1); return ok(); },
      check_provider: () => ok({ message: "Ollama готова, модель gemma4:e4b-it-qat" }),
      test_notification: () => ok({ message: "Рабочий стол — ОК; Telegram — ОК" }),
      start_agent: () => { demo(); return ok(); },
      stop_agent: () => { window.onAgentEvent("state", { running: false }); return ok(); },
      pull_model: () => { pullDemo(); return ok(); },
      submit_captcha: () => ok(),
      install_browser: () => ok(),
      open_settings_folder: () => ok(),
      open_url: () => ok(),
      open_ollama_app: () => ok(),
      get_areas: () => ok({
        source: "network",
        areas: [
          { id: "1", name: "Москва", parent: "Россия" },
          { id: "2", name: "Санкт-Петербург", parent: "Россия" },
          { id: "113", name: "Россия", parent: "" },
          { id: "4", name: "Новосибирск", parent: "Новосибирская область" },
          { id: "88", name: "Казань", parent: "Республика Татарстан" },
          { id: "66", name: "Нижний Новгород", parent: "Нижегородская область" },
          { id: "3", name: "Екатеринбург", parent: "Свердловская область" },
          { id: "104", name: "Челябинск", parent: "Челябинская область" },
        ],
        schedules: [
          { id: "", name: "Любой график" },
          { id: "remote", name: "Только удалёнка" },
          { id: "fullDay", name: "Полный день" },
          { id: "flexible", name: "Гибкий график" },
          { id: "shift", name: "Сменный график" },
        ],
      }),
    },
  };

  // Демонстрация работающего агента: журнал наполняется по одной строке,
  // после последней — событие паузы с обратным отсчётом.
  function demo() {
    window.onAgentEvent("state", { running: true, started_at: Date.now() / 1000 - 30 });
    let i = 0;
    const t = setInterval(() => {
      if (i >= LOG.length) {
        clearInterval(t);
        window.onAgentEvent("pause", { seconds: 600 });
        return;
      }
      const [line, level] = LOG[i++];
      window.onAgentEvent("log", { line, level });
      window.onAgentEvent("stats", stats);
    }, 900);
  }

  function pullDemo() {
    let p = 0;
    const t = setInterval(() => {
      p += 7;
      if (p >= 100) {
        clearInterval(t);
        window.onAgentEvent("pull_done", { ok: true, model: "gemma4:e4b-it-qat" });
        return;
      }
      window.onAgentEvent("pull_progress", { status: "pulling manifest", percent: p });
    }, 300);
  }

  // Показать модальное окно капчи: в консоли браузера вызвать showCaptchaDemo()
  window.showCaptchaDemo = function () {
    window.onAgentEvent("captcha", {
      image: "",
      prompt: "Похоже на капчу. Агент застрял на вакансии «Специалист по тестированию». " +
              "Введите текст с картинки (если там два слова — через пробел):",
    });
  };

  // Важно: ждём полной загрузки страницы. mock.js подключается синхронно через
  // document.write ДО icons.js/app.js — setTimeout(0) тут не гарантия: скрипты
  // после него ещё грузятся по сети и могут выполниться позже таймера. window
  // "load" наступает только когда ВСЕ синхронные скрипты уже отработали и
  // app.js точно успел повесить свой addEventListener — иначе событие уходит
  // в пустоту и экран остаётся на статичной заглушке из разметки.
  const fire = () => window.dispatchEvent(new Event("pywebviewready"));
  if (document.readyState === "complete") fire();
  else window.addEventListener("load", fire);
  console.info("Режим предпросмотра для дизайнера. showCaptchaDemo() — окно капчи.");
})();
