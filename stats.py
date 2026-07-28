from dataclasses import dataclass


@dataclass
class Stats:
    """Счётчики за сеанс работы агента."""
    viewed: int = 0         # открыто и рассмотрено вакансий
    fresh: int = 0          # вакансий, встреченных впервые (не из базы), за сеанс
    hard_skipped: int = 0   # отсечено жёстким фильтром по названию (стоп-слова)
    ai_pass: int = 0        # прошли проверку ИИ (признаны подходящими)
    ai_reject: int = 0      # отклонены ИИ
    letters: int = 0        # сгенерировано сопроводительных писем
    applied: int = 0        # успешно отправленных откликов
    applied_no_letter: int = 0  # из них ушедших без сопроводительного
    skipped_no_letter: int = 0  # подходили, но письмо приложить не удалось
    needs_manual: int = 0       # тест работодателя — отклик только вручную
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
            + (f"• ⚠️ Из них без письма: {self.applied_no_letter}\n"
               if self.applied_no_letter else "")
            + (f"• ⚠️ Пропущено — не вышло приложить письмо: {self.skipped_no_letter}\n"
               if self.skipped_no_letter else "")
            + (f"• 📝 Тест работодателя — откликнуться вручную: {self.needs_manual}\n"
               if self.needs_manual else "") +
            f"• Уже был отклик (пропущено): {self.already}\n"
            f"• Страница не открылась (архив/капча): {self.skipped_page}\n"
            f"• ⚠️ Не удалось откликнуться: {self.apply_failed}"
        )

    def summary_plain(self) -> str:
        """То же самое, но без HTML — для вывода в консоль."""
        import re
        return re.sub(r"</?b>", "", self.summary())
