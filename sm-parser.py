#!/usr/bin/env python3
"""Парсер врачей СМ-Клиники на НаПоправку.

Chromium и WebKit блокируются антибот-защитой сайта (Servicepipe), поэтому
используется Firefox. Профиль браузера сохраняется между запусками: после
однократно пройденной проверки cookies переиспользуются и headless-режим
работает без капчи. Если проверка всё же появилась, скрипт автоматически
перезапускает Firefox с окном и ждёт её прохождения.
"""

import argparse
import asyncio
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, ElementHandle

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PROFILE_DIR = DATA_DIR / ".firefox-profile"

BASE_URL = [
    "https://napopravku.ru/spb/clinics/sm-klinika-na-dybenko/vrachi/",
    "https://napopravku.ru/spb/clinics/21-vek-medicinskiy-centr/vrachi/#doctors",
    "https://napopravku.ru/spb/clinics/lakhta-klinika-kruglosutochnaya-mnogoprofilnaya-klinika-i-travmpunkt-primorskogo-rayona/vrachi/#doctors",
    "https://napopravku.ru/spb/clinics/medsi-set-castnyh-klinik/vrachi/#doctors",
]

# Тексты, которыми антибот-защита сообщает о блокировке/проверке.
BLOCK_MARKERS = (
    "Forbidden",
    "имеем дело именно с вами",
    "пройдите проверку",
    "подтвердите, что вы не робот",
    "доступ ограничен",
    "access denied",
)


class BlockedError(RuntimeError):
    """Сайт показал антибот-проверку вместо страницы."""


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


# ----------------------------- Антибот / загрузка -----------------------------

def _looks_blocked(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in BLOCK_MARKERS)


async def wait_for_cards(
    page: Page,
    timeout_ms: int = 90000,
    blocked_grace_ms: int = 30000,
) -> int:
    """Ждёт появления карточек, пережидая антибот-проверку.

    Если проверка не проходит за blocked_grace_ms, выбрасывает BlockedError,
    чтобы вызывающий код мог перезапустить браузер с окном.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_ms / 1000
    blocked_at = None
    last_text = ""

    while loop.time() < deadline:
        count = await page.locator(".doctor-card-v2").count()
        if count:
            if blocked_at is not None:
                print("  ✅ Проверка пройдена")
            return count

        try:
            last_text = await page.locator("body").inner_text()
        except Exception:
            last_text = ""

        if _looks_blocked(last_text):
            if blocked_at is None:
                blocked_at = loop.time()
                print("  ⛔ Антибот-проверка, ждём прохождения...")
            elif (loop.time() - blocked_at) * 1000 >= blocked_grace_ms:
                raise BlockedError(
                    "сайт показывает антибот-проверку вместо карточек. "
                    "Запустите скрипт с флагом --visible и пройдите "
                    "проверку вручную."
                )
        await page.wait_for_timeout(1000)

    count = await page.locator(".doctor-card-v2").count()
    if count:
        return count

    if blocked_at is not None or _looks_blocked(last_text):
        raise BlockedError(
            "сайт показывает антибот-проверку вместо карточек. "
            "Запустите скрипт с флагом --visible и пройдите проверку вручную."
        )
    raise RuntimeError(
        "карточки врачей не найдены — возможно, изменилась разметка сайта"
    )


async def goto_with_retry(
    page: Page,
    url: str,
    attempts: int = 4,
    timeout_ms: int = 60000,
):
    """Открывает URL с повторами при временных сетевых ошибках."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=timeout_ms
            )
            if response and response.status >= 500 and attempt < attempts:
                print(
                    f"  ⚠️ HTTP {response.status}, "
                    f"повтор {attempt + 1}/{attempts}..."
                )
                await page.wait_for_timeout(2000 * attempt)
                continue
            return response
        except Exception as e:
            last_error = e
            if attempt >= attempts:
                raise
            delay = min(2 ** attempt, 15)
            print(
                f"  ⚠️ Ошибка загрузки ({type(e).__name__}), "
                f"повтор {attempt + 1}/{attempts} через {delay} с..."
            )
            await page.wait_for_timeout(delay * 1000)
    raise last_error


