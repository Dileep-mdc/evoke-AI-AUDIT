"""Audit a site from a saved copy on disk instead of the live web.

A snapshot lives in a folder named after the host, under SNAPSHOT_ROOT:

    <SNAPSHOT_ROOT>/www.example.com/snapshot/manifest.json
    <SNAPSHOT_ROOT>/www.example.com/snapshot/raw/<sha1>.gz   (gzip, so a site stays tens of MB)

The manifest records every response exactly as the app's own fetch() received it -- status,
final URL, headers, redirect hops, timing -- and the body sits beside it, so a scan reading it
builds the same Pages, with the same page types, headings and structured data, as a live scan.

fetch() consults the snapshot first (see http.fetch). What is NOT served from it is anything
fetched with an explicit user-agent other than the app's own two: TECH-02 asks what GPTBot and
the other AI crawlers receive, which only the live site can answer. A URL the snapshot does not
hold falls through to the network as before.

Build or refresh a snapshot with backend/snapshot_site.py.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urldefrag, urlparse

from ..config import SNAPSHOT_ROOT, USE_SNAPSHOTS

MANIFEST = "manifest.json"


def folder_for(host: str) -> Path:
    return SNAPSHOT_ROOT / host.lower() / "snapshot"


def _keys(url: str) -> list[str]:
    """The spellings one URL is recorded and looked up under: with and without a trailing
    slash, since the crawl, the sitemap and in-page links do not agree on it."""
    url = urldefrag(url)[0]
    stripped = url.rstrip("/")
    return list(dict.fromkeys([url, stripped, stripped + "/"]))


class Snapshot:
    def __init__(self, folder: Path, manifest: dict):
        self.folder = folder
        self.host = manifest.get("host", "")
        self.created = manifest.get("created", "")
        self.pages: list[str] = manifest.get("pages", [])
        self._entries: dict[str, dict] = manifest.get("entries", {})
        self._index = {k: url for url in self._entries for k in _keys(url)}

    def entry(self, url: str) -> Optional[dict]:
        for key in _keys(url):
            hit = self._index.get(key)
            if hit:
                return self._entries[hit]
        return None

    def result(self, url: str):
        """The recorded response for `url` as a FetchResult, or None if it was never saved."""
        from .http import FetchResult

        e = self.entry(url)
        if e is None:
            return None
        content = b""
        if e.get("file"):
            path = self.folder / "raw" / e["file"]
            if path.exists():
                content = gzip.decompress(path.read_bytes())
        return FetchResult(
            url=url,
            final_url=e["final_url"],
            status_code=e["status_code"],
            headers=e.get("headers") or {},
            content=content,
            text=content.decode(e.get("encoding") or "utf-8", errors="replace"),
            content_type=e.get("content_type", ""),
            elapsed_ms=e.get("elapsed_ms", 0),
            error=e.get("error"),
            hops=e.get("hops", 0),
            bot_ua_refused=e.get("bot_ua_refused", False),
        )

    @property
    def has_renders(self) -> bool:
        return any(e.get("rendered_file") for e in self._entries.values())

    def rendered(self, url: str) -> Optional[str]:
        """The page's post-JavaScript HTML, saved by the builder, or None."""
        e = self.entry(url)
        if not e or not e.get("rendered_file"):
            return None
        path = self.folder / "raw" / e["rendered_file"]
        return gzip.decompress(path.read_bytes()).decode("utf-8", "replace") if path.exists() else None

    def summary(self) -> dict:
        return {"host": self.host, "folder": str(self.folder.parent), "created": self.created,
                "pages": len(self.pages), "responses": len(self._entries)}


_cache: dict[str, tuple[float, Optional[Snapshot]]] = {}
_lock = threading.Lock()
# The saved copy the CURRENT scan reads, when it was started from a folder path. A context
# variable, so it belongs to that scan's task alone: a concurrent live scan of the same site,
# or a later one typed as a URL, is not silently served from disk.
_active: ContextVar[Optional[Snapshot]] = ContextVar("active_snapshot", default=None)


def activate(snap: Optional[Snapshot]) -> None:
    """Make `snap` the copy this scan (the current asyncio task and what it spawns) reads."""
    _active.set(snap)


