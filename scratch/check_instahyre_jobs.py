import asyncio
from naukri_agent.db.pool import get_pool

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT j.job_id, j.title, j.company, j.source_keyword, a.run_id, a.detail
            FROM jobs j
            LEFT JOIN applications a ON j.job_id = a.job_id
            WHERE a.run_id = 221 AND j.company IN ('Altimate AI', 'Adobe', 'Purplle', 'eDAS', 'Juleo')
        """)
        for r in rows:
            print(dict(r))
    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
