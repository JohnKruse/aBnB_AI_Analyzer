"""Shared project branding for the app and offline reports."""

from base64 import b64encode
from pathlib import Path


GITHUB_URL = "https://github.com/JohnKruse/aBnB_AI_Analyzer"
LOGO_PATH = Path(__file__).resolve().parent.parent / "graphics" / "BnB_AI_Analyzer_logo_wide.png"


def github_logo_link() -> str:
    """Embed the local logo as a link to the project repository."""
    logo = b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    return (
        f'<a href="{GITHUB_URL}" target="_blank" rel="noopener noreferrer" '
        'aria-label="Airbnb AI Analyzer on GitHub (opens in a new tab)" '
        'title="View Airbnb AI Analyzer on GitHub" '
        'style="display:block;width:fit-content;max-width:100%;margin-bottom:8px">'
        f'<img src="data:image/png;base64,{logo}" alt="Airbnb AI Analyzer" '
        'width="300" height="100" style="display:block;max-width:100%;height:auto">'
        '</a>'
    )
