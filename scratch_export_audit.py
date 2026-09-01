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

        with open("audit_full_results.txt", "w", encoding="utf-8") as f:
            f.write(f"=== FULL AUDIT OF RUN {run_id} ({latest_run['started_at']}) ===\n")
            f.write(f"Total Evaluated: {len(apps)}\n\n")

            for idx, a in enumerate(apps, 1):
                st = str(a["status"]).upper()
                title = str(a["title"])
                comp = str(a["company"])
                loc = str(a["location"])
                exp = str(a["experience_text"])
                sal = str(a["salary_text"])
                detail = str(a["detail"] or a["reason"])
                url = str(a["url"])
                f.write(f"[{idx:2d}] {st:<12} | {title} @ {comp}\n")
                f.write(f"     Location: {loc} | Exp: {exp} | Salary: {sal}\n")
                f.write(f"     Outcome: {detail}\n")
                f.write(f"     URL: {url}\n\n")

    await close_pool()

if __name__ == "__main__":
    asyncio.run(main())
