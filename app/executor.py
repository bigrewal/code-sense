import traceback
import asyncio
from typing import List, Dict, Any
from .llm import GroqLLM
from .tools import TOOL_REGISTRY

class PlanExecutor:
    def __init__(self, llm: GroqLLM):
        self.llm = llm
        self.max_iterations = 3  # Limit to prevent infinite loops

    async def execute(self, plan: List[Dict[str, Any]], question: str, mental_model: Dict[str, str], parallel: bool = True) -> List[Dict[str, Any]]:
        """Execute the plan, calling tools in parallel or sequentially."""
        results = []
        current_plan = plan.copy()
        iteration = 0

        while current_plan and iteration < self.max_iterations:
            if parallel:
                # Parallel execution
                tasks = []
                for step in current_plan:
                    tool_name = step.get("tool")
                    params = step.get("params", {})
                    if tool_name not in TOOL_REGISTRY:
                        results.append({"step": step, "error": f"Unknown tool: {tool_name}"})
                        continue
                    try:
                        tool_func = TOOL_REGISTRY[tool_name]
                        tasks.append(asyncio.create_task(self._run_tool(tool_func, params)))
                    except Exception as e:
                        traceback.print_exc()
                        results.append({"step": step, "error": str(e)})
                
                # Await all tasks
                task_results = await asyncio.gather(*tasks, return_exceptions=True)
                for step, task_result in zip(current_plan, task_results):
                    if isinstance(task_result, Exception):
                        results.append({"step": step, "error": str(task_result)})
                    else:
                        results.append({"step": step, "result": task_result})
            else:
                # Sequential execution
                for step in current_plan:
                    tool_name = step.get("tool")
                    params = step.get("params", {})
                    if tool_name not in TOOL_REGISTRY:
                        results.append({"step": step, "error": f"Unknown tool: {tool_name}"})
                        continue
                    try:
                        tool_func = TOOL_REGISTRY[tool_name]
                        result = await self._run_tool(tool_func, params)
                        results.append({"step": step, "result": result})
                    except Exception as e:
                        traceback.print_exc()
                        results.append({"step": step, "error": str(e)})
            
            iteration += 1

        return results

    async def _run_tool(self, tool_func, params: Dict[str, Any]) -> Any:
        """Run a single tool asynchronously."""
        try:
            # If tool is async, await it; otherwise, run in default loop
            if asyncio.iscoroutinefunction(tool_func):
                return await tool_func(**params)
            else:
                return tool_func(**params)
        except Exception as e:
            raise e