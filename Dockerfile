# ---------------------------------------------------------------------------
# Naukri auto-apply agent.
#
# Design decision: build on Microsoft's official Playwright Python image. It
# already ships Chromium plus the ~90 shared libraries Chromium needs on Debian,
# so we avoid a 200-line apt-get incantation that drifts with every release.
# The image tag MUST track the playwright version pinned in requirements.txt —
# a mismatch makes Playwright re-download browsers at runtime.
# ---------------------------------------------------------------------------
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Kolkata \
    # Browsers live in the base image, not in $HOME.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CONFIG_PATH=/app/config/config.yaml \
    ARTIFACTS_DIR=/app/artifacts \
    LOG_DIR=/app/logs \
    RESUME_DIR=/app/resumes

WORKDIR /app

# Dependencies first so code edits do not invalidate the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY naukri_agent ./naukri_agent
COPY config/config.example.yaml ./config/config.example.yaml

RUN mkdir -p /app/artifacts /app/logs /app/resumes /app/config

# The base image ships a non-root `pwuser`; run as it so a compromised page
# cannot touch the host mount as root.
RUN chown -R pwuser:pwuser /app
USER pwuser

# Chromium in a container needs a bigger /dev/shm; compose sets shm_size.
# A one-shot `run` exits with 0/1/2 so cron and Kubernetes can alert on it.
ENTRYPOINT ["python", "-m", "naukri_agent"]
CMD ["schedule"]
