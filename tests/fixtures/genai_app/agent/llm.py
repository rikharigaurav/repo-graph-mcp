from langchain.chat_models import init_chat_model
from openai import OpenAI

client = OpenAI()


def ask(question: str) -> str:
    reply = client.chat.completions.create(model="gpt-4.1", messages=[{"role": "user", "content": question}])
    return reply.choices[0].message.content


def planner_model():
    return init_chat_model("anthropic:claude-sonnet-4-5")
