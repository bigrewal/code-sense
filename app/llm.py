from urllib import response
from groq import Groq, RateLimitError
from groq import AsyncGroq
from .config import Config
from typing import Optional, Dict
import time
import asyncio
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

class GroqLLM:
    def __init__(self):
        # Disable default retries to control retry logic manually
        self.client = Groq(api_key=Config.GROQ_API_KEY, max_retries=0)
        self.async_client = AsyncGroq(api_key=Config.GROQ_API_KEY, max_retries=0)

    def generate(
        self,
        prompt: str = "",
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, any]] = None,
        reasoning_effort: Optional[str] = "low",
        model: str = Config.GPT_OSS_20GB_MODEL,
        stream: bool = False,
        tools: Optional[list] = None,
        tool_choice: str = None,
        messages: Optional[list] = None,
        return_raw: bool = False
    ) -> str:
        
        if not messages:
            convo = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
        else:
            convo = messages

        kwargs = {
            "model": model,
            "messages": convo,
            "temperature": temperature if temperature is not None else Config.LLM_TEMPERATURE,
            "max_tokens": max_tokens if max_tokens is not None else Config.LLM_MAX_TOKENS,
        }
        if response_format:
            kwargs["response_format"] = response_format
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if stream:
            kwargs["stream"] = stream
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                response = self.client.chat.completions.create(**kwargs)
                if stream:
                    return response
                if return_raw:
                    return response
                return response.choices[0].message.content.strip()
            except RateLimitError as e:
                if attempt == max_attempts - 1:
                    logger.error("Max retry attempts reached for RateLimitError")
                    raise RuntimeError("Groq API rate limit exceeded after retries")
                # Use exponential backoff: 60s, 120s, 240s
                wait_time = 60 * (2 ** attempt)
                retry_after = e.response.headers.get("Retry-After", "N/A")
                logger.info(
                    f"RateLimitError caught, waiting {wait_time} seconds before retry {attempt + 1}/{max_attempts}, "
                    f"Retry-After header: {retry_after}"
                )
                time.sleep(wait_time)
            except Exception as e:
                logger.error(f"Groq API error: {str(e)}")
                raise RuntimeError(f"Groq API error: {str(e)}")

    async def generate_async(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, any]] = None,
        reasoning_effort: Optional[str] = None,
        model: str = Config.GPT_OSS_20GB_MODEL,
    ) -> any:
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature if temperature is not None else Config.LLM_TEMPERATURE,
            "max_tokens": max_tokens if max_tokens is not None else Config.LLM_MAX_TOKENS,
        }
        if response_format:
            kwargs["response_format"] = response_format
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort

        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                response = await self.async_client.chat.completions.create(**kwargs)
                return response.choices[0].message.content.strip()
            except RateLimitError as e:
                if attempt == max_attempts - 1:
                    logger.error("Max retry attempts reached for RateLimitError")
                    raise RuntimeError("Groq API rate limit exceeded after retries")
                # Use exponential backoff: 60s, 120s, 240s
                wait_time = 60 * (2 ** attempt)
                retry_after = e.response.headers.get("Retry-After", "N/A")
                logger.info(
                    f"RateLimitError caught, waiting {wait_time} seconds before retry {attempt + 1}/{max_attempts}, "
                    f"Retry-After header: {retry_after}"
                )
                await asyncio.sleep(wait_time)
            except Exception as e:
                logger.error(f"Groq API error: {str(e)}")
                raise RuntimeError(f"Groq API error: {str(e)}")