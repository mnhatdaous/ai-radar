"""URL handling: a stable identity for dedupe, and clean links for display."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_EXACT = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ref_src",
        "ref_url",
        "mkt_tok",
        "s_cid",
        "ocid",
        "ncid",
        "guccounter",
        "ito",
        "smid",
        "sr_share",
        "at_medium",
        "at_campaign",
        "trk",
        "cmpid",
        "taid",
        "wt.mc_id",
    }
)
_TRACKING_PREFIXES = ("utm_", "guce_", "oly_", "vero_", "mc_", "pk_", "hsa_", "_hs", "ga_")

_ARXIV_PATH = re.compile(r"^/(?:abs|pdf|html)/(?P<id>.+?)(?:v\d+)?(?:\.pdf)?/?$")
_HF_PAPER_PATH = re.compile(r"^/papers/(?P<id>\d{4}\.\d{4,5})(?:v\d+)?/?$")
_REDDIT_HOSTS = frozenset({"old.reddit.com", "np.reddit.com", "new.reddit.com", "i.reddit.com"})

Query = list[tuple[str, str]]


def _is_tracking(key: str, value: str) -> bool:
    k = key.lower()
    if k in _TRACKING_EXACT or k.startswith(_TRACKING_PREFIXES):
        return True
    # Medium and friends append ?source=rss-... to feed links.
    return k == "source" and value.lower().startswith(("rss", "feed", "newsletter"))


def _split(url: str) -> tuple[str, str, str, Query]:
    """(scheme, host, path, query-without-tracking). Raises ValueError for non-http(s) URLs."""
    raw = url.strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"unsupported URL: {url!r}")
    query = [kv for kv in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(*kv)]
    return parts.scheme.lower(), parts.hostname.lower(), parts.path or "/", query


def _alias(host: str, path: str, query: Query) -> tuple[str, str, Query]:
    """Collapse equivalent addresses of the same resource (arXiv PDF vs abstract, ...)."""
    if host in {"arxiv.org", "export.arxiv.org"} and (m := _ARXIV_PATH.match(path)):
        return "arxiv.org", f"/abs/{m['id']}", []
    if host == "huggingface.co" and (m := _HF_PAPER_PATH.match(path)):
        return "arxiv.org", f"/abs/{m['id']}", []
    if host in {"twitter.com", "mobile.twitter.com"}:
        host = "x.com"
    elif host in _REDDIT_HOSTS:
        host = "reddit.com"
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"} and path == "/watch":
        video = dict(query).get("v")
        if video:
            return "youtube.com", "/watch", [("v", video)]
    elif host == "youtu.be" and len(path) > 1:
        return "youtube.com", "/watch", [("v", path[1:].split("/")[0])]
    elif host == "github.com":
        segments = [s for s in path.split("/") if s]
        if len(segments) >= 2:
            segments[0] = segments[0].lower()
            segments[1] = re.sub(r"\.git$", "", segments[1].lower())
            path = "/" + "/".join(segments)
    elif host == "news.ycombinator.com" and path == "/item":
        story = dict(query).get("id")
        query = [("id", story)] if story else []
    return host, path, query


def canonicalize_url(url: str) -> str:
    """Identity key for dedupe. Aggressive on purpose: not meant to be fetched."""
    _, host, path, query = _split(url)
    host = host.removeprefix("www.")
    host, path, query = _alias(host, path, query)
    path = re.sub(r"/{2,}", "/", path)
    path = re.sub(r"/index\.(?:html?|php)$", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def clean_url(url: str) -> str:
    """Link to show people: tracking parameters and fragment removed, nothing else changed."""
    scheme, host, path, query = _split(url)
    canonical = canonicalize_url(url)
    if canonical.startswith("https://arxiv.org/abs/"):
        return canonical  # always show the abstract page, not a PDF or versioned link
    parts = urlsplit(url.strip() if not url.strip().startswith("//") else "https:" + url.strip())
    return urlunsplit((scheme, parts.netloc, path, urlencode(query), "")) if host else canonical


def item_id(canonical_url: str) -> str:
    return hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()[:16]


def domain_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.lower().removeprefix("www.")
