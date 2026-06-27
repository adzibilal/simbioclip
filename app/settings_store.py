import json
import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
import app.writable_config as user_config


class LLMRouterConfig(BaseModel):
    base_url: str
    api_key: str = ""
    model: str


class AppSettings(BaseModel):
    api_token: str = "admin"
    llm_routers: List[LLMRouterConfig] = Field(default_factory=list)
    llm_timeout: float = 240.0
    llm_max_retries: int = 1
    stt_mode: str = "local"
    stt_base_url: str = ""
    stt_api_key: str = ""
    stt_model: str = "whisper-large-v3-turbo"
    stt_model_fallback: str = "whisper-large-v3"
    cookies_file: str = ""
    cookies_from_browser: str = ""
    concurrent_fragments: int = 10
    aria2c_enabled: bool = False
    aria2c_connections: int = 16
    throttled_rate: str = "200M"
    repliz_access_key: str = ""
    repliz_secret_key: str = ""
    repliz_default_account_id: str = ""
    repliz_auto_schedule: bool = False
    repliz_schedule_offset_minutes: int = 5
    repliz_post_type: str = "video"
    public_base_url: str = ""
    aws_endpoint: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_bucket: str = ""
    aws_region: str = "us-east-1"
    aws_use_path_style: bool = True
    aws_folder_name: str = "simbioclip"


def _from_config(data: dict) -> AppSettings:
    def _llm(r):
        if isinstance(r, dict):
            return LLMRouterConfig(**r)
        return LLMRouterConfig(base_url="", model="")

    routers = [_llm(r) for r in data.get("llm_routers") or []]
    return AppSettings(
        api_token=data.get("api_token", "admin"),
        llm_routers=routers,
        llm_timeout=float(data.get("llm_timeout", 240)),
        llm_max_retries=int(data.get("llm_max_retries", 1)),
        stt_mode=data.get("stt_mode", "local"),
        stt_base_url=data.get("stt_base_url", ""),
        stt_api_key=data.get("stt_api_key", ""),
        stt_model=data.get("stt_model", "whisper-large-v3-turbo"),
        stt_model_fallback=data.get("stt_model_fallback", "whisper-large-v3"),
        cookies_file=data.get("cookies_file", ""),
        cookies_from_browser=data.get("cookies_from_browser", ""),
        concurrent_fragments=int(data.get("concurrent_fragments", 10)),
        aria2c_enabled=bool(data.get("aria2c_enabled", False)),
        aria2c_connections=int(data.get("aria2c_connections", 16)),
        throttled_rate=data.get("throttled_rate", "200M"),
        repliz_access_key=data.get("repliz_access_key", ""),
        repliz_secret_key=data.get("repliz_secret_key", ""),
        repliz_default_account_id=data.get("repliz_default_account_id", ""),
        repliz_auto_schedule=bool(data.get("repliz_auto_schedule", False)),
        repliz_schedule_offset_minutes=int(data.get("repliz_schedule_offset_minutes", 5)),
        repliz_post_type=data.get("repliz_post_type", "video"),
        public_base_url=data.get("public_base_url", ""),
        aws_endpoint=data.get("aws_endpoint", ""),
        aws_access_key_id=data.get("aws_access_key_id", ""),
        aws_secret_access_key=data.get("aws_secret_access_key", ""),
        aws_bucket=data.get("aws_bucket", ""),
        aws_region=data.get("aws_region", "us-east-1"),
        aws_use_path_style=bool(data.get("aws_use_path_style", True)),
        aws_folder_name=data.get("aws_folder_name", "simbioclip"),
    )


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return f"...{value[-4:]}"


def settings_to_public_dict(settings: AppSettings) -> Dict[str, Any]:
    data = settings.model_dump()
    data["api_token"] = mask_secret(settings.api_token)
    data["stt_api_key"] = mask_secret(settings.stt_api_key)
    data["repliz_access_key"] = mask_secret(settings.repliz_access_key)
    data["repliz_secret_key"] = mask_secret(settings.repliz_secret_key)
    data["aws_secret_access_key"] = mask_secret(settings.aws_secret_access_key)
    data["llm_routers"] = [
        {**r, "api_key": mask_secret(r.get("api_key", ""))}
        for r in data["llm_routers"]
    ]
    return data


_settings_cache: Optional["AppSettings"] = None


def load_settings() -> AppSettings:
    raw = user_config.get_cached()
    return _from_config(raw)


def get_settings() -> AppSettings:
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = load_settings()
    return _settings_cache


def refresh_settings() -> AppSettings:
    global _settings_cache
    user_config.invalidate_cache()
    _settings_cache = load_settings()
    return _settings_cache
