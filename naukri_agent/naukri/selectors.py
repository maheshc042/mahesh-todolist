"""
Central selector registry.

Design decision: every selector Naukri exposes lives here as an ORDERED LIST of
candidates, never inline in logic. Two consequences:

1. When Naukri ships a redesign (it happens every few months), the fix is a
   one-line addition at the top of one list — no logic changes, no redeploy of
   business rules.
2. Old selectors stay in the list, so the agent keeps working for users served
   the previous A/B variant.

Lists are ordered most-specific/most-stable first. `first_visible()` in
browser/resilience.py consumes them.
"""

from __future__ import annotations

BASE_URL = "https://www.naukri.com"
LOGIN_URL = f"{BASE_URL}/nlogin/login"
PROFILE_URL = f"{BASE_URL}/mnjuser/profile"
HOME_URL = f"{BASE_URL}/mnjuser/homepage"
RECOMMENDED_JOBS_URL = f"{BASE_URL}/mnjuser/recommendedjobs"
APPLIED_JOBS_URL = f"{BASE_URL}/mnjuser/myapply"

# --------------------------------------------------------------------- login
LOGIN_EMAIL_INPUT = [
    "input#usernameField",
    "input[placeholder*='Email' i]",
    "input[name='email']",
    "input[type='text'][id*='user' i]",
]

LOGIN_PASSWORD_INPUT = [
    "input#passwordField",
    "input[type='password']",
    "input[placeholder*='password' i]",
]

LOGIN_SUBMIT = [
    "button[type='submit'].loginButton",
    "button.loginButton",
    "button[type='submit']:has-text('Login')",
    "//button[contains(., 'Login')]",
]

LOGIN_ERROR = [
    "div.erLbl",
    "p.erLbl",
    "div[class*='error']:visible",
    "span.err",
]

# Presence of any of these means we are authenticated.
LOGGED_IN_MARKERS = [
    "div.nI-gNb-drawer__bars",
    "div.view-profile-wrapper",
    "a[href*='/mnjuser/profile']",
    "div.nI-gNb-info__sub-title",
    "div.user-name",
    "img.nI-gNb-icn-img",
]

# Presence of any of these means the session died mid-run.
LOGGED_OUT_MARKERS = [
    "a[title='Jobseeker Login']",
    "div#login_Layer",
    "a:has-text('Login')",
]

OTP_CHALLENGE = [
    "input[id*='otp' i]",
    "text=Verify with OTP",
    "text=Enter OTP",
    "div.otp-container",
]

CAPTCHA_MARKERS = [
    "iframe[src*='recaptcha']",
    "div.g-recaptcha",
    "text=unusual activity",
    "text=Are you a robot",
]

# ------------------------------------------------------------- search result
# Naukri has shipped several card layouts; support all of them.
JOB_CARD_CONTAINERS = [
    "div.srp-jobtuple-wrapper",
    "div.cust-job-tuple",
    "article.jobTuple",
    "div.jobTuple",
    "div.tuple",
    "div.tuple-wrapper",
    "div[data-job-id]",
]

# ------------------------------------------------- recommended jobs feed
# The recommended feed (/mnjuser/recommendedjobs) is a DIFFERENT React app from
# the search results page and does NOT render `div.srp-jobtuple-wrapper`. Reusing
# the search selectors here silently returned zero jobs.
RECO_PAGE_MARKERS = [
    "div.recommended-jobs",
    "div[class*='recommended']",
    "section[class*='recomm']",
    "div.list",
]

RECO_JOB_CARD_CONTAINERS = [
    "div.cust-job-tuple",
    "div.srp-jobtuple-wrapper",
    "div.tuple-wrapper",
    "article.jobTuple",
    "div.jobTuple",
    "div.recommended-jobs article",
    "div[data-job-id]",
    "div[class*='jobTupleWrapper']",
]

# Structural fallback: every job card, in every Naukri layout, contains an
# anchor pointing at a /job-listings-… detail URL. When the class-based
# selectors above all miss (a fresh redesign), we harvest these anchors and
# treat each anchor's nearest block ancestor as the card. This is the selector
# of last resort and it is why a Naukri redesign degrades instead of breaking.
RECO_JOB_LINK = [
    "a[href*='/job-listings-']",
    "a.title[href*='naukri.com']",
]

# The tab strip. Tabs are matched by their own text WITHIN this strip only —
# a bare `text=Profile` matched content inside job cards and navigated the
# browser away mid-scrape.
RECO_TAB_STRIP = [
    "div.tabs-wrapper",
    "ul.tabs",
    "div[role='tablist']",
    "div[class*='tab-list']",
    "div[class*='tabsWrapper']",
    "div[class*='nav-tabs']",
]
RECO_TAB_ITEM = [
    "[role='tab']",
    "li.tab",
    "a.tab",
    "div.tab",
    "li",
    "button",
]

