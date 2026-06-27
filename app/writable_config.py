import os
import json
import logging

logger = logging.getLogger("simbioclip.config")

_DEFAULTS = {
    "api_token": "admin",
    "llm_routers": [
        {"base_url": "https://api.openai.com/v1", "api_key": "sk-your-key-here", "model": "gpt-4o-mini"}
    ],
    "llm_timeout": 240,
    "llm_max_retries": 1,
    "stt_mode": "local",
    "stt_base_url": "",
    "stt_api_key": "",
    "stt_model": "whisper-large-v3-turbo",
    "stt_model_fallback": "whisper-large-v3",
    "cookies_file": "",
    "cookies_from_browser": "",
    "concurrent_fragments": 10,
    "aria2c_enabled": False,
    "aria2c_connections": 16,
    "throttled_rate": "200M",
    "repliz_access_key": "",
    "repliz_secret_key": "",
    "repliz_default_account_id": "",
    "repliz_auto_schedule": False,
    "repliz_schedule_offset_minutes": 5,
    "repliz_post_type": "video",
    "public_base_url": "",
    "aws_endpoint": "",
    "aws_access_key_id": "",
    "aws_secret_access_key": "",
    "aws_bucket": "",
    "aws_region": "us-east-1",
    "aws_use_path_style": True,
    "aws_folder_name": "simbioclip",
    "ai_providers": {
        "highlight_finder": {
            "provider_type": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-4o-mini",
        },
        "caption_maker": {
            "provider_type": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "whisper-1",
        },
        "hook_maker": {
            "provider_type": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "tts-1",
        },
    },
    "watermark": {
        "enabled": False,
        "image_path": "",
        "pos_x": 0.85,
        "pos_y": 0.05,
        "opacity": 0.8,
        "scale": 0.12,
    },
    "credit_watermark": {
        "enabled": True,
        "pos_x": 0.5,
        "pos_y": 0.95,
        "size": 0.022,
        "opacity": 0.3,
    },
    "hook_style": {
        "font_size": 0.045,
        "font_color": "#00a000",
        "bg_color": "#FFFFFF",
        "corner_radius": 8,
        "pos_x": 0.5,
        "pos_y": 0.7,
    },
}

_SECRET_KEYS = {
    "api_token", "stt_api_key", "repliz_access_key", "repliz_secret_key",
    "aws_secret_access_key", "aws_access_key_id",
}


