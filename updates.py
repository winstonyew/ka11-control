"""Check GitHub for a newer release. One small anonymous request to the public releases API."""
import json
import re
import urllib.request

from version import REPO, __version__

API = f"https://api.github.com/repos/{REPO}/releases/latest"


def parse(tag):
    """'v1.2.0' -> (1, 2, 0); unparseable tags sort lowest."""
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def is_newer(tag, current=__version__):
    return parse(tag) > parse(current)


def latest(timeout=5):
    """(tag, page_url) of the latest release, or None if it can't be reached."""
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"KA11-Control/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        return data.get("tag_name"), data.get("html_url")
    except (OSError, ValueError):
        return None


def check():
    """(tag, url) if a newer release exists, else None."""
    found = latest()
    if found and is_newer(found[0]):
        return found
    return None
