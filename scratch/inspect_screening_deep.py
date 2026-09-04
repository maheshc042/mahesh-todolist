import asyncio
import os
import asyncpg
from dotenv import load_dotenv
from urllib.parse import urlparse, urlunparse

load_dotenv()

async def inspect_screening():
    db_url = os.getenv('DATABASE_URL')
    parsed = urlparse(db_url)
    clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
    conn = await asyncpg.connect(clean_url, ssl=False)
    await conn.execute('SET search_path TO "naukri", public')
    
    # 1. Question review entries (unanswered or difficult questions)
    print("=== QUESTION REVIEW ENTRIES ===")
    q_reviews = await conn.fetch("SELECT * FROM question_review ORDER BY times_seen DESC, created_at DESC")
    for q in q_reviews:
        print(f"[{q['id']}] Q: '{q['question']}' | Prof: {q['profile']} | Resolved: {q['resolved']} | Ans: {q['answer']} | Times seen: {q['times_seen']}")

    # 2. Answer KB stats
    print("\n=== ANSWER KB ENTRIES ===")
    kb_rows = await conn.fetch("SELECT * FROM answer_kb ORDER BY hits DESC LIMIT 25")
    for r in kb_rows:
        print(f"[{r['id']}] Pattern: '{r['pattern']}' => Answer: '{r['answer']}' (Hits: {r['hits']}, Source: {r.get('source')})")

    # 3. Chatbot errors recorded across recent runs in runs.stats
    print("\n=== ERRORS ACROSS RECENT RUNS ===")
    runs = await conn.fetch("SELECT id, started_at, stats FROM runs WHERE started_at >= now() - interval '5 days' ORDER BY id DESC")
    for r in runs:
        st = r['stats']
        if st and 'errors' in st:
            chatbot_errs = [e for e in st['errors'] if 'chatbot' in e.lower() or 'answer' in e.lower() or 'question' in e.lower()]
            if chatbot_errs:
                print(f"Run {r['id']} ({r['started_at']}):")
                for err in chatbot_errs:
                    print(f"   - {err}")

    await conn.close()

if __name__ == '__main__':
    asyncio.run(inspect_screening())
