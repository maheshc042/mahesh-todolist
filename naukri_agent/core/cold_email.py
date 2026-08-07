"""
Autonomous Cold Email Agent Re-export.
Unifies all cold email sending through `ColdEmailer` in `mailer.py`.
"""

from .mailer import ColdEmailer

__all__ = ["ColdEmailer"]
