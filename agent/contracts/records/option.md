# Вариант `OPT`

Поля: `record_status`, `title`, `strategy_kind` (`keep`, `close`, `develop`), `audience_task`, `action`, `rejected_actions` (список), `benefit_mechanism`, `economics` (текст; неизвестное названо неизвестным, числа не выдумываются), `preconditions` (список), `objections` (непустой список), `risks` (список), `reversibility`, `verification`, `basis` (ID утверждений и расчётов). Пустой `basis` допустим, но даёт предупреждение `BASIS_UNCONFIRMED`.

При `options_format: 2` в `case.json` запись получает два необязательных для валидатора объекта (их наличие требует `gate`); без признака они недопустимы (`SCHEMA`). Поле `economics` остаётся: в нём механизм денег словами.

`cost` — что стоит вариант за срок брифа: прямые расходы и недополученная выручка.

| `status` | Поля (других нет) | Смысл |
|---|---|---|
| `estimated` | `calc_ids` — непустой список ID расчётов `CALC`; `note` — что вошло в оценку и что не вошло | оценка есть, хотя бы частичная |
| `none` | `note` — почему затрат нет | обычно `keep` |
| `not_estimated` | `reason`; `question_id` — ID вопроса `Q` | посчитать по материалам нельзя, пробел назван вопросом |

`limit_check` — сверка стоимости с ограничением брифа (бюджет, люди, срок): `brief_decision` — ID действующего решения `topic: brief` в статусе `chosen` либо `null`, если такого решения нет; `result` — `fits`, `exceeds`, `unknown` или `no_limit`; `calc_id` — расчёт сравнения, обязателен при `exceeds` и при `fits`, кроме варианта с `cost.status: none` (там считать нечего, `fits` пишется без `calc_id`), при `unknown` и `no_limit` не пишется; `note` — обязательна при `unknown` (чего не хватает) и `no_limit` (что бриф ограничения не называет), иначе необязательна; в `note` названо, что именно сравнивалось с ограничением. `no_limit` — утверждение агента о брифе: код его не проверяет, проверяет независимый проверяющий.

```json
"cost": {"status": "estimated", "calc_ids": ["CALC-0004"], "note": "Недополученная выручка за квартал; стоимость разработки не названа"},
"limit_check": {"brief_decision": "DEC-0007", "result": "exceeds", "calc_id": "CALC-0005", "note": "Бюджет брифа 600 тыс. руб., вариант стоит 1,2 млн"}
```

Валидатор проверяет типы полей и ссылки: `calc_ids`, `calc_id` ведут на расчёты, `question_id` — на вопрос, `brief_decision` — на решение; несуществующий ID — `ID_MISSING`, отозванный расчёт или вопрос — `WITHDRAWN_USED`. Заменённое решение `brief_decision` ошибкой валидатора не считается: это устаревшая сверка, о ней говорит `gate` (`OPTION_LIMIT_STALE`). Связи между полями (`fits` и `exceeds` только при `cost.status` `estimated` или `none`, `exceeds` не при `none`, `fits` без `calc_id` только при `none`, расчёты из `cost.calc_ids` и `limit_check.calc_id` входят в `basis`, расчёт посчитан, вопрос в статусе `partial` или `blocked` с `gap`) проверяет `gate`.
