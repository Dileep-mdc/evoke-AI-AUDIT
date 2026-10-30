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


def _cloud_synced(path: Path) -> bool:
    """Whether this path sits inside a folder a desktop sync client is watching.

    Matched on the directory NAMES rather than by asking the OS, because that is what is
    portable and what is stable: OneDrive for Business roots are always "OneDrive - <Org>",
    and the others use a fixed folder name too.

    The match is the exact folder name, or that name followed by " - <Org>". Matching on a
    bare prefix instead looks equivalent and is not: it also catches an ordinary project
    directory called "onedrive-exporter" and silently relocates its data.
    """
    markers = ("onedrive", "dropbox", "google drive", "googledrive", "iclouddrive", "box sync")
    for part in path.parts:
        name = part.lower()
        if name in markers or any(name.startswith(m + " - ") for m in markers):
            return True
    return False


def _default_data_dir() -> Path:
    """Where scan data lives when AUDIT_DATA_DIR does not say.

    backend/data, unless the checkout is inside a cloud-synced folder -- in which case that
    is the one place this data must NOT go, for two independent reasons:

      * SQLite in WAL mode (see db.py) keeps scans.db, scans.db-wal and scans.db-shm mutually
        consistent. A sync client treats them as three unrelated files and will happily upload
        one without the others, lock one mid-write, or dehydrate it to a placeholder. That is
        a well-known route to a corrupted database, and it corrupts silently.
      * A scan rewrites its whole scraped-content file. Left under OneDrive that is tens to
        hundreds of megabytes re-uploaded per scan, forever.

    Falling back to the local app-data directory keeps the data on the machine that produced
    it. Set AUDIT_DATA_DIR to override this anywhere.
    """
    override = os.getenv("AUDIT_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local = BASE_DIR / "data"
    if not _cloud_synced(local):
        return local
    root = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / "ai-audit" / "data"


DATA_DIR = _default_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
# Kept so the startup migration (db.migrate_legacy_data_dir) can find data written by an
# earlier build, back when this was unconditionally backend/data.
LEGACY_DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "scans.db"
REGISTRY_PATH = Path(__file__).resolve().parent / "parameters" / "registry.json"

ENGINE_VERSION = "1.0.0"
CRAWLER_UA = os.getenv("CRAWLER_UA", "AIVisibilityAuditBot/1.0 (compatible; automated site audit tool)")
BROWSER_UA = os.getenv(
    "BROWSER_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)
# High ceilings, not typical-case limits: the crawler is meant to cover every page of the
# audited site. These only kick in as a safety net for pathological cases (e.g. a site with
# millions of auto-generated, faceted-search URLs) so a single scan can't run forever.
MAX_PAGES = int(os.getenv("MAX_PAGES", "2500"))
MAX_SITEMAP_URLS = int(os.getenv("MAX_SITEMAP_URLS", "5000"))
# How many page fetches are in flight at once (httpx via crawler/http.py, scheduled by
# crawler/fetch_many.py). A real marketing site with blog/case-study/service subpages
# routinely queues ~1000 pages, so this is the single biggest lever on crawl wall-clock time.
# Connections are pooled and kept alive, so this is a concurrency ceiling against one origin
# rather than a count of TCP handshakes.
CONCURRENCY = int(os.getenv("CONCURRENCY", "24"))
# Read budget for a response body. Separate from the connect budget below, because the two
# fail for different reasons: a slow page is worth waiting for, an unroutable host is not.
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "8"))
RETRIES = int(os.getenv("RETRIES", "2"))
MAX_REDIRECTS = int(os.getenv("MAX_REDIRECTS", "8"))
MAX_BODY_BYTES = int(os.getenv("MAX_BODY_BYTES", "2000000"))
# Base pause before retrying a transient HTTP failure; the Nth retry waits N times this.
RETRY_BACKOFF_SECONDS = float(os.getenv("RETRY_BACKOFF_SECONDS", "0.4"))
# How many extra "keep following newly-discovered same-host links" rounds
# crawl_site() runs after the first pass (crawler/discover.py). Each round is a
# further batch of fetches, so this bounds that cost on a large site; a round
# that finds no new links still stops the loop early regardless of this ceiling.
MAX_DISCOVERY_ROUNDS = int(os.getenv("MAX_DISCOVERY_ROUNDS", "3"))
# Wall-clock ceiling on the whole crawl, in seconds, and the answer to a scan that appears
# to hang: whatever the site does -- a link farm of faceted-search URLs, a host that accepts
# connections and never answers -- the crawl stops here and the audit scores the pages it
# has. Every URL that was queued but not reached still comes back carrying that as its
# reason, so the failures file says what was skipped and why rather than omitting it.
CRAWL_TIME_BUDGET = float(os.getenv("CRAWL_TIME_BUDGET", "900"))
# Threads used to turn fetched HTML into Page objects. Parsing used to run one page at a time
# on the event loop thread while nothing else could progress, which on a large site was minutes
# of work in the middle of the crawl with nothing reported. Kept deliberately small: building a
# BeautifulSoup tree is Python work under the GIL, so more threads than this measured SLOWER
# than one. The parallelism that pays is in EXTRACT_WORKERS below, which uses processes.
PARSE_WORKERS = int(os.getenv("PARSE_WORKERS", "3"))
# Worker PROCESSES for trafilatura body-text extraction (crawler/extract.py) -- the single most
# expensive step in reading a page, and pure-Python CPU work that threads cannot speed up.
# One less than the core count leaves the event loop a core to keep fetching on.
EXTRACT_WORKERS = int(os.getenv("EXTRACT_WORKERS", str(max(1, min(4, (os.cpu_count() or 2) - 1)))))
# Pages in a batch below which extraction stays in this process. A worker is a fresh
# interpreter (~1.5s on Windows), worth paying for once across a large crawl and never worth
# paying for a handful of pages.
EXTRACT_POOL_MIN_BATCH = int(os.getenv("EXTRACT_POOL_MIN_BATCH", "24"))

