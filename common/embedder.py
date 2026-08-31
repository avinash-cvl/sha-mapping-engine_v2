"""Text embeddings via Azure OpenAI (text-embedding-3-small), with an
on-disk cache keyed by content hash so repeat runs don't re-pay API cost
for unchanged rows.

Deliberately no local/HuggingFace model here -- CPU-heavy local inference
was tried and explicitly ruled out; every embedding call goes to Azure.
"""
from __future__ import annotations

import hashlib
import os

import numpy as np
from langchain_openai import AzureOpenAIEmbeddings

import common.config as C

_embeddings: AzureOpenAIEmbeddings | None = None


def _get_embeddings() -> AzureOpenAIEmbeddings:
    global _embeddings
    if _embeddings is None:
        _embeddings = AzureOpenAIEmbeddings(
            azure_endpoint=C.AZURE_OPENAI_ENDPOINT,
            api_key=C.AZURE_OPENAI_API_KEY,
            api_version=C.AZURE_OPENAI_API_VERSION,
            azure_deployment=C.AZURE_EMBEDDING_DEPLOYMENT,
        )
    return _embeddings


def _cache_path(text: str, tag: str) -> str:
    key = hashlib.md5(f"{C.AZURE_EMBEDDING_DEPLOYMENT}:{tag}:{text}".encode()).hexdigest()
    return os.path.join(C.CACHE_DIR, f"{key}.npy")


def embed_one(text: str, tag: str = "default") -> list[float]:
    return embed_many([text], tag=tag)[0]


def embed_many(texts: list[str], tag: str = "default") -> list[list[float]]:
    os.makedirs(C.CACHE_DIR, exist_ok=True)

    results: list[list[float] | None] = [None] * len(texts)
    to_encode: list[str] = []
    to_encode_idx: list[int] = []

    for i, text in enumerate(texts):
        path = _cache_path(text, tag)
        if os.path.exists(path):
            results[i] = np.load(path).tolist()
        else:
            to_encode.append(text)
            to_encode_idx.append(i)

    if to_encode:
        vectors = _get_embeddings().embed_documents(to_encode)
        for idx, vec in zip(to_encode_idx, vectors):
            results[idx] = vec
            np.save(_cache_path(texts[idx], tag), np.array(vec))

    return [r for r in results if r is not None]
