import glob
import os
import re

html_files = glob.glob("scratch/gh_run_33730550784/artifacts/2026-09-03/run-211/*.html")
print(f"Found {len(html_files)} HTML debug files:")

for hf in sorted(html_files):
    basename = os.path.basename(hf)
    with open(hf, encoding="utf-8", errors="replace") as f:
        content = f.read()
    
    # check title, chatbot, apply button, success messages
    title_match = re.search(r"<title>(.*?)</title>", content, re.IGNORECASE)
    title = title_match.group(1) if title_match else "No title"
    
    has_chatbot = "chatbot" in content.lower() or "bot-wrapper" in content.lower() or "chat-container" in content.lower()
    has_applied = "successfully applied" in content.lower() or "already applied" in content.lower() or "application sent" in content.lower()
    has_modal = "modal" in content.lower() or "drawer" in content.lower()
    has_external = "apply on company" in content.lower()
    
    # search for specific error/status indicators
    print(f"\n--- {basename} ---")
    print(f"  Title: {title}")
    print(f"  Size: {len(content)} chars | Chatbot: {has_chatbot} | SuccessText: {has_applied} | Modal: {has_modal} | External: {has_external}")
    
    # extract interesting snippets
    snippets = []
    for keyword in ["apply", "chatbot", "screening", "success", "error", "drawer"]:
        matches = [m.strip() for m in re.findall(rf".{{0,50}}{keyword}.{{0,50}}", content, re.IGNORECASE)]
        if matches:
            snippets.append(f"{keyword}: {matches[0][:80]}")
    if snippets:
        print("  Snippets:", " | ".join(snippets[:3]))
