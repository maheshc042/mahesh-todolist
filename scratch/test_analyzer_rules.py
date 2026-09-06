import re

EXPERIENCED_REJECT_REGEX = re.compile(
    r"\b("
    r"[4-9]\+|1[0-9]\+|"
    r"[4-9]\s*\+\s*(?:yrs|years?)|"
    r"1[0-9]\s*\+\s*(?:yrs|years?)|"
    r"(?:3\s*[-–—to]+\s*[5-9]|[4-9]\s*[-–—to]+\s*\d+)\s*(?:yrs|years?)|"
    r"experience\s*:\s*(?:[4-9]|1[0-9])\s*[-–—to+]"
    r"|\bsenior\b|\bsr\b|\bsr\.\b|\blead\b|\bprincipal\b|\bstaff\b"
    r"|(?:engineering|tech|technical|product|project|program|delivery|design)\s+manager|head\s+of"
    r")\b",
    re.IGNORECASE,
)

SPAM_OR_UNPAID_REJECT_REGEX = re.compile(
    r"("
    r"\bunpaid\b|\bno\s+stipend\b|\bwithout\s+stipend\b|"
    r"\b(?:intern|internship|interns|trainee|trainees)\b|"
    r"\bthe\s+entrepreneurship\s+network\b|\bten\b|"
    r"\bb\.?\s*com\b|\bbcom\b|"
    r"\b50\+\s*openings\b|\b100\+\s*openings\b|"
    r"dm\s+on\s+whatsapp|whatsapp\s+group"
    r")",
    re.IGNORECASE,
)

TECH_DISQUALIFY_REGEX = re.compile(
    r"\b("
    r"wordpress|wix|shopify|magento|drupal|"
    r"php|laravel|codeigniter|"
    r"\.net|dotnet|c#|asp\.net"
    r")\b",
    re.IGNORECASE,
)

posts = [
    # 1. MOHSofttech (Govinda Hedau) - AI/Python
    ("rahul.singh@mohsofttech.com", "We’re Hiring – AI/ML & Python Candidates! MOHSofttech Technology, Ameerpet, Hyderabad is looking for talented and motivated candidates to join our team. Open Positions: • AI/ML Developer • Python Developer"),
    # 2. SGS Consultancy (HR Srinivasan) - 5-7 years
    ("hr.sgs.consultancy@gmail.com", "39. Python AWS Developer Location: Remote Experience: 5–7 Years Rate: 1.35 LPM We are hiring Python AWS Developers"),
    # 3. Neha Raghuvanshi - 50+ openings ad
    ("durgeshverma.india23@gmail.com", "DEVELOPER HIRING 2026 | 50+ OPENINGS | REMOTE WORK Looking to start or grow your career in software development"),
    # 4. Hilt Web Solutions - WordPress/PHP
    ("hiltwebhr@gmail.com", "Hiring Full Stack Developer. Work on WordPress, Wix, Shopify, PHP, Laravel"),
    # 5. Rashmi Sehrawat - 3-5 years C# (Disqualified by exp 3-5 or C#)
    ("badwelmohammadirfan@gmail.com", "Hiring: Full Stack Developer – Bangalore Location: Bangalore, India Experience: 3–5 Years Skills Required: Python | C# | SQL | React"),
    # 6. Unitxt Pro - .NET Full Stack
    ("lokamatha.u@unitxt.net", "URGENT HIRING | DOT NET FULL STACK DEVELOPER | BANGALORE"),
    # 7. LoopStack Technologies - Tirth Rawal
    ("hr@loopstacktechnologies.com", "WE’RE HIRING | FULL STACK DEVELOPER At LoopStack Technologies, we’re growing our team and looking for a passionate Full Stack Developer who is ready to build with React and Node.js"),
    # 8. YMTS India - MERN Stack Freshers
    ("gayatri.hr@ymtsindia.org", "WE’RE HIRING | MERN STACK DEVELOPER – FRESHERS Are you a recent graduate passionate about Web Development?"),
    # 9. TEN - Unpaid internship
    ("missvaishnavisahu@gmail.com", "The Entrepreneurship Network is inviting applications for a 3-month unpaid remote internship across multiple domains"),
    # 10. TEN - Intern
    ("maryam3altaf@gmail.com", "HIRING INTERNS | The Entrepreneurship Network (TEN)* Are you a student or fresher looking to gain real work experience from home?"),
    # 11. Dcodetech - Intern
    ("dimpal.dcodetech@gmail.com", "We’re Hiring | Software Developer (MERN) Intern"),
    # 12. TEN - Unpaid
    ("khushbo1750@gmail.com", "We’re Hiring! TEN (The Entrepreneurs Network) is offering unpaid 3-month internships"),
    # 13. TQS Logistics - Shashank Shekhar
    ("shashank@tqslogistic.com", "We're Hiring: Front-End & Back-End Software Developers (ERP Projects) Are you passionate about building scalable and innovative ERP solutions using Node.js and React?"),
    # 14. Trao AI - Abhyanand Jha
    ("rgupta@trao.ai", "Trao AI is hiring AI & Python Developers building agentic LLM pipelines"),
]

for email, text in posts:
    exp_rej = bool(EXPERIENCED_REJECT_REGEX.search(text))
    spam_rej = bool(SPAM_OR_UNPAID_REJECT_REGEX.search(text))
    tech_rej = bool(TECH_DISQUALIFY_REGEX.search(text))
    
    status = "REJECTED" if (exp_rej or spam_rej or tech_rej) else "PASSED"
    reasons = []
    if exp_rej: reasons.append("Experience Mismatch")
    if spam_rej: reasons.append("Spam/Unpaid/Intern")
    if tech_rej: reasons.append("Disqualified Tech")
    
    print(f"[{status:8}] {email} -> {', '.join(reasons) if reasons else 'Clean Match'}")
