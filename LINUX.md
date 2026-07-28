# HH Agent на Linux

Готовой сборки под Linux нет — есть только `.dmg` под macOS. Запускать нужно
из исходников: приложение на Python, никакой компиляции, но пакеты придётся
поставить руками. Минут 20 с учётом загрузок.

Команды ниже — для Ubuntu/Debian. Для Fedora и Arch пакеты называются иначе,
это отмечено отдельно.

---

## 1. Системные пакеты

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git wl-clipboard
```

`wl-clipboard` нужен, чтобы работала кнопка «Скопировать журнал». На X11
вместо него подойдёт `xclip`.

<details>
<summary>Fedora / Arch</summary>

```bash
# Fedora
sudo dnf install -y python3 python3-pip git wl-clipboard
# Arch
sudo pacman -S --needed python python-pip git wl-clipboard
```
</details>

## 2. Распаковать код и поставить зависимости

Оконная версия живёт в ветке, которой нет на GitHub, — код передаётся
архивом `hh-agent-src.tar.gz` (тем же способом, что и DMG: Телеграм, облако,
флешка).

```bash
mkdir -p ~/hh-ai-agent && tar -xzf hh-agent-src.tar.gz -C ~/hh-ai-agent
cd ~/hh-ai-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

На Linux окно рисует Qt — он ставится готовыми пакетами и подтянется сам,
ничего собирать не нужно.

## 3. Браузер для Playwright

```bash
.venv/bin/playwright install --with-deps chromium
```

`--with-deps` попросит пароль: Chromium тянет за собой системные библиотеки,
их ставит apt. Это ~150 МБ и единственный шаг, где нужен sudo.

## 4. Модель

### Путь А: локальная (бесплатно, нужно 16 ГБ памяти)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma4:e4b-it-qat
```

Установщик заводит службу `ollama.service`, она стартует сама при загрузке.
Проверить: `systemctl status ollama`.

### Путь Б: облачная (ничего не качать, нужен ключ)

Ключ на <https://openrouter.ai> → **Keys**. Вводится потом в самом окне,
на вкладке **Модель** → **OpenAI-совместимая**:
адрес `https://openrouter.ai/api/v1`, модель — любая с пометкой **free**.

## 5. Запуск

```bash
.venv/bin/python ui_app.py
```

Дальше — как в [УСТАНОВКА.md](УСТАНОВКА.md), начиная с шага 3: заполнить
название резюме и профиль для писем, нажать «Запустить», войти в hh.ru руками.

Чтобы не набирать путь каждый раз:

```bash
echo 'alias hhagent="cd ~/hh-ai-agent && .venv/bin/python ui_app.py"' >> ~/.bashrc
```

---

## Чем Linux-версия отличается от macOS

| | macOS | Linux |
|---|---|---|
| Установка | перетащить из DMG | из исходников, команды выше |
| Окно | системный WebKit | Qt WebEngine |
| Уведомления | нужна подпись приложения | работают через D-Bus, подпись не нужна |
| Иконка в трее | есть | зависит от окружения; в GNOME нужен `gnome-shell-extension-appindicator`, без него окно просто работает без иконки |
| Настройки | `~/Library/Application Support/HHAgent` | `~/.config/HHAgent` |

Уведомления на Linux даже проще: macOS показывает их только от подписанного
приложения, а здесь такого требования нет.

## Если что-то пошло не так

| Симптом | Причина и решение |
|---|---|
| `ModuleNotFoundError: No module named 'PyQt6'` | зависимости ставились не из `.venv` — повторите шаг 2 целиком |
| Окно открылось пустым и белым | нет Qt WebEngine: `sudo apt install -y libxcb-cursor0 libnss3 libxkbcommon-x11-0` |
| `Executable doesn't exist … chromium` | пропущен шаг 3 |
| «Модель перестала отвечать» | `systemctl status ollama`, при необходимости `sudo systemctl start ollama` |
| Кнопка «Скопировать журнал» молчит | не установлен `wl-clipboard` (Wayland) или `xclip` (X11) |
| Нет иконки в трее | нормально для GNOME без расширения AppIndicator — на работу агента не влияет |

## Честно о проверке

Код кроссплатформенный, платформенные ветки я проверил подстановкой
`sys.platform` — на Linux уходят `xdg-open`, `systemctl --user start ollama`
и `wl-copy`. Но **живьём на Linux эта сборка не запускалась**: у меня под
рукой только macOS. Вероятнее всего споткнётесь на шаге 2 или 5 — если так,
пришлите вывод команды целиком, поправлю.
