"""
scripts/build_kg.py — Pipeline for ingesting documents from data/generated/ into LightRAG
with ingestion-time structural entity identity resolution.

Usage:
    python scripts/build_kg.py [--input-dir PATH] [--working-dir PATH]
                               [--limit N] [--batch-size N] [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
from dotenv import load_dotenv
from google.genai import errors as genai_errors
from lightrag import LightRAG
from lightrag.kg.shared_storage import initialize_pipeline_status
from lightrag.llm.gemini import gemini_complete_if_cache, gemini_embed
from lightrag.prompt import PROMPTS
from lightrag.utils import EmbeddingFunc

# ---------------------------------------------------------------------------
# Model Constants
# ---------------------------------------------------------------------------
LLM_MODEL = "gemini-3.5-flash-lite"
EMBED_MODEL = "gemini-embedding-2"
EMBED_DIM = 3072
EMBED_MAX_TOKENS = 8192

# Keys from LightRAG kwargs that should not be passed directly to Gemini API
_LIGHTRAG_INTERNAL_KEYS = frozenset({
    "hashing_kv",
    "mode",
    "response_format",
    "stream",
    "keyword_extraction",
    "entity_extraction",
    "current_entity_types",
    "addon_params",
    "prompt_name",
})

# Safe keys to pass to Gemini API
_GEMINI_PASSTHROUGH_KEYS = frozenset({
    "response_format",
    "keyword_extraction",
    "entity_extraction",
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("build_kg")


# ---------------------------------------------------------------------------
# Entity Resolution & Disambiguation Engine
# ---------------------------------------------------------------------------

def _load_entity_registry(registry_path: Path = Path("data/entity_registry.json")) -> dict[str, Any]:
    """Load canonical entity registry master data at runtime if available."""
    if not registry_path.exists():
        return {}
    try:
        with registry_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read entity registry '%s': %s", registry_path, exc)
        return {}


class EntityResolver:
    """
    Performs runtime structural entity key resolution during ingestion.

    Uses data/entity_registry.json master data when available, supplemented by
    contextual alias/designation extraction for unregistered and held-out entities.
    """

    def __init__(self, registry_data: dict[str, Any] | None = None) -> None:
        self.registry = registry_data if registry_data is not None else _load_entity_registry()
        self._canonical_alias_map: dict[str, str] = {}
        self._registry_entities: list[dict[str, Any]] = []
        self._build_registry_index()

    def _normalize(self, text: str) -> str:
        if not text:
            return ""
        text = text.lower()
        text = re.sub(r"[^\w\s]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _build_registry_index(self) -> None:
        if not self.registry:
            return

        categories = ["houses", "megacorporations", "persons", "stations_and_ships"]
        for cat in categories:
            for entity in self.registry.get(cat, []):
                self._registry_entities.append(entity)
                name = entity.get("name", "")
                aliases = entity.get("aliases", [])

                norm_name = self._normalize(name)
                # Preferred canonical target key: distinct alias if present, else name
                target_key = aliases[0] if (aliases and entity.get("is_homonym_risk")) else name

                if norm_name:
                    self._canonical_alias_map[norm_name] = target_key

                for alias in aliases:
                    norm_alias = self._normalize(alias)
                    if norm_alias:
                        self._canonical_alias_map[norm_alias] = alias

    def resolve_entity(
        self,
        raw_name: str,
        entity_type: str,
        description: str,
        context_text: str = "",
    ) -> str:
        """Resolves a raw extracted entity name to a canonical structural key."""
        if not raw_name or not raw_name.strip():
            return raw_name

        norm_name = self._normalize(raw_name)

        # 1. Check master registry
        if norm_name in self._canonical_alias_map:
            candidates = [
                e for e in self._registry_entities
                if self._normalize(e.get("name", "")) == norm_name
                or any(self._normalize(a) == norm_name for a in e.get("aliases", []))
            ]
            if len(candidates) == 1:
                e = candidates[0]
                aliases = e.get("aliases", [])
                return aliases[0] if (aliases and e.get("is_homonym_risk")) else e.get("name", raw_name)
            elif len(candidates) > 1:
                # Disambiguate registry homonyms using type/description evidence
                combined_desc = (description + " " + context_text).lower()
                for e in candidates:
                    e_type = (e.get("type", "") + " " + e.get("category", "")).lower()
                    e_desc = e.get("description", "").lower()
                    keywords = set(e_type.split() + e_desc.split())
                    keywords = {k for k in keywords if len(k) >= 4}
                    if any(kw in combined_desc for kw in keywords):
                        aliases = e.get("aliases", [])
                        return aliases[0] if aliases else e.get("name", raw_name)

        # 2. Evidence-Based Disambiguation for entities absent from registry
        combined_info = description + "\n" + context_text

        # Regex patterns for explicit designations or aliases in text (e.g., in parentheses)
        alias_patterns = [
            r'(?:алиас|alias|позывной|callsign|реестровый алиас|code)\s*[:\-\—]?\s*[\*\`"]?([A-Za-z0-9\-\_]+(?:-[A-Za-z0-9\-\_]+)*)[\*\`"]?',
            r'\b([A-Z0-9]{2,}\-[A-Za-z0-9\-]+)\b',
        ]
        for pat in alias_patterns:
            matches = re.findall(pat, combined_info, re.IGNORECASE)
            for m in matches:
                norm_m = self._normalize(m)
                if norm_m and (norm_name in norm_m or norm_m in norm_name or len(m) >= 5):
                    return m.strip()

        return raw_name.strip()

    def canonicalize_extraction_response(
        self,
        response_text: str,
        input_text: str = "",
        tuple_delimiter: str = "<|#|>",
    ) -> str:
        """
        Parses LightRAG extraction output tuples, canonicalizes entity names,
        and rewrites relation endpoints to match.
        """
        if not response_text:
            return response_text

        lines = response_text.splitlines()
        name_updates: dict[str, str] = {}
        updated_lines: list[str] = []

        for line in lines:
            stripped = line.strip()
            if not stripped or tuple_delimiter not in stripped:
                updated_lines.append(line)
                continue

            parts = stripped.split(tuple_delimiter)
            rec_type = parts[0].strip()

            if rec_type == "entity" and len(parts) >= 4:
                raw_name = parts[1].strip()
                ent_type = parts[2].strip()
                ent_desc = parts[3].strip()

                resolved_name = self.resolve_entity(
                    raw_name=raw_name,
                    entity_type=ent_type,
                    description=ent_desc,
                    context_text=input_text,
                )
                if resolved_name and resolved_name != raw_name:
                    name_updates[raw_name] = resolved_name
                    name_updates[self._normalize(raw_name)] = resolved_name

                parts[1] = resolved_name if resolved_name else raw_name
                updated_lines.append(tuple_delimiter.join(parts))

            elif rec_type in ("relation", "relationship") and len(parts) >= 5:
                src_name = parts[1].strip()
                tgt_name = parts[2].strip()

                new_src = name_updates.get(src_name, name_updates.get(self._normalize(src_name), src_name))
                new_tgt = name_updates.get(tgt_name, name_updates.get(self._normalize(tgt_name), tgt_name))

                parts[1] = new_src
                parts[2] = new_tgt
                updated_lines.append(tuple_delimiter.join(parts))
            else:
                updated_lines.append(line)

        return "\n".join(updated_lines)


_GLOBAL_RESOLVER: EntityResolver | None = None


def _get_entity_resolver() -> EntityResolver:
    global _GLOBAL_RESOLVER
    if _GLOBAL_RESOLVER is None:
        _GLOBAL_RESOLVER = EntityResolver()
    return _GLOBAL_RESOLVER


# ---------------------------------------------------------------------------
# LLM Function Wrapper
# ---------------------------------------------------------------------------

async def llm_model_func(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> str:
    """
    Wrapper over gemini_complete_if_cache for LightRAG with entity canonicalization.
    """
    safe_kwargs: dict[str, Any] = {
        key: value
        for key, value in kwargs.items()
        if key in _GEMINI_PASSTHROUGH_KEYS
    }

    is_extraction = kwargs.get("entity_extraction") or ("---Input Text---" in prompt)

    response = await _call_with_429_retry(
        "llm",
        _LLM_LIMITER,
        lambda: gemini_complete_if_cache(
            LLM_MODEL,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            **safe_kwargs,
        ),
    )

    if is_extraction and response:
        input_text = ""
        match = re.search(r"---Input Text---\s*```(?:[a-zA-Z]*)\n(.*?)```", prompt, re.DOTALL)
        if match:
            input_text = match.group(1)

        resolver = _get_entity_resolver()
        response = resolver.canonicalize_extraction_response(
            response_text=response,
            input_text=input_text,
        )

    return response


# ---------------------------------------------------------------------------
# Rate Limiter & Retry Logic
# ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding 60-second window rate limiter."""

    def __init__(self, limit: int, name: str) -> None:
        self.limit = limit
        self.name = name
        self._lock = asyncio.Lock()
        self._calls: deque[float] = deque()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 60.0:
                    self._calls.popleft()
                if len(self._calls) < self.limit:
                    self._calls.append(now)
                    return
                sleep_for = 60.0 - (now - self._calls[0]) + 0.05
            logger.debug("%s: limit %d/min reached, waiting %.1f s", self.name, self.limit, sleep_for)
            await asyncio.sleep(max(sleep_for, 0.05))


