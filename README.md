### **CodeSense — Repo-wide QA via Graph-Based Compression**

> **Goal:** Build a repo-wide code understanding system that provides **accurate, evidence-grounded answers developers can rely on**.

Repo-wide code question answering is typically approached using **RAG** (retrieve top-k snippets) and/or **agentic traversal** (search → read → repeat). These methods give the model a **partial, fragmented view** of the codebase and often struggle with cross-file reasoning and global structure.

CodeSense takes a different approach.

> **I treat repo QA as a compression problem, not a search problem.**

Instead of retrieving isolated snippets, CodeSense:

* builds a **semantic dependency graph directly from code** (AST + LSP),
* compresses that graph into a **global repository context** that fits within an LLM’s context window,
* and uses this context to answer questions with a **repo-wide mental model**.

> Note: The repo-wide mental model is constructed exclusively from source code (AST + semantic dependencies) and does not rely on repository documentation or Markdown files.

The resulting mental model captures **upstream and downstream relationships across files**, enabling reliable reasoning about cross-component behavior.

Importantly, this mental model serves two roles:

1. **Answering**: the LLM can directly answer questions using a coherent global view of the repository.
2. **Navigation**: the model can also use the mental model as a guide to identify *where to look* when deeper inspection is needed, rather than relying on blind retrieval.

The goal is to give the LLM an **integrated understanding of the entire codebase**, rather than a handful of retrieved chunks or an agent’s transient working memory.


> **Outcome:** In a controlled comparison against DeepWiki (Cognition), I tested repo-level understanding with and without Markdown documentation. I found that DeepWiki’s explanations rely heavily on existing docs and degrade significantly when documentation is removed. In contrast, CodeSense continues to produce coherent, end-to-end explanations because its repo-wide mental model is derived entirely from code structure and semantic dependencies, not from written documentation. This makes the system more robust to undocumented, outdated, or poorly documented repositories and better suited for reliable, code-grounded answers.

---

## How is the mental model created

1. **Parse the repository** (tree-sitter) to capture structure.
2. **Resolve cross-file symbol references** (LSP) to capture semantics.
3. **Construct a repo graph** (Neo4j) from AST structure + LSP reference edges.
4. **Generate a “mental model”**: classify files (CRITICAL vs IGNORE) and summarize critical files with upstream/downstream interactions.
5. **Compress to global context**: traverse from entry points and assemble a repo-wide context that fits comfortably in the LLM context window.
6. **Answer questions** using the global context (no doc reliance required).

---

## Why this matters vs RAG/agents

* **RAG**: high recall is hard; you often miss the “glue” code, registry wiring, and multi-hop dependencies.
* **Agents**: can recover via iteration, but are slower, costlier, and still prone to partial views and drift.
* **Compression-first**: gives the model a **stable global view**, enabling more reliable cross-file reasoning.

---

## Architecture overview

**Stages**

* **Pre-ingestion analysis**: scans files, filters directories, estimates size/budget.
* **LSP reference extraction**: builds a persistent SQLite cache of symbol reference edges.
* **Repo graph construction**: stores nodes/edges in Neo4j.
* **Mental model generation**: uses graph queries + an LLM to produce short file-level “briefs” and criticality.
* **Repo context builder**: performs BFS from entry points to assemble a **global repo context** stored in MongoDB.

**Storage**

* **Neo4j**: repo dependency graph (AST + semantic reference edges)
* **MongoDB**: brief summaries + global repo context (document store)
* **SQLite**: LSP reference cache


## Evaluation

To demonstrate the system's ability to understand complex codebases **purely from source code** (without relying on documentation, READMEs, or markdown files), I conducted a ablation test using X's open-sourced recommendation algorithm repository (`twitter/the-algorithm`, ~1M LOC in Scala/Finagle).

### DeepWiki Ablation Test (Code-Only vs. With Documentation)

We compared our tool against **DeepWiki** (Cognition Labs / Devin-powered repository documentation and QA tool) on the same challenging questions, in two modes:

1. **Full repo (with all .md/README files)** — DeepWiki's default setting  
2. **Code-only (all *.md files removed)** — simulating real-world undocumented or sparsely documented codebases

#### Question 1: "Just tell me step by step what happens when I refresh my ForYou page."

