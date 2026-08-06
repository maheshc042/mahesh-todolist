import asyncio
from naukri_agent.db.pool import close_pool
from naukri_agent.db.repository import Repository
from naukri_agent.cli import _bootstrap

async def main():
    _, config = _bootstrap(None)
    repo = await Repository.create()
    try:
        count = await repo.seed_answer_kb(config.answers)
        print(f"✅ Successfully seeded {count} global answer patterns into DB answer_kb.")
    finally:
        await close_pool()

if __name__ == "__main__":
    asyncio.run(main())
