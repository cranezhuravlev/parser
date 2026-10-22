import json
from datetime import datetime
from typing import Optional

import pandas as pd


INPUT_FILE = "doctors.json"
OUTPUT_FILE = "doctors.xlsx"


def parse_slot_to_datetime(date_str: str, time_str: str) -> Optional[datetime]:
    """
    Преобразует "23.10" + "16:10" в datetime.
    Год берём текущий (или следующий, если дата уже прошла).
    """
    try:
        day, month = map(int, date_str.split("."))
        hour, minute = map(int, time_str.split(":"))
        now = datetime.now()
        year = now.year
        dt = datetime(year, month, day, hour, minute)
        # Если дата уже прошла — значит это следующий год
        if dt < now:
            dt = datetime(year + 1, month, day, hour, minute)
        return dt
    except Exception:
        return None


def flatten_doctor(doctor: dict) -> list[dict]:
    """
    Разворачивает одного врача в список строк:
    одна строка = одна услуга с ценой и слотами.
    """
    rows = []

    name = doctor.get("name") or "—"
    filial = doctor.get("filial") or "—"
    specialties = ", ".join(doctor.get("specialties") or [])
    prices = doctor.get("prices") or []
    slots_by_date = doctor.get("slots_by_date") or {}

    # --- Собираем все слоты с полными datetime для поиска ближайшего ---
    all_slots = []
    first_slots = []
    for date_str, times in slots_by_date.items():
        if first_slots == []:
            first_slots = times
        for t in times:
            dt = parse_slot_to_datetime(date_str, t)
            if dt:
                all_slots.append(dt)
    all_slots.sort()

    nearest_slot = all_slots[0].strftime("%d.%m.%Y %H:%M") if all_slots else "нет слотов"

    total_slots = len(all_slots)

    # --- Если услуг нет — всё равно добавляем строку с врачом ---
    if not prices:
        rows.append({
            "Врач": name,
            "Филиал": filial,
            "Специальности": specialties,
            "Услуга": "—",
            "Цена, ₽": None,
            "Ближайший слот": nearest_slot,
            "Слоты на дату": first_slots,
            "Кол-во слотов на дату": len(first_slots),
            "Всего слотов": total_slots,
        })
        return rows

    # --- Одна строка на каждую услугу ---
    for p in prices:
        rows.append({
            "Врач": name,
            "Филиал": filial,
            "Специальности": specialties,
            "Услуга": p.get("service") or "—",
            "Цена, ₽": p.get("price"),
            "Ближайший слот": nearest_slot,
            "Слоты на дату": first_slots,
            "Кол-во слотов на дату": len(first_slots),
            "Всего слотов": total_slots,
        })

    return rows


def main():
    # --- Читаем JSON ---
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        doctors = json.load(f)

    print(f"📖 Прочитано врачей: {len(doctors)}")

    # --- Разворачиваем в плоскую таблицу ---
    all_rows = []
    for d in doctors:
        all_rows.extend(flatten_doctor(d))

    df = pd.DataFrame(all_rows)

    # --- Сортировка: сначала по врачу, потом по цене ---
    if not df.empty:
        df = df.sort_values(
            by=["Врач", "Цена, ₽"],
            ascending=[True, True],
            na_position="last",
        ).reset_index(drop=True)

    # --- Сохраняем в Excel с автошириной колонок ---
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Врачи")

        ws = writer.sheets["Врачи"]

        # Автоширина
        for column_cells in ws.columns:
            length = max(
                len(str(cell.value)) if cell.value is not None else 0
                for cell in column_cells
            )
            ws.column_dimensions[column_cells[0].column_letter].width = min(length + 2, 60)

        # Жирная шапка + автофильтр
        from openpyxl.styles import Font
        for cell in ws[1]:
            cell.font = Font(bold=True)
        ws.auto_filter.ref = ws.dimensions

    print(f"✅ Готово! Строк в таблице: {len(df)}")
    print(f"📁 Файл: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()