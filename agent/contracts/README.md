# Контракты данных кейса

Этот каталог описывает формат папки кейса `projects/<case-id>/`. По нему исполнитель, не читавший спецификацию, пишет запись любой сущности, а валидатор `tools/validate_project.py` проверяет форму и связи. Источник правил — [спецификация B01](../../docs/specs/2026-11-05-b01-contracts.md); при расхождении верна она.

Состав каталога:

- `versions.json` — поддерживаемые версии формата и миграции;
- `schemas/*.schema.json` — JSON Schema (Draft 2020-12), по одной на тип файла, общие определения в `common.schema.json`;
- `examples/valid/demo-case/` — связный кейс с вымышленными учебными данными, образец для всех типов записей;
- `examples/invalid/<случай>/` — по одному нарушению на папку, только для тестов;
- [`modes.md`](modes.md) — правила режимов продукта и разработки.

Пользовательские кейсы лежат в `projects/`, этот каталог в `.gitignore` и не публикуется. Кейс не должен зависеть от чата, `.work/`, личных путей и секретов.

## Папка кейса

| Путь от корня кейса | Содержимое | Запись |
|---|---|---|
| `case.json` | версия формата, `case_id`, название, дата создания | один раз; единственный обязательный файл |
| `brief.md` | бриф | текст, без контракта |
| `sources/` | копии материалов; извлечённый текст в `sources/extracted/<SRC-id>.*` | файлы не меняются после регистрации |
| `evidence/sources.json` | источники (`SRC`) | реестр |
| `evidence/claims.json` | утверждения (`CLM`) | реестр |
| `evidence/calculations.json` | расчёты (`CALC`) | реестр |
| `research/questions.json` | вопросы исследования (`Q`) | реестр |
| `decisions/options.json` | варианты (`OPT`) | реестр |
| `decisions/decisions.json` | предложения и выбор (`DEC`) | реестр |
| `outputs/**/*.md` | стратегии и план проверки, подпапки допускаются | текст со ссылками вида `[CLM-0001]` |
| `state/actions.jsonl` | журнал действий (`ACT`) | только дописывание |
| `state/checkpoints/CKP-NNNN.json` | checkpoint (`CKP`) | новый файл на каждое сохранение |

Реестр — JSON-объект `{"format_version": 1, "records": [...]}`. Нет файла реестра — реестр пуст. Записи не удаляются: отзыв выражается статусом и причиной.

## ID, даты, пути

- ID имеет вид `ПРЕФИКС-NNNN`, минимум четыре цифры: `SRC`, `CLM`, `CALC`, `Q`, `OPT`, `DEC`, `ACT`, `CKP`. Следующий номер — максимальный существующий плюс один, дополненный нулями.
- ID уникален в кейсе и не используется повторно, в том числе после отзыва. Префикс задаёт тип: ссылка на запись другого типа — ошибка `ID_WRONG_TYPE`.
- `case_id`: строчные латинские буквы, цифры и дефис; совпадает с именем папки кейса.
- Даты — RFC 3339 со смещением: `2026-11-05T14:30:00+03:00` или с `Z`. Несуществующая дата, дата без смещения или с пробелом вместо `T` — `SCHEMA`.
- Значения ID, `topic` и `case_id` проверяются целиком: перевод строки в конце, не-ASCII цифры и лишние символы — `SCHEMA`.
- Пути в записях — относительные от корня кейса, через `/`. Нельзя: начало с `/`, обратная косая черта, двоеточие (буква диска), сегменты `.` и `..`, выход за корень. Нарушение — `PATH_UNSAFE`. Путь файла источника начинается с `sources/`. `url` источника и `source_url` — только `http` или `https`, иначе `PATH_UNSAFE`; абсолютный путь в `reproduce` расчёта — тоже.
- Неизвестное значение записывается как `null`, а не нулём и не пустой строкой. Необязательные поля (`status_reason`, `source_url`, `supersedes`, `change_reason`, `choice`, `retry_of`, `retry_reason`, `resolution`, `quote`, `estimate_source`, `amounts`) при отсутствии значения не пишутся вовсе, `null` в них не допускается. `null` допустим только там, где он назван в описании типа (например, `proposal`, `limits`, `gap`, `model`, `previous`, `sha256` для `url`).

## Версии формата

`format_version` — целое число, сейчас `1`. Оно есть в `case.json`, каждом реестре, каждом checkpoint и каждой строке журнала; у Markdown версии нет. Поддерживаемые версии перечислены в `versions.json`:

