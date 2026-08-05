"""Тесты для DOM-взаимодействия hh_client.py — раньше этот слой был не
покрыт вообще (только ai_analyzer-уровень промптов/сентинела). Playwright
здесь не поднимается — Locator/Page подменены минимальными фейками,
достаточными для проверки логики повторного резолва и all-or-nothing.
"""
import hh_client


class FakeLocator:
    """Минимальная замена Playwright Locator для fill/type/check/input_value."""

    def __init__(self, *, value="", fill_ok=True, count=1, checked=False, visible=True):
        self.value = value
        self.fill_ok = fill_ok
        self._count = count
        self.checked = checked
        self.visible = visible
        self.fill_calls = []
        self.type_calls = []
        self.check_calls = 0

    async def fill(self, text):
        if not self.fill_ok:
            raise RuntimeError("fill failed")
        self.fill_calls.append(text)
        self.value = text

    async def click(self):
        pass

    async def type(self, text, delay=0):
        self.type_calls.append(text)
        self.value = text

    async def input_value(self):
        return self.value

    async def count(self):
        return self._count

    async def check(self, timeout=None):
        self.check_calls += 1
        self.checked = True

    async def is_checked(self):
        return self.checked

    async def is_visible(self, timeout=None):
        return self.visible


class FakePage:
    """Отдаёт канонический вид _COLLECT_TASK_FIELDS_JS/_COLLECT_TASK_CHOICES_JS
    и локаторы по точному селектору — этого достаточно для answer_employer_
    questions/choices, которые ничего больше у page не спрашивают."""

    def __init__(self, task_fields=None, choice_groups=None, locators=None):
        self._task_fields = task_fields or []
        self._choice_groups = choice_groups or []
        self._locators = locators or {}

    async def evaluate(self, js):
        if js is hh_client._COLLECT_TASK_FIELDS_JS:
            return self._task_fields
        if js is hh_client._COLLECT_TASK_CHOICES_JS:
            return self._choice_groups
        return []

    def locator(self, selector):
        return self._locators.get(selector, FakeLocator(count=0))


# ---------- fill_letter: гонка со протухшим локатором (см. плана "Фикс 2.1") ----------

async def test_fill_letter_does_not_trust_a_different_field_at_verify_time():
    # Ключевая гарантия фикса: verify обязан смотреть на то же поле, что
    # реально заполнялось, а не на что попало. Если resolve() каждый раз
    # отдаёт СВЕЖИЙ, никогда не заполнявшийся локатор (имитация «на странице
    # в этот момент вообще другое поле»), заполнение технически проходит
    # без исключения, но verify честно видит пустоту — успеха быть не должно.
    # Старая реализация (single stale Locator, переиспользованный между
    # fill() и input_value()) для такого сценария была неприменима вообще —
    # именно поэтому гонка была возможна: она СЛУЧАЙНО могла попасть на
    # чужое непустое поле и доверить ему целиком. Здесь проверяем обратную,
    # более базовую гарантию: заполнение поля, которое verify не подтвердил,
    # не считается успехом ни при каких обстоятельствах.
    async def resolve():
        return FakeLocator(value="")  # каждый вызов — новый, ещё пустой объект

    ok = await hh_client.fill_letter(resolve, "hello")
    assert ok is False


async def test_fill_letter_succeeds_when_resolve_returns_stable_field():
    field = FakeLocator(value="")

    async def resolve():
        return field

    ok = await hh_client.fill_letter(resolve, "hello")
    assert ok is True
    assert field.value == "hello"


async def test_fill_letter_returns_false_when_field_never_found():
    async def resolve():
        return None

    ok = await hh_client.fill_letter(resolve, "hello")
    assert ok is False


# ---------- answer_employer_questions: all-or-nothing ----------