# JavaScript rendering: the optional second copy of a page (crawler/render.py).
#
# The crawl is hybrid by design. httpx fetches every page and owns the HTTP facts -- status,
# headers, redirect hops, raw bytes -- because only a real HTTP client knows them. A headless
# browser then re-fetches a SMALL SUBSET and owns the DOM, because on a client-rendered site
# the httpx copy is an empty shell and every on-page parameter reads zero content from it.
#
# Off unless Playwright is importable, and even then the browser only runs when a probe of the
# homepage shows rendering actually changes the reading. A scan never fails because rendering
# was unavailable; it falls back to the raw HTML, which is what every earlier build scored.
ENABLE_RENDER = os.getenv("ENABLE_RENDER", "true").strip().lower() in {"1", "true", "yes"}

# Saved copies of sites to audit from disk instead of the live web -- see crawler/snapshot.py.
# Typing a saved folder's path into the dashboard audits from it; a URL audits the live site.
# Build a folder with `python snapshot_site.py <url>` from backend/ (it lands in SNAPSHOT_ROOT).
# USE_SNAPSHOTS=true additionally makes a typed URL use <SNAPSHOT_ROOT>/<host>/ when present.
# Default: the project root (AI Audit/), next to backend/ and frontend/.
SNAPSHOT_ROOT = Path(os.getenv("SNAPSHOT_ROOT", str(Path(__file__).resolve().parent.parent.parent)))
USE_SNAPSHOTS = os.getenv("USE_SNAPSHOTS", "false").strip().lower() in {"1", "true", "yes"}
# Hard ceiling on pages sent to the browser per scan, and the cost stop for this whole feature.
# A rendered page costs roughly 1-3 seconds and ~50MB of Chromium against a few milliseconds
# for an httpx GET, so this is deliberately two orders of magnitude below MAX_PAGES: it buys
# the pages that need it, not the site.
RENDER_BUDGET = int(os.getenv("RENDER_BUDGET", "40"))
# Concurrent browser contexts. Far below CONCURRENCY because the constraint is RAM and CPU on
# this machine rather than politeness to the origin -- each context is a real Chromium tab.
RENDER_CONCURRENCY = int(os.getenv("RENDER_CONCURRENCY", "4"))
# Per-page navigation budget. Shorter than REQUEST_TIMEOUT: a page that has not reached
# DOMContentLoaded in this long is not going to produce a useful DOM, and the raw HTML is
# always there to fall back on.
RENDER_TIMEOUT = float(os.getenv("RENDER_TIMEOUT", "20"))
# How much more text the rendered copy must carry before a site is treated as client-rendered
# and worth spending the budget on. Expressed as a fraction of the raw word count, measured on
# the homepage probe. 0.25 means "the browser found at least 25% more words than the server
# sent". Below it, rendering is skipped entirely and the scan costs exactly what it used to.
RENDER_DELTA_THRESHOLD = float(os.getenv("RENDER_DELTA_THRESHOLD", "0.25"))
# Word count below which a raw page is treated as a possible JavaScript shell and queued for
# rendering ahead of fuller pages. A real content page under this many words is rare; a
# mounted-by-framework <div id="root"> is reliably beneath it.
RENDER_SHELL_WORDS = int(os.getenv("RENDER_SHELL_WORDS", "120"))
# After the DOM is parsed, how long to keep waiting for the network to go quiet (crawler/render.py).
# Expected to expire routinely on sites with analytics beacons or chat widgets; expiry is not an error.
RENDER_NETWORK_IDLE_MS = int(os.getenv("RENDER_NETWORK_IDLE_MS", "2500"))

