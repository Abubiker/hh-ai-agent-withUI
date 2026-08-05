import ui_app


# ---------- LETTER_INSTRUCTION_RE ----------
# Регэксп-триггер команды в чате «настрой сопроводительные так, чтобы...»
# (см. ui_app._run_chat_turn) — детерминированное действие без обращения
# к модели, поэтому здесь важно не путать команду с обычным вопросом.

def test_letter_instruction_re_matches_common_phrasings():
    matches = [
        "настрой сопроводительные так, чтобы не упоминалось тестовое",
        "поменяй письма, не пиши про тестовое задание",
        "запомни для писем: пиши покороче",
        "не пиши про тестовое задание в сопроводительных",
        "измени, пожалуйста, тон сопроводительного письма на более формальный",
    ]
    for text in matches:
        assert ui_app.LETTER_INSTRUCTION_RE.search(text), text


def test_letter_instruction_re_does_not_match_plain_questions():
    non_matches = [
        "привет как дела",
        "стоит ли мне вообще писать сопроводительные письма для этой вакансии?",
        "откликнись на эту вакансию",
        "расскажи про требования в этой вакансии",
    ]
    for text in non_matches:
        assert not ui_app.LETTER_INSTRUCTION_RE.search(text), text
