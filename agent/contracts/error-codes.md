# Коды ошибок

| Код | Условие |
|---|---|
| `ID_DUPLICATE` | ID повторён в кейсе |
| `ID_MISSING` | ссылка на несуществующий ID |
| `ID_WRONG_TYPE` | ссылка на запись другого типа |
| `CLAIM_NO_BASIS` | `confirmed` без `supports`; `inference` без `derived_from` при любом статусе |
| `SOURCE_MISSING` | файл активного источника отсутствует |
| `SOURCE_CHANGED` | отпечаток файла активного источника другой |
| `CLAIM_WEAK_SOURCE` | основание `confirmed` — отозванный, запрещённый или нечитаемый источник |
| `CLAIM_WEAK_DERIVATION` | `confirmed` опирается на запись не в статусе `confirmed`, на нерассчитанный расчёт на расчёт от слабого источника (включая цепочку расчётов) либо на самого себя через цепочку `derived_from` |
| `CLAIM_CONFLICT_HIDDEN` | `confirmed` при `contradicts` без `resolution` |
| `CLAIM_NOT_CONTESTED` | `contested` без `contradicts` |
| `FACT_NOT_CONFIRMABLE` | `confirmed` при `kind` не `fact` и не `inference` |
| `STATUS_NO_REASON` | отзыв без `status_reason` |
| `LOCATOR_MISMATCH` | вид локатора не подходит типу источника |
| `LOCATOR_NOT_FOUND` | у источника есть извлечённый текст, но локатор не указывает ни на один блок |
| `QUOTE_NOT_FOUND` | `quote` нет в тексте блоков, на которые указывает локатор |
| `WITHDRAWN_USED` | активная запись или текст `outputs/**/*.md` ссылается на отозванную |
| `DECISION_CHOICE_STATE` | `chosen` без `choice`, `proposed` с `choice`, запись без `proposal` и `choice`, выбор направления без варианта либо переданный агенту выбор не того варианта, что предложен |
| `DECISION_NOT_HUMAN` | выбор не опирается на завершённое `human_input` либо предложение не на `model` |
| `DECISION_SUPERSEDE` | замена без причины, заменённое не `superseded`, `superseded` без замены, цикл, ветвление, смена `topic` |
| `DECISION_MULTIPLE_ACTIVE` | больше одной незаменённой записи на `topic` |
| `ACTION_RETRY` | `retry_of` без `retry_reason`, `retry_reason` без `retry_of`, повтор самого себя |
| `ACTION_ORDER` | конец без начала либо два конца |
| `ACTION_COST` | суммы при `unknown`, `estimated` без `estimate_source`, неверный диапазон, денежная оценка без даты тарифа |
| `ACTION_HUMAN` | `human` не на `human_input`, выбор без `DEC`, обход поломки без `note`, бесконечное время, итог за кейс без времени |
| `OUTPUT_LINK` | ссылка в `outputs/**/*.md` на несуществующую запись, на `ACT`, `CKP` либо скобка, похожая на ссылку, но неверная |
| `PATH_UNSAFE` | абсолютный путь, обратная косая черта, выход за корень, источник вне `sources/`, `url` не `http`/`https`, абсолютный путь в `reproduce` |
| `SCHEMA` | нарушение схемы, неверная дата, значение ID с посторонними символами |
| `FORMAT_UNSUPPORTED`, `FORMAT_MIXED` | версия формата |
| `CASE_INVALID` | нет `case.json` либо `case_id` не совпадает с папкой |
| `FILE_CORRUPT` | файл не разбирается как JSON (в UTF-8) |
| `TIME_IN_FUTURE` | `created_at`, `proposed_at` или `chosen_at` записи реестра позже текущего времени больше чем на пять минут |
| `SYNTHETIC_NOT_EVIDENCE` | синтетический источник (в том числе отозванный) в `supports` активного утверждения не `hypothesis` или `confirmed`, в `contradicts` любого активного утверждения либо входом активного расчёта; отозванные и замещённые утверждения и расчёты не проверяются, как и прочие связи |
| `SYNTHETIC_PATH` | `origin: synthetic` вне `sources/interviews/synthetic-*` либо такой путь с другим `origin` |
| `CALC_MISMATCH` | активный расчёт с `method.formula` не следует из входов: значение входа расходится с материалом или с результатом входящего расчёта, результат — с формулой; не выдаётся при уже найденной ошибке ссылки, локатора, цитаты или отзыва этой записи |
| `CHECKPOINT_NONE_VALID` | все checkpoint негодны |
| `CHECKPOINT_CLAIM` | текущий checkpoint называет выбором человека решение без `choice` либо завершённым действие без исхода `completed` |

Предупреждения не меняют код выхода: `BASIS_UNCONFIRMED`, `SOURCE_PARTIAL`, `EXTRACT_MISSING`, `ACTION_UNKNOWN`, `JOURNAL_TORN`, `CHECKPOINT_STALE`, `CHECKPOINT_FALLBACK`, `CALC_UNVERIFIED` (рассчитанный расчёт без `method.formula`: число записано вручную; либо у входа расчёта нет извлечённого текста, и формула проверена только по записанным значениям входов), `REGION_AREA_DIFFERS` (в локаторе `region` область не та, что в наблюдении с этим номером).

Если реестр или журнал не читается (`FILE_CORRUPT`, нарушена общая форма файла), ссылки на записи его типа не проверяются, чтобы одна ошибка не порождала лавину.
