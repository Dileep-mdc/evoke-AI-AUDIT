import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "scans.db"
REGISTRY_PATH = Path(__file__).resolve().parent / "parameters" / "registry.json"

DEFAULT_URL = "https://www.evoketechnologies.com/"
ENGINE_VERSION = "1.0.0"
CRAWLER_UA = "EvokeAIVisibilityAudit/1.0 (+https://www.evoketechnologies.com; audit-bot)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
AI_CRAWLER_UAS = {
    "GPTBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.2",
    "ChatGPT-User": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0",
    "ClaudeBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ClaudeBot/1.0",
    "Google-Extended": "Mozilla/5.0 (compatible; Google-Extended/1.0; +https://developers.google.com/search)",
    "PerplexityBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; PerplexityBot/1.0",
}

MAX_PAGES = 28
MAX_SITEMAP_URLS = 250
LINK_SAMPLE = 40
CONCURRENCY = 6
REQUEST_TIMEOUT = 12.0
RETRIES = 2
MAX_REDIRECTS = 8
MAX_BODY_BYTES = 2_000_000

# Parameter-evaluation concurrency/timeout (separate from crawl CONCURRENCY above).
PARAMETER_CONCURRENCY = 6
PARAMETER_TIMEOUT = 25.0

# LLM-assisted scoring. Off by default so no scan makes API calls or incurs cost
# until a real key is configured and this is explicitly enabled.
ENABLE_LLM_SCORING = os.getenv("ENABLE_LLM_SCORING", "false").strip().lower() in {"1", "true", "yes"}
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT = 20.0
LLM_RETRIES = 2
LLM_CONCURRENCY = 4

WEIGHTS = {"technical": 0.35, "on_page": 0.40, "off_page": 0.25}

DATA_DIR.mkdir(parents=True, exist_ok=True)
