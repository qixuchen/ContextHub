from __future__ import annotations

import threading

import pytest

from core.commit.dataflow_llm import LLMDataflowExtractor

pytestmark = pytest.mark.unit


def test_extract_runs_dataflow_and_reasoning_in_parallel() -> None:
    extractor = LLMDataflowExtractor(api_key="dummy", model="dummy-model")
    barrier = threading.Barrier(2, timeout=1.0)

    def _fake_dataflow(*, nodes, threshold, top_k_per_dst, trace_sink=None):  # noqa: ANN001, ANN202
        barrier.wait()
        extractor._record_trace(
            call_type="dataflow",
            threshold=threshold,
            top_k_per_dst=top_k_per_dst,
            raw_response_text='{"dataflow_edges":[{"src_node_id":"n1","dst_node_id":"n2"}]}',
            parsed_result={"dataflow_edges": [{"src_node_id": "n1", "dst_node_id": "n2"}]},
            trace_sink=trace_sink,
        )
        return [{"src_node_id": "n1", "dst_node_id": "n2"}]

    def _fake_reasoning(*, nodes, threshold, top_k_per_dst, trace_sink=None):  # noqa: ANN001, ANN202
        barrier.wait()
        extractor._record_trace(
            call_type="reasoning",
            threshold=threshold,
            top_k_per_dst=top_k_per_dst,
            raw_response_text='{"reasoning_edges":[{"src_node_id":"n1","dst_node_id":"n2"}]}',
            parsed_result={"reasoning_edges": [{"src_node_id": "n1", "dst_node_id": "n2"}]},
            trace_sink=trace_sink,
        )
        return [{"src_node_id": "n1", "dst_node_id": "n2"}]

    extractor._extract_dataflow = _fake_dataflow
    extractor._extract_reasoning = _fake_reasoning

    out = extractor.extract(nodes=[{"node_id": "n1"}, {"node_id": "n2"}], threshold=0.7, top_k_per_dst=2)
    assert out["dataflow_edges"] == [{"src_node_id": "n1", "dst_node_id": "n2"}]
    assert out["reasoning_edges"] == [{"src_node_id": "n1", "dst_node_id": "n2"}]
    assert [item["call_type"] for item in extractor.last_traces] == ["dataflow", "reasoning"]


def test_extract_keeps_other_branch_when_one_branch_fails() -> None:
    extractor = LLMDataflowExtractor(api_key="dummy", model="dummy-model")

    def _bad_dataflow(*, nodes, threshold, top_k_per_dst, trace_sink=None):  # noqa: ANN001, ANN202
        raise RuntimeError("boom")

    def _ok_reasoning(*, nodes, threshold, top_k_per_dst, trace_sink=None):  # noqa: ANN001, ANN202
        extractor._record_trace(
            call_type="reasoning",
            threshold=threshold,
            top_k_per_dst=top_k_per_dst,
            raw_response_text='{"reasoning_edges":[{"src_node_id":"a","dst_node_id":"b"}]}',
            parsed_result={"reasoning_edges": [{"src_node_id": "a", "dst_node_id": "b"}]},
            trace_sink=trace_sink,
        )
        return [{"src_node_id": "a", "dst_node_id": "b"}]

    extractor._extract_dataflow = _bad_dataflow
    extractor._extract_reasoning = _ok_reasoning

    out = extractor.extract(nodes=[{"node_id": "a"}, {"node_id": "b"}], threshold=0.7, top_k_per_dst=2)
    assert out["dataflow_edges"] == []
    assert out["reasoning_edges"] == [{"src_node_id": "a", "dst_node_id": "b"}]
    assert [item["call_type"] for item in extractor.last_traces] == ["dataflow", "reasoning"]
    assert "RuntimeError: boom" in extractor.last_traces[0]["error"]
