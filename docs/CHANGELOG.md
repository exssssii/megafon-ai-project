# История разработки

Каждый слитый PR рабочего репозитория команды — один коммит этой истории, с его датой и номером `(#N)` в заголовке. Ниже они перечислены по порядку. Спецификации, карточки задач и отчёты приёмки ссылаются на версии так:

- «PR №N» — состояние после слияния PR №N;
- «коммит K ветки PR №N» — промежуточная версия внутри ветки PR №N до squash-слияния. Такие версии в историю не вошли: каждый PR представлен одним коммитом.

Документация разработки (`docs/`, `README.md` и другие описания) вошла последним коммитом в итоговом виде. Карточки задач — в [`docs/tasks/`](tasks/README.md), спецификации — в `docs/specs/`, отчёты приёмки — в `docs/acceptance/`.

| Дата | PR | Изменение |
|---|---|---|
| 2026-11-05 | 1 | chore: подготовить ТЗ агента и проверки репозитория |
| 2026-11-05 | 2 | chore: проверить среды агента и доступы |
| 2026-11-05 | 3 | docs: отметить приёмку B02 |
| 2026-11-05 | 4 | feat(contracts): зафиксировать контракты данных и режимы агента |
| 2026-11-05 | 5 | feat(tools): читать материалы кейса с локаторами и границами |
| 2026-11-05 | 7 | docs(acceptance): подготовить проверочные кейсы и протокол приёмки (B03) |
| 2026-11-06 | 8 | feat(checkpoint): сохранять кейс и продолжать его в новом чате (B06) |
| 2026-11-06 | 6 | feat(evidence): связать утверждения с источниками и проверять цитаты (B05) |
| 2026-11-06 | 10 | feat(calc): считать числа из материалов и перепроверять их в валидаторе (B10) |
| 2026-11-06 | 9 | feat(entry): вести руководителя продукта от первой фразы до утверждённого брифа (B07) |
| 2026-11-06 | 9 | docs(tasks): отметить приёмку B10 |
| 2026-11-06 | 13 | feat(strategy): вести кейс от исследования до стратегий и плана проверки (B08) |
| 2026-11-07 | 11 | feat(accounting): учитывать время, действия, расходы и помощь человека (B13) |
| 2026-11-07 | 14 | feat(run): пройти полный кейс в обеих средах с обрывом и переносом папки (B09) |
| 2026-11-07 | 15 | feat(run): пройти сложные случаи одним набором для отладки и приёмки (B11) |
| 2026-11-08 | 16 | feat(run): читать страницы без модели и останавливать исследование по лимиту брифа |
| 2026-11-08 | 17 | chore(run): подготовить прогоны B12: модель в журнале, период в тексте, перенос на любом кейсе |
| 2026-11-08 | 18 | docs(tasks): добавить кандидатов улучшений E10–E16 и расставить приоритеты |
| 2026-11-08 | 19 | feat(env): работать в любой совместимой среде и передавать выбор агенту (B18) |
| 2026-11-08 | 20 | fix(agent): проверять новое требование расчётом и не говорить пользователю о внутреннем устройстве |
| 2026-11-08 | 21 | fix(agent): сбой утилиты без отсылки в разработку, время в минутах, зачёт прогонов по шагам |
| 2026-11-09 | 22 | fix(accounting): считать первые варианты и после отзыва, сохранять поверх потери только после ответа (B12) |
| 2026-11-09 | 23 | docs(tasks): записать замечания прогонов B12 в Codex |
| 2026-11-09 | 24 | docs(tasks): записать недоработки прогонов B12 после PR №22 |
| 2026-11-09 | 25 | fix(agent): закрыть замечания прогонов B12, подготовить B14 и записать проверки для полного прогона |
| 2026-11-09 | 27 | feat(agent): сопровождать решение и пересматривать его при новых данных (E01) |
| 2026-11-10 | 26 | docs(acceptance): записать приёмку ядра B12 на #21 и закрыть B12 |
| 2026-11-10 | 28 | feat(agent): гайд интервью и модельные собеседники (E03) |
| 2026-11-10 | 29 | feat(agent): подключать процедуры навыками среды (E14) |
| 2026-11-10 | 30 | fix(scripts): восстановить подготовку pick-case и проверять сухой прогон наборов в CI |
| 2026-11-12 | 31 | feat(agent): разбирать скриншоты и снимки сайтов (E04) |
| 2026-11-12 | 32 | fix(agent): enforce respondent isolation and pin persona cards (D33, D34, D37) |
| 2026-11-12 | 33 | docs(e06): write the comparison protocol and the reply scenario |
| 2026-11-12 | 34 | feat(scripts): run the hard suite as a plain chat and time every reply (E06) |
| 2026-11-12 | 35 | feat(scripts): build anonymised judge dossiers for the comparison (E06) |
| 2026-11-12 | 36 | feat(agent): add an independent reviewer of the finished documents (E02) |
| 2026-11-12 | 37 | fix(agent): spawn Codex subagents without the parent history |
| 2026-11-12 | 38 | docs(tasks): freeze the improvement set before the full run |
| 2026-11-12 | 39 | fix(agent): keep preliminary strategies on a new choice and agree commitments in the run |
| 2026-11-12 | 40 | fix(agent): let the Codex reviewer read files and make the closing choice unambiguous |
| 2026-11-13 | 41 | fix(scripts): let the chosen alternative be added when the comparison has none |
| 2026-11-13 | 42 | fix(scripts): fall back to keeping the direction when no option fits the new condition |
| 2026-11-13 | 43 | docs(tasks): record minor findings from the Codex full runs |
| 2026-11-14 | 44 | docs(acceptance): report the final full run (E06, B12) |
| 2026-11-14 | 45 | fix(scripts): pass the archive check and mark the final run results |
| 2026-11-14 | 46 | fix(scripts): count only real .. traces, keep held files out of reach, name the condition in c3-2a |
| 2026-11-14 | 47 | fix(agent): act on the final run findings (D44, D46, D47, D51) and add short check suites |
| 2026-11-15 | 48 | fix(start): set up Python inside the folder on Windows for the Codex sandbox, record the B14 manual pass |
| 2026-11-15 | 49 | fix(examples): make generated fixtures byte-stable and put the sample interview in research order (D32, D36) |
| 2026-11-15 | 50 | ci: run the checks on a Windows runner |
| 2026-11-15 | 51 | fix(agent): verify screenshots and entry on the final version and fix what the runs found (B14, E04) |
| 2026-11-15 | 52 | fix(agent): put the status block template in product-mode so every chat sees it (D55) |
| 2026-11-16 | 53 | feat(agent): add the case page and the team roster (E17) |
| 2026-11-16 | 54 | fix(agent): keep revision paths across chats and verify the E01 checks 2-4 live |
| 2026-11-17 | 55 | feat(agent): route the model respondent to a cheap model with a role check (E02) |
| 2026-11-17 | 56 | fix(scripts): count the cost of a reply as a difference, not a running total (E18) |
| 2026-11-17 | 57 | feat(tools): move journal writing and record lookup into utilities (E18) |
| 2026-11-17 | 58 | feat(contracts): require option cost, brief limit checks and threshold basis (E19) |
| 2026-11-17 | 59 | docs(tasks): record the E02 routing, E18 and E19 results after merging |
| 2026-11-17 | 60 | refactor(agent): split the contract and checkpoint rules and name stage files on resume (E18) |
| 2026-11-17 | 61 | fix(agent): read less before resume and require stating what the limit was compared with |
| 2026-11-17 | 62 | test(scripts): add a short live suite for the team roster and the case page (E17) |
| 2026-11-18 | 63 | docs(acceptance): record the final run on #62, E17 and E18 status and the D69 analysis |
| 2026-11-18 | 64 | fix(agent): split long instruction files, ask about the team before commitments and see withdrawn consent (D69-D72) |
| 2026-11-18 | 65 | docs(tasks): record the E06 quality check of hard on #62 and close E18 with limits |
| 2026-11-18 | 66 | fix(agent): keep thresholds out of the team question and save every message of a reply (D73, D74) |
| 2026-11-18 | 67 | fix(agent): name the barrier as a hypothesis and write the key message as a reason to act |
| 2026-11-19 | 68 | fix(agent): build the key message from a known benefit, a reason to act now and the action (D75) |
| 2026-11-19 | 69 | docs(tasks): specify E20 required deliverable and split it into T1-T3 |
| 2026-11-19 | 70 | docs(tasks): record E20 order, owners and remaining steps |
| 2026-11-19 | 73 | feat(gate): check the deliverable document when the brief sets a form of the result (E20 T1) |
| 2026-11-19 | 71 | feat(agent): add estimate methods for briefs that require numbers on assumptions (E20 T2) |
| 2026-11-19 | 72 | feat(live): add the family-connect demo case with the brief passed by -Brief (E20 T3 prep) |
| 2026-11-19 | 74 | docs(acceptance): record the family-connect run in Claude Code on #72 |
| 2026-11-19 | 75 | fix(agent): carry required estimates to numbers: channel funnel, work and costs, one value per assumption |
| 2026-11-19 | 76 | docs(acceptance): record the family-connect runs in Claude Code and Codex on #75 |
| 2026-11-20 | 77 | fix(gate): reject false precision in the deliverable document and require the numbers the brief derives |
| 2026-11-20 | 78 | docs(acceptance): record the family-connect run in Codex on #77 and close E20 |
| 2026-11-20 | 79 | feat(live): transfer family-connect from one environment to the other for the demo |
| 2026-11-20 | 80 | docs(acceptance): record the transfer of family-connect between environments on #79 |
| 2026-11-20 | 81 | feat(demo): add the B15 demo project from the family-connect run on #77 |