def open_folder(path: str) -> Snapshot:
    """Open the saved site in `path` -- the site folder or its snapshot/ subfolder.
    Raises ValueError with a readable reason."""
    folder = Path(path.strip().strip('"').strip("'")).expanduser()
    if not folder.is_dir():
        raise ValueError(f"Folder not found: {folder}")
    for candidate in (folder / "snapshot", folder):
        manifest = candidate / MANIFEST
        if manifest.is_file():
            try:
                snap = Snapshot(candidate, json.loads(manifest.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                raise ValueError(f"The saved copy in {candidate} could not be read: {exc}")
            if not snap.host or not snap.pages:
                raise ValueError(f"The saved copy in {candidate} holds no pages.")
            return snap
    raise ValueError(
        f"{folder} has no saved copy of a site (snapshot/manifest.json). Create one with: "
        f"python snapshot_site.py <site url>  (run from the backend folder)."
    )


def looks_like_folder(text: str) -> bool:
    """Whether dashboard input is a local folder path rather than a website address."""
    t = text.strip().strip('"').strip("'")
    if t.lower().startswith(("http:", "https:")):
        return False
    drive = len(t) >= 3 and t[0].isalpha() and t[1] == ":" and t[2] in ("\\", "/")
    return drive or t.startswith(("\\\\", "/", "~"))


def load(host_or_url: str) -> Optional[Snapshot]:
    """The saved copy to read for this host: the one the current scan was started from, else
    (only with USE_SNAPSHOTS=true) <SNAPSHOT_ROOT>/<host>/snapshot/, else None -- live.

    Cached per host and reloaded when the manifest file changes, so refreshing a snapshot
    takes effect on the next scan without restarting the server.
    """
    if not host_or_url:
        return None
    host = (urlparse(host_or_url).netloc or host_or_url).lower()
    active = _active.get()
    if active is not None:
        return active if active.host == host else None
    if not USE_SNAPSHOTS:
        return None
    manifest = folder_for(host) / MANIFEST
    try:
        mtime = manifest.stat().st_mtime
    except OSError:
        return None
    with _lock:
        cached = _cache.get(host)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            snap = Snapshot(manifest.parent, json.loads(manifest.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            snap = None
        _cache[host] = (mtime, snap)
        return snap


class Recorder:
    """Collects responses into a new snapshot folder. Used by snapshot_site.py."""

    def __init__(self, host: str):
        self.host = host.lower()
        self.folder = folder_for(self.host)
        (self.folder / "raw").mkdir(parents=True, exist_ok=True)
        self.entries: dict[str, dict] = {}
        self.pages: list[str] = []

    def add(self, requested_url: str, res, *, keep_body: bool = True) -> None:
        file = None
        if keep_body and res.content:
            file = hashlib.sha1(requested_url.encode()).hexdigest() + ".gz"
            (self.folder / "raw" / file).write_bytes(gzip.compress(res.content, compresslevel=6))
        self.entries[urldefrag(requested_url)[0]] = {
            "final_url": res.final_url, "status_code": res.status_code, "headers": res.headers,
            "content_type": res.content_type, "elapsed_ms": res.elapsed_ms, "hops": res.hops,
            "error": res.error, "bot_ua_refused": getattr(res, "bot_ua_refused", False),
            "file": file,
        }

    def add_rendered(self, requested_url: str, html: str) -> None:
        """The page as a browser shows it, beside the server's own response."""
        key = urldefrag(requested_url)[0]
        file = hashlib.sha1(requested_url.encode()).hexdigest() + ".rendered.gz"
        (self.folder / "raw" / file).write_bytes(gzip.compress(html.encode("utf-8"), compresslevel=6))
        self.entries[key]["rendered_file"] = file

    def save(self) -> Path:
        manifest = {
            "host": self.host,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pages": self.pages,
            "entries": self.entries,
        }
        path = self.folder / MANIFEST
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest), encoding="utf-8")
        tmp.replace(path)
        # Drop bodies left over from an earlier snapshot of the same host.
        live = {f for e in self.entries.values() for f in (e.get("file"), e.get("rendered_file")) if f}
        for f in (self.folder / "raw").iterdir():
            if f.name not in live:
                f.unlink()
        return path
