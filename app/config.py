import os
import json
from dotenv import load_dotenv

# Load .env file if it exists (useful for local development)
load_dotenv()

# App paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# Configurations
API_TOKEN = os.getenv("API_TOKEN", "super-secret-admin-token")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# STT Config
STT_MODE = os.getenv("STT_MODE", "local").lower()  # 'router' or 'local'
STT_BASE_URL = os.getenv("STT_BASE_URL", "")
STT_API_KEY = os.getenv("STT_API_KEY", "")

# Whisper model used in router mode, with an automatic fallback if the primary
# model is unavailable / errors out. Defaults target Groq's lineup.
STT_MODEL = os.getenv("STT_MODEL", "whisper-large-v3-turbo")
STT_MODEL_FALLBACK = os.getenv("STT_MODEL_FALLBACK", "whisper-large-v3")

# LLM routers (stored as a JSON string)
LLM_ROUTERS_RAW = os.getenv(
    "LLM_ROUTERS",
    '[{"base_url": "https://api.openai.com/v1", "api_key": "sk-your-key-here", "model": "gpt-4o-mini"}]'
)

# Per-request timeout for LLM calls. Free models on heavy prompts (long
# transcripts + the detailed moments system prompt) often take 90-180s. 60s is
# too tight. Override via LLM_TIMEOUT env (in seconds).
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "240"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))

# YouTube cookies file path (for bot detection bypass)
# Export cookies from your browser (e.g. "Get cookies.txt" extension)
# and mount the file at COOKIES_FILE path.
COOKIES_FILE = os.getenv("COOKIES_FILE", "")

def get_llm_routers():
    try:
        return json.loads(LLM_ROUTERS_RAW)
    except Exception:
        # Fallback if parsing fails
        return []