```json
{"current": 1, "supported": [1], "migrations": {}}
```

- Неизвестная версия в `case.json`, реестре или строке журнала: `FORMAT_UNSUPPORTED`, валидатор останавливается и записи не читает.
- Checkpoint с неизвестной или отличной от `case.json` версией проверку не останавливает и ошибок `FORMAT_*` не даёт: он негоден, текущим становится более ранний (`CHECKPOINT_FALLBACK`).
- Известная версия, отличная от версии `case.json`: `FORMAT_MIXED`.
- Нет поля или оно не целое (`1.0`, `true`, строка): `SCHEMA`.

Миграция. Несовместимое изменение формата увеличивает версию. Вместе с новой версией добавляется `tools/migrate_v<N>.py`: он создаёт рядом копию кейса в новом формате, исходная папка не меняется, результат проходит валидатор. В `versions.json` в `migrations` записывается ключ `"<N>"` со ссылкой на скрипт, версия добавляется в `supported`. Изменение контракта согласуется с владельцами зависимых задач в PR. Пока версия одна, скрипта нет.

## Статусы и отзыв

| Тип | Как отзывается | Причина |
|---|---|---|
| `SRC`, `CALC`, `Q`, `OPT` | `record_status`: `active` или `withdrawn` | при `withdrawn` обязателен `status_reason` |
| `CLM` | `status: withdrawn` | обязателен `status_reason` |
| `DEC` | `status: superseded` | причина в `change_reason` заменяющего решения |
| `ACT`, `CKP` | не отзываются | — |

Правила связей проверяют только активные записи: отозванные и замещённые остаются в истории и не блокируют кейс. Активная запись не может ссылаться на отозванную (`WITHDRAWN_USED`). Это относится к вариантам, решениям, вопросам, расчётам и ссылкам `[ID]` в `outputs/**/*.md`.

## Записи по типам

Все поля, кроме помеченных необязательными, обязательны. Лишних полей быть не может. У каждой записи реестра есть `id` и `created_at`.

### Источник `SRC`

```json
{
  "id": "SRC-0001", "created_at": "2026-11-01T10:00:00+03:00",
  "record_status": "active",
  "type": "md", "title": "Заметки о сервисе",
  "location": {"path": "sources/service-notes.md"},
  "origin": "user_provided", "allowed": true,
  "retrieved_at": "2026-11-01T10:00:00+03:00",
  "sha256": "<64 hex>",
  "extraction": {"status": "ok", "complete": true, "limits": "Прочитан целиком."}
}
```

- `type`: `md`, `txt`, `docx`, `pdf`, `csv`, `xlsx`, `web`.
- `location`: ровно одно из `path` или `url`. Для `url` `sha256` равен `null`, для `path` это SHA-256 исходных байтов файла (без перекодировки и нормализации строк; 64 символа, строчные hex). Необязательное `source_url` хранит адрес, откуда скачан файл.
- `origin`: `user_provided`, `public_web`, `own_training`. `allowed: false` запрещает использовать источник как подтверждение.
- `extraction.status`: `ok`, `partial`, `empty`, `unreadable`, `unsupported`, `over_limit`, `unavailable` (последнее для сайта); `complete` — логическое; `limits` — текст границ чтения.
- Файл источника не меняется после регистрации: другой отпечаток — `SOURCE_CHANGED`, нужна новая запись источника.
- Отзыв: `record_status: "withdrawn"` и `status_reason`.

### Локатор

Объект с `kind`; указывает точное место в источнике. Необязательное `quote` — короткая цитата.

| `kind` | Поля | Для типов источника |
|---|---|---|
| `section` | `heading` | `md`, `txt`, `docx`, `web` |
| `paragraph` | `index` (с 1) | `docx`, `md`, `txt`, `web` |
| `page` | `page` (с 1) | `pdf` |
| `cells` | `sheet`, `range` | `xlsx` |
| `rows` | `from`, `to` | `csv` |
| `doc_table` | `table`, `row` | `docx`, `web` |

Индексы начинаются с единицы и считаются по извлечённому тексту: абзац отделён пустой строкой, в DOCX абзац — элемент документа. Другой вид для типа источника — `LOCATOR_MISMATCH`.

### Утверждение `CLM`

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

### Расчёт `CALC`

