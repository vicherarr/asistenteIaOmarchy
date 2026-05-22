"""Browser automation package - Sub-módulos para control_local_browser."""

from src.browser.launcher import ensure_browser, get_page
from src.browser.navigation import (
    browser_navigate,
    browser_click,
    browser_type,
    browser_read,
    browser_scroll,
)
from src.browser.clip import browser_clip
from src.browser.research import browser_research
from src.browser.translate import browser_translate

__all__ = [
    "ensure_browser",
    "get_page",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_read",
    "browser_scroll",
    "browser_clip",
    "browser_research",
    "browser_translate",
]