async def accept_cookies(page: Page) -> None:
    try:
        cookie_btn = page.locator(
            "button:has-text('Соглашаюсь'), button:has-text('Принять'), "
            "button:has-text('Согласен')"
        ).first
        if await cookie_btn.count() > 0 and await cookie_btn.is_visible():
            await cookie_btn.click()
            await page.wait_for_timeout(500)
            print("🍪 Куки приняты")
    except Exception as e:
        print(f"⚠️ Не удалось нажать куки: {e}")


async def make_context(p, headless: bool):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return await p.firefox.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        viewport={"width": 1440, "height": 2000},
        extra_http_headers={
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )


async def open_single_page(context) -> Page:
    """Открывает ровно одну страницу на браузер.

    Рекомендация: один URL обрабатывается в отдельной странице (вкладке).
    Перед новым филиалом закрываем все старые вкладки, чтобы не открывать
    несколько страниц одновременно и не создавать лишнюю нагрузку на сайт.
    """
    for old_page in list(context.pages):
        try:
            await old_page.close()
        except Exception:
            pass
    return await context.new_page()


# ----------------------------- Парсинг одной карточки -----------------------------

async def load_all_cards(page: Page) -> int:
    """Нажимает 'Показать ещё' пока кнопка есть и карточки добавляются."""
    prev_count = 0
    for attempt in range(50):  # защита от бесконечного цикла
        count = await page.locator(".doctor-card-v2").count()
        print(f"  итерация {attempt + 1}: карточек {count}")

        btn = page.locator("text=Показать ещё").first
        if await btn.count() == 0:
            print("  кнопки 'Показать ещё' больше нет")
            break

        await btn.scroll_into_view_if_needed()
        await page.wait_for_timeout(300)

        try:
            await btn.click(timeout=5000)
        except Exception as e:
            print(f"  клик не удался: {e}")
            break

        try:
            await page.wait_for_function(
                "n => document.querySelectorAll('.doctor-card-v2').length > n",
                arg=count,
                timeout=10000,
            )
        except Exception:
            print("  новых карточек не появилось — останавливаемся")
            break

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
    name_el = await card.query_selector(
        ".object-info__title-link, a[href*='/doctor-profile/'], [class*='name']"
    )
    name = (await name_el.inner_text()).strip() if name_el else None

    # Филиал
    filial_el = await card.query_selector(".workplace-address-card__name")
    filial = (await filial_el.inner_text()).strip() if filial_el else None

    # Рейтинг и отзывы
    rating_m = re.search(r"(\d\.\d)\s*\n?\s*(\d+)\s*отзыв", text)
    rating = float(rating_m.group(1)) if rating_m else None
    reviews = int(rating_m.group(2)) if rating_m else None

    # Стаж
    exp_m = re.search(r"Стаж\s+(\d+)\s+(?:год|года|лет)", text)
    experience = int(exp_m.group(1)) if exp_m else None

    # Категория
    cat_m = re.search(
        r"(Высшая категория|Первая категория|Вторая категория|"
        r"Кандидат мед\. наук|Доктор мед\. наук)",
        text,
    )
    category = cat_m.group(1) if cat_m else None

    # Специальности: сначала из чипов, затем запасной вариант из текста
    specialties: list[str] = []
    for chip in await card.query_selector_all(".speciality-list__chip"):
        value = (await chip.inner_text()).strip()
        if value and value not in ("Взрослый", "Детский", "Взрослый, детский"):
            specialties.append(value)
    if not specialties:
        spec_m = re.search(
            r"Документы проверены\s*\n(.+?)\n(Взрослый[^\n]*)", text, re.S
        )
        if spec_m:
            specialties = [
                s.strip() for s in spec_m.group(1).split("\n") if s.strip()
            ]

    # Взрослый / детский
    adult_m = re.search(r"(Взрослый(?:,\s*детский)?)", text)
    adults = adult_m.group(1) if adult_m else None

    # Цены: сначала пробуем разметку, затем текст
    prices: list[PriceItem] = []
    for row in await card.query_selector_all(".card-prices__price-row"):
        row_text = (await row.inner_text()).strip()
        if not row_text:
            continue
        service_el = await row.query_selector("div:first-child")
        service = (
            (await service_el.inner_text()).strip() if service_el else row_text
        )
        price_el = await row.query_selector(
            ".price-with-discount__price, .card-prices__price-container"
        )
        price_text = (await price_el.inner_text()) if price_el else row_text
        price = parse_price(price_text)
        if service and price:
            prices.append(PriceItem(service, price))
    if not prices:
        for m in re.finditer(
            r"([А-ЯA-Z][^\n₽]{3,}?)\n\s*(?:от\s*)?([\d\s]+)\s*₽", text
        ):
            service = m.group(1).strip()
            price = parse_price(m.group(2))
            if price:
                prices.append(PriceItem(service, price))

    # Статус
    if "окон нет" in text or "нет свободных окон" in text:
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

