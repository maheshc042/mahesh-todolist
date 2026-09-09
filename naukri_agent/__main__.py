"""`python -m naukri_agent ...` — the container's entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

# Support running directly via `python naukri_agent ...` as well as `python -m naukri_agent ...`
if not __package__:
    pkg_root = Path(__file__).resolve().parent.parent
    if str(pkg_root) not in sys.path:
        sys.path.insert(0, str(pkg_root))
    from naukri_agent.cli import main
else:
    from .cli import main

if __name__ == "__main__":
    main()
