import asyncio
import os
import asyncpg
from dotenv import load_dotenv
from urllib.parse import urlparse, urlunparse

load_dotenv()

async def unpause():
    db_url = os.getenv('DATABASE_URL')
    parsed = urlparse(db_url)
    clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
    conn = await asyncpg.connect(clean_url, ssl=False)
    await conn.execute('SET search_path TO "naukri", public')
    res = await conn.execute("UPDATE platform_state SET paused = false, reason = NULL WHERE platform = 'instahyre'")
    print('Update result:', res)
    rows = await conn.fetch('SELECT * FROM platform_state')
    for r in rows:
        print(dict(r))
    await conn.close()

if __name__ == '__main__':
    asyncio.run(unpause())
