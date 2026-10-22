"""Verified NaPopravku clinic roster prototype; no slot adapter yet."""
import argparse, asyncio, csv, json, sqlite3, uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parent
MOSCOW=timezone(timedelta(hours=3))

async def collect(page,url,max_pages):
    parsed=urlparse(url)
    if parsed.hostname!='napopravku.ru' or not parsed.path.startswith('/spb/clinics/'):
        raise ValueError('Only napopravku.ru/spb/clinics/ URLs are accepted')
    response=await page.goto(url,wait_until='domcontentloaded',timeout=60000)
    if response and response.status>=400:
        raise RuntimeError(f'HTTP {response.status}; collection stopped')
    await page.locator('.doctor-card-v2').first.wait_for(timeout=30000)
    heading=await page.locator('h1').inner_text()
    clinic=heading.removeprefix('Врачи ').strip()
    complete=False
    for _ in range(max_pages):
        button=page.get_by_role('button',name='Показать ещё',exact=True)
        if not await button.count() or not await button.is_visible():
            complete=True
            break
        before=await page.locator('.doctor-card-v2').count()
        await button.click()
        await page.wait_for_function("n => document.querySelectorAll('.doctor-card-v2').length > n",arg=before,timeout=30000)
        await asyncio.sleep(2)
    rows=await page.evaluate((ROOT/'extract.js').read_text())
    unique={}
    for row in rows:
        if not row['doctor'] or not row['doctor_url'] or not row['address']:
            raise RuntimeError('Card layout changed: required fields missing')
        if 'Санкт-Петербург' not in row['address']:
            continue
        row.update(clinic=clinic,source_url=url,aggregator='napopravku',city='Санкт-Петербург')
        unique[(row['doctor_url'],row['address'])]=row
    if not unique:
        raise RuntimeError('No Saint Petersburg doctors found')
    return list(unique.values()),complete

async def main(args):
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    output=Path(args.output); output.mkdir(parents=True,exist_ok=True)
    run_id=str(uuid.uuid4()); stamp=datetime.now(MOSCOW).isoformat()
    rows=[]; reports=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=not args.visible)
        page=await browser.new_page(locale='ru-RU',timezone_id='Europe/Moscow')
        try:
            for url in config['clinic_urls']:
                try:
                    collected,complete=await collect(page,url,args.max_pages)
                    for row in collected: row.update(run_id=run_id,snapshot_at=stamp)
                    rows.extend(collected)
                    reports.append(dict(url=url,status='ok' if complete else 'partial',rows=len(collected)))
                except Exception as exc:
                    reports.append(dict(url=url,status='error',error=str(exc)))
                await asyncio.sleep(3)
        finally:
            await browser.close()
    # Each run has separate files: failed runs never replace previous good output.
    report_path=output/f'{run_id}_report.json'
    report_path.write_text(json.dumps(dict(run_id=run_id,snapshot_at=stamp,clinics=reports),ensure_ascii=False,indent=2),encoding='utf-8')
    if rows:
        with (output/f'{run_id}.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]),delimiter=';');writer.writeheader();writer.writerows(rows)
        with sqlite3.connect(output/'history.sqlite') as db:
            db.execute('CREATE TABLE IF NOT EXISTS snapshots (run_id TEXT, snapshot_at TEXT, clinic_url TEXT, doctor_url TEXT, address TEXT, payload TEXT, UNIQUE(run_id,clinic_url,doctor_url,address))')
            db.executemany('INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?)',[(run_id,stamp,r['source_url'],r['doctor_url'],r['address'],json.dumps(r,ensure_ascii=False)) for r in rows])
    print(json.dumps(reports,ensure_ascii=False,indent=2))
    if any(r['status']!='ok' for r in reports): raise SystemExit(2)

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--config',default=str(ROOT/'config.json'))
    ap.add_argument('--output',default=str(ROOT/'output'))
    ap.add_argument('--visible',action='store_true')
    ap.add_argument('--max-pages',type=int,default=30)
    asyncio.run(main(ap.parse_args()))