# Parameter-evaluation concurrency/timeout (separate from crawl CONCURRENCY above).
# The timeout covers the rules-based handler only -- the model judgement that follows it
# has its own budget below, so a slow grader cannot eat the crawl's time and vice versa.
PARAMETER_CONCURRENCY = int(os.getenv("PARAMETER_CONCURRENCY", "6"))
# Sized for full-coverage classification (see LLM_FULL_COVERAGE): a handler that puts every
# heading or every list on a 750-page site in front of the model runs tens of batched calls
# inside this one budget. At the old 60s, ON-12 timed out on a mid-size site and the whole
# parameter came back UNKNOWN -- the check reported nothing rather than reporting late.
#
# 90s: a check's slow parts (AI batches, link and sitemap requests, web searches, the speed test)
# stop starting new work CHECK_WRAP_UP_SECONDS before it and score what finished (app/deadline.py),
# so a large site gets a score from an even sample instead of no score.
PARAMETER_TIMEOUT = float(os.getenv("PARAMETER_TIMEOUT", "90"))
# Left at the end of the time limit for a check to score and write up what it measured.
CHECK_WRAP_UP_SECONDS = float(os.getenv("CHECK_WRAP_UP_SECONDS", "10"))
# A scan retries the parameters that came back unscored once, after this many seconds -- long
# enough for a rate-limit window to reopen -- so no parameter needs re-running by hand.
SCAN_RETRY_DELAY = float(os.getenv("SCAN_RETRY_DELAY", "30"))
# Ceiling on the model's judgement of one parameter (parameters/engine.py). Sized above
# LLM_TIMEOUT x (LLM_RETRIES + 1) so an ordinary retry sequence finishes rather than being
# cut off and falling back to the rules-based score for no reason.
#
# Raised from 75s after a measured full-coverage run graded only 25 of 60 parameters: 24 of
# the 35 failures were this timeout. The call itself was not slow -- it was queued behind the
# classification fan-out. That contention is fixed properly by LLM_SCORING_CONCURRENCY below
# giving scoring its own lane; this is the headroom that stops a slow API turning a graded
# parameter into a silently rules-based one.
JUDGEMENT_TIMEOUT = float(os.getenv("JUDGEMENT_TIMEOUT", "240"))

