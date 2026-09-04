import asyncio
import json
import os
import asyncpg
from dotenv import load_dotenv
from urllib.parse import urlparse, urlunparse

load_dotenv()

async def get_all_45():
    db_url = os.getenv("DATABASE_URL")
    parsed = urlparse(db_url)
    clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    
    conn = await asyncpg.connect(clean_url, ssl=False)
    await conn.execute('SET search_path TO "naukri", public')
    
    rows = await conn.fetch("""
        SELECT a.id as app_id, a.job_id, a.account, a.profile, a.platform, a.status, a.reason, a.detail, 
               a.questions_answered, a.confirmation_type, a.confirmation_evidence, a.created_at,
               j.title, j.company, j.location, j.experience_text, j.salary_text, j.tags,
               j.min_experience, j.max_experience, j.url, j.posted_text, j.posted_days_ago,
               ms.keyskills_score, ms.experience_match, ms.overall_score
        FROM applications a
        LEFT JOIN jobs j ON a.job_id = j.job_id AND a.platform = j.platform
        LEFT JOIN match_scores ms ON a.job_id = ms.job_id AND a.platform = ms.platform
        WHERE a.run_id = 211
        ORDER BY a.created_at ASC
    """)
    
    print(f"Total applications in DB for Run 211: {len(rows)}")
    
    jobs_list = []
    for r in rows:
        jobs_list.append(dict(r))
        
    with open("scratch/run_211_applications_full.json", "w", encoding="utf-8") as f:
        json.dump(jobs_list, f, indent=2, default=str)
        
    print("Saved to scratch/run_211_applications_full.json")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(get_all_45())