async def test_answer_employer_questions_all_or_nothing_on_no_data(monkeypatch):
    fields = [
        {"index": 0, "name": "task_1_text", "id": "", "visible": True, "disabled": False, "label": "Вопрос 1"},
        {"index": 1, "name": "task_2_text", "id": "", "visible": True, "disabled": False, "label": "Вопрос 2"},
    ]
    loc1 = FakeLocator(count=1)
    loc2 = FakeLocator(count=1)
    page = FakePage(task_fields=fields, locators={
        'textarea[name="task_1_text"]': loc1,
        'textarea[name="task_2_text"]': loc2,
    })

    answers = iter(["Ответ 1", hh_client.NO_DATA_SENTINEL])

    async def fake_answer(*a, **k):
        return next(answers)
    monkeypatch.setattr(hh_client, "answer_employer_question", fake_answer)

    ok = await hh_client.answer_employer_questions(page, "Title", "Desc")
    assert ok is False
    # Второй вопрос — NO_DATA, значит писать не должны были вообще ничего.
    assert loc1.fill_calls == []
    assert loc2.fill_calls == []


async def test_answer_employer_questions_writes_all_on_success(monkeypatch):
    fields = [{"index": 0, "name": "task_1_text", "id": "", "visible": True, "disabled": False, "label": "Вопрос 1"}]
    loc = FakeLocator(count=1)
    page = FakePage(task_fields=fields, locators={'textarea[name="task_1_text"]': loc})

    async def fake_answer(*a, **k):
        return "Ответ"
    monkeypatch.setattr(hh_client, "answer_employer_question", fake_answer)

    ok = await hh_client.answer_employer_questions(page, "Title", "Desc")
    assert ok is True
    assert loc.fill_calls == ["Ответ"]


# ---------- answer_employer_choices: радио/чекбокс ----------

async def _choice_group(name="task_1_option", type_="radio"):
    return {
        "name": name,
        "type": type_,
        "questionLabel": "Формат работы?",
        "options": [
            {"value": "office", "id": "", "checked": False, "visible": True, "disabled": False, "label": "В офисе"},
            {"value": "remote", "id": "", "checked": False, "visible": True, "disabled": False, "label": "Удалённо"},
        ],
    }


async def test_answer_employer_choices_checks_selected_option(monkeypatch):
    group = await _choice_group()
    office_loc = FakeLocator(count=1)
    remote_loc = FakeLocator(count=1)
    page = FakePage(choice_groups=[group], locators={
        'input[name="task_1_option"][value="office"]': office_loc,
        'input[name="task_1_option"][value="remote"]': remote_loc,
    })

    async def fake_choice(*a, **k):
        return ["Удалённо"]
    monkeypatch.setattr(hh_client, "answer_employer_choice", fake_choice)

    ok = await hh_client.answer_employer_choices(page, "Title", "Desc")
    assert ok is True
    assert remote_loc.check_calls == 1
    assert office_loc.check_calls == 0


async def test_answer_employer_choices_no_data_sentinel_falls_back(monkeypatch):
    group = await _choice_group()
    page = FakePage(choice_groups=[group])

    async def fake_choice(*a, **k):
        return hh_client.NO_DATA_SENTINEL
    monkeypatch.setattr(hh_client, "answer_employer_choice", fake_choice)

    ok = await hh_client.answer_employer_choices(page, "Title", "Desc")
    assert ok is False


async def test_answer_employer_choices_no_groups_is_noop_success():
    page = FakePage(choice_groups=[])
    ok = await hh_client.answer_employer_choices(page, "Title", "Desc")
    assert ok is True


# ---------- response_confirmed: "спроси сам сайт" ----------

class _FakeConfirmLocator:
    def __init__(self, count=0):
        self._count = count

    async def wait_for(self, **kwargs):
        pass

    async def count(self):
        return self._count


async def test_response_confirmed_true_when_no_response_link():
    class FakePageConfirmed:
        async def goto(self, *a, **k):
            pass

        def locator(self, selector):
            return _FakeConfirmLocator(count=0)

    ok = await hh_client.response_confirmed(FakePageConfirmed(), "https://hh.ru/vacancy/1")
    assert ok is True


async def test_response_confirmed_false_when_response_link_present():
    class FakePageNotConfirmed:
        async def goto(self, *a, **k):
            pass

        def locator(self, selector):
            return _FakeConfirmLocator(count=1)

    ok = await hh_client.response_confirmed(FakePageNotConfirmed(), "https://hh.ru/vacancy/1")
    assert ok is False


async def test_response_confirmed_false_on_navigation_error():
    class FakePageBroken:
        async def goto(self, *a, **k):
            raise RuntimeError("nav failed")

    ok = await hh_client.response_confirmed(FakePageBroken(), "https://hh.ru/vacancy/1")
    assert ok is False
