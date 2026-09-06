import asyncio
from naukri_agent.db.pool import get_pool

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT j.job_id, j.title, j.company, a.run_id, a.status, a.reason, a.created_at
            FROM jobs j
            JOIN applications a ON j.job_id = a.job_id
            WHERE j.job_id LIKE 'cutshort-%'
            ORDER BY a.created_at DESC
        """)
        print(f"Total Cutshort applications in DB history: {len(rows)}")
        for r in rows:
            print(f"- [Run #{r['run_id']}] [{r['status']}] {r['title']} @ {r['company']} (ID: {r['job_id']}) reason={r['reason']}")

    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
