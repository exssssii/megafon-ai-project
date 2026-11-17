# Вариант `OPT`

Поля: `record_status`, `title`, `strategy_kind` (`keep`, `close`, `develop`), `audience_task`, `action`, `rejected_actions` (список), `benefit_mechanism`, `economics` (текст; неизвестное названо неизвестным, числа не выдумываются), `preconditions` (список), `objections` (непустой список), `risks` (список), `reversibility`, `verification`, `basis` (ID утверждений и расчётов). Пустой `basis` допустим, но даёт предупреждение `BASIS_UNCONFIRMED`.

При `options_format: 2` в `case.json` запись получает два необязательных для валидатора объекта (их наличие требует `gate`); без признака они недопустимы (`SCHEMA`). Поле `economics` остаётся: в нём механизм денег словами.

`cost` — что стоит вариант за срок брифа: прямые расходы и недополученная выручка.

| `status` | Поля (других нет) | Смысл |
|---|---|---|
| `estimated` | `calc_ids` — непустой список ID расчётов `CALC`; `note` — что вошло в оценку и что не вошло | оценка есть, хотя бы частичная |
| `none` | `note` — почему затрат нет | обычно `keep` |
| `not_estimated` | `reason`; `question_id` — ID вопроса `Q` | посчитать по материалам нельзя, пробел назван вопросом |

`limit_check` — сверка стоимости с ограничением брифа (бюджет, люди, срок): `brief_decision` — ID действующего решения `topic: brief` в статусе `chosen` либо `null`, если такого решения нет; `result` — `fits`, `exceeds`, `unknown` или `no_limit`; `calc_id` — расчёт сравнения, обязателен при `exceeds` и при `fits`, кроме варианта с `cost.status: none` (там считать нечего, `fits` пишется без `calc_id`), при `unknown` и `no_limit` не пишется; `compared` — что именно сравнивалось с ограничением: `direct_costs` (прямые расходы, включая скидки и компенсации, которые выплачиваются из бюджета) или `direct_costs_and_lost_revenue` (прямые расходы и недополученная выручка, только если известно, что ограничение покрывает и её); допустим только при `fits` и `exceeds`, при `unknown` и `no_limit` не пишется; `gate` требует его у `fits` и `exceeds`, кроме `fits` без `calc_id` при `cost.status: none` (сравнивать нечего), а валидатор запись без него принимает, чтобы прежние кейсы с признаком открывались; `direct_costs_and_lost_revenue` требует в `note` ссылки, откуда известно покрытие: `[бриф]`, `[CLM-…]` или `[SRC-…]`; `note` — обязательна при `unknown` (чего не хватает) и `no_limit` (что бриф ограничения не называет), иначе необязательна; в `note` названо, что именно сравнивалось с ограничением. `no_limit` — утверждение агента о брифе: код его не проверяет, проверяет независимый проверяющий.

```json
"cost": {"status": "estimated", "calc_ids": ["CALC-0004"], "note": "Недополученная выручка за квартал; стоимость разработки не названа"},
"limit_check": {"brief_decision": "DEC-0007", "result": "exceeds", "calc_id": "CALC-0005", "compared": "direct_costs", "note": "Бюджет брифа 600 тыс. руб., прямые расходы варианта 1,2 млн; недополученная выручка показана отдельно"}
```

Валидатор проверяет типы полей и ссылки: `calc_ids`, `calc_id` ведут на расчёты, `question_id` — на вопрос, `brief_decision` — на решение; несуществующий ID — `ID_MISSING`, отозванный расчёт или вопрос — `WITHDRAWN_USED`. Заменённое решение `brief_decision` ошибкой валидатора не считается: это устаревшая сверка, о ней говорит `gate` (`OPTION_LIMIT_STALE`). Связи между полями (`fits` и `exceeds` только при `cost.status` `estimated` или `none`, `exceeds` не при `none`, `fits` без `calc_id` только при `none`, расчёты из `cost.calc_ids` и `limit_check.calc_id` входят в `basis`, расчёт посчитан, вопрос в статусе `partial` или `blocked` с `gap`, наличие `compared` и ссылка в `note` при `direct_costs_and_lost_revenue`) проверяет `gate`; значение `compared` вне перечня и `compared` при `unknown` или `no_limit` — `SCHEMA`.