```json
{
  "id": "CALC-0001", "created_at": "…", "record_status": "active",
  "inputs": [{"name": "заявки за месяц", "source_id": "SRC-0002", "locator": {"kind": "rows", "from": 2, "to": 13}, "value": 120, "unit": "заявок", "period": "2025-01..2025-12"}],
  "method": {"kind": "formula", "description": "Среднее по месяцам"},
  "result": {"status": "computed", "value": 10, "unit": "заявок в месяц", "period": "2025-01..2025-12"},
  "gaps": [], "assumptions": [],
  "reproduce": "sum(значения) / 12"
}
```

- Вход расчёта — либо `{name, source_id, locator, value, unit, period}`, либо `{name, calculation_id, value, unit, period}` для результата другого расчёта; ID проверяются как остальные ссылки.
- `method.kind`: `tool` или `formula`. `reproduce` — команда от корня репозитория без личных путей (абсолютный путь — `PATH_UNSAFE`) либо формула.
- `result.status`: `computed` (значение — число) или `not_computable` (значение `null`). `value`, `unit`, `period` неизвестных данных — `null`.

### Вопрос исследования `Q`

Поля: `record_status`, `question`, `affects` (какой выбор зависит от ответа), `needed_evidence`, `status` (`open`, `answered`, `partial`, `blocked`), `findings` (ID утверждений и расчётов), `gap` (текст или `null`).

### Вариант `OPT`

Поля: `record_status`, `title`, `strategy_kind` (`keep`, `close`, `develop`), `audience_task`, `action`, `rejected_actions` (список), `benefit_mechanism`, `economics` (текст; неизвестное названо неизвестным, числа не выдумываются), `preconditions` (список), `objections` (непустой список), `risks` (список), `reversibility`, `verification`, `basis` (ID утверждений и расчётов). Пустой `basis` допустим, но даёт предупреждение `BASIS_UNCONFIRMED`.

### Решение `DEC`

```json
{
  "id": "DEC-0002", "created_at": "…", "topic": "direction", "status": "chosen",
  "proposal": {"option_id": "OPT-0003", "rationale": "…", "proposed_at": "…", "action_id": "ACT-0008"},
  "choice": {"option_id": "OPT-0001", "user_statement": "Слова пользователя", "chosen_at": "…", "action_id": "ACT-0009"},
  "supersedes": "DEC-0001",
  "change_reason": "Пользователь изменил ограничение"
}
```

- `status`: `proposed` (есть только `proposal`), `chosen` (есть `choice`), `superseded` (заменено). Нарушение — `DECISION_CHOICE_STATE`.
- `proposal` равно `null`, если пользователь сам назвал направление без предложения агента: такая запись сразу `chosen`. Необязательные `supersedes` и `change_reason` без значения не пишутся.
- Предложение модели и выбор человека разделены. `proposal.action_id` — действие `model`; `choice.action_id` — завершённое действие `human_input`, в `result_refs` конца которого есть это решение. Иначе `DECISION_NOT_HUMAN`. Выбор может отличаться от предложения.
- `proposed` становится `chosen` на месте: добавляется `choice`. Если меняется предложение или уже сделанный выбор, создаётся новая запись с `supersedes` и `change_reason`, прежняя получает `superseded` (`DECISION_SUPERSEDE`). Замены не образуют цикл.
- `topic` — короткий идентификатор предмета решения: строчные латинские буквы, цифры и `_`, начинается с буквы (`direction` в обязательном сценарии). Замена не меняет `topic`; одну запись заменяет не больше одной (ветвление — `DECISION_SUPERSEDE`). На один `topic` приходится ровно одна незаменённая запись, считая `proposed` (`DECISION_MULTIPLE_ACTIVE`).

### Действие `ACT`

Журнал `state/actions.jsonl` — по строке JSON на событие, у действия две строки:

```json
{"format_version": 1, "id": "ACT-0007", "phase": "start", "kind": "web_read", "tool": "WebFetch", "environment": "claude_code", "model": "…", "input_refs": ["SRC-0002"], "at": "…"}
{"format_version": 1, "id": "ACT-0007", "phase": "end", "outcome": "completed", "result_refs": ["SRC-0002"], "cost": {"basis": "unknown"}, "at": "…"}
```

