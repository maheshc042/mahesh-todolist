with open('logs/agent.log', 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

print(f"Total lines in agent.log: {len(lines)}")

# Find lines with run_id=221
r221 = [l for l in lines if 'run_id=221' in l]
print(f"Run 221 lines: {len(r221)}")

# Check event types in run 221
events = set()
for l in r221:
    parts = l.split()
    if len(parts) > 2:
        events.add(parts[2])

print(f"Unique events in Run 221: {len(events)}")
for e in sorted(events)[:25]:
    print("  ", e)

# Print Cutshort specific lines
print("\n--- CUTSHORT LINES IN RUN 221 ---")
for l in r221:
    if 'cutshort' in l.lower():
        print(l.strip()[:140])

# Print Instahyre specific lines
print("\n--- INSTAHYRE LINES IN RUN 221 ---")
for l in r221:
    if 'instahyre.apply' in l.lower() or 'instahyre.fetch' in l.lower():
        print(l.strip()[:140])

# Print LinkedIn specific lines
print("\n--- LINKEDIN EASY APPLY LINES IN RUN 221 ---")
for l in r221:
    if 'linkedin.apply' in l.lower() or 'linkedin.fetch' in l.lower():
        print(l.strip()[:140])
