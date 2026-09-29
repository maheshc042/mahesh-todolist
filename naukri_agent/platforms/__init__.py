"""
Platforms package for multi-platform support (Naukri, Instahyre, Cutshort, Wellfound, HiringCafe).
"""
from .base import BaseJobPlatform
from .cutshort import CutshortPlatform
from .hiringcafe import HiringCafePlatform
from .instahyre import InstahyrePlatform
from .linkedin import LinkedInPlatform
from .naukri_platform import NaukriPlatform
from .wellfound import WellfoundPlatform

__all__ = [
    "BaseJobPlatform",
    "CutshortPlatform",
    "HiringCafePlatform",
    "InstahyrePlatform",
    "LinkedInPlatform",
    "NaukriPlatform",
    "WellfoundPlatform",
]


