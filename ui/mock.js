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
    site: { active: "hh.ru" },
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
    letters: { style: "business", review_enabled: true },
    resume: {
      target_name: "Тестировщик",
      // Демонстрационный профиль: файл открывается в предпросмотре интерфейса,
      // настоящие места работы сюда попадать не должны.
      summary: "Middle QA Engineer с коммерческим опытом более 3 лет в продуктовой и заказной разработке.\n\nСейчас работаю в продуктовой команде: тестирую web и API, отвечаю за релизы и разбор инцидентов на проде.\n\nСтек: JavaScript, Playwright, Postman, Git, Docker, PostgreSQL, REST, SOAP, XML.",
    },
    screening: { salary_expectation: "", relocation_ready: false, availability: "", work_format: "" },
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
    ui: { theme: "system" },
    _secrets: { tg_bot_token: true, anthropic_api_key: false, openai_api_key: false },
  };

  // Хвост ключа считается по хосту адреса — как и в настоящем бэкенде
  // (settings.scoped_secret_name), чтобы в мок-превью тоже было видно, что
  // подсказка меняется при переключении OpenRouter ↔ Gemini, а не залипает.
  const openaiKeysByHost = { "api.groq.com": "gsk_demoDemoKeyDoNotUse1234" };
  function hostOf(url) { try { return new URL(url).hostname; } catch { return ""; } }
  function secretHint(value) {
    if (!value) return null;
    return value.length < 8 ? "••••••" : `•••••• ${value.slice(-4)}`;
  }
  function openaiKeyHint(url) { return secretHint(openaiKeysByHost[hostOf(url)] || ""); }

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
    ["🔒 HH показал проверку VPN — нажимаю «Я не использую VPN»...", "warn", "hh_client.handle_vpn_check:412"],
    ["⏭️ Пропускаю (archived (по тексту страницы)): QA Engineer (Mobile)", "warn", "hh_client.search_and_apply:718"],
    ["⚠️ Не нашёл кнопку отправки отклика: QA Engineer (ITSM)", "error", "hh_client.search_and_apply:964"],
    ["Проверка закончена: новых вакансий 6, следующая в 16:41.", "info"],
  ];

  const ok = (extra) => Promise.resolve(Object.assign({ ok: true }, extra || {}));

  // ?mock=notready показывает экран первого запуска (ничего не настроено)
  const notReady = /notready/.test(location.search);
  const setup = notReady
    ? { browser: true, ollama_installed: true, ollama_running: true, logged_in: false, site: "hh.ru", resume: false, summary: false }
    : { browser: true, ollama_installed: true, ollama_running: true, logged_in: true, site: "hh.ru", resume: true, summary: true };

  const SITES = [
    { id: "hh.ru", name: "hh.ru — Россия" },
    { id: "hh.kz", name: "hh.kz — Казахстан" },
    { id: "hh.uz", name: "hh.uz — Узбекистан" },
    { id: "rabota.by", name: "rabota.by — Беларусь" },
    { id: "hh1.az", name: "hh1.az — Азербайджан" },
  ];

  const models = [
    { name: "gemma4:e4b-it-qat", size_gb: 3.1, in_use: true },
    { name: "gemma4:12b-it-qat", size_gb: 7.6, in_use: false },
  ];

  window.pywebview = {
    api: {
      get_settings: () => {
        const s = JSON.parse(JSON.stringify(settings));
        s._secret_hints = {
          tg_bot_token: secretHint(s._secrets.tg_bot_token ? "123456:AADemoTelegramBotTokenDoNotUse" : ""),
          anthropic_api_key: null,
          openai_api_key: openaiKeyHint(settings.llm.openai_base_url),
        };
        s._secrets.openai_api_key = !!s._secret_hints.openai_api_key;
        return Promise.resolve(s);
      },
      get_openai_key_hint: (url) => Promise.resolve({ hint: openaiKeyHint(url) }),
      // Ведём себя как настоящее приложение: сохранённый ключ дальше виден
      // только фактом наличия, само значение назад не отдаётся.
      save_settings: (d) => {
        if (d && d._secrets && d._secrets.openai_api_key) {
          const url = (d.llm && d.llm.openai_base_url) || settings.llm.openai_base_url;
          openaiKeysByHost[hostOf(url)] = d._secrets.openai_api_key;
          settings._secrets.openai_api_key = true;
        }
        if (d && d.llm) Object.assign(settings.llm, d.llm);
        return ok({ path: "~/Library/Application Support/AbuHH/settings.json" });
      },
      get_state: () => Promise.resolve({ running: false, stats: notReady ? Object.fromEntries(Object.keys(stats).map(k => [k, 0])) : stats }),
      get_applied_jobs: (limit = 200) => Promise.resolve({
        ok: true,
        jobs: [
          { id: "1", title: "Fullstack QA-инженер (Java/Python)", url: "https://hh.ru/vacancy/1", applied_at: new Date().toISOString() },
          { id: "2", title: "Тестировщик", url: "https://hh.ru/vacancy/2", applied_at: new Date(Date.now() - 86400000).toISOString() },
        ],
      }),
      setup_status: () => Promise.resolve(setup),
      get_sites: () => Promise.resolve({ sites: SITES, active: settings.site.active }),
      set_active_site: (id) => { settings.site.active = id; setup.site = id; return ok(); },
      install_camoufox: () => {
        setTimeout(() => {
          setup.browser = true;
          window.onAgentEvent("setup_done", { ok: true, message: "Camoufox установлен." });
        }, 1000);
        return ok();
      },
      confirm_login: () => ok(),
      wizard_login: () => {
        setTimeout(() => window.onAgentEvent("await_login", { site: "hh.ru" }), 200);
        setTimeout(() => window.onAgentEvent("wizard_login_done", { ok: true, site: "hh.ru" }), 1400);
        return ok();
      },
      wizard_list_resumes: () => {
        setTimeout(() => window.onAgentEvent("wizard_resumes_done", { ok: true, resumes: [
          { title: "Тестировщик", url: "https://hh.ru/resume/aaa111bbb222" },
          { title: "QA Engineer (английский intermediate)", url: "https://hh.ru/resume/ccc333ddd444" },
        ] }), 800);
        return ok();
      },
      wizard_condense_resume: () => {
        setTimeout(() => window.onAgentEvent("wizard_profile_done", { ok: true, summary:
          "Middle QA Engineer с коммерческим опытом более 3 лет в продуктовой и заказной разработке.\n\n" +
          "Сейчас работаю в продуктовой команде: тестирую web и API, отвечаю за релизы и разбор инцидентов на проде.\n\n" +
          "Стек: JavaScript, Playwright, Postman, Git, Docker, PostgreSQL, REST, SOAP, XML." }), 1200);
        return ok();
      },
      get_letter_styles: () => Promise.resolve({ styles: [
        { id: "signature", name: "С характером", desc: "Живой голос, цепляющее начало, один личный акцент. Заметнее в потоке, но подходит не всем работодателям." },
        { id: "business", name: "Деловой", desc: "Ровный профессиональный тон, 3–4 абзаца. Так письма писались до сих пор." },
        { id: "strict", name: "Сдержанный", desc: "Короткое официальное письмо: только соответствие требованиям, без эмоций." },
      ] }),
      get_openai_presets: () => Promise.resolve({ presets: [
        { name: "OpenRouter", url: "https://openrouter.ai/api/v1", note: "есть бесплатные модели, ключ обязателен" },
        { name: "Mistral", url: "https://api.mistral.ai/v1", note: "ключ обязателен" },
        { name: "Groq", url: "https://api.groq.com/openai/v1", note: "быстрый, ключ обязателен" },
        { name: "Google Gemini", url: "https://generativelanguage.googleapis.com/v1beta/openai", note: "есть бесплатный лимит, ключ обязателен" },
        { name: "LM Studio", url: "http://localhost:1234/v1", note: "локально, ключ не нужен" },
        { name: "OpenAI", url: "https://api.openai.com/v1", note: "ключ обязателен" },
      ] }),
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
      send_chat_message: (text, image) => { chatDemo(text, false, image); return ok(); },
      retry_last_chat_message: () => { chatDemo(mockLastUserText, true); return ok(); },
      reset_chat: () => ok(),
      get_chat_history: () => Promise.resolve([]),
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
      const [line, level, origin] = LOG[i++];
      window.onAgentEvent("log", { line, level, origin });
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

  // Имитация ответа чата: обычный вопрос, оффтопик, ссылка на резюме,
  // ссылка на вакансию (с командой на отклик или без), скриншот — чтобы
  // вкладку можно было проверить без бэкенда.
  let mockLastUserText = "";
  function chatDemo(text, isRetry = false, image) {
    if (!isRetry) mockLastUserText = text;
    const isResumeLink = /hh\.ru\/resume\//i.test(text);
    const isVacancyLink = /hh\.ru\/vacancy\/\d+/i.test(text);
    const isApplyCommand = /откликнись|откликнитесь|отправ.{0,3}\s+отклик|подай.{0,3}\s+заявку|примени/i.test(text);
    const isOffTopic = /погод|рецепт|футбол/i.test(text);

    if (image) {
      setTimeout(() => window.onAgentEvent("chat_status", { text: "Смотрю на скриншот…" }), 300);
      setTimeout(() => window.onAgentEvent("chat_reply", { text:
        "Судя по скриншоту, это вакансия «Специалист по тестированию» — стек и грейд " +
        "похожи на то, что указано в вашем профиле. Основное расхождение — не видно " +
        "требований к английскому, уточните на собеседовании." }), 1400);
      return;
    }

    if (isVacancyLink && isApplyCommand) {
      const steps = ["Открываю вакансию…", "Пишу сопроводительное…", "Проверяю отклик…"];
      steps.forEach((s, i) => setTimeout(() =>
        window.onAgentEvent("chat_status", { text: s }), 400 + i * 700));
      setTimeout(() => window.onAgentEvent("chat_reply", { text:
        "Готово — откликнулась на «Специалист по тестированию» с сопроводительным письмом." }),
        400 + steps.length * 700);
      return;
    }

    if (isVacancyLink) {
      setTimeout(() => window.onAgentEvent("chat_status", { text: "Открываю вакансию…" }), 300);
      setTimeout(() => window.onAgentEvent("chat_status", { text: "Анализирую…" }), 1000);
      setTimeout(() => window.onAgentEvent("chat_reply", { text:
        "Вакансия в целом подходит: стек и грейд совпадают с профилем. Из настораживающего — " +
        "зарплата не указана явно. Если решите откликнуться, напишите «откликнись на эту вакансию»." }), 1800);
      return;
    }

    setTimeout(() => {
      if (isResumeLink) window.onAgentEvent("chat_status", { text: "Читаю резюме…" });
    }, 300);
    setTimeout(() => {
      if (isResumeLink) {
        window.onAgentEvent("chat_status", { text: "Анализирую…" });
      }
    }, 1000);
    setTimeout(() => {
      if (isResumeLink) {
        window.onAgentEvent("chat_reply", { text:
          "Резюме в целом сильное: понятная структура, есть конкретика по стеку. " +
          "Из того, что стоит усилить — в описании последнего места мало цифр: " +
          "добавьте конкретные метрики (сколько тест-кейсов, на сколько ускорили релизы и т.п.)." });
      } else if (isOffTopic) {
        window.onAgentEvent("chat_reply", { text:
          "Я помогаю только с поиском работы: резюме, вакансии, собеседования и всё в этом духе. " +
          "Задайте вопрос по этой теме — с радостью помогу." });
      } else {
        window.onAgentEvent("chat_reply", { text:
          "Хороший вопрос. Судя по вашему профилю в настройках, стоит подчеркнуть в письме " +
          "именно опыт с автоматизацией — это чаще всего смотрят в первую очередь." });
      }
    }, isResumeLink ? 1800 : 900);
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
