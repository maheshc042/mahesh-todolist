import json

with open('scratch/gh_run_33730550784/logs/agent.log', encoding='utf-8', errors='replace') as f:
    for line in f:
        if any(term in line for term in ['chatbot.', 'question_review', 'unanswered', 'apply.']):
            try:
                d = json.loads(line)
                ev = d.get('event')
                jid = d.get('job_id')
                ts = d.get('timestamp', '')[:19]
                print(f"[{ts}] {ev} | Job: {jid} | { {k:v for k,v in d.items() if k not in ['event', 'job_id', 'timestamp', 'logger', 'level']} }")
            except:
                pass
