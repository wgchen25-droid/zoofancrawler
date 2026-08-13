"""URL and text normalization used for deterministic article identity."""

from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


# Parameters known to be analytics/campaign noise.  Prefix matching handles
# all UTM variants while the explicit set covers common publisher trackers.
TRACKING_PARAMETERS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "vero_id",
        "ref_src",
        "yclid",
        "_ga",
        "_gl",
        "_hsenc",
        "_hsmi",
        "mkt_tok",
        "oly_anon_id",
        "oly_enc_id",
        "rb_clickid",
        "s_cid",
    }
)


def normalize_url(url: Optional[str], *, drop_tracking: bool = True) -> str:
    """Return a stable URL suitable for deduplication.

    Scheme and hostname are case-insensitive.  Fragments and conventional
    tracking query parameters are discarded, query pairs are sorted while
    preserving duplicate values, and non-root trailing slashes are removed.
    Relative URLs are retained as relative paths, which is useful for parser
    callers that have not supplied a page URL yet.
    """

    if not url:
        return ""
    raw = str(url).strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    netloc = parts.netloc
    if netloc:
        # urlsplit.hostname handles IPv6 brackets and IDNs for comparison, but
        # preserve explicit userinfo and non-default ports in the netloc.
        userinfo = ""
        host_port = netloc
        if "@" in host_port:
            userinfo, host_port = host_port.rsplit("@", 1)
            userinfo += "@"
        try:
            host = parts.hostname or ""
            host = host.lower().rstrip(".")
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = parts.port
            default_port = (scheme == "http" and port == 80) or (
                scheme == "https" and port == 443
            )
            if port is not None and not default_port:
                host += f":{port}"
            netloc = userinfo + host
        except ValueError:
            netloc = netloc.lower()
        if not userinfo:
            netloc = netloc.lower()

    path = parts.path or ("/" if netloc else "")
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    if drop_tracking:
        query_pairs = [
            (key, value)
            for key, value in query_pairs
            if key.lower() not in TRACKING_PARAMETERS
            and not key.lower().startswith("utm_")
        ]
    query_pairs.sort(key=lambda pair: (pair[0], pair[1]))
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize(url: Optional[str]) -> str:
    """Alias retained for small integrations."""

    return normalize_url(url)


def canonical_key(url: Optional[str]) -> str:
    """Return the same identity key used by storage for a URL."""

    return normalize_url(url)
