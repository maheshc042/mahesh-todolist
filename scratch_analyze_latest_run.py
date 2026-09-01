import asyncio
import os
import json
from dotenv import load_dotenv
from naukri_agent.db.pool import get_pool, close_pool

async def main():
    load_dotenv()
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1. Fetch the latest 5 runs
        runs = await conn.fetch(
            "SELECT id, mode, status, profiles, started_at, finished_at, duration_s, stats, error "
            "FROM runs ORDER BY id DESC LIMIT 5"
        )
        print("=== LATEST 5 RUNS ===")
        for r in runs:
            stats = json.loads(r["stats"]) if isinstance(r["stats"], str) else r["stats"]
            print(f"Run ID: {r['id']} | Mode: {r['mode']} | Status: {r['status']} | Profiles: {r['profiles']} | Started: {r['started_at']} | Duration: {r['duration_s']}s")
            print(f"Stats: {stats}")
            print("-" * 60)

        if not runs:
            print("No runs found.")
            return

        latest_run_id = runs[0]["id"]
        print(f"\n=== DETAILED APPLICATIONS FOR LATEST RUN: {latest_run_id} ===")

        apps = await conn.fetch(
            """
            SELECT a.id, a.job_id, a.profile, a.status, a.reason, a.detail, a.created_at,
                   j.title, j.company, j.location, j.experience_text, j.salary_text, j.posted_text, j.url
            FROM applications a
            LEFT JOIN jobs j ON a.job_id = j.job_id
            WHERE a.run_id = $1
            ORDER BY a.id ASC
            """,
            latest_run_id
        )

        print(f"Total evaluated jobs recorded for Run {latest_run_id}: {len(apps)}\n")

        status_counts = {}
        for idx, a in enumerate(apps, 1):
            st = a["status"]
            status_counts[st] = status_counts.get(st, 0) + 1
            print(f"{idx:2d}. [{st.upper():<14}] {a['title']} @ {a['company']}")
            print(f"    Location: {a['location']} | Exp: {a['experience_text']} | Salary: {a['salary_text']}")
            print(f"    Reason: {a['reason']} | Detail: {a['detail']}")
            print(f"    URL: {a['url']}")
            print()

        print("=== BREAKDOWN BY STATUS ===")
        for st, cnt in status_counts.items():
            print(f"  • {st.upper()}: {cnt}")

    await close_pool()

if __name__ == "__main__":
    asyncio.run(main())
