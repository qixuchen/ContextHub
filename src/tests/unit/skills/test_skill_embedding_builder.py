from __future__ import annotations

from dataclasses import replace

import pytest

from core.skills.skill_embedding_builder import SkillEmbeddingBuilder, skill_vector_id
from core.skills.skill_loader import SkillDoc

pytestmark = pytest.mark.unit


class _FakeVectorStore:
    def __init__(self) -> None:
        self.upserts: list[dict] = []
        self.deletes: list[str] = []

    def get_metadatas(self, ids: list[str]) -> dict[str, dict]:
        return {}

    def upsert_embeddings(self, records: list[dict]) -> None:
        self.upserts.extend(records)

    def delete_embeddings(self, ids: list[str]) -> None:
        self.deletes.extend(ids)

    def query(self, embedding: list[float], top_k: int, filters: dict | None = None) -> list[dict]:
        return []


def _doc(skill_name: str, desc: str) -> SkillDoc:
    return SkillDoc(
        skill_name=skill_name,
        description=desc,
        root_path=f"/tmp/{skill_name}",
        skill_md_path=f"/tmp/{skill_name}/SKILL.md",
        content_sha256=f"sha-{desc}",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def test_skill_embedding_builder_incremental_upsert_and_delete() -> None:
    fake = _FakeVectorStore()
    calls = {"n": 0}

    def _embed(text: str) -> list[float]:
        calls["n"] += 1
        return [float(len(text)), 1.0]

    builder = SkillEmbeddingBuilder(
        vector_store=fake,
        embedding_model="dummy",
        api_key="dummy",
        embedding_fn=_embed,
    )
    docs = [_doc("skill-a", "a"), _doc("skill-c", "ccc")]
    prev = {
        "skill-a": {"vector_id": skill_vector_id("skill-a"), "content_sha256": "sha-a"},
        "skill-b": {"vector_id": skill_vector_id("skill-b"), "content_sha256": "sha-b"},
    }
    out = builder.build_incremental(
        docs=docs,
        previous_state=prev,
        max_workers=4,
        dry_run=False,
        full_rebuild=False,
    )
    assert out["scanned_skills"] == 2
    assert out["changed_skills"] == 1
    assert out["removed_skills"] == 1
    assert out["upserted_vectors"] == 1
    assert out["deleted_vectors"] == 1
    assert calls["n"] == 1
    assert fake.deletes == [skill_vector_id("skill-b")]
    assert set(out["state"].keys()) == {"skill-a", "skill-c"}


def test_skill_embedding_builder_dry_run_has_no_writes() -> None:
    fake = _FakeVectorStore()
    builder = SkillEmbeddingBuilder(
        vector_store=fake,
        embedding_model="dummy",
        api_key="dummy",
        embedding_fn=lambda text: [float(len(text))],
    )
    doc = _doc("skill-a", "new")
    prev_doc = replace(doc, content_sha256="old-hash")
    prev = {"skill-a": {"vector_id": skill_vector_id("skill-a"), "content_sha256": prev_doc.content_sha256}}
    out = builder.build_incremental(
        docs=[doc],
        previous_state=prev,
        max_workers=1,
        dry_run=True,
        full_rebuild=False,
    )
    assert out["changed_skills"] == 1
    assert out["upserted_vectors"] == 0
    assert out["deleted_vectors"] == 0
    assert fake.upserts == []
    assert fake.deletes == []

