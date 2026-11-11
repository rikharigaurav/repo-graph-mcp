from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from .llm import ask, planner_model
from .tools import TOOLS


def plan(state):
    """Decide what to research."""
    planner_model()
    return {"plan": ask(state["q"])}


def act(state) -> Command[Literal["review"]]:
    if state.get("done"):
        return Command(goto="review")
    return {"messages": []}


def review(state):
    return state


def route(state) -> Literal["tools", "__end__"]:
    return "tools" if state.get("calls") else END


builder = StateGraph(dict)
builder.add_node("plan", plan)
builder.add_node("act", act)
builder.add_node("tools", ToolNode(TOOLS))
builder.add_node("review", review)
builder.add_edge(START, "plan")
builder.add_edge("plan", "act")
builder.add_conditional_edges("act", route)
builder.add_edge("tools", "act")
builder.add_edge("review", END)
research_agent = builder.compile(name="research_agent")
