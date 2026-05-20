"""LangGraph Studio entry point — exposes the compiled graph for `langgraph dev`."""
import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from db import Database
from graph import RcaWorkflow
from semantic_cache import install_semantic_cache

load_dotenv(override=True)

POSTGRES_DSN = os.getenv("POSTGRES_DSN", "postgresql://rca:rca@localhost:5432/rca")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

install_semantic_cache()

db = Database(POSTGRES_DSN)
llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0)
# No postgres_dsn — LangGraph Studio manages its own checkpointer
workflow = RcaWorkflow(db, llm)

# Module-level graph consumed by langgraph dev / LangGraph Studio
graph = workflow.graph
