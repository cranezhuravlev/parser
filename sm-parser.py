import asyncio
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from playwright.async_api import async_playwright, Page, ElementHandle


BASE_URL = ["https://napopravku.ru/spb/clinics/sm-klinika-na-dybenko/vrachi/",
            "https://napopravku.ru/spb/clinics/21-vek-medicinskiy-centr/vrachi/#doctors",

            ]



# ----------------------------- Модели данных -----------------------------

@dataclass
class PriceItem:
    service: str
    price: Optional[int]


@dataclass
class Doctor:
    name: Optional[str] = None
    filial: Optional[str] = None
    specialties: list[str] = field(default_factory=list)
    rating: Optional[float] = None
    reviews: Optional[int] = None
    experience_years: Optional[int] = None
    category: Optional[str] = None
    adults: Optional[str] = None
    prices: list[PriceItem] = field(default_factory=list)
    status: Optional[str] = None
    # Слоты, сгруппированные по дате: {"23.10": ["16:10", "18:10", ...]}
    slots_by_date: dict[str, list[str]] = field(default_factory=dict)


# ----------------------------- Парсинг одной карточки -----------------------------

async def load_all_cards(page):
    """Нажимает 'Показать ещё' пока кнопка есть и карточки добавляются."""
    prev_count = 0
    for attempt in range(50):  # защита от бесконечного цикла
        # 1. Считаем текущие карточки
        count = await page.locator(".doctor-card-v2").count()
        print(f"  итерация {attempt + 1}: карточек {count}")

        # 2. Ищем кнопку
        btn = page.locator("text=Показать ещё").first
        if await btn.count() == 0:
            print("  кнопки 'Показать ещё' больше нет")
            break

        # 3. Прокручиваем к кнопке (иначе клик может не сработать)
        await btn.scroll_into_view_if_needed()
        await page.wait_for_timeout(300)

        # 4. Кликаем
        try:
            await btn.click(timeout=5000)
        except Exception as e:
            print(f"  клик не удался: {e}")
            break

        # 5. Ждём, пока карточек станет больше (или таймаут)
        try:
            await page.wait_for_function(
                "n => document.querySelectorAll('.doctor-card-v2').length > n",
                arg=count,
                timeout=10000,
            )
        except Exception:
            print("  новых карточек не появилось — останавливаемся")
            break

        # 6. Дополнительная прокрутка вниз, чтобы догрузить ленивые элементы
        await page.mouse.wheel(0, 2000)
        await page.wait_for_timeout(500)

        new_count = await page.locator(".doctor-card-v2").count()
        if new_count == prev_count == count:
            print("  количество не растёт — останавливаемся")
            break
        prev_count = count

    return await page.locator(".doctor-card-v2").count()


