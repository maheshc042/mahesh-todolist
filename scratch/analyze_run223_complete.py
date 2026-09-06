import asyncio
import json
from naukri_agent.db.pool import get_pool

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Check run 223
        run = await conn.fetchrow("SELECT * FROM runs WHERE id = 223")
        print("\n=== RUN 223 OVERVIEW ===")
        if run:
            for k, v in dict(run).items():
                print(f"  {k}: {v}")
        else:
            print("Run 223 not found in runs table!")

        # Check applications in run 223
        apps = await conn.fetch("""
            SELECT a.*, j.title, j.company, j.location, j.experience_text, j.salary_text, j.url, j.tags
            FROM applications a
            JOIN jobs j ON a.job_id = j.job_id
            WHERE a.run_id = 223
            ORDER BY a.id ASC
        """)
        print(f"\n=== APPLICATIONS IN RUN 223 ({len(apps)} total) ===")
        
        by_platform = {}
        for a in apps:
            jid = a["job_id"]
            if jid.startswith("instahyre-"):
                p = "instahyre"
            elif jid.startswith("cutshort-"):
                p = "cutshort"
            elif jid.startswith("linkedin-"):
                p = "linkedin"
            elif jid.startswith("wellfound-"):
                p = "wellfound"
            else:
                p = "naukri"
            by_platform.setdefault(p, []).append(a)

        for p, p_apps in by_platform.items():
            print(f"\n--- Platform: {p} ({len(p_apps)} applications) ---")
            status_counts = {}
            for pa in p_apps:
                st = pa["status"]
                status_counts[st] = status_counts.get(st, 0) + 1
            print(f"  Status Summary: {status_counts}")
            for pa in p_apps:
                print(f"    * [{pa['status'].upper()}] {pa['title']} @ {pa['company']} | Profile: {pa['profile']} | Qs: {pa['questions_answered']} | Reason: {pa['reason']} | Detail: {pa['detail']}")

        # Check run events (logs / warnings / errors)
        events = await conn.fetch("""
            SELECT * FROM run_events
            WHERE run_id = 223 AND (level IN ('warning', 'error') OR event LIKE '%fail%' OR event LIKE '%skip%')
            ORDER BY id ASC
        """)
        print(f"\n=== RUN 223 WARNING/ERROR EVENTS ({len(events)}) ===")
        for ev in events:
            print(f"  [{ev['level'].upper()}] {ev['event']} | job: {ev['job_id']} | msg: {ev['message']}")

        # Check cold emails / contacted recruiters during this run window
        if run:
            t_start = run["started_at"]
            t_finish = run["finished_at"] or run["started_at"]
            emails = await conn.fetch("""
                SELECT * FROM contacted_recruiters
                WHERE email_sent_at >= $1
                ORDER BY email_sent_at ASC
            """, t_start)
            print(f"\n=== CONTACTED RECRUITERS IN RUN 223 WINDOW ({len(emails)}) ===")
            for em in emails:
                print(f"  * {em['recruiter_email']} ({em['recruiter_name']}) @ {em['company']} | Role: {em['role_title']} | Exp: {em['experience_required']} | Status: {em['status']}")

    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())

if __name__ == "__main__":
    asyncio.run(main())
