from __future__ import annotations

import re
from typing import Optional

_DNS_RE = re.compile(
    r"getaddrinfo|11001|Name or service not known|Failed to resolve|nodename nor servname|NameResolutionError",
    re.I,
)
_TIMEOUT_RE = re.compile(r"timed? ?out|TimeoutException|ReadTimeout|ConnectTimeout", re.I)
_REFUSED_RE = re.compile(r"Connection refused|WinError 10061", re.I)
_RESET_RE = re.compile(r"Connection reset|WinError 10054", re.I)
_SSL_RE = re.compile(r"SSL|certificate|CERTIFICATE_VERIFY_FAILED", re.I)
_UNREACH_RE = re.compile(r"Network is unreachable|No route to host|ConnectError", re.I)


def humanize_error(error: Optional[str]) -> Optional[str]:
    """Turn socket/httpx exceptions into a short auditor-facing sentence."""
    if not error:
        return None
    text = str(error).strip()
    if _DNS_RE.search(text):
        return "Could not reach this source. The hostname could not be resolved (DNS lookup failed)."
    if _TIMEOUT_RE.search(text):
        return "The request timed out before this source responded."
    if _REFUSED_RE.search(text):
        return "This source refused the connection."
    if _RESET_RE.search(text):
        return "The connection was closed before a response was received."
    if _SSL_RE.search(text):
        return "The site's security certificate could not be verified."
    if _UNREACH_RE.search(text):
        return "Could not connect to this source."
    if text.startswith("[Errno ") or "Traceback" in text:
        return "This source could not be reached. Retry the scan when the network is available."
    return text
