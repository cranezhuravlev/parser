"""Read-only UI collector for SM clinic public appointment widget."""
import argparse, asyncio, csv, hashlib, json, re, sqlite3, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parent
TZ=timezone(timedelta(hours=3))
URL='https://www.smclinic-spb.ru/appointment/?oz_source=widget_lkp_spb'
MONTHS='января февраля марта апреля мая июня июля августа сентября октября ноября декабря'.split()

def parse_date(label,now):
    m=re.search(r'(\d{1,2})\s+('+'|'.join(MONTHS)+r')',label.lower())
    if not m: raise ValueError('Unknown date: '+label)
    month=MONTHS.index(m[2])+1
    d=datetime(now.year,month,int(m[1]),tzinfo=TZ)
    if d.date()<now.date(): d=d.replace(year=now.year+1)
    return d

META='''b => {
 const c=b.closest('article');
 const before=b.previousElementSibling;
 return {doctor:c.querySelector('.personal-info__title')?.innerText.trim(),
 clinic:b.querySelector('.left-part__item')?.innerText.trim(),
 address:b.querySelector('.grey-text')?.innerText.trim(),
 service:before?.querySelector('p')?.innerText.trim() || '',
 price:before?.querySelector('p.text-medium')?.innerText.trim() || ''};
}'''

async def collect_specialty(frame,specialty,horizon,max_pages):
    await frame.get_by_text(specialty,exact=True).click()
    await frame.get_by_role('heading',name='Новая запись к врачу: '+specialty,exact=True).wait_for(timeout=60000)
    await frame.locator('article').first.wait_for(timeout=60000)
    complete=False
    for _ in range(max_pages):
        more=frame.get_by_role('button').filter(has_text=re.compile('^Показать ещё$'))
        if not await more.count() or not await more.is_visible(): complete=True;break
        old=await frame.locator('article').count()
        await more.click()
        for attempt in range(60):
            if await frame.locator('article').count()>old: break
            await asyncio.sleep(.5)
        else: raise RuntimeError('Show more did not increase card count')
        await asyncio.sleep(2)
    rows=[]
    bodies=frame.locator('article .card-body')
    for i in range(await bodies.count()):
        body=bodies.nth(i); now=datetime.now(TZ)
        meta=await body.evaluate(META)
        if not all(meta.get(k) for k in ('doctor','clinic','address')):
            raise RuntimeError('Missing doctor/clinic/address: layout changed')
        row=dict(meta,specialty=specialty,city='Санкт-Петербург',source='smclinic_official',source_url=URL,
                 age_profile='adult_1990',snapshot_at=now.isoformat(),nearest_slot=None,days_to_slot=None,
                 availability_status='schedule_unresolved',search_horizon_days=horizon)
        combo=body.get_by_role('combobox')
        if await combo.count():
            await combo.click()
            options=combo.get_by_role('option')
            labels=await options.all_text_contents()
            candidates=sorted((parse_date(x,now),x) for x in labels)
            candidates=[(d,x) for d,x in candidates if 0<=(d.date()-now.date()).days<=horizon]
            # Check earliest offered dates until a future slot appears. Never click a time.
            for d,label in candidates:
                if not await combo.get_attribute('aria-expanded')=='true': await combo.click()
                await combo.get_by_role('option',name=label,exact=True).click()
                await asyncio.sleep(1)
                await body.locator('.vs__selected').filter(has_text=label).wait_for(timeout=15000)
                times=await body.locator('.slot').all_text_contents()
                future=[]
                for t in times:
                    if re.fullmatch(r'\d{2}:\d{2}',t.strip()):
                        h,m=map(int,t.strip().split(':')); slot=d.replace(hour=h,minute=m)
                        if slot>datetime.now(TZ): future.append(slot)
                if future:
                    nearest=min(future)
                    row.update(nearest_slot=nearest.isoformat(),days_to_slot=(nearest.date()-now.date()).days,availability_status='available')
                    break
                await asyncio.sleep(1)
            if await combo.get_attribute('aria-expanded')=='true': await combo.click()
            # Without a verified completeness contract, an empty list is not no_slots.
        rows.append(row)
        await asyncio.sleep(.5)
    return rows,complete

async def run(args):
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    run_id=str(uuid.uuid4()); rows=[];reports=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=not args.visible)
        page=await browser.new_page(locale='ru-RU',timezone_id='Europe/Moscow')
        try:
            await page.goto(URL,wait_until='domcontentloaded',timeout=60000)
            frame=page.frame_locator('#online-form')
            print(frame)
            await frame.get_by_role('textbox').first.wait_for(timeout=60000)
            reject=frame.get_by_role('button').filter(has_text=re.compile('^Отказаться$'))
            if await reject.count(): await reject.click()

            from playwright.async_api import expect

            birth = frame.get_by_role("textbox")
            await birth.fill("01.01.1990")

            # 1а. Проверить, что значение записалось
            await expect(birth).to_have_value("01.01.1990", timeout=5000)
            # или без ожидания:
            print("value:", await birth.input_value())

            # await frame.get_by_role('textbox').fill('01.01.1990')
            print(frame)
            await frame.get_by_role('button').filter(has_text='Продолжить').click()
            await frame.get_by_text("Гепатолог", exact=True).click()
            print(frame)
            await frame.get_by_role('heading',name='Выберите специалиста:',exact=True).wait_for(timeout=10000)
            names=args.specialty or ['Кардиолог']
            if args.all_specialties:
                names=await frame.locator('main p').all_text_contents()
                names=[n.strip() for n in names if n.strip() not in ('Ваш город:','Найдите нужную специальность врача')]
            for name in names:
                try:
                    new,complete=await collect_specialty(frame,name,args.horizon,args.max_pages)
                    rows.extend(new);reports.append(dict(specialty=name,status='ok' if complete else 'partial',rows=len(new)))
                    await frame.get_by_role('link',name='Запись к врачу',exact=True).click()
                    await frame.get_by_role('heading',name='Выберите специалиста:',exact=True).wait_for(timeout=60000)
                    await asyncio.sleep(3)
                except Exception as e:
                    reports.append(dict(specialty=name,status='error',error=str(e)))
                    break
        finally: await browser.close()
    unique={}
    for r in rows:
        key=tuple(r[k] for k in ('doctor','address','service','specialty'))
        r['run_id']=run_id;r['record_key']=hashlib.sha256(json.dumps(key,ensure_ascii=False).encode()).hexdigest()
        unique[key]=r
    rows=list(unique.values())
    if rows:
        with (output/f'{run_id}.csv').open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]),delimiter=';');w.writeheader();w.writerows(rows)
        with sqlite3.connect(output/'sm_history.sqlite') as db:
            db.execute('CREATE TABLE IF NOT EXISTS snapshots(run_id TEXT, record_key TEXT, payload TEXT, PRIMARY KEY(run_id,record_key))')
            db.executemany('INSERT OR IGNORE INTO snapshots VALUES(?,?,?)',[(run_id,r['record_key'],json.dumps(r,ensure_ascii=False)) for r in rows])
    (output/f'{run_id}_report.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(reports,ensure_ascii=False,indent=2))
    if not reports or any(r['status']!='ok' for r in reports): raise SystemExit(2)

if __name__=='__main__':
    a=argparse.ArgumentParser()
    a.add_argument('--visible',action='store_true')
    a.add_argument('--specialty',action='append')
    a.add_argument('--all-specialties',action='store_true')
    a.add_argument('--horizon',type=int,default=60)
    a.add_argument('--max-pages',type=int,default=40)
    a.add_argument('--output',default=str(ROOT/'sm_output'))
    asyncio.run(run(a.parse_args()))
