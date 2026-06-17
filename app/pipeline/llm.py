import logging
from typing import List, Dict, Any, Optional
from openai import OpenAI
from app.config import get_llm_routers, LLM_TIMEOUT, LLM_MAX_RETRIES

logger = logging.getLogger("simbioclip.pipeline.llm")

class LLMRouterClient:
    def __init__(self):
        self.routers = get_llm_routers()
        if not self.routers:
            logger.warning("No LLM routers loaded. Check LLM_ROUTERS environment variable.")

    def get_completion(
        self, 
        messages: List[Dict[str, str]], 
        response_format: Optional[Dict[str, Any]] = None,
        temperature: float = 0.2
    ) -> str:
        if not self.routers:
            raise ValueError("No LLM routers configured in application settings.")

        last_exception = None
        for i, router in enumerate(self.routers):
            base_url = router.get("base_url")
            api_key = router.get("api_key")
            model = router.get("model")

            if not base_url or not model:
                logger.error(f"Router config at index {i} is invalid (missing base_url or model)")
                continue

            logger.info(f"Attempting completion with LLM Router {i} (URL: {base_url}, Model: {model})")
            
            try:
                # Initialize OpenAI client for this router. Long transcripts +
                # the detailed moments prompt push free-model latencies past
                # 60s; use a generous timeout and minimal retries so we don't
                # multiply wait times unnecessarily.
                client = OpenAI(
                    base_url=base_url,
                    api_key=api_key,
                    timeout=LLM_TIMEOUT,
                    max_retries=LLM_MAX_RETRIES,
                )

                kwargs = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature
                }
                
                # Check for json mode / response format support
                if response_format:
                    kwargs["response_format"] = response_format

                response = client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                
                if content:
                    logger.info(f"Successfully received response from LLM Router {i}")
                    return content
                else:
                    raise ValueError("Received empty content from LLM provider.")
            except Exception as e:
                logger.warning(f"LLM Router {i} (URL: {base_url}) failed: {e}")
                last_exception = e
                # Fall through to the next router
                continue
                
        raise RuntimeError(f"All LLM routers failed. Last error: {last_exception}")

# Instantiate global LLM client
llm_client = LLMRouterClient()
