import asyncio
import json
from naukri_agent.db.pool import get_pool

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        latest_run = await conn.fetchrow('SELECT * FROM runs ORDER BY id DESC LIMIT 1')
        if not latest_run:
            print('No runs found.')
            return
        
        run_dict = dict(latest_run)
        print('=== LATEST RUN RECORD ===')
        for k, v in run_dict.items():
            print(f'  {k}: {v}')
        
        run_id = latest_run['id']

        # Recent runs list to see if multiple were run today
        recent_runs = await conn.fetch('SELECT id, status, started_at, finished_at, account, profiles FROM runs ORDER BY id DESC LIMIT 5')
        print('\n=== RECENT RUNS ===')
        for r in recent_runs:
            print(f"  Run {r['id']} | Account: {r['account']} | Status: {r['status']} | Started: {r['started_at']} | Finished: {r['finished_at']}")

        # Applications by platform and status
        apps = await conn.fetch('SELECT platform, status, count(*) FROM applications WHERE run_id = $1 GROUP BY platform, status', run_id)
        print(f'\n=== APPLICATIONS BREAKDOWN FOR RUN {run_id} ===')
        for a in apps:
            p = a['platform']
            s = a['status']
            c = a['count']
            print(f'  Platform: {p} | Status: {s} | Count: {c}')

        # Detailed applications
        detailed_apps = await conn.fetch('SELECT id, job_id, platform, company, title, status, applied_at, detail, error_message FROM applications WHERE run_id = $1 ORDER BY id ASC', run_id)
        print(f'\n=== DETAILED APPLICATIONS FOR RUN {run_id} ({len(detailed_apps)} total) ===')
        for app in detailed_apps:
            print(f"  [{app['platform']}] {app['company']} - {app['title']} -> Status: {app['status']} | Detail: {app['detail']} | Error: {app['error_message']}")

        # Events
        events = await conn.fetch('SELECT event_type, level, payload, created_at FROM events WHERE run_id = $1 ORDER BY id ASC', run_id)
        print(f'\n=== EVENTS FOR RUN {run_id} ({len(events)} total) ===')
        for e in events:
            etype = e['event_type']
            lvl = e['level']
            payload = e['payload']
            print(f"  [{lvl}] {etype}: {payload}")

asyncio.run(main())
