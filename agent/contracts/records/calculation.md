# Расчёт `CALC`

```json
{
  "id": "CALC-0001", "created_at": "…", "record_status": "active",
  "inputs": [{"name": "заявки", "source_id": "SRC-0002", "locator": {"kind": "rows", "from": 2, "to": 13}, "column": 2, "aggregate": "sum", "value": 1440, "unit": "заявок", "period": "2025-01..2025-12"}],
  "method": {"kind": "tool", "description": "Среднее число заявок в месяц", "formula": "заявки / 12", "digits": 6},
  "result": {"status": "computed", "value": 120, "unit": "заявок в месяц", "period": "2025"},
  "gaps": [], "assumptions": [],
  "reproduce": "tools/calc.py check projects/<case-id> CALC-0001"
}
```

- Вход расчёта — либо `{name, source_id, locator, value, unit, period}`, либо `{name, calculation_id, value, unit, period}` для результата другого расчёта; ID проверяются как остальные ссылки.
- Необязательные поля входа из источника: `aggregate` (`value`, `sum`, `mean`, `min`, `max`, `count`) — как свёрнуты ячейки диапазона; `column` — номер столбца CSV с 1 (у XLSX столбец задан диапазоном `cells` в одном столбце). Вход-число из текста хранит локатор блока с `quote`, в которой ровно одно число.
- `method.kind`: `tool` или `formula`. `method.formula` — арифметика над именами входов (`+ - * /`, скобки, числа), `method.digits` — знаки округления результата, 0–10, по умолчанию 6, половина вверх. `reproduce` — команда от корня репозитория без личных путей (абсолютный путь — `PATH_UNSAFE`) либо формула.
- `result.status`: `computed` (значение — число) или `not_computable` (значение `null`). `value`, `unit`, `period` неизвестных данных — `null`.
- Запись с `method.formula` валидатор пересчитывает: значения входов из извлечённого текста и результатов входящих расчётов, затем формулу. Арифметика идёт в десятичных числах, поэтому сумма долей, равная целому, даёт ровно ноль. Расхождение — `CALC_MISMATCH`. Рассчитанная запись без формулы либо вход без извлечённого текста — предупреждение `CALC_UNVERIFIED`. Новые поля необязательны, поэтому `format_version` остаётся 1.

Расчёты записываются утилитой `tools/calc.py` (`add`, `check`), отзываются `tools/evidence.py withdraw`. Процедура — [`agent/workflows/calculations.md`](../../workflows/calculations.md), методика — [`agent/knowledge/calculations.md`](../../knowledge/calculations.md).