# Model-based scoring. Off by default so no scan makes API calls or incurs cost until a
# real key is configured and this is explicitly enabled. With it off, every parameter falls
# back to its rules-based score, flagged as such in the report.
ENABLE_LLM_SCORING = os.getenv("ENABLE_LLM_SCORING", "false").strip().lower() in {"1", "true", "yes"}
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "20"))
LLM_RETRIES = int(os.getenv("LLM_RETRIES", "2"))
# Pause before retrying a failed model call; the Nth retry waits N times this.
LLM_RETRY_BACKOFF_SECONDS = float(os.getenv("LLM_RETRY_BACKOFF_SECONDS", "0.6"))
# Rate-limit (HTTP 429) refusals get their own, longer, exponential schedule (llm/client.py):
# this many retries, starting near the base and never sleeping longer than the max.
LLM_RATE_LIMIT_RETRIES = int(os.getenv("LLM_RATE_LIMIT_RETRIES", "5"))
LLM_RATE_LIMIT_BASE_SECONDS = float(os.getenv("LLM_RATE_LIMIT_BASE_SECONDS", "2"))
LLM_RATE_LIMIT_MAX_SLEEP = float(os.getenv("LLM_RATE_LIMIT_MAX_SLEEP", "30"))
# Now the binding constraint, and no longer tied to PARAMETER_CONCURRENCY. Under
# LLM_FULL_COVERAGE a single handler fans out into many batched classification calls at once,
# so the in-flight ceiling has to be higher than the number of parameters running. Rate-limit
# responses are absorbed by client.py's separate RATE_LIMIT_RETRIES budget with backoff.
LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "12"))
# Requests per minute the OpenAI project allows. 0 (the default) learns it from OpenAI's first
# rate-limit refusal, which names the limit; set it to pace from the very first call.
LLM_RPM_LIMIT = int(os.getenv("LLM_RPM_LIMIT", "0"))
# Below this many requests per minute, a check judges an evenly spread SAMPLE of its items --
# about half a minute of calls -- instead of every item. Full coverage of a 2,000-page site is ~550
# calls; at 10 per minute that is an hour, and the checks hit PARAMETER_TIMEOUT and came back
# unscored. The evidence states the sample ("judged 400 of 2,313") and the reason.
LLM_SAMPLE_BELOW_RPM = int(os.getenv("LLM_SAMPLE_BELOW_RPM", "120"))
# TECH-06 re-requests the internal links the crawl never fetched (beyond MAX_PAGES, or failed
# discovery), up to this many. A large site can have tens of thousands; the crawl already answered
# for every page it fetched, so the rest are sampled and the evidence says how many were left.
LINK_RECHECK_LIMIT = int(os.getenv("LINK_RECHECK_LIMIT", "300"))
# A separate budget for the one call per parameter that produces the reported score, so it
# never waits behind a handler's classification fan-out. Sized to PARAMETER_CONCURRENCY:
# at most that many parameters are in flight, so at most that many scoring calls exist, and
# the lane is therefore never itself a queue. Total in-flight requests are the sum of the two.
LLM_SCORING_CONCURRENCY = int(os.getenv("LLM_SCORING_CONCURRENCY", str(PARAMETER_CONCURRENCY)))

# Full-coverage classification.
#
# Each on-page handler used to put a capped sample in front of the model -- 24 pages, 40
# headings, 20 pairs -- and then score the whole site from it, without recording what the
# sample was drawn from. A 3,863-heading site was graded on 40 headings and the report said
# only "assessed: 40". With this on, handlers batch through EVERY candidate they found and
# the evidence always carries the true population next to the sample.
#
# Turning it off restores sampling, bounded by LLM_MAX_ITEMS, for a cheaper/faster scan.
LLM_FULL_COVERAGE = os.getenv("LLM_FULL_COVERAGE", "true").strip().lower() in {"1", "true", "yes"}
# How many items ride in one classification call. Large enough that a full pass is a
# reasonable number of requests, small enough that one reply stays inside the model's output
# budget and a single failure costs only this many items.
LLM_BATCH_SIZE = int(os.getenv("LLM_BATCH_SIZE", "40"))
# Character budget for one classification request, and the companion to the count above:
# a batch closes when it hits EITHER bound. That is what lets handlers send whole pages
# instead of excerpts without a fixed item count producing a request no model will accept.
# A limit on request SIZE, never on how much of the site is measured -- llm/batch.py keeps
# issuing requests until every candidate has been judged.
LLM_BATCH_CHARS = int(os.getenv("LLM_BATCH_CHARS", "120000"))
# Hard ceiling on items per parameter, applied whether or not full coverage is on. 0 means
# no ceiling. This is the cost stop: it exists so one pathological site cannot turn a scan
# into thousands of calls, and when it bites the evidence says so explicitly.
LLM_MAX_ITEMS = int(os.getenv("LLM_MAX_ITEMS", "0"))