async def collect_slots_for_card(
    page: Page, card: ElementHandle
) -> dict[str, list[str]]:
    """
    Кликает по каждому доступному дню в календаре и собирает время приёма.
    Возвращает словарь {дата: [время, ...]}.
    """
    result: dict[str, list[str]] = {}

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
        days_meta.append({"date": raw_date})

    # Для каждого доступного дня кликаем и собираем слоты
    for i, day in enumerate(days_meta):
        try:
            # Перезапрашиваем кнопки, чтобы избежать stale element
            fresh_buttons = await card.query_selector_all(
                ".slider-calendar__day-button"
            )
            target = None
            for fb in fresh_buttons:
                sub = await fb.query_selector(
                    ".slider-calendar__day-button-subtitle"
                )
                if sub and (await sub.inner_text()).strip() == day["date"]:
                    target = fb
                    break

            if not target:
                continue
            if i:
                await target.click()

            await page.wait_for_timeout(600)  # ждём перерисовку списка слотов

            # Собираем слоты из текущего выбранного дня
            slot_buttons = await card.query_selector_all(
                ".time-slots-list__time-slot"
            )
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
            try:
                await page.screenshot(
                    path=str(DATA_DIR / f"click_error_{day['date']}.png")
                )
            except Exception:
                pass
            continue

    return result


# ----------------------------- Основной парсер -----------------------------

async def parse_page(
    page: Page,
    url: str,
    blocked_grace_ms: int = 30000,
) -> list[Doctor]:
    doctors: list[Doctor] = []

    print(f"\n🌐 Загружаем страницу: {url}")
    response = await goto_with_retry(page, url)
    if response and response.status >= 400:
        if response.status in (403, 429):
            raise BlockedError(f"HTTP {response.status}")
        raise RuntimeError(f"HTTP {response.status}")

    await wait_for_cards(page, blocked_grace_ms=blocked_grace_ms)
    await accept_cookies(page)

    # Прокрутка для подгрузки всех карточек
    print("📜 Прокручиваем страницу...")
    for _ in range(15):
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(700)

    total = await load_all_cards(page)
    print(f"✅ Итого карточек: {total}")

    cards = await page.query_selector_all(".doctor-card-v2")
    print(f"🔍 Найдено карточек: {len(cards)}")

    slug = re.sub(r"[^a-zA-Z0-9]+", "_", url)[-80:]
    for i, card in enumerate(cards):
        html = await card.inner_html()
        with open(
            DATA_DIR / f"{slug}_{i}_page.html", "w", encoding="utf-8"
        ) as f:
            f.write(html)

    for i, card in enumerate(cards):
        try:
            doc = await parse_card_static(card)
            with open(ROOT / "output.txt_", "a", encoding="utf-8") as f:
                print(doc, sep="\n", file=f)
            if not doc.name:
                continue

            print(f"  [{i + 1}/{len(cards)}] {doc.name[:40]}...", end=" ")

            # Собираем слоты, если есть запись
            if doc.status == "есть запись":
                slots = await collect_slots_for_card(page, card)
                doc.slots_by_date = slots
                total_slots = sum(len(v) for v in slots.values())
                print(f"→ {len(slots)} дн., {total_slots} слотов")
            else:
                print("→ без записи")

            doctors.append(doc)

        except Exception as e:
            print(f"  ⚠️ Пропуск карточки {i + 1}: {e}")
            continue

    return doctors


