"""
LinkedIn Cold Email Campaign Entry Point.
"""
import asyncio
import sys

from naukri_agent.db.pool import close_pool
from naukri_agent.linkedin.campaign import run_campaign

if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    headed = "--headed" in sys.argv
    limit = 15
    for arg in sys.argv:
        if arg.startswith("--limit="):
            try:
                limit = int(arg.split("=")[1])
            except ValueError:
                pass
    try:
        asyncio.run(run_campaign(daily_email_limit=limit, headed=headed, dry_run=dry_run))
    finally:
        asyncio.run(close_pool())

