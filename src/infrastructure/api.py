import logging
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field

from src.application.agent import AgentEngine, ChatResult
from src.infrastructure.config import Settings
from src.infrastructure.fake_home import FakeHomeState, create_registry
from src.infrastructure.ollama import OllamaLLMProvider


class ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message: str = Field(min_length=1, max_length=16000)


def create_app(engine: AgentEngine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO)
        if engine is not None:
            app.state.engine = engine
            yield
        else:
            settings = Settings()
            async with httpx.AsyncClient(base_url=str(settings.ollama_url),
                                         timeout=settings.ollama_timeout_seconds) as client:
                app.state.engine = AgentEngine(
                    OllamaLLMProvider(client, settings.ollama_model),
                    create_registry(FakeHomeState()), settings.agent_max_steps)
                yield

    app = FastAPI(title="DEXTER", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def trace(request: Request, call_next):
        request.state.trace_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Trace-ID"] = request.state.trace_id
        return response

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/chat", response_model=ChatResult)
    async def chat(body: ChatRequest, request: Request):
        return await request.app.state.engine.run(body.message, request.state.trace_id)

    return app


app = create_app()
