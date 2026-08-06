import asyncio
import yaml
from pathlib import Path
from naukri_agent.db.repository import Repository
from naukri_agent.db.pool import close_pool
from naukri_agent.cli import _bootstrap

async def sync_db_answers():
    settings, config = _bootstrap(None)
    repo = await Repository.create()

    try:
        # Fetch human-resolved questions from question_review
        rows = await repo.pool.fetch(
            """
            SELECT profile, question, answer
              FROM question_review
             WHERE resolved = TRUE AND answer IS NOT NULL
            """
        )

        kb_rows = await repo.pool.fetch(
            """
            SELECT profile, pattern, answer
              FROM answer_kb
             WHERE source = 'human' OR priority < 100
            """
        )

        config_path = Path("config/config.yaml")
        raw_yaml = config_path.read_text(encoding="utf-8")

        added_count = 0
        current_answers = set(config.answers.keys())

        new_entries = {}
        for row in rows:
            q = row["question"].strip().lower()
            ans = row["answer"]
            if q not in current_answers and q not in new_entries:
                new_entries[q] = ans

        for row in kb_rows:
            p = row["pattern"].strip().lower()
            ans = row["answer"]
            if p not in current_answers and p not in new_entries:
                new_entries[p] = ans

        print(f"📊 Found {len(new_entries)} human-resolved answers to sync into config.yaml.")
        for q, a in new_entries.items():
            print(f"   • '{q}' -> '{a}'")

        if new_entries:
            # Append new answers under global answers block
            lines = raw_yaml.splitlines()
            insert_idx = -1
            for idx, line in enumerate(lines):
                if line.strip().startswith("answers:"):
                    insert_idx = idx
                    break

            if insert_idx != -1:
                # Find end of global answers section or insert after line
                formatted_new = [f"  \"{q}\": \"{a}\"" for q, a in new_entries.items()]
                lines[insert_idx+1:insert_idx+1] = formatted_new
                config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                print(f"✅ Successfully appended {len(new_entries)} answers to config/config.yaml!")

        # Optional DB cleanup: clear resolved question_review rows
        deleted = await repo.pool.execute("DELETE FROM question_review WHERE resolved = TRUE;")
        print(f"🧹 Cleaned up resolved review rows in database: {deleted}")

    finally:
        await close_pool()

if __name__ == "__main__":
    asyncio.run(sync_db_answers())
