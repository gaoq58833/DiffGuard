"""FastAPI 服务层。

图在 lifespan 里编译一次并复用；每次请求只新建状态，不重建模型与拓扑。
并发由信号量限流，避免一批请求同时打满模型 API。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .config import load_settings
from .graph import build_graph, run_graph
from .schemas import ReviewReport


class ReviewRequest(BaseModel):
    diff: str = Field(min_length=1, description="unified diff 文本")
    pr_title: str = ""
    use_llm: bool = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    app.state.graph_llm = build_graph(settings, use_llm=True)
    app.state.graph_rules = build_graph(settings, use_llm=False)
    app.state.slots = asyncio.Semaphore(settings.service.max_concurrent_reviews)
    yield


app = FastAPI(title="DiffGuard", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "status": "ok",
        "llm_enabled": bool(settings.provider.api_key),
        "models": {agent: cfg.name for agent, cfg in settings.models.items()},
        "max_concurrent_reviews": settings.service.max_concurrent_reviews,
    }


@app.post("/review", response_model=ReviewReport)
async def create_review(payload: ReviewRequest, request: Request) -> ReviewReport:
    settings = request.app.state.settings
    use_llm = payload.use_llm and bool(settings.provider.api_key)
    graph = request.app.state.graph_llm if use_llm else request.app.state.graph_rules
    try:
        async with request.app.state.slots:
            return await run_graph(graph, payload.diff, payload.pr_title, settings)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="审查超时") from exc
