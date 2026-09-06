import asyncio
from naukri_agent.db.pool import get_pool

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT email, role_pitched, post_url, post_snippet, contacted_at
            FROM contacted_recruiters
            WHERE contacted_at > '2026-09-06 00:00:00'
            ORDER BY contacted_at ASC
        """)
        print(f"Total leads contacted today (2026-09-06): {len(rows)}\n")
        for i, r in enumerate(rows, 1):
            email = r['email']
            role = r['role_pitched']
            contacted = r['contacted_at']
            snippet = (r['post_snippet'] or '').strip().replace('\n', ' ')
            print(f"{i}. {email} | Role: {role}")
            print(f"   Time: {contacted}")
            print(f"   Post Snippet: {snippet[:280]}...\n")
    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
