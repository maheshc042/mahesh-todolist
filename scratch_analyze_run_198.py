import asyncio
from naukri_agent.db.pool import get_pool

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        run = await conn.fetchrow("SELECT * FROM runs WHERE id = 198")
        print("=== RUN RECORD ===")
        if run:
            for k, v in dict(run).items():
                print(f"  {k}: {v}")

        apps = await conn.fetch("SELECT status, count(*) FROM applications WHERE run_id = 198 GROUP BY status")
        print("\n=== APPLICATIONS BREAKDOWN ===")
        for a in apps:
            print(f"  Status '{a['status']}': {a['count']}")

        all_apps = await conn.fetch("SELECT id, job_id, status, detail, submitted_at, confirmation_evidence FROM applications WHERE run_id = 198 ORDER BY id ASC")
        print(f"\n=== ALL 28 APPLICATIONS IN RUN 198 ===")
        for i, a in enumerate(all_apps, 1):
            print(f"{i:02d}. ID {a['id']} | {a['job_id']} | Status: {a['status']} | Evidence: {a['confirmation_evidence']} | Detail: {a['detail']}")

if __name__ == "__main__":
    asyncio.run(main())
