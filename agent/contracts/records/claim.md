# Утверждение `CLM`

```json
{
  "id": "CLM-0001", "created_at": "2026-11-01T11:00:00+03:00",
  "text": "…", "kind": "fact", "status": "confirmed",
  "supports": [{"source_id": "SRC-0001", "locator": {"kind": "section", "heading": "Условия"}, "note": "Что именно подтверждает"}],
  "contradicts": [],
  "derived_from": [],
  "limits": "Только за 2025 год."
}
```

- `kind`: `fact`, `hypothesis`, `forecast`, `inference`. `inference` — вывод из других записей (в том числе прежний ответ модели): он не становится фактом.
- `status`: `confirmed`, `unconfirmed`, `contested`, `withdrawn`. Процент уверенности не записывается.
- `supports`, `contradicts`: списки `{source_id, locator, note}`. У `contradicts` необязательное `resolution` — почему противоречие снято.
- `derived_from`: ID утверждений и расчётов, из которых сделан вывод. `limits`: текст или `null`.
- Подтверждённое (`confirmed`) утверждение: должно быть `fact` или `inference`; у `fact` есть `supports`, у `inference` есть `derived_from`; основание не отозвано, разрешено и читаемо; `derived_from` указывает на подтверждённые утверждения и рассчитанные расчёты; все `contradicts` имеют `resolution`.
- `contested` требует непустого `contradicts`. `withdrawn` требует `status_reason`. Пустое или из одних пробелов `resolution` считается отсутствующим.

Утверждения записываются утилитой `tools/evidence.py` (`add`, `update`, `withdraw`), а не руками: она копирует локатор из блока извлечённого текста, сверяет цитату, проверяет кейс валидатором и при новой ошибке на самом утверждении восстанавливает реестр. Отзыв источника или утверждения понижает зависимые утверждения до `unconfirmed` с пометкой в `limits`; расчёты, варианты, вопросы, решения и тексты `outputs/` утилита только называет. `trace` проходит путь от записи или файла `outputs/` вниз до текста блока, `impact` печатает зависимые записи и файлы. Процедура — [`agent/workflows/evidence.md`](../../workflows/evidence.md), методика — [`agent/knowledge/evidence.md`](../../knowledge/evidence.md).
