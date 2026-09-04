import asyncio
import json
import os
import asyncpg
from dotenv import load_dotenv
from urllib.parse import urlparse, urlunparse

load_dotenv()

async def detailed_analysis():
    db_url = os.getenv("DATABASE_URL")
    parsed = urlparse(db_url)
    clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    
    conn = await asyncpg.connect(clean_url, ssl=False)
    await conn.execute('SET search_path TO "naukri", public')
    
    # 1. All runs for today and yesterday
    print("==================================================")
    print("ALL RECENT RUNS (since 2026-09-02):")
    runs = await conn.fetch("""
        SELECT id, mode, status, account, profiles, started_at, finished_at, duration_s, error, stats
        FROM runs
        WHERE started_at >= '2026-09-02 00:00:00+00'
        ORDER BY started_at ASC
    """)
    for r in runs:
        print(f"\n--- Run ID: {r['id']} ---")
        print(f"  Account: {r['account']} | Profiles: {r['profiles']} | Status: {r['status']}")
        print(f"  Started: {r['started_at']} | Finished: {r['finished_at']} | Duration: {r['duration_s']}s")
        print(f"  Error: {r['error']}")
        st = r['stats']
        if st:
            if isinstance(st, str):
                st = json.loads(st)
            print(f"  Stats overview: Scraped={st.get('scraped')}, Considered={st.get('considered')}, Applied={st.get('applied')}, External={st.get('external')}, Failed={st.get('failed')}, AlreadyApplied={st.get('already_applied')}, NeedsReview={st.get('needs_review')}")
            if st.get('errors'):
                print(f"  Errors recorded ({len(st['errors'])}):")
                for err in st['errors']:
                    print(f"    - {err}")

    # 2. Detailed events for Run 211
    print("\n==================================================")
    print("RUN 211 EVENTS BREAKDOWN:")
    events = await conn.fetch("""
        SELECT level, event, job_id, profile, payload, created_at
        FROM run_events
        WHERE run_id = 211
        ORDER BY created_at ASC
    """)
    print(f"Total events in Run 211: {len(events)}")
    for ev in events:
        pl = ev['payload']
        if isinstance(pl, str):
            try: pl = json.loads(pl)
            except: pass
        print(f"[{ev['created_at'].strftime('%H:%M:%S')}] {ev['level'].upper()} | {ev['event']} | job: {ev['job_id']} | payload: {pl}")

    # 3. All applications in Run 211
    print("\n==================================================")
    print("APPLICATIONS IN RUN 211:")
    apps_211 = await conn.fetch("""
        SELECT a.id, a.job_id, a.account, a.profile, a.platform, a.status, a.reason, a.detail, 
               a.questions_answered, a.confirmation_type, a.confirmation_evidence, a.created_at,
               j.title, j.company, j.location, j.experience_text, j.salary_text, j.tags,
               j.min_experience, j.max_experience, j.url
        FROM applications a
        LEFT JOIN jobs j ON a.job_id = j.job_id AND a.platform = j.platform
        WHERE a.run_id = 211
        ORDER BY a.created_at ASC
    """)
    print(f"Applications count for Run 211: {len(apps_211)}")
    for a in apps_211:
        print(f"\n- JobID: {a['job_id']} | Status: {a['status']} | Time: {a['created_at'].strftime('%H:%M:%S')}")
        print(f"  Title: {a['title']} | Company: {a['company']} | Location: {a['location']}")
        print(f"  Exp: {a['experience_text']} ({a['min_experience']}-{a['max_experience']}) | Salary: {a['salary_text']}")
        print(f"  Tags: {a['tags']}")
        print(f"  Reason: {a['reason']} | Detail: {a['detail']}")
        print(f"  Conf: {a['confirmation_type']} | Evidence: {a['confirmation_evidence']} | Qs: {a['questions_answered']}")
        print(f"  URL: {a['url']}")

    # 4. Check all applications created today (2026-09-03) across ANY run or platform
    print("\n==================================================")
    print("ALL APPLICATIONS CREATED ON 2026-09-03:")
    apps_today = await conn.fetch("""
        SELECT a.id, a.run_id, a.job_id, a.account, a.profile, a.platform, a.status, a.reason, a.detail,
               j.title, j.company, a.created_at
        FROM applications a
        LEFT JOIN jobs j ON a.job_id = j.job_id AND a.platform = j.platform
        WHERE (a.created_at AT TIME ZONE 'Asia/Kolkata')::date = '2026-09-03'
        ORDER BY a.created_at ASC
    """)
    print(f"Total applications created on 2026-09-03: {len(apps_today)}")
    for a in apps_today:
        print(f"  Run {a['run_id']} | Plat: {a['platform']} | Acct: {a['account']} | Status: {a['status']} | Job: {a['title']} at {a['company']} ({a['job_id']})")

    # 5. Check platform states
    print("\n==================================================")
    print("PLATFORM STATES:")
    pstates = await conn.fetch("SELECT * FROM platform_state")
    for ps in pstates:
        print(f"  Account: {ps['account']} | Platform: {ps['platform']} | Paused: {ps['paused']} | PausedAt: {ps['paused_at']} | Reason: {ps['reason']}")

    await conn.close()

if __name__ == "__main__":
    asyncio.run(detailed_analysis())