- `kind`: `model`, `search`, `web_read`, `file_read`, `compute`, `save`, `human_input`. `environment`: `claude_code`, `codex`. `outcome`: `completed`, `failed`, `unknown`. `model` — строка или `null` (модель неизвестна).
- Журнал только дописывается. Каждая строка заканчивается переводом строки. Перед дописыванием писатель отсекает незавершённую последнюю строку (всё после последнего перевода строки): иначе новая запись склеится с обрывком и испортит обе. Порядок: оборвалось, отсекли, дописали.
- Начало без конца означает неизвестный исход: перед повтором его проверяют (`ACTION_UNKNOWN`). Конец без начала или два конца — `ACTION_ORDER`.
- Повтор: на строке `start` необязательное `retry_of` (ID прежнего действия, не само действие) и обязательное при нём `retry_reason`; `retry_reason` без `retry_of` не бывает (`ACTION_RETRY`).
- `cost.basis`: `measured`, `estimated`, `unknown`. Суммы лежат в `amounts` (`input_tokens`, `output_tokens`, `money_usd`; число или `null`, при `estimated` ещё и диапазон `{"min": …, "max": …}`). При `measured` и `estimated` поле `amounts` обязательно: без него `SCHEMA`. При `unknown` полей суммы нет, иначе `ACTION_COST`; при `estimated` обязателен `estimate_source` (`ACTION_COST`); диапазон допустим только в оценке и с `min` не больше `max` (`ACTION_COST`). Состав сумм уточняет задача учёта B13 без изменения этих полей.
- `human_input` агент записывает, когда ответ уже получен: обе строки сразу, `input_refs` пуст (непустой — `SCHEMA`), `result_refs` содержит ID созданной записи (например, решения). Слова пользователя лежат в `user_statement` решения.
- Незавершённая последняя строка (обрыв записи) игнорируется с предупреждением `JOURNAL_TORN`; неразбираемая строка не в конце — `FILE_CORRUPT`.

### Checkpoint `CKP`

```json
{
  "format_version": 1, "id": "CKP-0002", "case_id": "demo-case",
  "created_at": "…", "seq": 2, "previous": "CKP-0001",
  "goal": "…", "stage": "strategies",
  "artifacts": [{"path": "outputs/product-strategy.md", "sha256": "<64 hex>"}],
  "completed_actions": ["ACT-0001"], "human_choices": ["DEC-0002"],
  "open_items": ["…"], "next_step": "…",
  "totals": {"elapsed_seconds": 3600, "time_to_first_result_seconds": 600, "human_active_seconds": null, "cost": {"basis": "unknown"}}
}
```

- `stage`: `intake`, `research`, `options`, `decision`, `strategies`, `validation_plan`, `done`. `previous` — ID прежнего checkpoint либо `null`; `seq` растёт на единицу от `previous`. `totals`: `elapsed_seconds`, `time_to_first_result_seconds` и `human_active_seconds` (число либо `null`), `cost` по правилам учёта действий.
- Отпечаток артефакта служит его версией. Checkpoint записывается новым файлом после записи и проверки артефактов; порядок и атомарность — задача B06.
- Негодный checkpoint: неразбираемый, с нарушением схемы или версии, чужим `case_id`, `id`, не совпавшим с именем файла, отсутствующим артефактом, неверным учётом или нарушением цепочки (`previous` не найден либо `seq` не на единицу больше). Имя файла — только `CKP-NNNN.json`; любой другой файл в `state/checkpoints/` (кроме скрытых, начинающихся с точки) считается негодным. Текущий — годный с наибольшим `seq`. Отдельного указателя нет.
- Текущий checkpoint не должен называть выбором человека решение без `choice` и завершённым — действие без исхода `completed`: это ошибка `CHECKPOINT_CLAIM`.
- Другой отпечаток артефакта не делает checkpoint негодным, а даёт предупреждение `CHECKPOINT_STALE` со списком файлов: агент сверяет их и сохраняет новый checkpoint.
- Если новейший негоден, текущим становится более ранний, и выводится `CHECKPOINT_FALLBACK` с причинами и номерами потерянных файлов (для негодного файла номер берётся из имени, поэтому потеря новейшего видна всегда; посторонние файлы тоже названы в предупреждении). Если негодны все — ошибка `CHECKPOINT_NONE_VALID`. Кейс без checkpoint допустим.

### Тексты `outputs/**/*.md`

Ссылки на записи пишутся в квадратных скобках: `[CLM-0001]`, `[CALC-0002]`, `[OPT-0001]`; несколько ID в одной скобке — через запятую: `[CLM-0001, CLM-0002]`. Ссылка на несуществующую запись либо на `ACT` или `CKP` — `OUTPUT_LINK`. Скобка, которая выглядит как ссылка, но написана неверно (строчные буквы, меньше четырёх цифр), — тоже `OUTPUT_LINK`. Проверяются файлы во всех подпапках `outputs/`. Ссылка на отозванную — `WITHDRAWN_USED`; на неподтверждённое или спорное утверждение — допустимо, но даёт предупреждение `BASIS_UNCONFIRMED`.

