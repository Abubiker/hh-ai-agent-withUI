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
        { name: "Москва (любой график)", params: "&area=1" },
        { name: "Вся Россия (только удаленка)", params: "&area=113&schedule=remote" },
      ],
      experience: ["between1And3", "between3And6", "moreThan6"],
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

  // Настоящие строки журнала — включая длинные названия вакансий
  const LOG = [
    ["🔍 Поиск по запросу: Тестировщик", "info"],
    ["📍 Режим: Москва (любой график)", "info"],
    ["📄 Парсим страницу 1 по запросу 'Тестировщик' (Москва (любой график))...", "info"],
    ["⏩ Пропускаем (Неподходящий грейд/профессия — 'руководитель'): Teamlead QA Engineer/ Руководитель команды тестирования в TravelTech", "info"],
    ["⏩ Пропускаем (Неподходящий грейд/профессия — '1с'): QA Engineer 1C / Тестировщик 1С", "info"],
    ["👁️ Открываем вакансию: Специалист по тестированию", "info"],
    ["✨ Вакансия подходит: Специалист по тестированию", "info"],
    ["✅ Отклик отправлен: Специалист по тестированию", "info"],
    ["❌ ИИ отклонил: Инженер по нагрузочному тестированию (Performance QA)", "info"],
    ["🔒 HH показал проверку VPN — нажимаю «Я не использую VPN»...", "warn"],
    ["⏭️ Пропускаю (archived (по тексту страницы)): QA Engineer (Mobile)", "warn"],
    ["⚠️ Не нашёл кнопку отправки отклика: QA Engineer (ITSM)", "error"],
    ["😴 Круг закончен. Жду 10 мин до следующего.", "info"],
  ];

  const ok = (extra) => Promise.resolve(Object.assign({ ok: true }, extra || {}));

  window.pywebview = {
    api: {
      get_settings: () => Promise.resolve(JSON.parse(JSON.stringify(settings))),
      save_settings: () => ok({ path: "~/Library/Application Support/HHAgent/settings.json" }),
      get_state: () => Promise.resolve({ running: false, stats }),
      setup_status: () => Promise.resolve({
        browser: true, ollama_installed: true, ollama_running: true,
        logged_in: true, resume: true, summary: true }),
      list_models: () => ok({ models: ["gemma4:12b-it-qat", "gemma4:e4b-it-qat"] }),
      check_provider: () => ok({ message: "Ollama готова, модель gemma4:e4b-it-qat" }),
      test_notification: () => ok({ message: "Рабочий стол — ОК; Telegram — ОК" }),
      start_agent: () => { demo(); return ok(); },
      stop_agent: () => { window.onAgentEvent("state", { running: false }); return ok(); },
      pull_model: () => { pullDemo(); return ok(); },
      submit_captcha: () => ok(),
      install_browser: () => ok(),
      open_settings_folder: () => ok(),
      open_url: () => ok(),
    },
  };

  // Демонстрация работающего агента: журнал наполняется по одной строке
  function demo() {
    window.onAgentEvent("state", { running: true });
    let i = 0;
    const t = setInterval(() => {
      if (i >= LOG.length) { clearInterval(t); return; }
      const [line, level] = LOG[i++];
      window.onAgentEvent("log", { line, level });
      window.onAgentEvent("stats", stats);
    }, 700);
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

  window.dispatchEvent(new Event("pywebviewready"));
  console.info("Режим предпросмотра для дизайнера. showCaptchaDemo() — окно капчи.");
})();
