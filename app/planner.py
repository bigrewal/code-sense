import asyncio
from typing import List, Dict
from .llm import GroqLLM
from .prompts.system_prompts import PLANNER_SYSTEM_PROMPT, MENTAL_MODEL_SUMMARISER_PROMPT
import traceback

class QueryPlanner:
    def __init__(self, llm: GroqLLM):
        self.llm = llm
        self.available_tools = [
            "fetch_code_for_def",
            "fetch_code_file",
            "fetch_cross_file_refs_for_file",
            "lookup_refs_for_def",
            "lookup_def_usages",
            "fetch_entry_point_files"
        ]

    async def plan(self, question: str, mental_model: str) -> any:
        """Generate a plan to answer the question using the mental model."""
        # system_prompt = PLANNER_SYSTEM_PROMPT

        planner_prompt = f"""
            Mental model overview: {mental_model}

            Question: {question}

            Generate a JSON plan to answer the question. Only output a valid JSON array of objects, where each object has the keys "tool", "params", and "reason". Here's an example of a valid output:
            ```json
            [
                {{
                    "tool": "fetch_code_for_def",
                    "params": {{"def_name": "my_function"}},
                    "reason": "To analyze the implementation of my_function."
                }},
                {{
                    "tool": "fetch_code_file",
                    "params": {{"file_path": "src/my_file.py"}},
                    "reason": "To get the full context of the file where my_function is defined."
                }}
            ]

            Ensure that the output is syntactically correct JSON and adheres to the same format.

            Instructions:
            Only output a valid JSON string.

            Use the same structure as the example above: an array of objects, each with tool, params, and reason fields.

            Correct syntax errors (e.g. unmatched braces, incorrect quoting).

            Do not add any explanations or extra text—only the final valid JSON string.
            """

        # Summarise the mental model to extract information necessary to answer the question
        summariser_prompt = f"""
            Mental model overview: {mental_model}

            Question: {question}

            Generate a summary of the mental model that includes key components, relationships, and context relevant to the question.
            """

        try:
            # Make both llm.generate at the same time and wait for the responses

            print(f"About to create a mental model summary and a query plan...")
            mental_model_summary, response = await asyncio.gather(
                self.llm.generate_async(summariser_prompt,
                                  system_prompt=MENTAL_MODEL_SUMMARISER_PROMPT,
                                  reasoning_effort="low"),
                self.llm.generate_async(planner_prompt,
                                  system_prompt=PLANNER_SYSTEM_PROMPT,
                                  reasoning_effort="medium")
            )
            
            print("Generated plan:", response)
            import json
            plan = json.loads(response)
            if not isinstance(plan, list):
                raise ValueError("Plan must be a JSON array")
            return plan, mental_model_summary
        except json.JSONDecodeError:
            traceback.print_exc()
            raise ValueError("LLM returned invalid JSON plan")
        except Exception as e:
            traceback.print_exc()
            raise RuntimeError(f"Planning error: {str(e)}")