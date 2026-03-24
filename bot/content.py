from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


START_TEXT = """Привет🤍
Это Милана
Поздравляю! Ты попала в мой бот, а значит хочешь научится делать макияж и прически для себя.

<i>«Искусство быть красивой»</i>
Для девушек, которые хотят:

✨ делать дневной макияж, чтобы выглядеть ухоженно и дорого каждый день

✨ делать роскошный вечерний макияж со стрелкой

✨ делать актуальные укладки быстро и красиво"""


START_PHOTO_URL = "https://downloader.disk.yandex.ru/preview/1ae5d701258a0cbf769b6b7a26a114c09978b08eea36334ea5a8fce7ddddff7a/69c1e943/_WP18JlBQza-DgxclvoxXzXMY-ieV2pSykoPIzDQMK64JASGyXmUP5VN2LMMN9yAwSxdD8nuU4woEEI1uGEwgA%3D%3D?uid=0&filename=IMG_4609.JPG&disposition=inline&hash=&limit=0&content_type=image%2Fjpeg&owner_uid=0&tknv=v3&size=2048x2048"

COURSE_OVERVIEW_TEXT = """<b>Что вас ждет:</b>

4 основных онлайн урока:
- Дневной макияж
- Вечерний макияж со стрелкой
- Укладка на стайлере «Луна»
- Объемная укладка с помощью бигуди

Плюс бонусные уроки:
- Подготовка кожи к макияжу
- Оформление декольте
- Как создать идеальный объём на волосах

Плюс мой личный гайд <b>«ХОЧУ/ МОГУ»</b> я покажу люксовую косметику и ее бюджетные аналоги, чтобы выглядеть роскошно без лишних трат

💬 Общий чат с участницами
🎥 Прямой эфир
📩 Обратная связь по вашим отработкам
❓ Ответы на любые вопросы
🎁РОЗЫГРЫШИ крутых подарков

<b>ДОСТУП</b>: навсегда

<b>СТОИМОСТЬ</b>: 2999₽ вместо 3999₽

⏰скидка действует до 6 апреля

👉 Нажимай <b>«ОПЛАТИТЬ»</b> и забирай доступ

⏩️ по возникшим вопросам пиши @beautymi30

Нажимая кнопку «Оплатить» я безоговорочно соглашаюсь с условиями Оферты, даю согласие на обработку своих персональных данных в соответствии с Политикой обработки персональных данных.

https://disk.yandex.ru/i/ekLrZ4k5qdWVsQ"""


@dataclass(frozen=True)
class LessonShowcase:
    text: str
    photos: tuple[str, ...]
    typing_before_seconds: int = 0


LESSON_SHOWCASE = [
    LessonShowcase(
        text="""<b>ДНЕВНОЙ МАКИЯЖ</b>
Суперлегкий нюд на каждый день.
Акцент на сияющей, ровной коже и мягкой коррекции сухими продуктами.""",
        photos=(
            "https://downloader.disk.yandex.ru/preview/3712467a8755b72dcce619257922b9f9c3d3c49689142462d6bf3641b7a78034/69c1ea10/LoNhhxmUVIa5i2mxEYxuWTXMY-ieV2pSykoPIzDQMK6YezRZdBm73XypeMcjamejcHWkkEVIQgq1ZiGiKzkApw%3D%3D?uid=0&filename=IMG_3764.JPG&disposition=inline&hash=&limit=0&content_type=image%2Fjpeg&owner_uid=0&tknv=v3&size=2048x2048",
            "https://downloader.disk.yandex.ru/preview/ab27716a4385d4897b961d967c4943855348c70474fd2ff8d0ce9df04470478a/69c1ea2e/uEOD26o8O-Bud2n36JE99MmAR647dkQ62DWTrRC4yzEZFhQCHgIpSmwVHyVXVZQNxuzFQoXPRobEG85GQspRdw%3D%3D?uid=0&filename=IMG_3761.JPG&disposition=inline&hash=&limit=0&content_type=image%2Fjpeg&owner_uid=0&tknv=v3&size=2048x2048",
        ),
    ),
    LessonShowcase(
        text="""<b>ВЕЧЕРНИЙ МАКИЯЖ</b>
Самая простая и быстрая техника вечернего макияжа с аккуратной стрелкой.
Подходит абсолютно всем — легко повторит каждая.""",
        photos=(
            "https://downloader.disk.yandex.ru/preview/daa6688fd0eaa621257278ce419c6fb2f271413d44a4eb6e70d6128a726ec66f/69c1ea71/MO25ThnZ1ijyhZbZaK1kZpk6LUxXSNjpTD0bHERBembv_2cwPNLjDuBojpEPFDdGziy52jJJzNxtTeDrDYHPHQ%3D%3D?uid=0&filename=IMG_3762.JPG&disposition=inline&hash=&limit=0&content_type=image%2Fjpeg&owner_uid=0&tknv=v3&size=2048x2048",
            "https://downloader.disk.yandex.ru/preview/be8b0971ecf8d0575e8e6b21b3ffd2ecc4366c564f9cb3390ebbf1d3626ac06d/69c1ea93/4hM2d74PGwlT-yDzk2nSQItW8aaQpNmWOfSlJt-4fjD41Iyt0e_4nvnKc_V1tT6Pfb6N4flsikJE01mfT8jgMA%3D%3D?uid=0&filename=IMG_3763.JPG&disposition=inline&hash=&limit=0&content_type=image%2Fjpeg&owner_uid=0&tknv=v3&size=2048x2048",
        ),
        typing_before_seconds=6,
    ),
    LessonShowcase(
        text="""<b>УКЛАДКА «НА ЛУНУ»</b>
Быстрая укладка с мягкими, подвижными локонами на бюджетный стайлер.
Просто, понятно и реально повторить даже новичку.""",
        photos=(
            "https://downloader.disk.yandex.ru/preview/13d0ab5c6f0e05baa048ce20a00bb9f546a379bfa944bcb98bccdc1739c198be/69c1ead1/oO3n4PG5pGWjyRft_J6udM5MxByW2OANKfui0UoPJSWm9rE5rR9ZGHPcBKJbes0kXtmSuWmRXR4LVd2owSW-tg%3D%3D?uid=0&filename=IMG_3760.HEIC&disposition=inline&hash=&limit=0&content_type=image%2Fjpeg&owner_uid=0&tknv=v3&size=2048x2048",
        ),
        typing_before_seconds=5,
    ),
    LessonShowcase(
        text="""<b>ОБЪЁМНАЯ УКЛАДКА НА БИГУДИ</b>
Роскошная укладка в стиле Old Money.
Идеально для тех, кто любит объём и эффект «дорого».
Потребует чуть больше времени, но результат того стоит.""",
        photos=(
            "https://downloader.disk.yandex.ru/preview/0180f3a3a4decc972f697288ff8d078d18f4c1a4d00a07c730c01b2c8c2e22f8/69c1eafb/BSZIYs46bftndK3Nv_aQCYtW8aaQpNmWOfSlJt-4fjC8jOi9UUhbXiKp7E9Lmr9GGmbAb7Pa7ZnvRse-hGqITQ%3D%3D?uid=0&filename=IMG_3759.HEIC&disposition=inline&hash=&limit=0&content_type=image%2Fjpeg&owner_uid=0&tknv=v3&size=2048x2048",
        ),
        typing_before_seconds=5,
    ),
]


