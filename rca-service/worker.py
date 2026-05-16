"""Redis BRPOP worker — dequeues RCA jobs and drives the LangGraph workflow."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from langchain_openai import ChatOpenAI

from db import Database
from graph import RcaWorkflow

QUEUE_KEY = os.getenv("RCA_QUEUE_KEY", "rca:jobs:queue")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
POSTGRES_DSN = os.getenv("POSTGRES_DSN", "postgresql://rca:rca@localhost:5432/rca")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# LangSmith tracing — set these env vars to enable:
#   LANGCHAIN_TRACING_V2=true
#   LANGCHAIN_API_KEY=<your-langsmith-key>
#   LANGCHAIN_PROJECT=agentic-rca


# ── Minimal async Redis client (RESP protocol, no external library) ───────────

class RedisError(RuntimeError):
    pass


class AsyncRedis:
    def __init__(self, url: str) -> None:
        self.host, self.port, self.db_index = self._parse(url)

    @staticmethod
    def _parse(url: str) -> tuple[str, int, int]:
        if not url.startswith("redis://"):
            raise ValueError("REDIS_URL must start with redis://")
        rest = url.removeprefix("redis://")
        host_port, _, db_text = rest.partition("/")
        host, _, port_text = host_port.partition(":")
        return host or "localhost", int(port_text or "6379"), int(db_text or "0")

    async def _execute(self, *parts: str) -> Any:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            await self._send(writer, "SELECT", str(self.db_index))
            await self._recv(reader)
            await self._send(writer, *parts)
            return await self._recv(reader)
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, *parts: str) -> None:
        encoded = [p.encode() for p in parts]
        payload = [f"*{len(encoded)}\r\n".encode()]
        for p in encoded:
            payload += [f"${len(p)}\r\n".encode(), p + b"\r\n"]
        writer.write(b"".join(payload))
        await writer.drain()

    @staticmethod
    async def _recv(reader: asyncio.StreamReader) -> Any:
        prefix = await reader.readexactly(1)
        if prefix == b"+":
            return (await reader.readline()).rstrip(b"\r\n").decode()
        if prefix == b"-":
            raise RedisError((await reader.readline()).rstrip(b"\r\n").decode())
        if prefix == b":":
            return int((await reader.readline()).rstrip(b"\r\n"))
        if prefix == b"$":
            length = int((await reader.readline()).rstrip(b"\r\n"))
            if length == -1:
                return None
            data = await reader.readexactly(length)
            await reader.readexactly(2)
            return data.decode()
        if prefix == b"*":
            count = int((await reader.readline()).rstrip(b"\r\n"))
            if count == -1:
                return None
            return [await AsyncRedis._recv(reader) for _ in range(count)]
        raise RedisError(f"Unsupported Redis response prefix: {prefix!r}")

    async def ping(self) -> bool:
        return await self._execute("PING") == "PONG"

    async def blpop_json(self, key: str, timeout: int) -> Any | None:
        row = await self._execute("BLPOP", key, str(timeout))
        if not row:
            return None
        return json.loads(row[1])


# ── Worker loop ───────────────────────────────────────────────────────────────

async def worker_loop(redis: AsyncRedis, workflow: RcaWorkflow) -> None:
    mode = "langgraph" if workflow.graph is not None else "sequential-fallback"
    print(
        f"RCA worker started. queue={QUEUE_KEY} redis={REDIS_URL} "
        f"postgres={POSTGRES_DSN} model={OPENAI_MODEL} mode={mode}",
        flush=True,
    )

    while True:
        try:
            job = await redis.blpop_json(QUEUE_KEY, 5)
            if job is None:
                continue

            job_id = job.get("jobId")
            incident_id = job.get("incidentId")
            print(f"Processing job={job_id} incident={incident_id}", flush=True)

            completed = workflow.run(job)

            if not completed:
                # Graph is paused at human_review_node — wait for analyst input
                resume_key = f"rca:resume:{job_id}"
                print(f"job={job_id} awaiting analyst approval on {resume_key}", flush=True)
                resume_msg = await redis.blpop_json(resume_key, 3600)
                if resume_msg:
                    feedback = resume_msg.get("analyst_feedback", "")
                    print(f"job={job_id} resuming with feedback: {feedback[:80]}", flush=True)
                    workflow.resume(job_id, feedback)
                else:
                    print(f"job={job_id} timed out waiting for analyst approval", flush=True)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Worker loop error: {exc}", flush=True)
            await asyncio.sleep(2)


async def main() -> None:
    redis = AsyncRedis(REDIS_URL)
    await redis.ping()

    llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0)
    db = Database(POSTGRES_DSN)
    workflow = RcaWorkflow(db, llm)

    await worker_loop(redis, workflow)


if __name__ == "__main__":
    asyncio.run(main())