RECO_SHOW_MORE = [
    "button:has-text('Show more jobs')",
    "a:has-text('Show more jobs')",
    "button:has-text('Show more')",
    "a:has-text('View all')",
    "div.loadMore",
]

RECO_EMPTY = [
    "text=No recommended jobs",
    "text=we could not find",
    "div[class*='noResult']",
    "div.empty-state",
]

CARD_TITLE = [
    "a[href*='job-listings']",
    "a[href*='job-details']",
    "a.title",
    "a.jobTitle",
    "a[class*='title']",
    "div[class*='title'] a",
    "h2 a",
    "h3 a",
    "a.row1",
]
CARD_COMPANY = [
    "a.comp-name",
    "a.subTitle",
    "span.comp-name",
    "a[class*='comp-name']",
    "div.companyInfo a",
]
CARD_EXPERIENCE = ["span.expwdth", "li.experience span", "span[class*='exp']", "li.exp"]
CARD_SALARY = ["span.sal-wrap span", "span.sal", "li.salary span", "span[class*='sal']"]
CARD_LOCATION = ["span.locWdth", "li.location span", "span[class*='loc']", "li.loc"]
CARD_POSTED = ["span.job-post-day", "span.date", "span[class*='post-day']", "span.jobPostDay"]
CARD_DESCRIPTION = ["span.job-desc", "div.job-description", "span[class*='job-desc']"]
CARD_TAGS = ["ul.tags-gt li", "ul.tags li", "div.tags span"]
CARD_RATING = ["a.rating span.main-2", "span.rating", "span[class*='rating']"]
CARD_ALREADY_APPLIED = [
    "span.applied-status",
    "span:has-text('Applied')",
    "div.already-applied",
]

PAGINATION_NEXT = [
    "a.styles_btn-secondary__2AsIP:has-text('Next')",
    "a:has-text('Next')",
    "a.fright.fs14.btn-secondary.br2",
    "div.styles_pagination__oIvXh a:last-child",
]

NO_RESULTS = [
    "div.styles_no-result__2Rz1a",
    "text=No jobs found",
    "div.noResultsFound",
]

# -------------------------------------------------------------- job detail
JD_APPLY_BUTTON = [
    "button#apply-button",
    "button:has-text('Apply')",
    "button.apply-button",
    "#apply-button",
]

JD_COMPANY_SITE_BUTTON = [
    "button#company-site-button",
    "button:has-text('Apply on company site')",
    "a:has-text('Apply on company site')",
    "#company-site-button",
]

JD_ALREADY_APPLIED = [
    "span#already-applied",
    "span:has-text('Applied')",
    "div:has-text('You have already applied')",
    ".already-applied-text",
]

JD_TITLE = ["h1.styles_jd-header-title__rZwM1", "h1[class*='jd-header-title']", "h1"]
JD_COMPANY = [
    "div.styles_jd-header-comp-name__MvqAI a",
    "div[class*='jd-header-comp-name'] a",
    "a[class*='comp-name']",
]
JD_DESCRIPTION = [
    "div.styles_JDC__dang-inner-html__h0K4t",
    "div[class*='dang-inner-html']",
    "section.job-desc",
    "div.dang-inner-html",
]
JD_EXPERIENCE = ["div.styles_jhc__exp__k_giM span", "div[class*='jhc__exp'] span"]
JD_SALARY = ["div.styles_jhc__salary__jdfEC span", "div[class*='jhc__salary'] span"]
JD_LOCATION = ["span.styles_jhc__location__W_pVs a", "span[class*='jhc__location']"]
JD_POSTED = ["span:has-text('Posted:') + span", "div.styles_jhc__stat__PgY67 span"]

# Success confirmation after a successful Easy Apply.
APPLY_SUCCESS = [
    "div.apply-message",
    "span:has-text('You have successfully applied')",
    "div:has-text('successfully applied')",
    "span#already-applied",
    "div.styles_apply-message__2Sd0v",
]

APPLY_ERROR_TOAST = [
    "div.apply-message-error",
    "div[class*='error-toast']",
    "div:has-text('Something went wrong')",
]