## Валидатор

```text
.venv/Scripts/python.exe tools/validate_project.py <папка кейса> [--json]
```

Код выхода: `0` — ошибок нет, `1` — есть ошибки, `2` — неверный вызов, `3` — внутренний сбой валидатора: текст исключения не печатается (там могут оказаться данные кейса), а с `--json` выводится объект с `"internal_error": true`. Вывод содержит код, файл, ID записи и короткое пояснение, без содержимого полей и материалов. `--json` печатает `{"errors": [...], "warnings": [...], "current_checkpoint": "CKP-0002"}`; элемент списка — `{"code", "file", "record_id", "message"}`, `current_checkpoint` — ID текущего checkpoint или `null`. Файл checkpoint — `state/checkpoints/<ID>.json`. Валидатор только читает.

### Коды ошибок

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
| `WITHDRAWN_USED` | активная запись или текст `outputs/**/*.md` ссылается на отозванную |
| `DECISION_CHOICE_STATE` | `chosen` без `choice` либо `proposed` с `choice` |
| `DECISION_NOT_HUMAN` | выбор не опирается на завершённое `human_input` либо предложение не на `model` |
| `DECISION_SUPERSEDE` | замена без причины, заменённое не `superseded`, `superseded` без замены, цикл, ветвление, смена `topic` |
| `DECISION_MULTIPLE_ACTIVE` | больше одной незаменённой записи на `topic` |
| `ACTION_RETRY` | `retry_of` без `retry_reason`, `retry_reason` без `retry_of`, повтор самого себя |
| `ACTION_ORDER` | конец без начала либо два конца |
| `ACTION_COST` | суммы при `unknown`, `estimated` без `estimate_source`, неверный диапазон |
| `OUTPUT_LINK` | ссылка в `outputs/**/*.md` на несуществующую запись, на `ACT`, `CKP` либо скобка, похожая на ссылку, но неверная |
| `PATH_UNSAFE` | абсолютный путь, обратная косая черта, выход за корень, источник вне `sources/`, `url` не `http`/`https`, абсолютный путь в `reproduce` |
| `SCHEMA` | нарушение схемы, неверная дата, значение ID с посторонними символами |
| `FORMAT_UNSUPPORTED`, `FORMAT_MIXED` | версия формата |
| `CASE_INVALID` | нет `case.json` либо `case_id` не совпадает с папкой |
| `FILE_CORRUPT` | файл не разбирается как JSON (в UTF-8) |
| `CHECKPOINT_NONE_VALID` | все checkpoint негодны |
| `CHECKPOINT_CLAIM` | текущий checkpoint называет выбором человека решение без `choice` либо завершённым действие без исхода `completed` |

Предупреждения не меняют код выхода: `BASIS_UNCONFIRMED`, `SOURCE_PARTIAL`, `ACTION_UNKNOWN`, `JOURNAL_TORN`, `CHECKPOINT_STALE`, `CHECKPOINT_FALLBACK`.

Если реестр или журнал не читается (`FILE_CORRUPT`, нарушена общая форма файла), ссылки на записи его типа не проверяются, чтобы одна ошибка не порождала лавину.

## Примеры

`examples/valid/demo-case/` — полный кейс: по записи каждого типа, отозванное утверждение, снятое противоречие, замещённое решение, действие с неизвестным расходом, два checkpoint. Файл `examples/valid/expected-warnings.json` перечисляет предупреждения образца (сейчас пусто).

`examples/invalid/<случай>/` содержит нарушение одного правила как наложение на `valid/demo-case/`:

- `expected.json` — `{"errors": ["КОД"], "warnings": []}`: точный набор ожидаемых кодов; необязательное `"delete": ["путь/от/корня/кейса"]` — файлы образца, которые убираются; необязательное `"supported_versions": [1, 2]` — версии, считающиеся поддерживаемыми в этом тесте;
- остальные файлы папки повторяют путь от корня кейса и заменяют файлы образца целиком.

Тест собирает образец и наложение во временную папку `demo-case` (имя должно совпадать с `case_id`) и запускает валидатор. Хэши файлов в примерах зависят от байтов, поэтому для `agent/contracts/examples/` в `.gitattributes` отключены преобразования строк.

## Изменение контракта

1. Изменить схему и таблицы этого README, в тесты добавить валидный и невалидный пример.
2. Несовместимое изменение: увеличить версию, добавить `tools/migrate_v<N>.py`, обновить `versions.json`.
3. Согласовать изменение с владельцами B04, B05, B06, B07, B13 в PR.
