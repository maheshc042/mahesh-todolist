import asyncio
import json
from naukri_agent.db.pool import get_pool

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                a.id as app_id,
                a.job_id,
                a.status,
                a.reason,
                a.detail,
                a.questions_answered,
                a.submitted_at,
                j.title,
                j.company,
                j.location,
                j.min_experience,
                j.max_experience,
                j.experience_text,
                j.salary_text,
                j.url,
                j.tags,
                j.source_keyword
            FROM applications a
            LEFT JOIN jobs j ON a.job_id = j.job_id
            WHERE a.run_id = 223 AND a.platform = 'naukri' AND a.status = 'applied'
            ORDER BY a.submitted_at ASC NULLS LAST, a.id ASC
        """)
        print(f"Total applied Naukri rows: {len(rows)}")
        results = []
        for idx, r in enumerate(rows, 1):
            results.append({
                "num": idx,
                "job_id": r['job_id'],
                "title": r['title'],
                "company": r['company'],
                "location": r['location'],
                "experience": r['experience_text'] or f"{r['min_experience']}-{r['max_experience']} Yrs",
                "salary": r['salary_text'] or "Not Disclosed",
                "url": r['url'],
                "questions_answered": r['questions_answered'],
                "detail": r['detail'],
                "tags": r['tags'],
                "source_keyword": r['source_keyword'],
                "submitted_at": str(r['submitted_at'])
            })
            
        with open('scratch/naukri_run223_applied_details.json', 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2)
            
        for r in results:
            qs_info = f" | Qs answered: {r['questions_answered']}" if r['questions_answered'] else ""
            print(f"#{r['num']:02d} | {r['title']} @ {r['company']}{qs_info}")
            print(f"     Exp: {r['experience']} | Loc: {r['location']} | Sal: {r['salary']}")
            print(f"     URL: {r['url']}")
            print(f"     Tags: {r['tags']}")
            print()

    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