OUTPUT_FILE = ROOT / "doctors.json"


def load_output() -> list[dict]:
    if not OUTPUT_FILE.exists():
        return []
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"⚠️ Не удалось прочитать {OUTPUT_FILE}: {e}")
        return []


def merge_doctors(existing: list[dict], new: list[dict]) -> list[dict]:
    """Объединяет результаты, заменяя запись того же врача и филиала."""
    merged: dict[tuple, dict] = {}
    for item in existing + new:
        key = (item.get("name"), item.get("filial"))
        merged[key] = item
    return list(merged.values())


def save_output(output: list[dict]) -> None:
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


async def run(
    urls: list[str],
    headless: bool,
    append: bool = False,
) -> list[dict]:
    output: list[dict] = load_output() if append else []
    if append and output:
        print(f"📥 В doctors.json уже {len(output)} записей")

    async with async_playwright() as p:
        context = await make_context(p, headless)
        try:
            for url in urls:
                page = await open_single_page(context)
                try:
                    try:
                        data = await parse_page(page, url)
                    except BlockedError as e:
                        print(f"\n⛔ Блокировка: {e}")
                        if not headless:
                            raise
                        print(
                            "🔁 Перезапускаю Firefox с окном, "
                            "чтобы пройти проверку..."
                        )
                        await context.close()
                        context = await make_context(p, headless=False)
                        page = await open_single_page(context)
                        data = await parse_page(
                            page,
                            url,
                            blocked_grace_ms=180000,
                        )
                except Exception as e:
                    print(f"\n⚠️ Не удалось собрать {url}: {e}")
                    print("   Переходим к следующему URL.")
                    continue
                finally:
                    # Одна страница на один URL: закрываем вкладку после сбора.
                    try:
                        await page.close()
                    except Exception:
                        pass

                output = merge_doctors(
                    output, [asdict(d) for d in data]
                )
                save_output(output)
                print(
                    f"\n✅ Готово! Собрано врачей: {len(data)} для {url}"
                )
        finally:
            await context.close()

    return output


# ----------------------------- Точка входа -----------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Парсер врачей НаПоправку")
    parser.add_argument(
        "--visible",
        action="store_true",
        help="запустить Firefox с окном (нужно при первой проверке)",
    )
    parser.add_argument(
        "--url",
        action="append",
        dest="urls",
        help="URL для парсинга; можно указать несколько раз",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="добавить результат к существующему doctors.json",
    )
    args = parser.parse_args()

    print("🚀 Запуск парсера...")
    urls = args.urls or BASE_URL

    try:
        result = asyncio.run(
            run(urls, headless=not args.visible, append=args.append)
        )
    except BlockedError as e:
        print(f"\n❌ Не удалось обойти блокировку: {e}")
        raise SystemExit(2)

    save_output(result)
    print(f"\n📁 Результат: {ROOT / 'doctors.json'}")

    # Краткая статистика
    with_slots = sum(1 for d in result if d.get("slots_by_date"))
    total_slots = sum(
        sum(len(v) for v in d.get("slots_by_date", {}).values())
        for d in result
    )
    print(f"📊 Врачей со слотами: {with_slots}")
    print(f"📊 Всего слотов: {total_slots}")
