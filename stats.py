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
    db_skipped: int = 0     # уже в базе (отклик или отказ раньше) — молча continue, без токенов

    @classmethod
    def load(cls) -> "Stats":
        """Собирает Stats из накопленных в БД тотал-счётчиков (agent.db,
        stats_totals) — чтобы воронка не обнулялась при перезапуске."""
        import database
        totals = database.load_stats_totals()
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in totals.items() if k in known})

    def bump(self, field: str, amount: int = 1):
        """Увеличивает счётчик и сразу же сохраняет его в БД (agent.db),
        чтобы воронка переживала перезапуск приложения (см. database.py:
        stats_totals)."""
        import database
        setattr(self, field, getattr(self, field) + amount)
        database.bump_stat(field, amount)

    def summary(self, baseline: dict | None = None) -> str:
        """baseline — снепшот Stats.__dict__ на начало сеанса. Если передан,
        показывает разницу за сеанс, а не накопленный с БД итог (self.*
        теперь считается нарастающим, см. bump())."""
        def d(field: str) -> int:
            return getattr(self, field) - (baseline.get(field, 0) if baseline else 0)

        return (
            "📊 <b>Итоги работы агента</b>\n"
            f"• Просмотрено вакансий: <b>{d('viewed')}</b>\n"
            f"• Отсечено по стоп-словам: {d('hard_skipped')}\n"
            f"• Прошли ИИ-фильтр: {d('ai_pass')}\n"
            f"• Отклонены ИИ: {d('ai_reject')}\n"
            f"• Написано сопроводительных: {d('letters')}\n"
            f"• ✅ Откликов отправлено: <b>{d('applied')}</b>\n"
            + (f"• ⚠️ Из них без письма: {d('applied_no_letter')}\n"
               if d('applied_no_letter') else "")
            + (f"• ⚠️ Пропущено — не вышло приложить письмо: {d('skipped_no_letter')}\n"
               if d('skipped_no_letter') else "")
            + (f"• 📝 Тест работодателя — откликнуться вручную: {d('needs_manual')}\n"
               if d('needs_manual') else "") +
            f"• Уже был отклик (пропущено): {d('already')}\n"
            f"• Страница не открылась (архив/капча): {d('skipped_page')}\n"
            f"• ⚠️ Не удалось откликнуться: {d('apply_failed')}"
        )

    def summary_plain(self, baseline: dict | None = None) -> str:
        """То же самое, но без HTML — для вывода в консоль."""
        import re
        return re.sub(r"</?b>", "", self.summary(baseline))
