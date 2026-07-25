from dataclasses import dataclass


@dataclass
class Stats:
    """Счётчики за сеанс работы агента."""
    viewed: int = 0         # открыто и рассмотрено вакансий
    hard_skipped: int = 0   # отсечено жёстким фильтром по названию (стоп-слова)
    ai_pass: int = 0        # прошли проверку ИИ (признаны подходящими)
    ai_reject: int = 0      # отклонены ИИ
    letters: int = 0        # сгенерировано сопроводительных писем
    applied: int = 0        # успешно отправленных откликов
    already: int = 0        # вакансия уже была с откликом (кнопки отклика нет)
    apply_failed: int = 0   # подходила, но откликнуться не удалось
    skipped_page: int = 0   # страница не открылась: архив, редирект, капча

    def summary(self) -> str:
        return (
            "📊 <b>Итоги работы агента</b>\n"
            f"• Просмотрено вакансий: <b>{self.viewed}</b>\n"
            f"• Отсечено по стоп-словам: {self.hard_skipped}\n"
            f"• Прошли ИИ-фильтр: {self.ai_pass}\n"
            f"• Отклонены ИИ: {self.ai_reject}\n"
            f"• Написано сопроводительных: {self.letters}\n"
            f"• ✅ Откликов отправлено: <b>{self.applied}</b>\n"
            f"• Уже был отклик (пропущено): {self.already}\n"
            f"• Страница не открылась (архив/капча): {self.skipped_page}\n"
            f"• ⚠️ Не удалось откликнуться: {self.apply_failed}"
        )

    def summary_plain(self) -> str:
        """То же самое, но без HTML — для вывода в консоль."""
        import re
        return re.sub(r"</?b>", "", self.summary())
