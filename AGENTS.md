# Simbioclip — Agent Summary

## Project Overview
Simbioclip: FastAPI app with LangChain Agent SDK, MCP server, and RAG-powered chat.

## Architecture
- `app/` — FastAPI routes, config, settings store, writable config
- `simbioclip/` — Agent SDK integration, MCP server, agent logic (`agents.py`)
- `data/` — Runtime data (config.json, chroma_db/, anchored_summary.md)

## What We Did

### 1. RAG System (`simbioclip/embeddings.py`)
- Loads `.txt` files from `data/documents/` via `DirectoryLoader`
- Splits text with `RecursiveCharacterTextSplitter` (chunk_size=1000, overlap=200)
- Embeds + stores in ChromaDB (`data/chroma_db/`)
- OpenAI embeddings via `env OPENAI_API_KEY`
- `RAGManager` class wraps search/retrieval

### 2. Agent Integration (`simbioclip/agents.py`)
- `SimbioclipAgent._get_relevant_context(query)` queries ChromaDB, returns top-k docs
- `_build_prompt()` injects context into system prompt
- Uses `ChatOpenAI` (gpt-4o-mini) with LangChain Agent SDK
- Created `app/__init__.py` to make it a proper package

### 3. Settings
- `settings.yml`: `rag.top_k`, `rag.chunk_size`, `rag.chunk_overlap`, `rag.embeddings_model`, `rag.document_path`
- `app/settings.py`: Pydantic `Settings` model loads from settings.yml
- `OPENAI_API_KEY` loaded from env var

### 4. Bug Fix — Circular Import
**Fixed at `app/writable_config.py:_get_path()`:**
- **Problem:** Circular import chain: `writable_config._get_path()` → `from app.config import DATA_DIR` → `app.config.__getattr__` → `settings_store.get_settings()` → `load_settings()` → `user_config.get_cached()` → `writable_config.load()` → `_get_path()` → ∞ recursion
- **Fix:** Inline `DATA_DIR` computation directly in `_get_path()` instead of importing from `app.config`:
  ```python
  base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  data_dir = os.getenv("DATA_DIR", os.path.join(base, "data"))
  ```

### 5. Project State (as of last session)
- Import chain clean: `from simbioclip.agents import simbioclip_agent` works
- Circular import is resolved
- RAG settings exist in `settings.yml` but untested at runtime
- Next step: run the app and verify RAG + agent pipeline end-to-end

## Key Files
| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI entry point |
| `app/config.py` | Runtime config (lazy settings) |
| `app/writable_config.py` | JSON config file read/write |
| `app/settings.py` | Pydantic Settings model |
| `app/settings_store.py` | Settings initialization |
| `simbioclip/agents.py` | Agent logic (FakeLLM, SimbioclipAgent) |
| `simbioclip/embeddings.py` | RAG: ChromaDB + OpenAI embeddings |
| `simbioclip/mcp.py` | MCP server setup |
| `settings.yml` | User-facing settings |
