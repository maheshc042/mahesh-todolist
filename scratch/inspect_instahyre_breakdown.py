import asyncio
from naukri_agent.db.pool import get_pool
from naukri_agent.config import load_config
from naukri_agent.core.application_planner import ApplicationPlanner
from naukri_agent.core.models import Job

async def main():
    pool = await get_pool()
    cfg = load_config()
    profile = next(p for p in cfg.profiles if "Full Stack" in p.name)
    candidate = profile.to_candidate_profile(cfg)
    rules = profile.filters_for("recommended")

    planner = ApplicationPlanner(
        candidate=candidate,
        rules=rules,
        daily_limit=150,
        minimum_score=profile.min_rank_score,
    )

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT j.*, a.status, a.reason, a.run_id
            FROM jobs j
            LEFT JOIN applications a ON j.job_id = a.job_id AND a.run_id = 221
            WHERE j.job_id LIKE 'instahyre-%'
            ORDER BY j.first_seen_at DESC
            LIMIT 52
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

        print(f"Loaded {len(jobs)} Instahyre jobs.")
        plan = planner.create_plan(jobs)
        print(f"Eligible: {len(plan.eligible_jobs)}, Rejected: {len(plan.rejected_jobs)}")
        
        reasons = {}
        for rj in plan.rejected_jobs:
            reasons.setdefault(rj.reason, []).append((rj.job.title, rj.job.company, rj.job.tags))

        for reason, jlist in reasons.items():
            print(f"\nRejection Reason: {reason} ({len(jlist)} jobs)")
            for title, comp, tags in jlist[:10]:
                print(f"  - {title} @ {comp} | tags: {tags[:3]}")

        print("\nEligible Instahyre Jobs:")
        for ej in plan.eligible_jobs:
            print(f"  * [Score {ej.score:.1f}] {ej.job.title} @ {ej.job.company}")

    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
