"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.api.routes.continuity_api import router as continuity_api_router
from app.api.routes.draft import router as draft_router
from app.api.routes.generation import router as generation_router
from app.api.routes.memory_api import router as memory_api_router
from app.api.routes.planning import router as planning_router
from app.api.routes.story import router as story_router
from app.api.routes.trial_ui import api_router as trial_api_router
from app.api.routes.trial_ui import page_router as trial_page_router
from app.config import get_settings
from app.api.routes.studio import router as studio_router, page_router as studio_page_router
from app.api.routes.book_launch import router as launch_router, page_router as launch_page_router


def create_app() -> FastAPI:
    settings = get_settings()
    @asynccontextmanager
    async def lifespan(app):
        yield
        if hasattr(app.state, "book_launch"):
            await app.state.book_launch.close()
        if hasattr(app.state, "studio"):
            await app.state.studio.close()

    app = FastAPI(
        title="Novel Narrative Engine",
        description="长篇玄幻小说自动生成与管理系统 — Canonical Story State API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(story_router, prefix=settings.api_prefix)
    app.include_router(memory_api_router, prefix=settings.api_prefix)
    app.include_router(generation_router, prefix=settings.api_prefix)
    app.include_router(planning_router, prefix=settings.api_prefix)
    app.include_router(draft_router, prefix=settings.api_prefix)
    app.include_router(continuity_api_router, prefix=settings.api_prefix)
    app.include_router(trial_api_router, prefix=settings.api_prefix)
    app.include_router(trial_page_router)
    app.include_router(studio_router, prefix=settings.api_prefix)
    app.include_router(studio_page_router)
    app.include_router(launch_router, prefix=settings.api_prefix)
    app.include_router(launch_page_router)
    return app


app = create_app()
