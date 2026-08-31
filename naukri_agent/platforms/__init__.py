"""
Platforms package for multi-platform support (Naukri, Instahyre, Cutshort, Wellfound).
"""
from .base import BaseJobPlatform
from .cutshort import CutshortPlatform
from .instahyre import InstahyrePlatform
from .naukri_platform import NaukriPlatform
from .wellfound import WellfoundPlatform

__all__ = [
    "BaseJobPlatform",
    "NaukriPlatform",
    "InstahyrePlatform",
    "CutshortPlatform",
    "WellfoundPlatform",
]


