import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Pinned to backend/.env rather than left to search upward from the current directory.
# A bare load_dotenv() resolves against the working directory, so starting the server from
# the repository root instead of backend/ found no .env at all and every setting silently
# took its default -- including ENABLE_LLM_SCORING, which turned model scoring off for the
# whole scan while the report still rendered, just entirely rules-based.
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "scans.db"
REGISTRY_PATH = Path(__file__).resolve().parent / "parameters" / "registry.json"

ENGINE_VERSION = "1.0.0"
CRAWLER_UA = "AIVisibilityAuditBot/1.0 (compatible; automated site audit tool)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# High ceilings, not typical-case limits: the crawler is meant to cover every page of the
# audited site. These only kick in as a safety net for pathological cases (e.g. a site with
# millions of auto-generated, faceted-search URLs) so a single scan can't run forever.
MAX_PAGES = 2500
MAX_SITEMAP_URLS = 5000
LINK_SAMPLE = 40
# Shared pool for every page fetch (httpx via crawler/http.py, scheduled by Crawlee). A
# real marketing site with a blog/case-studies/services subpages routinely queues ~1000
# pages, so this is the single biggest lever on crawl wall-clock time.
CONCURRENCY = 20
REQUEST_TIMEOUT = 30.0
RETRIES = 2
MAX_REDIRECTS = 8
MAX_BODY_BYTES = 2_000_000
# How many extra "keep following newly-discovered same-host links" rounds
# crawl_site() runs after the first pass (crawler/discover.py). Each round is a
# further batch of fetches, so this bounds that cost on a large site; a round
# that finds no new links still stops the loop early regardless of this ceiling.
MAX_DISCOVERY_ROUNDS = 3

# Parameter-evaluation concurrency/timeout (separate from crawl CONCURRENCY above).
# The timeout covers the rules-based handler only -- the model judgement that follows it
# has its own budget below, so a slow grader cannot eat the crawl's time and vice versa.
PARAMETER_CONCURRENCY = 6
PARAMETER_TIMEOUT = 60.0
# Ceiling on the model's judgement of one parameter (parameters/engine.py). Sized above
# LLM_TIMEOUT x (LLM_RETRIES + 1) so an ordinary retry sequence finishes rather than being
# cut off and falling back to the rules-based score for no reason.
JUDGEMENT_TIMEOUT = 75.0

# Model-based scoring. Off by default so no scan makes API calls or incurs cost until a
# real key is configured and this is explicitly enabled. With it off, every parameter falls
# back to its rules-based score, flagged as such in the report.
ENABLE_LLM_SCORING = os.getenv("ENABLE_LLM_SCORING", "false").strip().lower() in {"1", "true", "yes"}
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT = 20.0
LLM_RETRIES = 2
# Not the lever it looks like. run_all_parameters holds one PARAMETER_CONCURRENCY slot across
# both the handler and its judgement, so at most PARAMETER_CONCURRENCY judgements are ever in
# flight and this semaphore is never the binding constraint. It is kept at that same value so
# the two cannot silently disagree; raise PARAMETER_CONCURRENCY to speed the scoring phase up,
# bearing in mind that a measured 59-parameter run at 6 already drew rate-limit responses.
LLM_CONCURRENCY = PARAMETER_CONCURRENCY

WEIGHTS = {"technical": 0.35, "on_page": 0.40, "off_page": 0.25}

DATA_DIR.mkdir(parents=True, exist_ok=True)
