"""Contextual retrieval module for semantic code search."""

import asyncio
import hashlib
from pathlib import Path
from typing import List, Dict, Any

import chromadb
import voyageai
from tree_sitter_languages import get_parser

from .config import Config
from .llm_grok import GrokLLM
from .db import get_mongo_client

CHUNK_CONTEXT_PROMPT = """Here are chunks we want to situate within the whole document
{chunks_formatted}

Give a short succinct context for each chunk to situate it for search retrieval.
Answer with a JSON object mapping chunk index to context, like: {{"0": "context for chunk 0", "1": "context for chunk 1"}}
Only return the JSON object, no other text."""


class ContextualRetrieval:
    """Handles contextual embeddings and semantic search for code chunks."""

    def __init__(self, repo_name: str, max_concurrent_llm: int = 10):
        self.repo_name = repo_name
        self.llm = GrokLLM()
        self.voyage = voyageai.Client(api_key=Config.VOYAGE_API_KEY)
        self.mongo_client = get_mongo_client()
        self._semaphore = asyncio.Semaphore(max_concurrent_llm)

        store_path = Path(Config.BASE_REPO_DIR) / repo_name / "vector_store"
        store_path.mkdir(parents=True, exist_ok=True)
        self.chroma = chromadb.PersistentClient(path=str(store_path))
        self.collection = self.chroma.get_or_create_collection(
            name="code_chunks",
            metadata={"hnsw:space": "cosine"}
        )

    async def generate_context(self, file_content: str, chunks: List[str]) -> Dict[int, str]:
        """Generate situating context for multiple chunks using a single LLM call.

        Args:
            file_content: The full file content for context
            chunks: List of code chunks to contextualize

        Returns:
            Dictionary mapping chunk index to its context
        """
        async with self._semaphore:
            # Format chunks with their indices
            chunks_formatted = "\n\n".join(
                f"<chunk index='{i}'>\n{chunk}\n</chunk>"
                for i, chunk in enumerate(chunks)
            )

            prompt = f"<document>\n{file_content}\n</document>\n\n{CHUNK_CONTEXT_PROMPT.format(chunks_formatted=chunks_formatted)}"
            response = await self.llm.generate_async(prompt=prompt, system_prompt="", temperature=0.0)

            # Parse JSON response
            import json
            try:
                contexts = json.loads(response)
                return {int(k): v for k, v in contexts.items()}
            except json.JSONDecodeError:
                # Fallback: if JSON parsing fails, return empty context for all chunks
                return {i: "" for i in range(len(chunks))}

    def _embed(self, texts: List[str]) -> List[List[float]]:
        """Create embeddings using Voyage AI."""
        result = self.voyage.embed(texts, model="voyage-code-3", input_type="document")
        return result.embeddings

    def _chunk_code(self, code: str, file_path: str) -> List[str]:
        """Split code into chunks by function/class definitions using tree-sitter."""
        suffix = Path(file_path).suffix.lower()
        language = Config.SUPPORTED_LANGUAGES.get(suffix)

        if not language:
            # Fallback for unsupported languages
            lines = code.split('\n')
            return ['\n'.join(lines[i:i+50]) for i in range(0, len(lines), 40) if lines[i:i+50]]

        parser = get_parser(language)
        tree = parser.parse(bytes(code, "utf8"))
        definition_types = Config.LANGUAGE_DEFINITION_MAP.get(language, set())

        chunks = []
        code_bytes = bytes(code, "utf8")

        def extract_definitions(node):
            if node.type in definition_types:
                chunk_text = code_bytes[node.start_byte:node.end_byte].decode("utf8")
                if chunk_text.strip():
                    chunks.append(chunk_text.strip())
            for child in node.children:
                extract_definitions(child)

        extract_definitions(tree.root_node)

        if not chunks:
            # Fallback if no definitions found
            lines = code.split('\n')
            chunks = ['\n'.join(lines[i:i+50]) for i in range(0, len(lines), 40) if lines[i:i+50]]

        return chunks

    async def index_file(self, file_path: str, code: str):
        """Index a file's chunks with contextual embeddings."""
        chunks = self._chunk_code(code, file_path)
        if not chunks:
            return

        # Generate context and embeddings for each chunk
        texts_to_embed = []
        metadatas = []
        ids = []

        # Generate contexts in batches with single LLM calls
        all_contexts = {}
        for i in range(0, len(chunks), 10):
            batch_chunks = chunks[i:i+10]
            batch_contexts = await self.generate_context(code, batch_chunks)
            for local_idx, context in batch_contexts.items():
                all_contexts[i + local_idx] = context

        # Prepare texts, metadata, and IDs for embedding
        for i, chunk in enumerate(chunks):
            context = all_contexts.get(i, "")
            contextualized = f"{context}\n\n{chunk}" if context else chunk
            texts_to_embed.append(contextualized)
            chunk_id = hashlib.md5(f"{file_path}:{i}:{chunk}".encode()).hexdigest()
            ids.append(chunk_id)
            metadatas.append({
                "file_path": file_path,
                "context": context,
                "content": chunk[:2000],
            })

        # Embed and upsert in batches
        # print(f"Indexing {len(texts_to_embed)} chunks for {file_path}")
        for i in range(0, len(texts_to_embed), 128):
            batch_texts = texts_to_embed[i:i+128]
            batch_ids = ids[i:i+128]
            batch_metadatas = metadatas[i:i+128]

            embeddings = self._embed(batch_texts)

            self.collection.upsert(
                ids=batch_ids,
                embeddings=embeddings,
                metadatas=batch_metadatas,
            )

    def search(self, query: str, n: int = 20) -> List[Dict[str, Any]]:
        """Search for relevant chunks with Voyage reranking."""
        # Vector search - get more candidates for reranking
        query_embedding = self._embed([query])[0]
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=n * 10,
        )

        if not results["metadatas"] or not results["metadatas"][0]:
            return []

        # Prepare docs for reranking with context
        docs = [
            f"{m['content']}\n\nContext: {m['context']}"
            if m.get('context') else m['content']
            for m in results["metadatas"][0]
        ]

        # Voyage rerank
        reranked = self.voyage.rerank(
            query=query,
            documents=docs,
            top_k=n,
            model="rerank-2",
        )

        # Return reranked results with metadata
        output = []
        for r in reranked.results:
            meta = results["metadatas"][0][r.index]
            output.append({
                "file_path": meta["file_path"],
                "content": meta["content"],
                "context": meta["context"],
                "score": r.relevance_score,
            })
        return output
