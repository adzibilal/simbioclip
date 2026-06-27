import logging
from typing import List, Dict, Any, Optional
from openai import OpenAI
from app.settings_store import get_settings
import app.writable_config as user_config

logger = logging.getLogger("simbioclip.pipeline.llm")

_PROVIDER_CACHE = None


def _get_highlight_provider() -> Optional[Dict[str, Any]]:
    global _PROVIDER_CACHE
    if _PROVIDER_CACHE is None:
        cfg = user_config.load()
        _PROVIDER_CACHE = cfg.get("ai_providers", {}).get("highlight_finder")
    return _PROVIDER_CACHE


def _build_routers_from_config() -> List[Dict[str, Any]]:
    prov = _get_highlight_provider()
    if prov and prov.get("api_key"):
        return [{"base_url": prov["base_url"], "api_key": prov["api_key"], "model": prov["model"]}]
    return []


def get_caption_provider() -> Optional[Dict[str, Any]]:
    cfg = user_config.load()
    return cfg.get("ai_providers", {}).get("caption_maker")


def get_hook_provider() -> Optional[Dict[str, Any]]:
    cfg = user_config.load()
    return cfg.get("ai_providers", {}).get("hook_maker")


def invalidate_provider_cache():
    global _PROVIDER_CACHE
    _PROVIDER_CACHE = None


class LLMRouterClient:
    @property
    def routers(self) -> List[Dict[str, Any]]:
        routers = _build_routers_from_config()
        if not routers:
            settings = get_settings()
            routers = [r.model_dump() for r in settings.llm_routers]
        return routers

    def get_completion(
        self,
        messages: List[Dict[str, str]],
        response_format: Optional[Dict[str, Any]] = None,
        temperature: float = 0.2
    ) -> str:
        routers = self.routers
        if not routers:
            raise ValueError("No LLM routers configured.")

        last_exception = None
        for i, router in enumerate(routers):
            base_url = router.get("base_url")
            api_key = router.get("api_key")
            model = router.get("model")

            if not base_url or not model:
                logger.error(f"Router config at index {i} is invalid")
                continue

            logger.info(f"Attempting completion with LLM Router {i} ({base_url}, {model})")

            try:
                client = OpenAI(
                    base_url=base_url,
                    api_key=api_key,
                    timeout=get_settings().llm_timeout,
                    max_retries=get_settings().llm_max_retries,
                )
                kwargs = {"model": model, "messages": messages, "temperature": temperature}
                if response_format:
                    kwargs["response_format"] = response_format
                response = client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                if content:
                    logger.info(f"Success from LLM Router {i}")
                    return content
                raise ValueError("Empty content from LLM provider.")
            except Exception as e:
                logger.warning(f"LLM Router {i} failed: {e}")
                last_exception = e
                continue

        raise RuntimeError(f"All LLM routers failed. Last error: {last_exception}")


llm_client = LLMRouterClient()
