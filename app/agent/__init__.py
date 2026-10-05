"""Conversational agent (LangGraph) over the hardened provider.

``tools`` holds provider/tool construction (shared with the chat route),
``context`` holds recall/history/persistence/extraction, and ``graph``
wires them into one stateful turn: load_context -> respond -> persist
-> extract. The provider's bounded tool loop is untouched — the graph
orchestrates it, forwards its events, and adds history + durability.
"""

from app.agent.graph import build_graph, run_turn

__all__ = ["build_graph", "run_turn"]
