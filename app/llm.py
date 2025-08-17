from groq import Groq
from groq import AsyncGroq
from .config import Config
from typing import Optional, Dict


class GroqLLM:
    def __init__(self):
        self.client = Groq(api_key=Config.GROQ_API_KEY)
        self.async_client = AsyncGroq(api_key=Config.GROQ_API_KEY)

    def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, any]] = None,
        reasoning_effort: Optional[str] = None,
        stream: bool = False
    ) -> str:
        kwargs = {
            "model": Config.LLM_MODEL,
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
        if stream:
            kwargs["stream"] = stream

        try:
            response = self.client.chat.completions.create(**kwargs)

            if not stream:
                return response.choices[0].message.content.strip()
            else:
                return response
        except Exception as e:
            raise RuntimeError(f"Groq API error: {str(e)}")
    
    async def generate_async(
        self,
        prompt: str,
        system_prompt: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, any]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> any:
        kwargs = {
            "model": Config.LLM_MODEL,
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

        try:
            response = await self.async_client.chat.completions.create(**kwargs)
            return response.choices[0].message.content.strip()
        except Exception as e:
            raise RuntimeError(f"Groq API error: {str(e)}")
        