# Third-party sources the off-page parameters query. Named here rather than in the handler
# so a provider swap, a regional mirror, or a proxy is a configuration change and not a code
# edit -- and so the full set of external services this tool talks to can be read in one place
# rather than grepped out of a 450-line module.
# Web search for the off-page checks: Google's Custom Search JSON API. It needs an API key and a
# Programmable Search Engine ID (set to search the entire web), both from Google Cloud. With
# either missing, every search-based check reports UNKNOWN with a message saying so -- it never
# falls back to scraping a results page, which is what made the old DuckDuckGo checks unreliable.
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID", "").strip()
GOOGLE_SEARCH_ENDPOINT = os.getenv("GOOGLE_SEARCH_ENDPOINT", "https://www.googleapis.com/customsearch/v1")
# Results requested per query; the API returns at most 10 per request.
GOOGLE_SEARCH_RESULTS = min(10, int(os.getenv("GOOGLE_SEARCH_RESULTS", "10")))
# Or OpenAI web search: an OpenAI Agents SDK agent with the hosted web-search tool, using
# OPENAI_API_KEY above. SEARCH_PROVIDER picks one: "google", "openai", or "auto" (the default),
# which uses Google when its key and engine ID are set and OpenAI otherwise.
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "auto").strip().lower()
OPENAI_SEARCH_MODEL = os.getenv("OPENAI_SEARCH_MODEL", LLM_MODEL)
# One query is a full agent run (the model searches, reads, then answers): seconds, not millis.
OPENAI_SEARCH_TIMEOUT = float(os.getenv("OPENAI_SEARCH_TIMEOUT", "90"))
# Searches at once, and retries of one refused by the rate limit (HTTP 429) after waiting what
# OpenAI asks, or 10, 20 then 40 seconds. Four at once tripped the limit and blanked OFF-07.
OPENAI_SEARCH_CONCURRENCY = int(os.getenv("OPENAI_SEARCH_CONCURRENCY", "2"))
OPENAI_SEARCH_RETRIES = int(os.getenv("OPENAI_SEARCH_RETRIES", "3"))
# Seconds to wait before each rate-limit retry when OpenAI does not say how long (comma-separated;
# the last value repeats), and the ceiling on any wait OpenAI does ask for.
OPENAI_SEARCH_RATE_LIMIT_WAITS = tuple(
    float(w) for w in os.getenv("OPENAI_SEARCH_RATE_LIMIT_WAITS", "10,20,40").split(",") if w.strip())
OPENAI_SEARCH_MAX_WAIT = float(os.getenv("OPENAI_SEARCH_MAX_WAIT", "60"))
WIKIDATA_API = os.getenv("WIKIDATA_API", "https://www.wikidata.org/w/api.php")
WIKIPEDIA_API = os.getenv("WIKIPEDIA_API", "https://en.wikipedia.org/w/api.php")
# Wikimedia's API policy asks clients to name themselves. Requests sent as a generic browser were
# throttled to HTTP 429 from the second search on, so a Wikidata retry by the company's short
# name came back "unavailable" -- and OFF-01/OFF-02 UNKNOWN -- for a company that is listed.
# AUDIT_CONTACT_URL is the operator's contact (a website or mailto:) the policy asks to be included.
AUDIT_CONTACT_URL = os.getenv("AUDIT_CONTACT_URL", "").strip()
WIKIMEDIA_USER_AGENT = os.getenv(
    "WIKIMEDIA_USER_AGENT",
    f"AIVisibilityAudit/1.0 ({AUDIT_CONTACT_URL + '; ' if AUDIT_CONTACT_URL else ''}website audit tool)")
# Language edition for the Wikipedia/Wikidata lookups. Was implicit in the "en." hostname and
# the hardcoded "language=en" query argument, so auditing a non-English company meant editing
# two unrelated string literals.
REFERENCE_LANGUAGE = os.getenv("REFERENCE_LANGUAGE", "en")
# How many candidate entities an entity-search returns. A retrieval width, not a score.
ENTITY_SEARCH_LIMIT = int(os.getenv("ENTITY_SEARCH_LIMIT", "5"))
# Off-page source verification: the Evidence & Validation Agent opens up to this many of each
# check's source pages to confirm they are about the company, each within this many seconds.
# Verification is recorded as evidence and never changes a score.
OFFPAGE_VERIFY_SOURCES = int(os.getenv("OFFPAGE_VERIFY_SOURCES", "3"))
OFFPAGE_VERIFY_TIMEOUT = float(os.getenv("OFFPAGE_VERIFY_TIMEOUT", "15"))
# Characters of a verified source page's text kept for the agents to read.
OFFPAGE_SOURCE_TEXT_CHARS = int(os.getenv("OFFPAGE_SOURCE_TEXT_CHARS", "20000"))
# The "view this search" link shown beside a search-based check; the query is appended URL-encoded.
SEARCH_DISPLAY_URL = os.getenv("SEARCH_DISPLAY_URL", "https://www.google.com/search?q=")

# AI crawlers whose access TECH-01/TECH-02 check in robots.txt and with a live request (comma-separated).
AI_BOTS = [b.strip() for b in os.getenv(
    "AI_BOTS", "GPTBot,ChatGPT-User,ClaudeBot,Google-Extended,Googlebot").split(",") if b.strip()]