PAYMENT_TEXT = """Чтобы получить доступ, нажми «ОПЛАТИТЬ».
После оплаты нажми кнопку «Я ОПЛАТИЛА», и бот отправит доступ в чат и канал."""


PAID_PENDING_TEXT = """Спасибо! Запрос на проверку оплаты отправлен.
Как только оплата подтверждена, ты получишь ссылки для вступления 🤍"""


@dataclass(frozen=True)
class Reminder:
    reminder_id: str
    when: datetime
    text: str
    with_photo: bool = True


def build_reminders(year: int, tz: ZoneInfo) -> list[Reminder]:
    return [
        Reminder(
            reminder_id="r_04_03_10_00",
            when=datetime(year, 4, 3, 10, 0, tzinfo=tz),
            text="""Осталось всего 3 дня до окончания скидки ⏰

Ты еще можешь присоединиться к обучению и научиться делать макияж и укладки для себя 💄
Не откладывай — потом будет дороже 🤍

👉 Успей по выгодной цене""",
        ),
        Reminder(
            reminder_id="r_04_05_10_00",
            when=datetime(year, 4, 5, 10, 0, tzinfo=tz),
            text="""Завтра последний день, когда можно присоединиться по выгодной цене 💔

Скидка сгорит уже через 24 часа ⏰

Если ты давно хотела научиться делать макияж и укладки — это знак ✨

👉 Забери доступ и начни менять себя""",
        ),
        Reminder(
            reminder_id="r_04_06_10_00",
            when=datetime(year, 4, 6, 10, 0, tzinfo=tz),
            text="""Сегодня последний день! ⏰

Скидка заканчивается уже сегодня, и цена вырастет 💔

Это твой шанс купить пакет уроков «Искусство быть красивой» по лучшей цене за 2999₽ 🤍

👉 Успей присоединиться сейчас""",
        ),
        Reminder(
            reminder_id="r_04_06_23_00",
            when=datetime(year, 4, 6, 23, 0, tzinfo=tz),
            text="""Последний час, и мы повышаем цену ⏰

👉 Успей оплатить прямо сейчас""",
        ),
        Reminder(
            reminder_id="r_04_07_10_00",
            when=datetime(year, 4, 7, 10, 0, tzinfo=tz),
            text="""Ты давно хотела научиться собирать образ самостоятельно, чтобы выглядеть дорого и роскошно каждый день ✨

Я создала уроки, где ты этому научишься.
Без сложностей и без лишних трат.

Чтобы ты могла:
— сама собирать себя на любое событие
— выглядеть ухоженно каждый день
— чувствовать себя уверенно на 100%

Это навык, который останется с тобой навсегда 🤍""",
        ),
    ]
