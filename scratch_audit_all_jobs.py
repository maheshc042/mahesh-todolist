import asyncio
import os
import json
from dotenv import load_dotenv
from naukri_agent.db.pool import get_pool, close_pool

async def main():
    load_dotenv()
    pool = await get_pool()
    async with pool.acquire() as conn:
        latest_run = await conn.fetchrow("SELECT id, started_at, stats FROM runs ORDER BY id DESC LIMIT 1")
        run_id = latest_run["id"]
        print(f"=== FULL AUDIT OF RUN {run_id} ({latest_run['started_at']}) ===")

        apps = await conn.fetch(
            """
            SELECT a.id, a.job_id, a.profile, a.status, a.reason, a.detail, a.created_at,
                   j.title, j.company, j.location, j.experience_text, j.salary_text, j.url
            FROM applications a
            LEFT JOIN jobs j ON a.job_id = j.job_id
            WHERE a.run_id = $1
            ORDER BY a.id ASC
            """,
            run_id
        )

        for idx, a in enumerate(apps, 1):
            st = str(a["status"]).upper()
            title = str(a["title"])
            comp = str(a["company"])
            loc = str(a["location"])
            exp = str(a["experience_text"])
            sal = str(a["salary_text"])
            detail = str(a["detail"] or a["reason"])
            url = str(a["url"])
            print(f"[{idx:2d}] {st:<12} | {title} @ {comp}")
            print(f"     Location: {loc} | Exp: {exp} | Salary: {sal}")
            print(f"     Outcome: {detail}")
            print(f"     URL: {url}")
            print()

    await close_pool()

if __name__ == "__main__":
    asyncio.run(main())
