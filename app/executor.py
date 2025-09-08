import traceback
import asyncio
from typing import List, Dict, Any, Optional, Callable, Awaitable, Union
from .llm import GroqLLM
from .tools import TOOL_REGISTRY

ToolFunc = Callable[..., Union[Any, Awaitable[Any]]]

# Only these tools should be post-processed by the LLM
LLM_POSTPROCESS_TOOLS = frozenset({"fetch_code_for_def", "fetch_code_file"})


class PlanExecutor:
    def __init__(self, llm: GroqLLM):
        """
        Args:
            llm: LLM client used to optionally post-process tool outputs.
        """
        self.llm = llm
        self.max_iterations = 3  # Safety guard; we execute once and then break.

    async def execute(
        self,
        plan: List[Dict[str, Any]],
        question: str,
        mental_model: Dict[str, str],
        repo_id: str,
        parallel: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Execute the plan. For each step:
          1) Run the specified tool with its params.
          2) If the tool is in LLM_POSTPROCESS_TOOLS, send the tool's result to the LLM
             with system_prompt = step['reason'] and store the LLM output.
             Otherwise, store the raw tool output.

        Returns:
            List of dicts with {"step": step, "result": <output>} or {"step": step, "error": <message>}
        """
        results: List[Dict[str, Any]] = []
        current_plan = plan.copy()
        iteration = 0

        while current_plan and iteration < self.max_iterations:
            if parallel:
                tasks = []
                steps_for_tasks = []
                for step in current_plan:
                    tool_name: Optional[str] = step.get("tool")
                    params: Dict[str, Any] = step.get("params", {})
                    reason: str = step.get("reason", "")

                    if not tool_name or tool_name not in TOOL_REGISTRY:
                        results.append({"step": step, "error": f"Unknown tool: {tool_name}"})
                        continue

                    tool_func: ToolFunc = TOOL_REGISTRY[tool_name]
                    use_llm: bool = tool_name in LLM_POSTPROCESS_TOOLS
                    tasks.append(asyncio.create_task(
                        self._execute_step(tool_func, params, reason, use_llm, repo_id)
                    ))
                    steps_for_tasks.append(step)

                task_results = await asyncio.gather(*tasks, return_exceptions=True)
                for step, task_result in zip(steps_for_tasks, task_results):
                    if isinstance(task_result, Exception):
                        results.append({"step": step, "error": str(task_result)})
                    else:
                        results.append({"step": step, "result": task_result})
            else:
                for step in current_plan:
                    tool_name: Optional[str] = step.get("tool")
                    params: Dict[str, Any] = step.get("params", {})
                    reason: str = step.get("reason", "")

                    if not tool_name or tool_name not in TOOL_REGISTRY:
                        results.append({"step": step, "error": f"Unknown tool: {tool_name}"})
                        continue

                    tool_func: ToolFunc = TOOL_REGISTRY[tool_name]
                    use_llm: bool = tool_name in LLM_POSTPROCESS_TOOLS
                    try:
                        output = await self._execute_step(tool_func, params, reason, repo_id, use_llm)
                        results.append({"step": step, "result": output})
                    except Exception as e:
                        traceback.print_exc()
                        results.append({"step": step, "error": str(e)})

            iteration += 1
            # Run the plan once; prevent repeating the same steps
            current_plan = []

        return results

    async def _execute_step(
        self,
        tool_func: ToolFunc,
        params: Dict[str, Any],
        reason: str,
        repo_id: str,
        use_llm: bool
    ) -> Any:
        """
        Run a single tool; optionally post-process its output with the LLM.

        If use_llm is True, the LLM call mirrors:
            self.llm.generate_async(
                tool_result,
                system_prompt=f"Your task is {reason}",
                reasoning_effort="low",
            )
        """
        tool_result = await self._run_tool(tool_func, params, repo_id)

        if not use_llm:
            return tool_result

        # Post-process with LLM
        llm_output = await self.llm.generate_async(
            prompt=tool_result,
            system_prompt=f"Your task is {reason}",
            reasoning_effort="low",
        )
        return llm_output

    async def _run_tool(self, tool_func: ToolFunc, params: Dict[str, Any], repo_id: str) -> Any:
        """Run a single tool (supports sync or async functions)."""
        try:
            if asyncio.iscoroutinefunction(tool_func):
                return await tool_func(**params, repo_id=repo_id)
            else:
                return tool_func(**params, repo_id=repo_id)
        except Exception as e:
            raise e
