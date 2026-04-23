"""Skill retrieval against precomputed skill embedding index."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import httpx
from openai import OpenAI

from infra.storage.vector.base import VectorStoreAdapter


def _distance_to_score(distance: float | int | None) -> float:
    if distance is None:
        return 0.0
    d = float(distance)
    if d < 0:
        d = 0.0
    return 1.0 / (1.0 + d)


@dataclass
class SkillHit:
    skill_name: str
    description: str
    uri: str
    path: str
    score: float


@dataclass
class SkillRetriever:
    vector_store: VectorStoreAdapter
    embedding_model: str
    api_key: str
    embedder_base_url: str | None = None
    embedding_mode: str = "multimodal"
    embedding_fn: Callable[[str], list[float]] | None = None

    def _embed_text(self, text: str) -> list[float]:
        client = OpenAI(api_key=self.api_key, base_url=self.embedder_base_url or None)
        resp = client.embeddings.create(model=self.embedding_model, input=text)
        return [float(x) for x in resp.data[0].embedding]

    def _embed_multimodal(self, text: str) -> list[float]:
        if not self.embedder_base_url:
            raise ValueError("AMC_EMBEDDING_BASE_URL is required for multimodal embedding mode")
        endpoint = f"{self.embedder_base_url.rstrip('/')}/embeddings/multimodal"
        payload: dict[str, Any] = {
            "model": self.embedding_model,
            "input": [{"type": "text", "text": text}],
            "encoding_format": "float",
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        row = data.get("data")
        if isinstance(row, dict):
            emb = row.get("embedding")
            if isinstance(emb, list):
                return [float(x) for x in emb]
        if isinstance(row, list) and row and isinstance(row[0], dict):
            emb = row[0].get("embedding")
            if isinstance(emb, list):
                return [float(x) for x in emb]
        raise RuntimeError("invalid multimodal embedding response shape: missing embedding vector")

    def _embed(self, text: str) -> list[float]:
        if self.embedding_fn is not None:
            return self.embedding_fn(text)
        mode = (self.embedding_mode or "multimodal").strip().lower()
        if mode == "text":
            return self._embed_text(text)
        if mode == "multimodal":
            return self._embed_multimodal(text)
        raise ValueError(f"unsupported embedding mode: {self.embedding_mode}")

    def recall(self, *, query_text: str, top_k: int, score_threshold: float) -> list[SkillHit]:
        vec = self._embed(query_text)
        rows = self.vector_store.query(
            vec,
            top_k=max(int(top_k) * 4, 20),
            filters={"source_type": "skill"},
        )
        grouped: dict[str, SkillHit] = {}
        for row in rows:
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            skill_name = str(meta.get("skill_name") or "").strip()
            if not skill_name:
                continue
            score = _distance_to_score(row.get("distance"))
            old = grouped.get(skill_name)
            if old is not None and old.score >= score:
                continue
            grouped[skill_name] = SkillHit(
                skill_name=skill_name,
                description=str(meta.get("description") or ""),
                uri=str(meta.get("uri") or ""),
                path=str(meta.get("path") or ""),
                score=score,
            )
        out = [h for h in grouped.values() if float(h.score) >= float(score_threshold)]
        out.sort(key=lambda x: float(x.score), reverse=True)
        return out[: max(1, int(top_k))]

