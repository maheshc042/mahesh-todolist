import asyncio
from naukri_agent.db.repository import Repository

async def main():
    repo = await Repository.create()
    await repo.pool.execute("DELETE FROM browser_sessions;")
    print("All sessions cleared from database!")

if __name__ == "__main__":
    asyncio.run(main())