def parse_price(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


async def parse_card_static(card: ElementHandle) -> Doctor:
    """Парсит данные, которые видны сразу (без кликов)."""
    text = await card.inner_text()

    # ФИО
    name_el = await card.query_selector("a[href*='/vrachi/'] h3, [class*='name']")
    name = (await name_el.inner_text()).strip() if name_el else None
    # filial workplace-address-card__name
    filial_el = await card.query_selector(".workplace-address-card__name")
    filial = (await filial_el.inner_text()).strip() if filial_el else None

    # Рейтинг и отзывы
    rating_m = re.search(r"(\d\.\d)\s*\n?\s*(\d+)\s*отзыв", text)
    rating = float(rating_m.group(1)) if rating_m else None
    reviews = int(rating_m.group(2)) if rating_m else None

    # Стаж
    exp_m = re.search(r"Стаж\s+(\d+)\s+год", text)
    experience = int(exp_m.group(1)) if exp_m else None

    # Категория
    cat_m = re.search(r"(Высшая категория|Первая категория|Кандидат мед\. наук|Доктор мед\. наук)", text)
    category = cat_m.group(1) if cat_m else None

    # Специальности (запасной вариант — из текста)
    specialties = []
    spec_m = re.search(r"Документы проверены\s*\n(.+?)\n(Взрослый[^\n]*)", text, re.S)
    if spec_m:
        specialties = [s.strip() for s in spec_m.group(1).split("\n") if s.strip()]

    # Взрослый / детский
    adult_m = re.search(r"(Взрослый(?:,\s*детский)?)", text)
    adults = adult_m.group(1) if adult_m else None

    # Цены
    prices = []
    for m in re.finditer(r"([А-ЯA-Z][^\n₽]{3,}?)\n\s*(?:от\s*)?([\d\s]+)\s*₽", text):
        service = m.group(1).strip()
        price = parse_price(m.group(2))
        if price:
            prices.append(PriceItem(service, price))

    # Статус
    if "окон нет" in text:
        status = "нет окон на 2 недели"
    elif "только по телефону" in text:
        status = "только по телефону"
    elif "Запись на приём" in text:
        status = "есть запись"
    else:
        status = None

    return Doctor(
        name=name,
        filial=filial,
        specialties=specialties,
        rating=rating,
        reviews=reviews,
        experience_years=experience,
        category=category,
        adults=adults,
        prices=prices,
        status=status,
    )


# ----------------------------- Сбор слотов -----------------------------

async def collect_slots_for_card(page: Page, card: ElementHandle) -> dict[str, list[str]]:
    """
    Кликает по каждому доступному дню в календаре и собирает время приёма.
    Возвращает словарь {дата: [время, ...]}.
    """
    result: dict[str, list[str]] = {}

    # Находим календарь внутри карточки
    day_buttons = await card.query_selector_all(".slider-calendar__day-button")
    if not day_buttons:
        return result

    # Сначала собираем метаданные о днях (дата и доступность)
    days_meta = []
    for btn in day_buttons:
        classes = await btn.get_attribute("class") or ""
        if "--dim" in classes:
            # День без слотов — пропускаем клик
            continue

        sub_el = await btn.query_selector(".slider-calendar__day-button-subtitle")
        if not sub_el:
            continue
        raw_date = (await sub_el.inner_text()).strip()
        days_meta.append({"date": raw_date, "button": btn})

    # Для каждого доступного дня кликаем и собираем слоты
    for i, day in enumerate(days_meta):
        try:
            # Перезапрашиваем кнопки, чтобы избежать stale element
            fresh_buttons = await card.query_selector_all(".slider-calendar__day-button")
            target = None
            for fb in fresh_buttons:
                sub = await fb.query_selector(".slider-calendar__day-button-subtitle")
                if sub and (await sub.inner_text()).strip() == day["date"]:
                    target = fb
                    break

            if not target:
                continue
            if i:
                await target.click()

            await page.wait_for_timeout(600)  # ждём перерисовку списка слотов

            # Собираем слоты из текущего выбранного дня
            slot_buttons = await card.query_selector_all(".time-slots-list__time-slot")
            times = []
            for sb in slot_buttons:
                time_el = await sb.query_selector("time")
                if time_el:
                    label = (await time_el.inner_text()).strip()
                    if label:
                        times.append(label)

            if times:
                result[day["date"]] = times

        except Exception as e:
            print(f"    ⚠️ Ошибка при клике по дню {day['date']}: {e}")
            await page.screenshot(path=f"./data/click_error{datetime.now()}.png")
            continue

    return result


# ----------------------------- Основной парсер -----------------------------

async def parse_page(url, headless: bool = True) -> list[Doctor]:
    doctors: list[Doctor] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page(
            viewport={"width": 1440, "height": 2000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
        )

        print("🌐 Загружаем страницу...")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        # --- Принять куки, если баннер есть ---
        try:
            cookie_btn = page.locator(
                "button:has-text('Соглашаюсь'), button:has-text('Принять'), button:has-text('Согласен')"
            ).first
            if await cookie_btn.count() > 0 and await cookie_btn.is_visible():
                await cookie_btn.click()
                await page.wait_for_timeout(500)
                print("🍪 Куки приняты")
        except Exception as e:
            print(f"⚠️ Не удалось нажать куки: {e}")

        # Прокрутка для подгрузки всех карточек
        print("📜 Прокручиваем страницу...")
        for _ in range(15): ### TODO
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(700)
        total = await load_all_cards(page)
        print(f"✅ Итого карточек: {total}")
        # Ищем карточки врачей
        # ⚠️ Если карточки не находятся — проверьте селектор через DevTools
        cards = await page.query_selector_all(".doctor-card-v2")
        print(f"🔍 Найдено карточек: {len(cards)}")
        for i, card in enumerate(cards):
            html = await card.inner_html()
            with open(f"./data/{i}_page.html", "w", encoding="utf-8") as f:
                f.write(html)

        for i, card in enumerate(cards):
            try:
                doc = await parse_card_static(card)
                with open("output.txt", "a", encoding="utf-8") as f:
                    print(doc, sep='\n', file=f)
                if not doc.name:
                    continue

                print(f"  [{i+1}/{len(cards)}] {doc.name[:40]}...", end=" ")

                # Собираем слоты, если есть кнопка записи
                if doc.status == "есть запись":
                    slots = await collect_slots_for_card(page, card)
                    print(slots)
                    doc.slots_by_date = slots
                    total_slots = sum(len(v) for v in slots.values())
                    print(f"→ {len(slots)} дн., {total_slots} слотов")
                else:
                    print("→ без записи")

                doctors.append(doc)

            except Exception as e:
                print(f"  ⚠️ Пропуск карточки {i+1}: {e}")
                continue

        await browser.close()

    return doctors


# ----------------------------- Точка входа -----------------------------

if __name__ == "__main__":
    print("🚀 Запуск парсера...")

    # Сохраняем в JSON
    output = []
    for url in BASE_URL:
        data = asyncio.run(parse_page(url, headless=True))
        for d in data:
            item = asdict(d)
            print(data)
            # Преобразуем PriceItem в словарь для JSON
            item["prices"] = [{"service": p.service, "price": p.price} for p in d.prices]
            output.append(item)
        print(f"\n✅ Готово! Собрано врачей: {len(data)} для {url}")

    with open("doctors.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


    print(f"📁 Результат: doctors.json")

    # Краткая статистика
    with_slots = sum(1 for d in data if d.slots_by_date)
    total_slots = sum(sum(len(v) for v in d.slots_by_date.values()) for d in data)
    print(f"📊 Врачей со слотами: {with_slots}")
    print(f"📊 Всего слотов: {total_slots}")