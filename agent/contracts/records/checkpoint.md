# Checkpoint `CKP`

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
- Отпечаток артефакта служит его версией. Checkpoint записывается новым файлом после записи и проверки артефактов; порядок и атомарность описаны в разделе «`save`» [спецификации B06](../../../docs/specs/2026-11-06-b06-checkpoint.md) и в [процедуре](../../workflows/checkpoint.md), запись выполняет `tools/checkpoint.py`.
- Негодный checkpoint: неразбираемый, с нарушением схемы или версии, чужим `case_id`, `id`, не совпавшим с именем файла, отсутствующим артефактом, неверным учётом или нарушением цепочки (`previous` не найден либо `seq` не на единицу больше). Имя файла — только `CKP-NNNN.json`; любой другой файл в `state/checkpoints/` (кроме скрытых, начинающихся с точки) считается негодным. Скрытые `.lost-CKP-…` (потерянный негодный файл, признанный пользователем) и `.rejected-CKP-…` (опубликованный, но не прошедший повторную проверку) — служебные: содержимое сохраняется, валидатор их пропускает, номера в них не используются повторно, а ссылка на такой ID из журнала не даёт `ID_MISSING`. Текущий — годный с наибольшим `seq`. Отдельного указателя нет.
- Текущий checkpoint не должен называть выбором человека решение без `choice` и завершённым — действие без исхода `completed`: это ошибка `CHECKPOINT_CLAIM`.
- Другой отпечаток артефакта не делает checkpoint негодным, а даёт предупреждение `CHECKPOINT_STALE` со списком файлов: агент сверяет их и сохраняет новый checkpoint.
- Потерянным считается негодный файл с номером больше номера `previous` текущего checkpoint (0, если `previous` — `null`) и посторонний файл. Если файл `previous` существует, но не читается, `seq` с ним не сравнивается. Если новейший негоден, текущим становится более ранний, и выводится `CHECKPOINT_FALLBACK` с причинами и номерами потерянных файлов (для негодного файла номер берётся из имени, поэтому потеря новейшего видна всегда; посторонние файлы тоже названы в предупреждении). Если негодны все — ошибка `CHECKPOINT_NONE_VALID`. Кейс без checkpoint допустим.
