"""The extractor (docs/design.md 5.2): documents to claims, through Lexora."""

from verimend.extractor.chunking import Chunk, chunk_document, chunk_hash
from verimend.extractor.client import Completion, LexoraClient, LLMClient, LLMHTTPError, LLMTransportError
from verimend.extractor.extract import (
    ChunkFailure,
    Document,
    DocumentExtraction,
    ExtractionSession,
    ExtractorAborted,
    ExtractorConfigError,
    extractor_key,
    load_prompt,
)
from verimend.extractor.stage import extract_documents


def session_from_settings(settings, **kwargs) -> ExtractionSession:
    """An ``ExtractionSession`` on Lexora as configured. Raises ``ExtractorConfigError`` without a URL.

    Called before a single chunk is built, so an unset URL fails the run
    before any work is done.
    """
    if not settings.lexora_url:
        raise ExtractorConfigError("lexora_url_unset")
    client = LexoraClient(settings.lexora_url, settings.lexora_timeout_s)
    return ExtractionSession(
        client, tier=settings.extractor_tier, retry_after_max_s=settings.lexora_retry_after_max_s, **kwargs
    )


__all__ = [
    "Chunk",
    "ChunkFailure",
    "Completion",
    "Document",
    "DocumentExtraction",
    "ExtractionSession",
    "ExtractorAborted",
    "ExtractorConfigError",
    "LLMClient",
    "LLMHTTPError",
    "LLMTransportError",
    "LexoraClient",
    "chunk_document",
    "chunk_hash",
    "extract_documents",
    "extractor_key",
    "load_prompt",
    "session_from_settings",
]
