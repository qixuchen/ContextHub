"""Incremental embedding builder for skills."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from openai import OpenAI

from core.skills.skill_loader import SkillDoc
from infra.storage.vector.base import VectorStoreAdapter


def skill_vector_id(skill_name: str) -> str:
    return f"skill::{skill_name}::description"


def load_skill_embedding_state(path: str) -> dict[str, dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    skills = payload.get("skills")
    if isinstance(skills, dict):
        return {str(k): v for k, v in skills.items() if isinstance(v, dict)}
    # Backward-compat: allow direct map format.
    return {str(k): v for k, v in payload.items() if isinstance(v, dict)}


def save_skill_embedding_state(path: str, state: dict[str, dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(UTC).isoformat(),
        "skills": state,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class SkillEmbeddingBuilder:
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

    def build_incremental(
        self,
        *,
        docs: list[SkillDoc],
        previous_state: dict[str, dict[str, Any]],
        max_workers: int,
        dry_run: bool = False,
        full_rebuild: bool = False,
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        prev = dict(previous_state or {})
        current = {d.skill_name: d for d in docs}
        if full_rebuild:
            changed_docs = list(docs)
        else:
            changed_docs = []
            for d in docs:
                old = prev.get(d.skill_name) or {}
                if str(old.get("content_sha256") or "") != d.content_sha256:
                    changed_docs.append(d)
        removed_names = sorted([name for name in prev if name not in current])

        upsert_records: list[dict[str, Any]] = []
        errors: list[str] = []
        changed_names = {d.skill_name for d in changed_docs}
        success_names: set[str] = set()
        if not dry_run and changed_docs:
            workers = max(1, min(int(max_workers), len(changed_docs)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                fut_map = {pool.submit(self._embed, doc.description): doc for doc in changed_docs}
                for fut in as_completed(fut_map):
                    doc = fut_map[fut]
                    try:
                        emb = fut.result()
                        upsert_records.append(
                            {
                                "id": skill_vector_id(doc.skill_name),
                                "embedding": emb,
                                "metadata": {
                                    "source_type": "skill",
                                    "skill_name": doc.skill_name,
                                    "description": doc.description,
                                    "uri": f"ctx://skills/{doc.skill_name}",
                                    "path": doc.root_path,
                                    "content_sha256": doc.content_sha256,
                                    "updated_at": now,
                                },
                            }
                        )
                        success_names.add(doc.skill_name)
                    except Exception as exc:
                        errors.append(
                            f"embed failed for '{doc.skill_name}': {type(exc).__name__}: {exc}"
                        )
            if upsert_records:
                self.vector_store.upsert_embeddings(upsert_records)

        delete_ids = [str((prev.get(name) or {}).get("vector_id") or skill_vector_id(name)) for name in removed_names]
        deleted_count = 0
        if not dry_run and delete_ids:
            self.vector_store.delete_embeddings(delete_ids)
            deleted_count = len(delete_ids)

        next_state: dict[str, dict[str, Any]] = {}
        for d in docs:
            old = prev.get(d.skill_name) or {}
            if d.skill_name in changed_names and (not dry_run) and d.skill_name not in success_names:
                # Keep previous snapshot on failed updates so next run retries this skill.
                if old:
                    next_state[d.skill_name] = dict(old)
                continue
            next_state[d.skill_name] = {
                "skill_name": d.skill_name,
                "vector_id": skill_vector_id(d.skill_name),
                "content_sha256": d.content_sha256,
                "updated_at": now if (full_rebuild or d in changed_docs) else str(old.get("updated_at") or now),
            }

        return {
            "scanned_skills": len(docs),
            "changed_skills": len(changed_docs),
            "removed_skills": len(removed_names),
            "upserted_vectors": len(upsert_records) if not dry_run else 0,
            "deleted_vectors": deleted_count,
            "errors": errors,
            "state": next_state,
            "removed_skill_names": removed_names,
        }

