import asyncio
from naukri_agent.db.pool import get_pool
from naukri_agent.config import load_config
from naukri_agent.core.application_planner import ApplicationPlanner
from naukri_agent.core.models import Job

async def main():
    pool = await get_pool()
    cfg = load_config()
    
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT j.*
            FROM jobs j
            WHERE j.job_id LIKE 'cutshort-%'
            ORDER BY j.first_seen_at DESC
            LIMIT 37
        """)
        
        jobs = []
        for r in rows:
            d = dict(r)
            jobs.append(Job(
                job_id=d["job_id"],
                title=d["title"],
                company=d["company"],
                url=d["url"],
                location=d["location"] or "",
                experience_text=d["experience_text"] or "",
                salary_text=d["salary_text"] or "",
                tags=d["tags"] or [],
                min_experience=d["min_experience"],
                max_experience=d["max_experience"],
                min_salary_lpa=d["min_salary_lpa"],
                posted_days_ago=d["posted_days_ago"],
            ))

        for prof_name in ["Full Stack / Web Developer", "AI / Python Engineer"]:
            profile = next((p for p in cfg.profiles if p.name == prof_name), None)
            if not profile:
                continue
            candidate = profile.to_candidate_profile(cfg)
            rules = profile.filters_for("recommended")
            planner = ApplicationPlanner(
                candidate=candidate,
                rules=rules,
                daily_limit=150,
                minimum_score=profile.min_rank_score,
            )
            plan = planner.create_plan(jobs)
            print(f"\n================ PROFILE: {prof_name} ================")
            print(f"Scraped: {len(jobs)} | Eligible: {len(plan.eligible_jobs)} | Selected: {len(plan.selected_jobs)} | Rejected: {len(plan.rejected_jobs)}")
            print("Selected Jobs:")
            for sj in plan.selected_jobs:
                print(f"  * [Score {sj.score:.1f}] {sj.job.title} @ {sj.job.company} (ID: {sj.job.job_id})")

    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
