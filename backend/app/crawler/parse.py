"""One place that decides how HTML is parsed.

Every BeautifulSoup tree in the audit comes from make_soup(), so the parser is a single
decision rather than a string repeated at each call site.

lxml is preferred over the stdlib html.parser for two reasons, in this order:

1. Correctness. html.parser gives up on real-world malformed markup in ways that silently
   lose content -- unclosed <p>, stray </div>, tags inside <table> -- and content it drops
   is content the audit then reports as missing. lxml recovers the way a browser does, so
   the tree matches what a reader (and an AI crawler) actually sees.
2. Speed. It is a C extension rather than pure Python, several times faster on the large
   documents this crawls. At the 2500-page ceiling, parsing is a real share of scan
   wall-clock time.

The fallback is not decoration: lxml ships as a compiled wheel, and if one is unavailable
for the running interpreter a scan on html.parser is far better than an import crash.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

try:  # pragma: no cover - depends on whether a compiled wheel is installed
    import lxml  # noqa: F401  (imported for the availability check only)

    HTML_PARSER = "lxml"
except ImportError:  # pragma: no cover
    HTML_PARSER = "html.parser"


def make_soup(html: str | None) -> BeautifulSoup:
    """Parse an HTML document with the best parser available."""
    return BeautifulSoup(html or "", HTML_PARSER)
