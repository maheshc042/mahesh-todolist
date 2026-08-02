"""
Standalone script to log into Naukri and download the full rendered HTML of the Recommended Jobs page.
"""

import asyncio
from pathlib import Path
from naukri_agent.config import load_config, get_settings
from naukri_agent.browser.manager import BrowserManager
from naukri_agent.browser.artifacts import ArtifactStore
from naukri_agent.naukri.auth import NaukriAuth
from naukri_agent.db.pool import close_pool
from naukri_agent.db.migrations import run_migrations
from naukri_agent.db.repository import Repository


async def main() -> None:
    print("[1/5] Loading configuration and initializing database...")
    settings = get_settings()
    config = load_config()
    target = settings.validate_for_run("primary")
    await run_migrations()

    print("[2/5] Starting browser manager...")
    repo = await Repository.create()
    manager = BrowserManager(config.browser, repo=repo, session_key=target.session_key)
    await manager.start()
    page = await manager.new_page()

    try:
        print("[3/5] Authenticating / restoring session...")
        artifacts = ArtifactStore(settings.artifacts_dir)
        pwd = target.password.get_secret_value() if hasattr(target.password, "get_secret_value") else target.password
        auth = NaukriAuth(
            browser=manager,
            email=target.email,
            password=pwd,
            artifacts=artifacts,
        )
        page = await auth.ensure_logged_in(page)

        print("[4/5] Navigating to Recommended Jobs page...")
        await page.goto("https://www.naukri.com/mnjuser/recommendedjobs", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)  # Allow React feed cards to fully hydrate

        html_content = await page.content()
        target_path = Path.cwd() / "recommended_jobs_page.html"
        target_path.write_text(html_content, encoding="utf-8")

        print(f"[5/5] SUCCESS! Saved Recommended Jobs HTML to: {target_path}")
        print(f"File size: {len(html_content):,} bytes")
    finally:
        await manager.stop()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
