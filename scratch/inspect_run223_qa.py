import asyncio
from naukri_agent.db.pool import get_pool

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        events = await conn.fetch("""
            SELECT * FROM run_events
            WHERE run_id = 223 AND (event LIKE '%question%' OR event LIKE '%chatbot%' OR event LIKE '%answer%')
            ORDER BY id ASC
        """)
        print(f"Total question/chatbot events: {len(events)}")
        for ev in events:
            print(f"  [{ev['level']}] {ev['event']} -> data: {ev['data']}")

        qr = await conn.fetch("SELECT * FROM question_review ORDER BY created_at DESC LIMIT 5")
        print(f"\nRecent Question Reviews ({len(qr)}):")
        for r in qr:
            print(dict(r))

    await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
