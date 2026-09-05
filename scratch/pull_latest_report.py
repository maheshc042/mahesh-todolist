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

        # Check applications schema
        cols = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'applications' ORDER BY ordinal_position")
        col_names = [c['column_name'] for c in cols]
        print(f"\nApplications columns: {col_names}")

        # Check jobs schema
        job_cols = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'jobs' ORDER BY ordinal_position")
        job_col_names = [c['column_name'] for c in job_cols]
        print(f"Jobs columns: {job_col_names}")

        # Applications by platform and status
        apps = await conn.fetch('SELECT platform, status, count(*) FROM applications WHERE run_id = $1 GROUP BY platform, status', run_id)
        print(f'\n=== APPLICATIONS BREAKDOWN FOR RUN {run_id} ===')
        for a in apps:
            p = a['platform']
            s = a['status']
            c = a['count']
            print(f'  Platform: {p} | Status: {s} | Count: {c}')

        # Detailed applications with job title and company
        query = """
            SELECT a.id, a.job_id, a.platform, a.status, a.submitted_at, a.reason, a.detail,
                   j.company, j.title, j.location
            FROM applications a
            LEFT JOIN jobs j ON a.job_id = j.job_id
            WHERE a.run_id = $1
            ORDER BY a.id ASC
        """
        detailed_apps = await conn.fetch(query, run_id)
        print(f'\n=== DETAILED APPLICATIONS FOR RUN {run_id} ({len(detailed_apps)} total) ===')
        for app in detailed_apps:
            print(f"  [{app['platform']}] {app['company']} - {app['title']} ({app['location']})")
            print(f"       -> Status: {app['status']} | Reason: {app['reason']} | Detail: {app['detail']}")

        # Events breakdown
        event_counts = await conn.fetch('SELECT event_type, count(*) FROM events WHERE run_id = $1 GROUP BY event_type', run_id)
        print(f'\n=== EVENT COUNTS FOR RUN {run_id} ===')
        for ec in event_counts:
            print(f"  {ec['event_type']}: {ec['count']}")

        # Recent key events (errors, warnings, questionnaires, apply)
        events = await conn.fetch("SELECT event_type, level, payload, created_at FROM events WHERE run_id = $1 AND (level IN ('warning', 'error') OR event_type LIKE '%questionnaire%' OR event_type LIKE '%message%' OR event_type LIKE '%apply%' OR event_type LIKE '%search%') ORDER BY id ASC", run_id)
        print(f'\n=== KEY EVENTS FOR RUN {run_id} ({len(events)} total) ===')
        for e in events:
            etype = e['event_type']
            lvl = e['level']
            payload = e['payload']
            print(f"  [{lvl}] {etype}: {payload}")

asyncio.run(main())
