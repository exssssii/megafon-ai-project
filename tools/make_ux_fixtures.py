"""Draw synthetic interface screenshots with known content and an answer sheet.

Run with the fixtures group: uv run --locked --group fixtures python tools/make_ux_fixtures.py <out-dir>
Every screen is fictional and drawn here, so what is visible is known in advance: the answer sheet
`answers.md` lists, per file, the expected screen label, the visible texts, the parts that are
unreadable on purpose, and the factual differences between the own and the rival checkout.
"""
from dataclasses import dataclass, field
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

from make_acceptance_cases import FONT_CANDIDATES

WIDTH, HEIGHT = 1366, 768
INK, MUTED, PAPER, LINE = (30, 34, 40), (120, 126, 134), (255, 255, 255), (214, 218, 224)
ACCENT, RIVAL_ACCENT = (36, 99, 235), (22, 163, 74)


@dataclass(frozen=True)
class Screen:
    name: str
    label: str
    summary: str
    visible: tuple[str, ...]
    unreadable: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default=())


OWN_STEPS = (
    ("Шаг 1 из 4. Выберите тариф", "Тариф «Салон»", "Дальше"),
    ("Шаг 2 из 4. Данные салона", "Название салона", "Дальше"),
    ("Шаг 3 из 4. Контакты", "Телефон для связи", "Дальше"),
    ("Шаг 4 из 4. Подтверждение", "Проверьте данные и подтвердите заказ", "Оформить заказ"),
)
OWN_PRICE = "от 1990 ₽/мес при оплате за год"
RIVAL_STEPS = (
    ("Шаг 1 из 2. Ваш салон", "Название салона и телефон", "Подключить за 1 минуту"),
    ("Шаг 2 из 2. Готово", "Оплата после пробного периода", "Начать работу"),
)
RIVAL_PRICE = "1990 ₽ в месяц"
TINY_HEADING = "Условия возврата"
TINY_BODY = "Возврат возможен в течение 14 дней после оплаты при обращении в поддержку."
TINY_FINE_PRINT = "Мелкий шрифт: комиссия платёжного сервиса не возвращается"
COOKIE_TEXT = "Мы используем файлы cookie"
COOKIE_BUTTONS = ("Принять все", "Настроить")
CAPTCHA_TEXT = "Подтвердите, что вы не робот"
BLANK_LOGO = "Загрузка"


def load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    return image, ImageDraw.Draw(image)


def header(draw: ImageDraw.ImageDraw, font: Path, brand: str, color: tuple[int, int, int]) -> None:
    draw.rectangle((0, 0, WIDTH, 70), fill=color)
    draw.text((40, 18), brand, fill=PAPER, font=load_font(font, 32))


def checkout(font: Path, brand: str, color: tuple[int, int, int], step: tuple[str, str, str], price: str,
             price_size: int, button_y: int) -> Image.Image:
    image, draw = canvas()
    header(draw, font, brand, color)
    draw.text((120, 130), step[0], fill=INK, font=load_font(font, 44))
    draw.text((120, 220), step[1], fill=INK, font=load_font(font, 30))
    draw.rectangle((120, 280, 760, 340), outline=LINE, width=3)
    draw.text((120, 400), price, fill=MUTED if price_size < 20 else INK, font=load_font(font, price_size))
    draw.rounded_rectangle((120, button_y, 480, button_y + 64), radius=10, fill=color)
    draw.text((150, button_y + 14), step[2], fill=PAPER, font=load_font(font, 30))
    return image


def own_checkout(font: Path, number: int) -> Image.Image:
    return checkout(font, "Свой продукт", ACCENT, OWN_STEPS[number - 1], OWN_PRICE, 15, 688)


def rival_checkout(font: Path, number: int) -> Image.Image:
    return checkout(font, "Конкурент", RIVAL_ACCENT, RIVAL_STEPS[number - 1], RIVAL_PRICE, 56, 520)


def tiny_text(font: Path) -> Image.Image:
    image, draw = canvas()
    header(draw, font, "Свой продукт", ACCENT)
    draw.text((120, 130), TINY_HEADING, fill=INK, font=load_font(font, 44))
    draw.text((120, 220), TINY_BODY, fill=INK, font=load_font(font, 26))
    draw.text((120, 330), TINY_FINE_PRINT, fill=MUTED, font=load_font(font, 5))
    draw.text((WIDTH - 90, 450), "Подробные условия доступны на странице тарифов", fill=INK, font=load_font(font, 26))
    return image


def cookie_wall(font: Path) -> Image.Image:
    image = own_checkout(font, 1)
    overlay = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    image = Image.blend(image, overlay, 0.6)
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 90, WIDTH - 100, HEIGHT - 60), fill=PAPER)
    draw.text((150, 150), COOKIE_TEXT, fill=INK, font=load_font(font, 44))
    draw.text((150, 240), "Файлы cookie помогают сайту работать. Выберите, что разрешить.", fill=MUTED,
              font=load_font(font, 28))
    for index, caption in enumerate(COOKIE_BUTTONS):
        left = 150 + index * 300
        draw.rounded_rectangle((left, 520, left + 260, 590), radius=10, fill=ACCENT if index == 0 else LINE)
        draw.text((left + 36, 538), caption, fill=PAPER if index == 0 else INK, font=load_font(font, 30))
    return image


