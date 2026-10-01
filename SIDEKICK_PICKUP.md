# Sidekick Pickup Contract (v1)

The agent **only enqueues**. Sidekick drains `naukri.external_dispatch_queue`
whenever it runs. No POSTs from the agent, no shared schedule, no choreography.

## Table: `naukri.external_dispatch_queue`

| column | type | meaning |
|---|---|---|
| `id` | bigint PK | claim key |
| `job_id` | text UNIQUE | agent-side stable id (`hc-<sha1>`, `reco-<id>`, …). Insert is idempotent: re-enqueues are `DO NOTHING`. |
| `url` | text | **resolved employer ATS link only** — never a job-board listing page. The agent filters raw listing URLs before enqueue. |
| `company` / `title` | text | employer + posting title |
| `platform` | text | `naukri` \| `linkedin` \| `instahyre` \| `wellfound` \| `hiringcafe` \| `cutshort` \| `manual` |
| `profile` / `account` | text | which job family + login produced it |
| `source_metadata` | jsonb | `{posted_days_ago, location, source_keyword}` (keys may be absent/null) |
| `status` | text | `pending` → `dispatching` → `dispatched` \| `retry` \| `dead` |
| `attempts` | int | deliveries attempted |
| `last_error` | text | last failure, truncated to 500 chars |
| `next_retry_at` | timestamptz | null unless `retry` |
| `created_at` / `dispatched_at` | timestamptz | — |

## Claim (copy verbatim — `SKIP LOCKED` makes parallel readers safe)

```sql
BEGIN;
SELECT id, job_id, url, company, title, platform, profile,
       account, source_metadata, attempts
  FROM naukri.external_dispatch_queue
 WHERE status IN ('pending', 'retry')
   AND (next_retry_at IS NULL OR next_retry_at <= now())
 ORDER BY created_at ASC
 LIMIT 25
 FOR UPDATE SKIP LOCKED;
-- then:
UPDATE naukri.external_dispatch_queue
   SET status = 'dispatching' WHERE id = ANY($1);
COMMIT;
```

On success: `status='dispatched', dispatched_at=now(), last_error=NULL`.
On transient failure: `status='retry', attempts=attempts+1, last_error=$e, next_retry_at=now()+backoff`
with backoff ladder **30s → 5m → 30m**, then `status='dead'`.
On HTTP-422-style validation failure: straight to `dead` (retry can never succeed).

## Idempotency key (must replicate EXACTLY)

`key = sha1(normalize(url))` where normalize lowercases, strips the path's
trailing `/`, drops every query param except `gh_jid, job_id, id, p, job`,
and lowercases the whole result:

```python
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import hashlib

def normalize_dispatch_url(url: str) -> str:
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/") or "/"
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() in ("gh_jid", "job_id", "id", "p", "job")]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(),
                       path, "", urlencode(kept), "")).lower()

key = hashlib.sha1(normalize_dispatch_url(url).encode()).hexdigest()
```

## Rules for the reader

1. **Resolved URLs only, guaranteed.** The agent never enqueues `naukri.com`,
   `linkedin.com`, `instahyre.com`, `cutshort.io`, `wellfound.com` listing
   links. If one ever appears, treat it as an agent bug: dead-letter it and
   tell us.
2. **Dedupe on `job_id` first, idempotency key second.** Either is stable
   across re-enqueues.
3. **Claim-then-mark.** Never process a row left in `dispatching` by a dead
   reader without reaping it (stale `dispatching` older than ~1h may be
   reset to `pending`).
4. The agent's `flush-external` CLI can still push rows over HTTP if ever
   needed — the two paths share the idempotency key, so redelivery is safe.
