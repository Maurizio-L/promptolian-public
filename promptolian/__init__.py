"""Promptolian — prompt compression engine."""

__version__ = "2.3.0"
__author__  = "Maurizio Lospi"

from .compress import compress, compress_messages, CompressionStats, count_tokens
from .patch    import patch_anthropic, patch_openai, get_stats

__all__ = [
    "compress",
    "compress_messages",
    "CompressionStats",
    "count_tokens",
    "patch_anthropic",
    "patch_openai",
    "get_stats",
]