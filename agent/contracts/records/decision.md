# Решение `DEC`

```json
{
  "id": "DEC-0002", "created_at": "…", "topic": "direction", "status": "chosen",
  "proposal": {"option_id": "OPT-0003", "rationale": "…", "proposed_at": "…", "action_id": "ACT-0008"},
  "choice": {"option_id": "OPT-0001", "user_statement": "Слова пользователя", "chosen_at": "…", "action_id": "ACT-0009"},
  "supersedes": "DEC-0001",
  "change_reason": "Пользователь изменил ограничение"
}
```

- `status`: `proposed` (есть только `proposal`), `chosen` (есть `choice`), `superseded` (заменено). Заменённая запись прежде была предложением или выбором, поэтому без `proposal` и `choice` её не бывает: недействительность прежнего выбора выражает новая запись с `supersedes`, а не пустая запись. Нарушение — `DECISION_CHOICE_STATE`.
- `proposal` равно `null`, если решение принял сам пользователь без предложения агента: такая запись сразу `chosen`. Необязательные `supersedes` и `change_reason` без значения не пишутся.
- `choice.option_id` равно `null`, если выбор не относится к варианту: `topic: brief` — утверждение брифа, `topic: budget` — расширение бюджета исследования, `topic: interview_method` — способ бесед с модельными клиентами (гайд и персонажи), `topic: thresholds` — согласование порогов плана проверки, `topic: commitments` — согласование документа `outputs/commitments.md`, `topic: revision` — выбор человека после новых данных (`user_statement` — его слова). Записи о порогах, обязательствах и пересмотре создаются только после ответа человека, сразу `chosen`, с `proposal: null`. Новое согласие или следующий пересмотр заменяет прежнюю запись через `supersedes`. Для `topic: direction` это ошибка `DECISION_CHOICE_STATE`. `proposal.option_id` всегда ссылается на вариант.
- Предложение модели и выбор человека разделены. `proposal.action_id` — действие `model`; `choice.action_id` — завершённое действие `human_input`, в `result_refs` конца которого есть это решение. Иначе `DECISION_NOT_HUMAN`. Выбор может отличаться от предложения.
- `choice.delegated: true` — человек передал выбор агенту («реши сам»). Тогда `choice.option_id` совпадает с `proposal.option_id`: под видом передачи нельзя выбрать другой вариант, чем рекомендован. Действие предложения в журнале заканчивается раньше, чем начинается действие человека: передать можно только уже показанную рекомендацию. Иначе `DECISION_CHOICE_STATE`. Поле пишется только со значением `true`.
- `proposed` становится `chosen` на месте: добавляется `choice`. Если меняется предложение или уже сделанный выбор, создаётся новая запись с `supersedes` и `change_reason`, прежняя получает `superseded` (`DECISION_SUPERSEDE`). Замены не образуют цикл.
- `topic` — короткий идентификатор предмета решения: строчные латинские буквы, цифры и `_`, начинается с буквы (`direction` в обязательном сценарии). Замена не меняет `topic`; одну запись заменяет не больше одной (ветвление — `DECISION_SUPERSEDE`). На один `topic` приходится ровно одна незаменённая запись, считая `proposed` (`DECISION_MULTIPLE_ACTIVE`).
