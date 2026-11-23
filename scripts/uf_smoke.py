import argparse
import os
from typing import TypedDict

from openai import OpenAI
from langgraph.graph import END, START, StateGraph


class State(TypedDict):
    message: str
    response: str


def call_model(state: State, client: OpenAI, model: str) -> State:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": state["message"]}],
    )
    return {**state, "response": response.choices[0].message.content or ""}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        print(f"Provider configuration OK: {args.provider}")
        return

    if args.provider not in {"openai", "uf"}:
        raise ValueError("Supported providers: openai, uf")

    client = OpenAI(
        api_key=os.environ["UF_API_KEY"],
        base_url="https://api.navigator.ai.ufl.edu",
    )

    graph = StateGraph(State)
    graph.add_node(
        "call_model", lambda state: call_model(state, client, "gpt-oss-120b")
    )
    graph.add_edge(START, "call_model")
    graph.add_edge("call_model", END)

    result = graph.compile().invoke(
        {"message": "Reply with: LangGraph connection OK", "response": ""}
    )
    print(result["response"])


if __name__ == "__main__":
    main()
