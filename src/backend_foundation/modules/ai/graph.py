from typing import TypedDict

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph


class SmokeState(TypedDict):
    value: str


def build_smoke_graph() -> CompiledStateGraph[SmokeState, None, SmokeState, SmokeState]:
    transform: RunnableLambda[SmokeState, SmokeState] = RunnableLambda(
        lambda value: {"value": f"{value['value']}-graph"}
    )
    graph = StateGraph(SmokeState)
    graph.add_node("transform", transform)
    graph.set_entry_point("transform")
    graph.add_edge("transform", END)
    return graph.compile()


graph = build_smoke_graph()


async def run_smoke_graph(value: str) -> str:
    result = await graph.ainvoke({"value": value})
    return str(result["value"])
