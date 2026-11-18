# Проверка этапа

Связь: [общий контракт](README.md). Агент читает этот файл, когда разбирает вывод `gate.py check`; заранее его читать не нужно.

```text
.venv/Scripts/python.exe tools/gate.py check projects/<case-id> <этап> [--preliminary]
```

Этапы: `research`, `options`, `decision`, `strategies`, `validation_plan`, `done`. Проверка накопительная и ничего не пишет. Первая строка вывода — JSON с `passed`, `problems` и `warnings`, элементы содержат код, файл, ID и пояснение без текста материалов. Коды выхода: 0 пройдено, 1 найдены проблемы, 4 отказ, 5 сбой. Ошибки валидатора прекращают проверку: сначала идёт общая проблема `CASE_INVALID` со списком кодов, затем каждая ошибка валидатора отдельной проблемой со своим кодом, файлом и пояснением. Если ошибок так много, что вывод вышел бы за предел `max_output_chars`, последней идёт проблема `VALIDATOR_ERRORS_NOT_SHOWN` с числом непоказанных ошибок: полный список даёт `tools/validate_project.py`. `--preliminary` допустим для `strategies` и `validation_plan`.

| Код | Условие |
|---|---|
| `RESEARCH_EMPTY`, `RESEARCH_OPEN`, `RESEARCH_NO_FINDING`, `RESEARCH_NO_GAP`, `RESEARCH_HIDDEN_CONFLICT` | нет вопросов, вопрос открыт, ответ без оснований, пробел не назван, спорное основание без пробела |
| `OPTIONS_TOO_FEW`, `OPTIONS_NO_KEEP`, `OPTIONS_NO_CLOSE`, `OPTIONS_NO_DEVELOP`, `OPTIONS_DUPLICATE`, `OPTION_FIELD_EMPTY`, `OPTIONS_TABLE` | требования к набору и полям вариантов, вариант не в таблице сравнения |
| `OPTION_COST_MISSING`, `OPTION_COST_UNFOUNDED`, `OPTION_COST_GAP`, `OPTION_GAP_NOT_SHOWN`, `OPTION_LIMIT_MISSING`, `OPTION_LIMIT_STALE`, `OPTION_LIMIT_UNFOUNDED`, `OPTION_LIMIT_COMPARED_MISSING`, `OPTION_LIMIT_COVERAGE_UNFOUNDED` | только при `options_format: 2` в `case.json`, у каждого действующего варианта, на этапе `options` и на всех следующих. Нет `cost`; `estimated` без расчётов, расчёт не `computed` или отозван либо его нет в `basis`; `not_estimated` с вопросом не в статусе `partial` или `blocked` либо без `gap`; вопрос из `cost.question_id` не назван ссылкой `[Q-…]` в разделе `Чего не хватает` файла `outputs/options.md`; нет `limit_check`; `limit_check.brief_decision` не совпадает с действующим решением `brief` в статусе `chosen` (`null`, если такого нет): после нового решения `brief` проверка не проходит ни на одном этапе, пока сверки не пересчитаны; `fits` или `exceeds` без посчитанного расчёта в `basis` (`fits` без `calc_id` допустим при `cost.status: none`), `exceeds` при `cost.status: none` либо любой из двух при `cost.status: not_estimated`; `fits` или `exceeds` без `limit_check.compared` (кроме `fits` без расчёта при `cost.status: none`); `compared: direct_costs_and_lost_revenue` без ссылки `[бриф]`, `[CLM-…]` или `[SRC-…]` в `note`. Сообщение называет поле и что в нём должно быть |
| `OPTIONS_COST_UNKNOWN`, `CHOICE_EXCEEDS_LIMIT` | предупреждения при `options_format: 2`: ни у одного варианта `develop` нет `cost.status: estimated`; предложенный или выбранный вариант направления имеет `limit_check.result: exceeds` (человек вправе выбрать его, если ему об этом сказано) |
| `THRESHOLD_BASIS_MISSING` | только при `options_format: 2` в `case.json`, на `validation_plan` и `done`: в разделе `Условие успеха и неуспеха` файла `outputs/validation-plan.md` нет ни одной ссылки на расчёт `[CALC-…]` или вопрос `[Q-…]`. Проверка грубая: одна ссылка на раздел, а не на каждый порог |
| `PROPOSAL_MISSING`, `DECISION_PENDING` | нет решения `direction`, нет выбора человека |
| `OUTPUT_MISSING`, `OUTPUT_SECTION`, `STATUS_LINE`, `WRONG_OPTION`, `SECTION_UNLINKED`, `PRELIMINARY_LEFT` | структура файла, статус, связь с выбором, раздел с выводом без ссылки, предварительный статус на `done` |
| `PROMISE_TABLE`, `PROMISE_WEAK`, предупреждение `PROMISE_PLANNED` | таблица обещаний, основание неподтверждённое, основание только выбранный вариант |
| `NUMBER_UNLINKED`, `NUMBER_MISMATCH`, `NUMBER_AMBIGUOUS` | число без ссылки, не совпадает с записью или брифом, знак не читается |
| `INTERVIEW_METHOD_MISSING` | есть активный синтетический источник или `outputs/interview-guide.md`, а действующего решения `interview_method` в статусе `chosen` нет; либо источник создан раньше первого выбора способа. Повторный выбор (новое решение `interview_method` через `supersedes`) не отменяет прежние беседы: срок отсчитывается от самого раннего решения по цепочке `supersedes` |
| `INTERVIEW_GUIDE` | `outputs/interview-guide.md`: нет статус-строки `Статус: способ выбран [DEC-…]` либо она не ссылается на действующее решение `interview_method`; нет ни одного раздела `Q-NNNN. …`; раздел `Q-NNNN. …` неверного вида, с несуществующим `Q` или с `Q` не в статусе `partial` или `blocked`. Разделы `Кого спрашивать` и `Согласие и данные` и числа проверяются кодами `OUTPUT_SECTION` и `NUMBER_*` |
| `INTERVIEW_LIMIT` | больше трёх активных синтетических источников без решения `budget` в статусе `chosen`, принятого после первого выбора способа (самого раннего решения `interview_method` по цепочке `supersedes`, повторный выбор срок не сдвигает); считаются только активные источники, заменённая повтором беседа отозвана и не в счёт; либо больше десяти разделов `## Вопрос N` в одной беседе |
| `SYNTHETIC_UNMARKED` | в `outputs/options.md`, `product-strategy.md`, `communication-strategy.md`, `validation-plan.md` или `commitments.md` ссылка на утверждение или расчёт с синтетическим основанием (в `supports` синтетический источник, в том числе отозванный, транзитивно через `derived_from` и входы расчёта) без пометки `(синтетика)` сразу после ссылки; ссылки на варианты пометки не требуют |
| `THRESHOLDS_PENDING`, `THRESHOLDS_NOT_CITED`, `ACTIONS_OPEN` | пороги не согласованы либо согласованы раньше текущего выбора, `outputs/validation-plan.md` не ссылается на действующее решение `thresholds`, действие начато и не закрыто |
| `COMMITMENTS_PENDING` | на `done`: нет действующего решения `commitments` с выбором человека, либо оно принято раньше текущего выбора направления, либо раньше состава в `team.md` (действие из строки `Источник` закончилось позже согласия и это не то же действие) |
| `COMMITMENTS_NOT_CITED` | раздел `Согласование` в `outputs/commitments.md` не ссылается на действующее решение `commitments` |
| `COMMITMENTS_TABLE` | таблица `Действия`: другие колонки, нет строк, в строке пустая ячейка или нет ссылки в `Основание` |
| `TEAM_FORMAT` | на `validation_plan` и `done`: `team.md` есть, но нет строки `Источник` с существующим завершённым действием `human_input`, либо нет ни таблицы с колонками `Имя`, `Роль`, ни строки «Состав не назван», либо имя повторяется, содержит запятую или ячейка пуста |
| `TEAM_UNKNOWN_OWNER` | в `team.md` есть таблица, а в колонке `Ответственный` строки `Действия` стоит имя не из неё; имена через запятую, сравнение точное после сжатия пробелов |
| `TEAM_NOT_ASKED` | предупреждение: `outputs/commitments.md` есть, а `team.md` нет |
| `REVIEW_UNLINKED` | условие пересмотра в `outputs/commitments.md` без ссылки |
| `REVISION_FORMAT` | `outputs/revisions.md`: нет заголовка `# Пересмотры`, неверный заголовок записи, не хватает или повторяется строка записи, `Выбор` или `Прежние версии` не в допустимой форме |
| `REVISION_NUMBER` | номера записей идут не подряд с 1 |
| `REVISION_UNRECORDED` | есть решение `revision`, а журнала нет, либо решение названо не ровно в одной записи |
| `REVISION_NO_SNAPSHOT` | у записи с решением нет непустой папки `outputs/.history/<N>/` |
| `REAPPROVAL_PENDING` | пересмотр затронул `validation-plan.md` или `commitments.md`, а действующее решение `thresholds` или `commitments` принято раньше этого пересмотра |
