"""Address selection: crawl the host over an address that can actually be reached.

The failure this exists for, measured on a real scan that appeared to hang:

    www.evoketechnologies.com resolves to four CloudFront edge addresses. From the auditing
    machine, TCP connects to all four in ~0.1s and the TLS handshake then completes on
    exactly ONE of them; the other three accept the connection and never finish the
    handshake. curl fetches the site in 5s because it moves on to the next address after a
    failed handshake. Python does not: anyio's happy-eyeballs covers the TCP connect only,
    so httpx establishes a socket to whichever address came back first and then sits in
    start_tls until the connect timeout expires -- and the next retry, and the next page,
    resolve to the same first address and hang the same way.

    With a 3-in-4 chance of picking a dead edge, a crawl of a thousand pages is a thousand
    timeouts. That is what "stuck at 16%" was.

A middlebox, an MTU black hole on some path, or a per-edge firewall rule can all produce it,
and none of them are ours to fix. What is ours is not to keep dialling a number that does not
answer: probe the addresses ONCE per host at the start of a crawl, keep the ones that complete
a handshake, and resolve that host to those for the rest of the process.

The hook is socket.getaddrinfo because that is the one funnel every client here goes through
(asyncio's loop.getaddrinfo runs it in an executor; anyio, httpcore and httpx all sit on top
of that). It delegates to the real resolver for every host, and only filters the answer for
hosts this module has measured and found partly unreachable -- so nothing else in the process,
including the OpenAI client, sees a different DNS answer than it would have.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from typing import Optional
from urllib.parse import urlparse

from ..config import CONNECT_TIMEOUT

log = logging.getLogger("crawler.resolver")

# host -> the addresses that completed a handshake. Only hosts where SOME address worked and
# some did not are ever recorded; a host that is wholly fine, or wholly unreachable, is left
# to the system resolver so this module can never be the reason a site cannot be crawled.
_preferred: dict[str, set[str]] = {}
_probed: set[str] = set()
_original_getaddrinfo = socket.getaddrinfo
_installed = False


def _host_key(host) -> str:
    """The lookup key for a hostname as the resolver may be handed it.

    Not cosmetic: anyio ASCII- or IDNA-encodes the hostname before it reaches
    socket.getaddrinfo, so the crawl's own lookups arrive as bytes while a direct caller's
    arrive as str. Keying on str(host) matched neither reliably, and a key that does not match
    turns this whole module into a no-op that reports success -- which is exactly how it
    failed the first time.
    """
    if isinstance(host, (bytes, bytearray)):
        try:
            host = bytes(host).decode("ascii")
        except Exception:
            return ""
    return str(host or "").strip().rstrip(".").lower()


def _patched_getaddrinfo(host, port, *args, **kwargs):
    infos = _original_getaddrinfo(host, port, *args, **kwargs)
    good = _preferred.get(_host_key(host))
    if not good:
        return infos
    kept = [info for info in infos if info[4][0] in good]
    # Never return an empty list: an answer this module cannot vouch for is still better than
    # telling the caller the host does not resolve.
    return kept or infos


def _install() -> None:
    global _installed
    if not _installed:
        socket.getaddrinfo = _patched_getaddrinfo
        _installed = True


def restore() -> None:
    """Undo the hook and forget every measurement. For tests and for a clean shutdown."""
    global _installed
    if _installed:
        socket.getaddrinfo = _original_getaddrinfo
        _installed = False
    _preferred.clear()
    _probed.clear()


async def _handshake_ok(address: str, host: str, port: int, timeout: float) -> bool:
    """Whether a usable connection to this one address can be established.

    For HTTPS that means a completed TLS handshake, not a completed TCP connect -- the whole
    point of the exercise is that the two disagree. Certificate verification is left ON: an
    address serving a certificate for a different name is not this site, and crawling it would
    silently audit someone else's content.
    """
    writer = None
    try:
        if port == 443:
            context = ssl.create_default_context()
            opened = asyncio.open_connection(address, port, ssl=context, server_hostname=host)
        else:
            opened = asyncio.open_connection(address, port)
        _, writer = await asyncio.wait_for(opened, timeout)
        return True
    except Exception:
        return False
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


async def prefer_reachable_addresses(url: str, timeout: Optional[float] = None) -> dict:
    """Probe this URL's host once and pin the crawl to the addresses that answer.

    Returns a small summary for logging. Cheap and bounded: the addresses are probed
    concurrently, so the whole check costs one handshake's latency, and a host is only ever
    probed once per process. A host with a single address is skipped -- there is nothing to
    choose between -- and so is any failure of the check itself, which leaves resolution
    exactly as the system would have done it.
    """
    parsed = urlparse(url)
    host = _host_key(parsed.hostname)
    summary = {"host": host, "checked": False, "total": 0, "reachable": 0}
    if not host or host in _probed:
        return summary
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    budget = timeout if timeout is not None else CONNECT_TIMEOUT
    _probed.add(host)

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, proto=socket.IPPROTO_TCP
        )
    except Exception:
        return summary
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    summary["total"] = len(addresses)
    if len(addresses) < 2:
        return summary

    summary["checked"] = True
    outcomes = await asyncio.gather(
        *(_handshake_ok(a, host, port, budget) for a in addresses)
    )
    reachable = {a for a, ok in zip(addresses, outcomes) if ok}
    summary["reachable"] = len(reachable)
    if reachable and len(reachable) < len(addresses):
        _preferred[host] = reachable
        _install()
        log.info(
            "%s: %d of %d addresses complete a handshake; crawling over %s",
            host, len(reachable), len(addresses), ", ".join(sorted(reachable)),
        )
    return summary
