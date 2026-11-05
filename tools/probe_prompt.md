Это проверка инструментов, а не продуктовая задача. Работай только в текущей папке. Не читай .env и файлы с ключами, ничего не изменяй, не запрашивай новых доступов.

1. Локальные файлы. Для каждого файла выполни `.venv/Scripts/python.exe tools/read_material.py inspect --preview tests/fixtures/environment/<файл>` (вне Windows: `.venv/bin/python`). Файлы: probe-document.docx, probe-document.pdf, probe-table.xlsx, probe-table.csv. Найди маркеры вида ДОК-, ДОКТАБ-, ПДФ-, ТАБ-, ЦСВ- и значение ячейки Выручка!B3. Укажи ячейку без сохранённого значения формулы.
2. Открытая страница: средством чтения сайтов этой среды прочитай https://www.rfc-editor.org/rfc/rfc9110.html и приведи название документа и номер раздела про метод GET.
3. Поиск: одним запросом найди официальную страницу спецификации HTTP Semantics и приведи URL.
4. Недоступная страница: попробуй открыть https://www.rfc-editor.org/rfc/rfc0000000.html и честно сообщи результат.
5. Бюджет: на этот шаг разрешено одно чтение страницы. Нужны заголовки https://example.com/ и https://example.org/. Прочитай одну, вторую не открывай и сообщи, что бюджет исчерпан и что осталось непроверенным.

Верни только JSON с полями markers, cell_b3, formula_without_value, web_title, web_get_section, search_url, unavailable_result, budget_result, tools_used, problems.
