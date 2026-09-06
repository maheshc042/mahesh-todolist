import asyncio
from naukri_agent.db.pool import get_pool
from naukri_agent.config import get_settings

async def main():
    pool = await get_pool()
    settings = get_settings()
    async with pool.acquire() as conn:
        print("=== RUN 221 METRICS ===")
        run = await conn.fetchrow("SELECT * FROM runs WHERE id = 221")
        if run:
            for k, v in dict(run).items():
                print(f"{k}: {v}")
        
        print("\n=== APPLICATIONS IN RUN 221 ===")
        apps = await conn.fetch("""
            SELECT a.id, a.job_id, j.title, j.company, j.url, a.profile, a.status, a.reason, a.detail
            FROM applications a
            LEFT JOIN jobs j ON a.job_id = j.job_id
            WHERE a.run_id = 221
        """)
        print(f"Total: {len(apps)}")
        for a in apps:
            d = dict(a)
            print(f"- [{d['job_id']}] {d['title']} @ {d['company']} | Status: {d['status']} | Reason: {d['reason']} | Detail: {d['detail']}")

        print("\n=== RUN EVENTS FOR RUN 221 (APPLY / DECISION / FAIL / SKIP) ===")
        events = await conn.fetch("""
            SELECT event, job_id, profile, payload, created_at
            FROM run_events
            WHERE run_id = 221
            ORDER BY id ASC
        """)
        print(f"Total run events: {len(events)}")
        for e in events:
            print(f"[{e['event']}] job_id={e['job_id']} payload={e['payload']}")


    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
