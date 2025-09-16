from __future__ import annotations

import re
from typing import Dict, Any, Iterator, Sequence, Callable
import json

from dataclasses import dataclass, field
from collections import deque
from typing import Callable, Sequence, Set, List, Any, Iterator, Optional
import textwrap

# ---- Provided tools (imported directly; no separate CodeTools protocol) ----
from .tools.fetch_entry_point_files import fetch_entry_point_files_as_list
from .tools.fetch_cross_file_refs_for_file import fetch_cross_file_refs_for_file_as_list
from .tools.fetch_code_file import fetch_code_file

from .router import RoutePlan

from .db import get_mongo_client


from .tools.fetch_code_file import fetch_code_file as fetch_code_file_tool
from .tools.lookup_refs_for_def import lookup_refs_for_def
from .tools.lookup_def_usages import lookup_def_usages
from .db import attention_db_runtime
# Optional import for dir listing (ask the user to provide if unavailable)
try:  # pragma: no cover - availability depends on host project
    from .tools.list_files_in_dir import list_files_in_dir  # type: ignore
except Exception:  # pragma: no cover
    list_files_in_dir = None  # type: ignore

from .retriever import retrieve_records




class FreeformQA:
    def __init__(
        self,
        llm: Any,
        repo_name: str,
        seed_prompt: str,
        user_question: str,
    ):
        self.llm = llm
        self.repo_name = repo_name
        self.seed_prompt = seed_prompt
        self.user_question = user_question
        self.temperature = 0.0

    
    def answer(self) -> Any:
        
        with open(f"{self.repo_name}/mental_model/mental_model.jsonl") as f:
            data = [json.loads(line) for line in f]

        records = retrieve_records(
            repo_name=self.repo_name,
            question=self.user_question,
            llm=self.llm,
            jsonl_records=data
        )

        print("Retrieved records: ", records)

        # _, pack_items = attention_db_runtime._pack_blocking(self.repo_name, self.user_question)
        
        # Decision prompt to decide whether the {file_summary} at {file_path} is relevant to answer the question. If it is relevant respond with "YES" else "NO". 
        decision_system_prompt = (
            "You are an expert code analyst. Your task is to determine whether a given code file is relevant to answer the user's question about a codebase. "
            "You will be provided with the user's question and a code file summary along with its file path. "
            "For the given file summary, you must decide if it is relevant to answering the user's question. "
            "Respond with 'YES' if the file is relevant, otherwise respond with 'NO'. "
            "Do not provide any additional information or context; only respond with 'YES' or 'NO'."
        )

        # User Q/A prompt - Given the user question, file path and file code, provide a detailed answer to the user's question. Don't make up any information that is not present in the file.
        qa_system_prompt = (
            "You are an expert code analyst. Your task is to provide a detailed answer to the user's question about a codebase. "
            "You will be provided with the user's question, a code file path, and the full content of the code file. "
            "Using the information from the code file, you must provide a comprehensive answer to the user's question. "
            "If the code file does not contain information relevant to the user's question, respond with 'The provided file does not contain information relevant to the user's question.' "
            "Do not make up any information that is not present in the file."
        )

        messages = [
            {"role": "system", "content": decision_system_prompt},
        ]

        if records == []:
            yield "Sorry, I could not find any relevant information in the codebase to answer your question."
            return

        for record in records:
            file_path = record['file_path']
            look_for = record['look_for']

            file_content = fetch_code_file_tool(file_path=file_path)
            user_content_qa = (
                f"User's Question: {self.user_question}\n\n"
                f"File Path: {file_path}\n"
                f"File Content:\n{file_content}\n\n"
                f"In the above file, look for: {look_for}\n\n"
            )
            messages_qa = [
                {"role": "system", "content": qa_system_prompt},
                {"role": "user", "content": user_content_qa},
            ]
            resp_qa = self.llm.generate(
                messages=messages_qa,
                reasoning_effort="low",
                temperature=self.temperature,
                stream=True,
                response_format=None,
            )

            for chunk in resp_qa:
                content = chunk.choices[0].delta.content or ""
                if content:
                    yield content


        # for item in pack_items:
        #     file_path = item['record']['file_path']
        #     summary = item['record']['summary']
            
        #     user_content = (
        #         f"User's Question: {self.user_question}\n\n"
        #         f"File Path: {file_path}\n"
        #         f"File Summary: {summary}\n\n"
        #         "Is this file relevant to answer the user's question? Respond with 'YES' or 'NO'."
        #     )
        #     messages.append({"role": "user", "content": user_content})

        #     resp = self.llm.generate(
        #         messages=messages,
        #         reasoning_effort="medium",
        #         temperature=self.temperature,
        #         stream=False,
        #         response_format=None,
        #     )
        #     print(f"========== Decision response: ============ {resp} for file: {file_path}")

        #     if resp == "YES":
        #         file_content = fetch_code_file_tool(file_path=file_path)
        #         user_content_qa = (
        #             f"User's Question: {self.user_question}\n\n"
        #             f"File Path: {file_path}\n"
        #             f"File Content:\n{file_content}\n\n"
        #             "Based on the above file content, provide a detailed answer to the user's question."
        #         )
        #         messages_qa = [
        #             {"role": "system", "content": qa_system_prompt},
        #             {"role": "user", "content": user_content_qa},
        #         ]
        #         resp_qa = self.llm.generate(
        #             messages=messages_qa,
        #             reasoning_effort="low",
        #             temperature=self.temperature,
        #             stream=True,
        #             response_format=None,
        #         )

        #         for chunk in resp_qa:
        #             content = chunk.choices[0].delta.content or ""
        #             if content:
        #                 yield content

        #     elif resp == "NO":
        #         print(f"File {file_path} deemed not relevant.")
        #         continue