# ------------------------------------------------------- chatbot (questions)
CHATBOT_DRAWER = [
    "div._drawerContent",
    "div.chatbot_DrawerContentWrapper",
    "div#chatbot_DrawerContentWrapper",
    "div.chatbot_Drawer",
]
CHATBOT_QUESTION = [
    "div.botMsg span",
    "div.botItem span",
    "span.botMsg",
    "div._msg",
]
CHATBOT_TEXT_INPUT = [
    "div.textArea[contenteditable='true']",
    "div[contenteditable='true']",
    "textarea.textArea",
    "input.textInput",
]
CHATBOT_SEND = [
    "div.sendMsg",
    "div.send",
    "button.sendMsg",
    "svg.sendMsg",
]
CHATBOT_RADIO_OPTIONS = [
    "div.ssrc__radio-btn-container label",
    "div.singleselect-radiobutton-container label",
    "label.ssrc__label",
    "div.radioOption label",
]
CHATBOT_CHECKBOX_OPTIONS = [
    "div.multi-checkbox-container label",
    "div.msrc__checkbox-container label",
    "label.mcrc__label",
]
CHATBOT_CHIPS = ["div.chatbot_MessageContainer div.chip", "div.chipsContainer div.chip"]
CHATBOT_DROPDOWN = ["select.dropdownWrapper", "select[class*='dropdown']"]
CHATBOT_SAVE = [
    "div.botItem button:has-text('Save')",
    "button:has-text('Save')",
    "div.sendMsg:has-text('Save')",
]
CHATBOT_CLOSE = ["div.chatbot_Header span.crossIcon", "span.crossIcon", ".crossIcon"]
CHATBOT_COMPLETE = [
    "div:has-text('Your application has been')",
    "div:has-text('successfully applied')",
    "div.botMsg:has-text('Thank you')",
]

# -------------------------------------------------------- resume management
RESUME_UPLOAD_INPUT = ["input#attachCV", "input[type='file'][name='resume']", "input[type='file']"]
RESUME_UPDATE_TRIGGER = [
    "input#attachCV",
    "span:has-text('Update resume')",
    "div.updateResume input[type='file']",
]
RESUME_SUCCESS = [
    "p.error.success",
    "div:has-text('Resume has been successfully uploaded')",
    "span:has-text('successfully uploaded')",
]
RESUME_CURRENT_NAME = ["div.filename", "span.fileName", "div[class*='filename']"]

# ------------------------------------------------------- profile refresh
# Naukri ranks profiles in recruiter search by "profile last updated", so a
# daily touch is the single highest-leverage action on the platform. The resume
# HEADLINE is the safest field to touch: it is a plain <textarea> in a modal, it
# has an explicit Save button, and re-saving it moves the timestamp.
PROFILE_LAST_UPDATED = [
    "span:has-text('Profile last updated')",
    "div:has-text('Profile last updated')",
    "span[class*='lastUpdated']",
    "div[class*='last-updated']",
]

# The pencil / "Edit" affordance on the Resume headline card.
HEADLINE_SECTION = [
    "div.resumeHeadline",
    "div[class*='resumeHeadline']",
    "section:has-text('Resume headline')",
    "div:has-text('Resume headline')",
]
HEADLINE_EDIT_TRIGGER = [
    "div.resumeHeadline span.edit.icon",
    "div.resumeHeadline span[class*='edit']",
    "div[class*='resumeHeadline'] span[class*='edit']",
    "span#resumeHeadline .edit",
    "//div[contains(., 'Resume headline')]//span[contains(@class,'edit')]",
]
# Current headline text as rendered on the profile page (read-only view).
HEADLINE_TEXT = [
    "div.resumeHeadline span[class*='truncate']",
    "div.resumeHeadline p",
    "div[class*='resumeHeadline'] p",
    "span#resumeHeadlineTxt",
]
# The editable field inside the modal.
HEADLINE_TEXTAREA = [
    "textarea#resumeHeadlineTxt",
    "form[name='resumeHeadlineForm'] textarea",
    "div.modal textarea[name*='headline' i]",
    "textarea[placeholder*='headline' i]",
    "div[role='dialog'] textarea",
]
HEADLINE_SAVE = [
    "form[name='resumeHeadlineForm'] button[type='submit']",
    "div[role='dialog'] button:has-text('Save')",
    "div.modal button:has-text('Save')",
    "button.btn-dark-ot:has-text('Save')",
    "button:has-text('Save')",
]
# Naukri shows a green toast on success and a red one on validation errors.
PROFILE_SAVE_SUCCESS = [
    "p.error.success",
    "div:has-text('Resume headline has been successfully saved')",
    "span:has-text('successfully saved')",
    "div[class*='toast'][class*='success']",
    "div.success-toast",
]
PROFILE_SAVE_ERROR = [
    "div[role='dialog'] p.error:not(.success)",
    "div.modal p.error:not(.success)",
    "span.errorTxt",
]
PROFILE_MODAL = [
    "div[role='dialog']",
    "div.modal",
    "div.crossform",
]
PROFILE_MODAL_CLOSE = [
    "div[role='dialog'] span.crossIcon",
    "div.modal span.crossIcon",
    "div[role='dialog'] button[aria-label='Close']",
]

# Key-skills widget (optional `skills` refresh strategy).
KEY_SKILLS_EDIT_TRIGGER = [
    "div.keySkills span.edit.icon",
    "div[class*='keySkills'] span[class*='edit']",
    "//div[contains(., 'Key skills')]//span[contains(@class,'edit')]",
]
KEY_SKILLS_INPUT = [
    "input#keySkillSugg",
    "div[role='dialog'] input[placeholder*='skill' i]",
    "input[placeholder*='skill' i]",
]
