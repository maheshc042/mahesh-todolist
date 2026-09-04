import asyncio
import json
import os
import sys
from datetime import datetime, timezone
import asyncpg
import requests

GITHUB_REPO = "maheshchichkoti/naukri-auto-apply-agent"

def check_github_actions():
    print("=== GITHUB ACTIONS RUNS ===")
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?per_page=10"
    headers = {"Accept": "application/vnd.github.v3+json"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"GitHub API returned {resp.status_code}: {resp.text[:200]}")
            return
        data = resp.json()
        total_count = data.get("total_count", 0)
        runs = data.get("workflow_runs", [])
        print(f"Total runs: {total_count}, showing latest {len(runs)}:")
        for r in runs:
            print(f"  ID: {r['id']} | Event: {r['event']} | Status: {r['status']} | Conclusion: {r['conclusion']} | Title: {r.get('display_title')} | Created: {r['created_at']} | Updated: {r['updated_at']}")
    except Exception as e:
        print(f"Error querying GitHub API: {e}")

async def check_database():
    print("\n=== LIVE DATABASE RUNS & STATS ===")
    # parse .env DATABASE_URL
    from dotenv import load_dotenv
    load_dotenv()
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not found in .env")
        return

    # asyncpg clean url
    from urllib.parse import urlparse, parse_qs, urlunparse
    parsed = urlparse(db_url)
    clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    
    conn = await asyncpg.connect(clean_url, ssl=False)
    try:
        await conn.execute('SET search_path TO "naukri", public')
        
        # Check latest runs
        runs = await conn.fetch("""
            SELECT id, mode, status, account, profiles, started_at, finished_at, duration_s, stats, error
            FROM runs
            ORDER BY started_at DESC
            LIMIT 10
        """)
        print(f"Found {len(runs)} latest runs:")
        for r in runs:
            print(f"  Run {r['id']} | Acct: {r['account']} | Profiles: {r['profiles']} | Status: {r['status']} | Started: {r['started_at']} | Finished: {r['finished_at']} | Dur: {r['duration_s']}s | Error: {r['error']}")
            if r['stats']:
                print(f"    Stats: {r['stats']}")

        # Check today's date in IST / UTC
        today_apps = await conn.fetch("""
            SELECT a.id, a.job_id, a.account, a.profile, a.platform, a.status, a.reason, a.detail, 
                   a.questions_answered, a.confirmation_type, a.created_at,
                   j.title, j.company, j.location, j.experience_text, j.salary_text, j.tags,
                   j.min_experience, j.max_experience
            FROM applications a
            LEFT JOIN jobs j ON a.job_id = j.job_id AND a.platform = j.platform
            WHERE a.created_at >= (now() - interval '2 days')
            ORDER BY a.created_at DESC
        """)
        print(f"\nApplications in last 48 hours: {len(today_apps)}")
        for app in today_apps[:25]:
            print(f"  [{app['created_at'].strftime('%Y-%m-%d %H:%M:%S')}] Acct: {app['account']} | Plat: {app['platform']} | Prof: {app['profile']} | Status: {app['status']}")
            print(f"    Job: {app['title']} at {app['company']} ({app['job_id']})")
            print(f"    Reason: {app['reason']} | Detail: {app['detail']}")

        # Group by date and platform
        daily_summary = await conn.fetch("""
            SELECT (created_at AT TIME ZONE 'Asia/Kolkata')::date AS day,
                   account, platform, profile, status, count(*) as count
            FROM applications
            WHERE created_at >= (now() - interval '3 days')
            GROUP BY 1, 2, 3, 4, 5
            ORDER BY 1 DESC, 2, 3, 4, 5
        """)
        print("\nDaily breakdown (last 3 days):")
        for row in daily_summary:
            print(f"  Day: {row['day']} | Acct: {row['account']} | Plat: {row['platform']} | Prof: {row['profile']} | Status: {row['status']} | Count: {row['count']}")

        # Check question review
        questions = await conn.fetch("""
            SELECT * FROM question_review
            ORDER BY created_at DESC
            LIMIT 10
        """)
        print(f"\nQuestion review table entries: {len(questions)}")
        for q in questions:
            print(f"  Q: {q['question']} | Prof: {q['profile']} | Res: {q['resolved']} | Ans: {q['answer']}")

        # Check platform_state
        states = await conn.fetch("SELECT * FROM platform_state")
        print(f"\nPlatform state entries: {len(states)}")
        for s in states:
            print(f"  Acct: {s['account']} | Plat: {s['platform']} | Paused: {s['paused']} | Reason: {s['reason']}")

    finally:
        await conn.close()

if __name__ == "__main__":
    check_github_actions()
    asyncio.run(check_database())
