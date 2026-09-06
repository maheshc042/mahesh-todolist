import json

with open('logs/agent.log', 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

r221 = []
for l in lines:
    l = l.strip()
    if not l:
        continue
    try:
        obj = json.loads(l)
        if obj.get('run_id') == 221:
            r221.append(obj)
    except Exception:
        pass

print(f"Parsed {len(r221)} log entries for run_id=221.")

events_summary = {}
for item in r221:
    evt = item.get('event', 'unknown')
    events_summary[evt] = events_summary.get(evt, 0) + 1

print("\nEvent counts:")
for k, v in sorted(events_summary.items(), key=lambda x: x[1], reverse=True)[:30]:
    print(f"  {k}: {v}")

print("\n--- CUTSHORT EVENTS ---")
for item in r221:
    if 'cutshort' in str(item).lower():
        print(f"[{item.get('event')}] {item}")

print("\n--- INSTAHYRE EVENTS (sample) ---")
for item in r221[:50]:
    if 'instahyre' in str(item).lower():
        print(f"[{item.get('event')}] {item}")

print("\n--- LINKEDIN EVENTS (sample) ---")
for item in r221[:50]:
    if 'linkedin' in str(item).lower() and 'email' not in str(item.get('event')):
        print(f"[{item.get('event')}] {item}")
