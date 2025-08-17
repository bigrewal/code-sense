import asyncio
from inspect import trace
from typing import List, Dict, Any
from .llm import GroqLLM
import json
import traceback

class ResponseSynthesizer:
    def __init__(self, llm: GroqLLM):
        self.llm = llm

    async def synthesize(
        self, question: str, mental_model_summary: str, execution_results: List[Dict[str, Any]]
    ) -> Any:
        
        print("Synthesizing response...")

        """Synthesize a complete Markdown response from execution results."""
        system_prompt = """
You are a code analysis assistant. Given a question, mental model, and tool execution results, synthesize a clear, accurate, and complete answer in Markdown format. Structure the response with:
- **Overview**: Summarize the answer briefly.
- **Details**: Explain the main findings (e.g., function code, behavior).
- **Dependencies**: List dependencies or interactions (if any).
- **Notes**: Highlight edge cases, errors, or missing info.

Use the mental model to contextualize results and ensure completeness.
"""

        prompt = f"""
Question: {question}
Mental model overview: {mental_model_summary}
Execution results: {json.dumps(execution_results, indent=2)}

Generate a Markdown response answering the question.
"""
        try:
            completion = self.llm.generate(
                prompt,
                system_prompt=system_prompt,
                reasoning_effort="low",
                temperature=0.0,
                stream=True
            )

            # return completion
            for chunk in completion:
                # print(chunk.choices[0].delta.content or "", end="")
                content = chunk.choices[0].delta.content or ""
                if content:
                    yield content
                await asyncio.sleep(0)

        except Exception as e:
            traceback.print_exc()
            raise RuntimeError(f"Synthesis error: {str(e)}")

#     def check_completeness(
#         self, question: str, mental_model: Dict[str, str], response: str, execution_results: List[Dict[str, Any]]
#     ) -> bool:
#         """Check if the response is complete using the LLM."""
#         system_prompt = """
# You are a code analysis assistant. Given a question, mental model, execution results, and a draft response, determine if the response is complete and accurate based on the mental model and results. Return JSON: {"complete": bool, "missing": str}.
# """
#         prompt = f"""
# Question: {question}
# Mental model overview: {mental_model['overview']}
# Execution results: {json.dumps(execution_results, indent=2)}
# Draft response: {response}

# Is the response complete? If not, specify what's missing.
# """
#         try:
#             result = self.llm.generate(
#                 prompt,
#                 system_prompt=system_prompt,
#                 response_format={"type": "json_object"},
#                 reasoning_effort="medium"
#             )
#             check = json.loads(result)
#             return check.get("complete", False), check.get("missing", "")
#         except Exception:
#             return False, "Failed to check completeness"