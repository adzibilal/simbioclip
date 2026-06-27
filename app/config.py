import os
import json
from dotenv import load_dotenv

from app.settings_store import get_settings

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")


def get_llm_routers():
    routers = get_settings().llm_routers
    return [r.model_dump() for r in routers]


def __getattr__(name):
    s = get_settings()
    mapping = {
        "API_TOKEN": s.api_token,
        "STT_MODE": s.stt_mode,
        "STT_BASE_URL": s.stt_base_url,
        "STT_API_KEY": s.stt_api_key,
        "STT_MODEL": s.stt_model,
        "STT_MODEL_FALLBACK": s.stt_model_fallback,
        "LLM_ROUTERS_RAW": json.dumps([r.model_dump() for r in s.llm_routers]),
        "LLM_TIMEOUT": s.llm_timeout,
        "LLM_MAX_RETRIES": s.llm_max_retries,
        "COOKIES_FILE": s.cookies_file,
        "COOKIES_FROM_BROWSER": s.cookies_from_browser,
        "CONCURRENT_FRAGMENTS": s.concurrent_fragments,
        "ARIA2C_ENABLED": s.aria2c_enabled,
        "ARIA2C_CONNECTIONS": s.aria2c_connections,
        "THROTTLED_RATE": s.throttled_rate,
    }
    if name in mapping:
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