def captcha(font: Path) -> Image.Image:
    image, draw = canvas()
    draw.text((420, 200), CAPTCHA_TEXT, fill=INK, font=load_font(font, 40))
    draw.rectangle((420, 300, 520, 400), outline=INK, width=4)
    draw.text((560, 330), "Я не робот", fill=INK, font=load_font(font, 32))
    return image


def blank(font: Path) -> Image.Image:
    image, draw = canvas()
    draw.text((640, 360), BLANK_LOGO, fill=(235, 237, 240), font=load_font(font, 20))
    return image


def screens() -> list[Screen]:
    """The answer for every file, in the order of the answer sheet."""
    own = [Screen(
        f"own-checkout-{number}.png", "целевой",
        f"Свой экран оформления, шаг {number} из 4.",
        (OWN_STEPS[number - 1][0], OWN_STEPS[number - 1][1], OWN_STEPS[number - 1][2], "Свой продукт"),
        (),
        (f"Цена «{OWN_PRICE}» напечатана мелко, серым.", "Кнопка у нижнего края экрана.") if number == 1 else (),
    ) for number in range(1, 5)]
    rival = [Screen(
        f"rival-checkout-{number}.png", "целевой",
        f"Экран конкурента, шаг {number} из 2.",
        (RIVAL_STEPS[number - 1][0], RIVAL_STEPS[number - 1][1], RIVAL_STEPS[number - 1][2], "Конкурент"),
        (),
        (f"Цена «{RIVAL_PRICE}» напечатана крупно.",) if number == 1 else (),
    ) for number in range(1, 3)]
    rest = [
        Screen("tiny-text.png", "целевой", "Страница условий возврата с мелкой строкой и обрезанным краем.",
               ("Свой продукт", TINY_HEADING, TINY_BODY),
               (f"Строка мелким шрифтом под основным текстом («{TINY_FINE_PRINT}») на снимке не читается.",
                "Правая нижняя строка обрезана краем снимка: виден только её левый край.")),
        Screen("cookie-wall.png", "заслонён", "Окно cookie закрывает почти весь экран оформления.",
               (COOKIE_TEXT, *COOKIE_BUTTONS),
               ("Всё, что за окном, затемнено: основное содержимое экрана не видно, за краем окна едва различимы",
                "«Свой продукт» в шапке и «Дальше» на кнопке.")),
        Screen("captcha.png", "капча", "Проверка «я не робот» вместо страницы.", (CAPTCHA_TEXT, "Я не робот")),
        Screen("blank.png", "пусто", "Почти пустой экран с едва заметной подписью.", (),
               (f"Подпись «{BLANK_LOGO}» светло-серая и почти не видна.",)),
    ]
    return own + rival + rest


def draw_all(font: Path) -> dict[str, Image.Image]:
    drawn = {f"own-checkout-{number}.png": own_checkout(font, number) for number in range(1, 5)}
    drawn.update({f"rival-checkout-{number}.png": rival_checkout(font, number) for number in range(1, 3)})
    drawn["tiny-text.png"] = tiny_text(font)
    drawn["cookie-wall.png"] = cookie_wall(font)
    drawn["captcha.png"] = captcha(font)
    drawn["blank.png"] = blank(font)
    return drawn


def answers_text() -> str:
    lines = [
        "# Контрольные ответы по синтетическим скриншотам",
        "",
        f"Все экраны вымышленные, размер {WIDTH}×{HEIGHT}. Сверка: в наблюдениях нет ничего, чего нет в этом списке;",
        "нечитаемое лежит в «Не разобрано» либо помечено «неразборчиво», а не угадано.",
        "",
    ]
    for screen in screens():
        lines += [f"## {screen.name}", "", f"- Метка экрана: `{screen.label}`.", f"- Что это: {screen.summary}"]
        lines.append("- Видимые тексты: " + ("; ".join(f"«{text}»" for text in screen.visible) or "нет."))
        lines.append("- Нечитаемо намеренно: " + (" ".join(screen.unreadable) or "нет."))
        lines += [f"- Особенность: {note}" for note in screen.notes]
        lines.append("")
    lines += [
        "## Различия между своим экраном и конкурентом",
        "",
        "Факты, сверяемые по снимкам:",
        "",
        "| Признак | Свой продукт | Конкурент |",
        "|---|---|---|",
        "| Шагов до оформления | 4 | 2 |",
        f"| Подача цены | «{OWN_PRICE}», мелко, серым | «{RIVAL_PRICE}», крупно |",
        f"| Кнопка первого шага | «{OWN_STEPS[0][2]}», у нижнего края | «{RIVAL_STEPS[0][2]}», в середине |",
        f"| Кнопка последнего шага | «{OWN_STEPS[-1][2]}» | «{RIVAL_STEPS[-1][2]}» |",
        "",
        "Оценки «у конкурента удобнее» и причины («уходят из-за числа шагов») — только гипотезы с названным",
        "способом проверки: метрика, A/B, интервью.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: make_ux_fixtures.py <out-dir>", file=sys.stderr)
        return 2
    font = next((item for item in FONT_CANDIDATES if item.exists()), None)
    if font is None:
        print("No Cyrillic TrueType font found", file=sys.stderr)
        return 1
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for name, image in draw_all(font).items():
        image.save(out / name, format="PNG")
    (out / "answers.md").write_text(answers_text(), encoding="utf-8", newline="\n")
    print(f"Screenshots and answers written: {len(screens())} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
