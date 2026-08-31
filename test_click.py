import asyncio
from naukri_agent.config import get_settings
from naukri_agent.db.pool import get_pool, close_pool
from naukri_agent.browser.manager import BrowserManager, BrowserConfig
from naukri_agent.db.repository import Repository

async def test():
    settings = get_settings()
    pool = await get_pool()
    repo = Repository(pool)
    config = BrowserConfig(headless=False, artifacts_dir=settings.artifacts_dir)
    bm = BrowserManager(config, repo)
    await bm.start(session_restored=True)
    page = bm.get_page()
    
    print("Navigating...")
    await page.goto("https://www.naukri.com/job-listings-310826012649")
    await page.wait_for_load_state("domcontentloaded")
    
    print("Capturing pre-click...")
    await page.screenshot(path="pre_click.png")
    
    print("Waiting 2 seconds...")
    await asyncio.sleep(2)
    
    print("Finding buttons...")
    btns = await page.locator("button#apply-button").all()
    print(f"Found {len(btns)} buttons")
    
    for i, btn in enumerate(btns):
        vis = await btn.is_visible()
        print(f"Button {i} visible: {vis}")
        if vis:
            print(f"Clicking button {i}...")
            await btn.scroll_into_view_if_needed()
            await btn.click()
            await asyncio.sleep(2)
            await page.screenshot(path=f"post_click_{i}.png")
            
    await bm.stop()
    await close_pool()

if __name__ == "__main__":
    asyncio.run(test())