def _seed_from_env() -> dict:
    """Seed defaults from env vars if config.json doesn't exist yet."""
    data = dict(_DEFAULTS)
    data["api_token"] = os.getenv("API_TOKEN", data["api_token"])
    data["llm_timeout"] = float(os.getenv("LLM_TIMEOUT", str(data["llm_timeout"])))
    data["llm_max_retries"] = int(os.getenv("LLM_MAX_RETRIES", str(data["llm_max_retries"])))
    data["stt_mode"] = os.getenv("STT_MODE", data["stt_mode"]).lower()
    data["stt_base_url"] = os.getenv("STT_BASE_URL", data["stt_base_url"])
    data["stt_api_key"] = os.getenv("STT_API_KEY", data["stt_api_key"])
    data["stt_model"] = os.getenv("STT_MODEL", data["stt_model"])
    data["stt_model_fallback"] = os.getenv("STT_MODEL_FALLBACK", data["stt_model_fallback"])
    data["cookies_file"] = os.getenv("COOKIES_FILE", data["cookies_file"])
    data["cookies_from_browser"] = os.getenv("COOKIES_FROM_BROWSER", data["cookies_from_browser"])
    data["concurrent_fragments"] = int(os.getenv("CONCURRENT_FRAGMENTS", str(data["concurrent_fragments"])))
    data["aria2c_enabled"] = os.getenv("ARIA2C_ENABLED", str(data["aria2c_enabled"])).lower() == "true"
    data["aria2c_connections"] = int(os.getenv("ARIA2C_CONNECTIONS", str(data["aria2c_connections"])))
    data["throttled_rate"] = os.getenv("THROTTLED_RATE", data["throttled_rate"])
    data["repliz_access_key"] = os.getenv("REPLIZ_ACCESS_KEY", data["repliz_access_key"])
    data["repliz_secret_key"] = os.getenv("REPLIZ_SECRET_KEY", data["repliz_secret_key"])
    data["repliz_default_account_id"] = os.getenv("REPLIZ_DEFAULT_ACCOUNT_ID", data["repliz_default_account_id"])
    data["repliz_auto_schedule"] = os.getenv("REPLIZ_AUTO_SCHEDULE", str(data["repliz_auto_schedule"])).lower() == "true"
    data["repliz_schedule_offset_minutes"] = int(os.getenv("REPLIZ_SCHEDULE_OFFSET_MINUTES", str(data["repliz_schedule_offset_minutes"])))
    data["repliz_post_type"] = os.getenv("REPLIZ_POST_TYPE", data["repliz_post_type"])
    data["public_base_url"] = os.getenv("PUBLIC_BASE_URL", data["public_base_url"])
    data["aws_endpoint"] = os.getenv("AWS_ENDPOINT", data["aws_endpoint"])
    data["aws_access_key_id"] = os.getenv("AWS_ACCESS_KEY_ID", data["aws_access_key_id"])
    data["aws_secret_access_key"] = os.getenv("AWS_SECRET_ACCESS_KEY", data["aws_secret_access_key"])
    data["aws_bucket"] = os.getenv("AWS_BUCKET", data["aws_bucket"])
    data["aws_region"] = os.getenv("AWS_DEFAULT_REGION", data["aws_region"])
    data["aws_use_path_style"] = os.getenv("AWS_USE_PATH_STYLE_ENDPOINT", str(data["aws_use_path_style"])).lower() == "true"
    data["aws_folder_name"] = os.getenv("AWS_FOLDER_NAME", data["aws_folder_name"])

    llm_raw = os.getenv("LLM_ROUTERS")
    if llm_raw:
        try:
            data["llm_routers"] = json.loads(llm_raw)
        except Exception:
            pass
    return data


def _get_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = os.getenv("DATA_DIR", os.path.join(base, "data"))
    return os.path.join(data_dir, "config.json")


def load() -> dict:
    path = _get_path()
    if not os.path.exists(path):
        seeded = _seed_from_env()
        try:
            save(seeded)
            logger.info(f"Seeded config.json from environment")
        except Exception as e:
            logger.warning(f"Could not write initial config.json: {e}")
        return dict(seeded)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(_DEFAULTS)
        merged.update(data)
        for section in ("ai_providers", "watermark", "credit_watermark", "hook_style"):
            if section in data and isinstance(data[section], dict):
                merged[section] = dict(_DEFAULTS.get(section, {}))
                merged[section].update(data[section])
        return merged
    except Exception as e:
        logger.error(f"Failed to load config.json: {e}")
        return dict(_DEFAULTS)


def save(data: dict) -> None:
    path = _get_path()
    merged = dict(_DEFAULTS)
    merged.update(data)
    for section in ("ai_providers", "watermark", "credit_watermark", "hook_style"):
        if section in data and isinstance(data[section], dict):
            merged[section] = dict(_DEFAULTS.get(section, {}))
            merged[section].update(data[section])
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save config.json: {e}")
        raise


def mask_secrets(data: dict) -> dict:
    masked = json.loads(json.dumps(data))
    for key in _SECRET_KEYS:
        val = masked.get(key, "")
        if val and len(val) > 4:
            masked[key] = f"...{val[-4:]}"
        elif val:
            masked[key] = "****"
    providers = masked.get("ai_providers", {})
    for pk in providers:
        ak = providers[pk].get("api_key", "")
        if ak and len(ak) > 4:
            providers[pk]["api_key"] = f"...{ak[-4:]}"
        elif ak:
            providers[pk]["api_key"] = "****"
    routers = masked.get("llm_routers", [])
    for r in routers:
        ak = r.get("api_key", "")
        if ak and len(ak) > 4:
            r["api_key"] = f"...{ak[-4:]}"
        elif ak:
            r["api_key"] = "****"
    return masked


def invalidate_cache() -> None:
    global _config_cache
    _config_cache = None


_config_cache: dict | None = None


def get_cached() -> dict:
    global _config_cache
    if _config_cache is None:
        _config_cache = load()
    return _config_cache