_EMBED_LIMITER = RateLimiter(limit=45, name="embedding")
_LLM_LIMITER = RateLimiter(limit=12, name="llm")
_EMBED_SEMAPHORE = asyncio.Semaphore(2)


def _retry_delay_from_error(exc: Exception, attempt: int) -> float:
    """Extracts retryDelay from Gemini 429 response or computes backoff."""
    match = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", str(exc))
    if match:
        return float(match.group(1)) + 1.0
    return min(2.0 * (2 ** attempt), 60.0)


async def _call_with_429_retry(
    what: str,
    limiter: RateLimiter,
    call: Any,
    attempts: int = 6,
) -> Any:
    """Executes API call under rate limit and handles 429 retries."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        await limiter.acquire()
        try:
            return await call()
        except genai_errors.ClientError as exc:
            if getattr(exc, "code", None) != 429:
                raise
            last_exc = exc
            delay = _retry_delay_from_error(exc, attempt)
        logger.warning("%s: 429 error, attempt %d/%d, waiting %.1f s", what, attempt + 1, attempts, delay)
        await asyncio.sleep(delay)
    raise RuntimeError(f"{what}: failed after {attempts} attempts: {last_exc}")


async def embed_texts_one_by_one(
    texts: list[str],
    max_token_size: int | None = None,
    context: str = "document",
    **kwargs: Any,
) -> np.ndarray:
    """Embeds list of texts one by one to avoid batch mismatch."""
    async def one(text: str) -> np.ndarray:
        async with _EMBED_SEMAPHORE:
            return await _call_with_429_retry(
                "embedding",
                _EMBED_LIMITER,
                lambda: gemini_embed.func(
                    [text],
                    model=EMBED_MODEL,
                    api_key=os.environ["GEMINI_API_KEY"],
                    max_token_size=max_token_size,
                    context=context,
                    **kwargs,
                ),
            )

    parts = await asyncio.gather(*(one(text) for text in texts))
    return np.vstack(parts)


# ---------------------------------------------------------------------------
# RAG Factory
# ---------------------------------------------------------------------------

async def create_rag(working_dir: str = "rag_storage") -> LightRAG:
    """
    Assembles and initializes LightRAG instance.
    """
    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not found in environment.")

    # Inject entity disambiguation guidance into LightRAG default prompt template
    disambiguation_rule = (
        "\n- Disambiguation & Specificity: Entity names must uniquely identify the entity. "
        "If an entity has a specific designation code, official alias, or title stated in the text "
        "(such as an alphanumeric registry code or specific ship name given in parentheses or text), "
        "use that specific designation as the entity_name. If a generic name is shared across different entity types "
        "or owners, qualify the entity_name with its specific type or owner in parentheses to prevent identity collisions.\n"
    )
    if "Disambiguation & Specificity" not in PROMPTS.get("default_entity_types_guidance", ""):
        PROMPTS["default_entity_types_guidance"] = (
            PROMPTS.get("default_entity_types_guidance", "") + disambiguation_rule
        )

    embedding_func = EmbeddingFunc(
        embedding_dim=EMBED_DIM,
        max_token_size=EMBED_MAX_TOKENS,
        func=embed_texts_one_by_one,
        model_name=EMBED_MODEL,
        supports_asymmetric=True,
    )

    rag = LightRAG(
        working_dir=working_dir,
        llm_model_func=llm_model_func,
        llm_model_name=LLM_MODEL,
        embedding_func=embedding_func,
        llm_model_max_async=4,
        embedding_batch_num=10,
        embedding_func_max_async=2,
        max_parallel_insert=3,
        summary_max_tokens=1200,
    )

    await rag.initialize_storages()
    await initialize_pipeline_status()

    return rag


# ---------------------------------------------------------------------------
# Ingestion Journal & Storage Verification
# ---------------------------------------------------------------------------

def _load_ingested_log(log_path: Path) -> dict[str, str]:
    if log_path.exists():
        try:
            with log_path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read log %s: %s", log_path, exc)
    return {}


def _save_ingested_log(log_path: Path, log: dict[str, str]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as fh:
        json.dump(log, fh, ensure_ascii=False, indent=2)


def _file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _report_doc_statuses(working_dir: Path) -> bool:
    status_file = working_dir / "kv_store_doc_status.json"
    if not status_file.exists():
        logger.warning("Status file %s not found.", status_file)
        return False

    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s: %s", status_file, exc)
        return False

    counts: dict[str, int] = {}
    for record in data.values():
        status = str(record.get("status", "unknown")).lower()
        counts[status] = counts.get(status, 0) + 1

    logger.info("Document statuses: %s", counts or "empty")
    bad = {s: c for s, c in counts.items() if s != "processed"}
    if bad:
        logger.error("Not all documents processed: %s", bad)
        return False
    return True


def _verify_storage(working_dir: Path) -> None:
    storage_files = list(working_dir.rglob("*"))
    nonempty = [f for f in storage_files if f.is_file() and f.stat().st_size > 0]
    total_bytes = sum(f.stat().st_size for f in nonempty)

    logger.info("=== Storage verification: %s ===", working_dir)
    if nonempty:
        logger.info(
            "Found %d non-empty files, total volume: %.1f KB",
            len(nonempty),
            total_bytes / 1024,
        )
    else:
        logger.warning("No non-empty storage files found in '%s'.", working_dir)


# ---------------------------------------------------------------------------
# Ingestion Pipeline & CLI
# ---------------------------------------------------------------------------

async def run_ingestion(
    input_dir: Path,
    working_dir: Path,
    limit: int | None,
    batch_size: int,
    dry_run: bool,
    force: bool,
) -> None:
    start_ts = time.monotonic()

    all_files: list[Path] = sorted(input_dir.glob("*.md"))
    if not all_files:
        logger.warning("No .md files found in '%s'.", input_dir)
        return

    if limit is not None:
        all_files = all_files[:limit]

    logger.info("Found %d files in '%s'.", len(all_files), input_dir)
    log_path = working_dir / "ingested.json"

    if dry_run:
        logger.info("=== DRY-RUN ===")
        for fp in all_files:
            logger.info("  [dry-run] %s", fp.name)
        return

    ingested_log: dict[str, str] = {} if force else _load_ingested_log(log_path)

    pending: list[tuple[Path, str, str]] = []
    for fp in all_files:
        try:
            content = fp.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Could not read '%s': %s — skipping.", fp, exc)
            continue
        sha = _file_hash(content)
        key = str(fp)
        if not force and ingested_log.get(key) == sha:
            continue
        pending.append((fp, content, sha))

    if not pending:
        logger.info("Nothing to ingest.")
        _verify_storage(working_dir)
        return

    logger.info("Initializing LightRAG (working_dir='%s')…", working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)
    rag = await create_rag(working_dir=str(working_dir))

    total_ingested = 0
    num_batches = (len(pending) + batch_size - 1) // batch_size

    try:
        for batch_idx in range(0, len(pending), batch_size):
            batch = pending[batch_idx : batch_idx + batch_size]
            batch_num = batch_idx // batch_size + 1

            texts = [item[1] for item in batch]
            file_paths = [str(item[0]) for item in batch]
            names = [item[0].name for item in batch]

            logger.info("Batch %d/%d ingesting: %s", batch_num, num_batches, ", ".join(names))

            try:
                await rag.ainsert(texts, file_paths=file_paths)
                for fp, _content, sha in batch:
                    ingested_log[str(fp)] = sha
                _save_ingested_log(log_path, ingested_log)

                total_ingested += len(batch)
            except Exception as exc:
                logger.error("Error ingesting batch %d/%d: %s", batch_num, num_batches, exc, exc_info=True)
    finally:
        await rag.finalize_storages()

    elapsed = time.monotonic() - start_ts
    logger.info("Ingestion completed: %d/%d files in %.1f s", total_ingested, len(pending), elapsed)
    _verify_storage(working_dir)
    _report_doc_statuses(working_dir)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest documents into LightRAG with structural identity.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input-dir", type=Path, default=Path("data/generated"))
    parser.add_argument("--working-dir", type=Path, default=Path("rag_storage"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--force", action="store_true", default=False)
    return parser


def main() -> None:
    load_dotenv()
    parser = _build_arg_parser()
    args = parser.parse_args()

    if not args.input_dir.exists():
        logger.error("Input directory '%s' does not exist.", args.input_dir)
        sys.exit(1)

    asyncio.run(
        run_ingestion(
            input_dir=args.input_dir,
            working_dir=args.working_dir,
            limit=args.limit,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            force=args.force,
        )
    )


if __name__ == "__main__":
    main()
