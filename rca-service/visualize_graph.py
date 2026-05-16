"""
Generate a visual image of the RCA LangGraph workflow.

Usage:
    uv run python visualize_graph.py
    # or
    python visualize_graph.py

Outputs:
    rca_graph.png  — PNG image of the graph (via Mermaid API)
    rca_graph.md   — Mermaid source markup (viewable at mermaid.live)
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from state import RcaState


def _stub_node(name: str):
    """Return a no-op node function labelled with the given name."""
    def node(state: RcaState) -> dict[str, Any]:  # noqa: ARG001
        return {}
    node.__name__ = name
    return node


def _should_review(state: RcaState) -> str:
    return "human_review_node" if state.get("confidence") == "LOW" else "postmortem_writer_agent"


def build_graph():
    wf = StateGraph(RcaState)

    wf.add_node("load_incident",             _stub_node("load_incident"))
    wf.add_node("log_analyzer_agent",        _stub_node("log_analyzer_agent"))
    wf.add_node("anomaly_detection_agent",   _stub_node("anomaly_detection_agent"))
    wf.add_node("alert_correlation_agent",   _stub_node("alert_correlation_agent"))
    wf.add_node("rag_context_agent",         _stub_node("rag_context_agent"))
    wf.add_node("root_cause_reasoner_agent", _stub_node("root_cause_reasoner_agent"))
    wf.add_node("human_review_node",         _stub_node("human_review_node"))
    wf.add_node("postmortem_writer_agent",   _stub_node("postmortem_writer_agent"))

    # Fan-out: three specialist agents run in parallel
    wf.add_edge(START, "load_incident")
    wf.add_edge("load_incident",            "log_analyzer_agent")
    wf.add_edge("load_incident",            "anomaly_detection_agent")
    wf.add_edge("load_incident",            "alert_correlation_agent")

    # Fan-in: rag_context waits for all three
    wf.add_edge("log_analyzer_agent",       "rag_context_agent")
    wf.add_edge("anomaly_detection_agent",  "rag_context_agent")
    wf.add_edge("alert_correlation_agent",  "rag_context_agent")

    wf.add_edge("rag_context_agent",        "root_cause_reasoner_agent")
    wf.add_conditional_edges(
        "root_cause_reasoner_agent",
        _should_review,
        {
            "human_review_node":        "human_review_node",
            "postmortem_writer_agent":  "postmortem_writer_agent",
        },
    )
    wf.add_edge("human_review_node",        "postmortem_writer_agent")
    wf.add_edge("postmortem_writer_agent",  END)

    return wf.compile(checkpointer=MemorySaver())


def main() -> None:
    graph = build_graph()

    # ── Mermaid source ────────────────────────────────────────────────────────
    mermaid_src = graph.get_graph().draw_mermaid()
    with open("rca_graph.md", "w") as f:
        f.write("```mermaid\n")
        f.write(mermaid_src)
        f.write("```\n")
    print("Mermaid source written to rca_graph.md")
    print("  → paste at https://mermaid.live to view interactively\n")

    # ── PNG via Playwright (offline, no internet needed) ─────────────────────
    try:
        import asyncio
        from playwright.async_api import async_playwright

        html = f"""<!DOCTYPE html>
<html>
<body style="background:white;margin:0;padding:20px">
<pre class="mermaid">{mermaid_src}</pre>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';
  mermaid.initialize({{startOnLoad:true}});
</script>
</body>
</html>"""

        async def _render():
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page()
                await page.set_content(html)
                await page.wait_for_timeout(3000)
                await page.screenshot(path="rca_graph.png", full_page=True)
                await browser.close()

        asyncio.run(_render())
        print("Graph image written to rca_graph.png")
    except ImportError:
        print("PNG generation skipped — install playwright: uv add playwright && uv run playwright install chromium")
    except Exception as exc:
        print(f"PNG generation failed: {exc}")


if __name__ == "__main__":
    main()