# Core Web Vitals for TECH-07 (crawler/vitals.py). Field data first: Google's Chrome UX Report
# API, the real-user LCP/INP/CLS Google itself uses. It takes a Google Cloud API key with the
# Chrome UX Report API enabled; the Custom Search key above is reused when CRUX_API_KEY is unset.
CRUX_API_KEY = os.getenv("CRUX_API_KEY", "").strip() or GOOGLE_API_KEY
CRUX_ENDPOINT = os.getenv("CRUX_ENDPOINT", "https://chromeuxreport.googleapis.com/v1/records:queryRecord")
# When CrUX has no data for the site (common below a few thousand monthly Chrome visitors), a lab
# run in headless Chromium measures LCP, CLS and Total Blocking Time on this many key pages. It is
# a separate pass from the rendering one, because that pass blocks images, fonts and stylesheets --
# exactly what decides LCP and CLS -- to save time.
CWV_LAB_PAGES = int(os.getenv("CWV_LAB_PAGES", "3"))
CWV_LAB_TIMEOUT = float(os.getenv("CWV_LAB_TIMEOUT", "45"))
# How long to keep observing after the load event, so a late hero image or a late layout shift
# is still counted.
CWV_LAB_SETTLE_MS = int(os.getenv("CWV_LAB_SETTLE_MS", "3000"))
# Throttle the lab run the way Lighthouse's mobile preset does (4x slower CPU, slow-4G network),
# so a lab figure is comparable with PageSpeed Insights rather than flattered by this machine.
CWV_LAB_THROTTLE = os.getenv("CWV_LAB_THROTTLE", "true").strip().lower() in {"1", "true", "yes"}

# AI spend per scan (llm/usage.py). Prices are US dollars per million tokens for LLM_MODEL and
# default to gpt-4o-mini's list price; set them when you change the model. A web search run by the
# OpenAI search agent also carries a per-call tool fee, which is only counted when set below.
LLM_PRICE_INPUT_PER_M = float(os.getenv("LLM_PRICE_INPUT_PER_M", "0.15"))
LLM_PRICE_OUTPUT_PER_M = float(os.getenv("LLM_PRICE_OUTPUT_PER_M", "0.60"))
OPENAI_SEARCH_PRICE_PER_CALL = float(os.getenv("OPENAI_SEARCH_PRICE_PER_CALL", "0"))
# Spending cap for one scan's AI calls, in US dollars. 0 means no cap. Once a scan reaches it,
# further model calls are refused: classification stops where it is (each check's evidence says
# how much of the site was judged) and the remaining checks keep their rules-based scores.
LLM_SCAN_BUDGET_USD = float(os.getenv("LLM_SCAN_BUDGET_USD", "0"))

# Stored scan output (scan_output.py): gzip level for the saved pages, and threads writing them.
OUTPUT_GZIP_LEVEL = int(os.getenv("OUTPUT_GZIP_LEVEL", "6"))
OUTPUT_WRITE_WORKERS = int(os.getenv("OUTPUT_WRITE_WORKERS", "8"))

# The readable copy of every scanned site (site_folder.py): <SITE_FOLDERS_ROOT>/<host>/ with one
# .txt per page, _index.csv and the links workbook. Same root as the snapshots, so the folder a
# scan writes is the one snapshot_site.py reads. SAVE_SITE_FOLDER=false turns it off.
SITE_FOLDERS_ROOT = Path(os.getenv("SITE_FOLDERS_ROOT", str(SNAPSHOT_ROOT)))
# Every audit's crawl log PDF (crawl_log.py) is also copied here, beside the site folders, where it is
# easy to find; the original stays in data/scan_output/.
CRAWL_LOGS_DIR = Path(os.getenv("CRAWL_LOGS_DIR", str(SITE_FOLDERS_ROOT / "Crawl Logs")))
SAVE_SITE_FOLDER = os.getenv("SAVE_SITE_FOLDER", "true").strip().lower() in {"1", "true", "yes"}

# Section weights are scoring methodology, so they live with the other scoring rules in
# parameters/scoring_rules.json (overridable via SCORING_RULES_PATH).
from .parameters import rules as _rules  # noqa: E402  (needs the .env loaded above)

WEIGHTS = {k: v for k, v in _rules.all_rules()["thresholds"]["section_weights"].items() if not k.startswith("_")}

DATA_DIR.mkdir(parents=True, exist_ok=True)
