import asyncio
import json
from naukri_agent.db.pool import get_pool

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        print("=== RUN 221 STATS RECORD ===")
        run = await conn.fetchrow("SELECT stats FROM runs WHERE id = 221")
        if run:
            stats = json.loads(run["stats"]) if isinstance(run["stats"], str) else run["stats"]
            print(json.dumps(stats, indent=2))

        print("\n=== CUTSHORT JOBS (FIRST SEEN IN RUN 221) ===")
        cutshort_jobs = await conn.fetch("""
            SELECT j.job_id, j.title, j.company, j.location, j.experience_text, j.min_experience, j.max_experience,
                   a.status, a.reason, a.detail
            FROM jobs j
            LEFT JOIN applications a ON j.job_id = a.job_id AND a.run_id = 221
            WHERE j.job_id LIKE 'cutshort-%'
            ORDER BY j.first_seen_at DESC
            LIMIT 40
        """)
        print(f"Total Cutshort jobs: {len(cutshort_jobs)}")
        for j in cutshort_jobs:
            st = j['status'] or 'scraped_not_applied'
            print(f"- [{st}] {j['title']} @ {j['company']} (Exp: {j['experience_text']} | min={j['min_experience']} max={j['max_experience']}) reason={j['reason']}")

        print("\n=== INSTAHYRE JOBS (FIRST SEEN IN RUN 221) ===")
        instahyre_jobs = await conn.fetch("""
            SELECT j.job_id, j.title, j.company, j.location, j.experience_text, j.tags,
                   a.status, a.reason, a.detail
            FROM jobs j
            LEFT JOIN applications a ON j.job_id = a.job_id AND a.run_id = 221
            WHERE j.job_id LIKE 'instahyre-%'
            ORDER BY j.first_seen_at DESC
            LIMIT 60
        """)
        print(f"Total Instahyre jobs: {len(instahyre_jobs)}")
        for j in instahyre_jobs:
            st = j['status'] or 'scraped_not_applied'
            print(f"- [{st}] {j['title']} @ {j['company']} (Tags: {j['tags'][:3]}) reason={j['reason']}")

        print("\n=== LINKEDIN JOBS (FIRST SEEN IN RUN 221) ===")
        linkedin_jobs = await conn.fetch("""
            SELECT j.job_id, j.title, j.company, j.location, j.experience_text,
                   a.status, a.reason, a.detail
            FROM jobs j
            LEFT JOIN applications a ON j.job_id = a.job_id AND a.run_id = 221
            WHERE j.job_id LIKE 'linkedin-%'
            ORDER BY j.first_seen_at DESC
            LIMIT 30
        """)
        print(f"Total LinkedIn jobs: {len(linkedin_jobs)}")
        for j in linkedin_jobs:
            st = j['status'] or 'scraped_not_applied'
            print(f"- [{st}] {j['title']} @ {j['company']} reason={j['reason']}")

    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