| Mode                  | DeepWiki Response Quality                                                                 | Our Tool Response Quality                                                                 |
|-----------------------|--------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| With .md files        | Comprehensive, accurate, detailed pipeline (candidate sources, ranking, mixing rules)     | N/A                                                  |
| **Code-only (no .md)**| Shallow & incomplete — missed core components (Earlybird, TweetMixer, UTEG, heavy ranker, diversity filters, feature hydration) | **Excellent** — reconstructed full flow: parallel candidate pipelines (15+ sources incl. SimClusters, UTEG, EvergreenVideos), ~30+ feature hydrators, Phoenix/Navi heavy rankers, debunching/diversity, latency breakdown, all grounded in precise file/class references |

#### Question 2: "I'm a newcomer, how does it all work?" (high-level architecture overview)

| Mode                  | DeepWiki Response Quality                                                                 | Our Tool Response Quality                                                                 |
|-----------------------|--------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| With .md files        | Solid high-level summary, but heavily derived from top-level README.md                     | N/A                                                                              |
| **Code-only (no .md)**| N/A (test not run, but expected to degrade significantly based on prior behavior)         | **Superior** — synthesized rich overview: data ingestion, candidate sources (SimClusters ANN, RealGraph, Earlybird), ML ranking (Phoenix, Navi, ClemNet), mixing heuristics (diversity, freshness, ads), serving infra (Finagle, Manhattan, Kafka), concrete ForYou flow example, scale numbers, key tech table — all inferred purely from code structure |

### Key Takeaways
- **DeepWiki** relies heavily on human-written markdown documentation for accurate high-level reasoning and architectural synthesis. When documentation is removed, its answers become shallow, incomplete, and miss critical system components.
- Our system **excels in the code-only setting** — deriving deeper, more accurate architectural understanding directly from:
  - LSP-resolved cross-file references
  - Tree-sitter AST + pruned Neo4j graph
  - Dependency-aware critical file selection & summarization
  - BFS-ordered repo context from inferred entry points

This demonstrates a significant advantage in real-world scenarios where documentation is sparse, outdated, or absent — a common situation in large production codebases.

We believe this is a meaningful step toward more robust, doc-independent repository-level code understanding, and plan to evaluate further on benchmarks like SWE-QA in pure code-only mode.

---

## Limitations (current)

* **Compression loss**: summaries can omit critical edge-case behavior.
* **Framework “magic”**: plugin/registry systems can require additional heuristics (imports, decorators, dynamic dispatch).
* **Evidence pointers**: answers are stronger when they include file/symbol evidence; this is still being improved.
* **Taxonomy slicing**: SWE-QA question types (What/Why/Where/How) aren’t always present in scorer outputs; slicing requires joining metadata.

--- 

## Run Locally

### Prerequisites

Make sure you have the following installed:

* **Docker** and **Docker Compose**
* **`uv`** (Python package manager)

---

### Setup Environment

1. Install dependencies and create the virtual environment:

   ```bash
   uv sync
   ```

2. Activate the virtual environment:

   ```bash
   source .venv/bin/activate
   ```

3. Create your local environment file:

   ```bash
   cp .env.local.example .env.local
   ```

   Then update the following values in `.env.local`:

   * `XAI_API_KEY`
   * `NEO4J_PASSWORD`

4. Make scripts executable:

   ```bash
   chmod +x scripts/bootstrap_local.sh scripts/install_language_servers.sh
   ```

5. Bootstrap the local environment:

   ```bash
   ./scripts/bootstrap_local.sh 2>&1 | tee /tmp/bootstrap.log
   ```

6. Go to http://localhost:8000/docs#/ to check if the CodeSense API is up.

7. Run `git clone https://github.com/bigrewal/code-sense-ui` and point it to the API:

   ```bash
   npm install
   Create .env.local and set VITE_API_BASE=http://localhost:8000
   npm run dev
   ```

---

### Shutdown Services

To stop and remove Neo4j and MongoDB containers (including volumes):

```bash
docker compose down -v
```

---

**Hard requirements**

* The following language servers **must** be installed or bootstrap will fail:

  * `jdtls` (Java)
  * `pylsp` (Python)
  * `rust-analyzer` (Rust)
  * `metals` (Scala)

**Endpoints**

* API: [http://localhost:8000](http://localhost:8000)
* Neo4j UI: [http://localhost:7474](http://localhost:7474)
* MongoDB: mongodb://localhost:27017

---


## License

[MIT / Apache-2.0 / Proprietary — choose one]

---

## Contact

* [Bimal Grewal]
* [Email / Twitter / LinkedIn]
* [Optional: short demo link]

---