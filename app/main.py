from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import database
from .crawler import TiebaCrawler
from .models import CrawlRequest


ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
crawler = TiebaCrawler()
running_tasks: dict[int, asyncio.Task[None]] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    yield
    tasks = list(running_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="贴吧内容采集台", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/crawl", status_code=202)
async def start_crawl(request: CrawlRequest):
    if any(not task.done() for task in running_tasks.values()):
        raise HTTPException(
            status_code=409,
            detail="已有采集任务正在进行，请等待当前任务完成后再开始新的采集",
        )
    payload = request.model_dump(mode="json")
    job_id = database.create_job(payload)
    task = asyncio.create_task(crawler.run(job_id, request))
    running_tasks[job_id] = task

    def remove_completed(completed: asyncio.Task[None]) -> None:
        if running_tasks.get(job_id) is completed:
            running_tasks.pop(job_id, None)

    task.add_done_callback(remove_completed)
    return {"job_id": job_id, "status": "pending"}


@app.get("/api/jobs")
def jobs():
    return {"items": database.list_jobs()}


@app.get("/api/jobs/{job_id}")
def job(job_id: int):
    result = database.get_job(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return result


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int):
    result = database.get_job(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    task = running_tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return database.get_job(job_id)
    if result["status"] in {"pending", "running"}:
        raise HTTPException(status_code=409, detail="任务不在当前服务进程中，无法取消")
    return result


@app.get("/api/threads")
def threads(
    query: str = Query(default="", max_length=100),
    forum: str = Query(default="", max_length=64),
    author: str = Query(default="", max_length=64),
    exclude_author: list[str] = Query(default=[]),
    has_images: str = Query(default="all", pattern="^(all|yes|no)$"),
    start_date: date | None = None,
    end_date: date | None = None,
    sort: str = Query(default="newest", pattern="^(newest|oldest|replies|views)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    anchor_tid: int | None = Query(default=None, ge=1),
):
    excluded_authors = list(dict.fromkeys(
        value.strip() for value in exclude_author
        if value.strip() and len(value.strip()) <= 64
    ))
    if len(excluded_authors) > 100:
        raise HTTPException(status_code=422, detail="最多可屏蔽 100 位作者")
    return database.search_threads(
        query=query.strip(), forum=forum.strip(), author=author.strip(),
        exclude_authors=excluded_authors,
        has_images=has_images,
        start_date=start_date.isoformat() if start_date else "",
        end_date=end_date.isoformat() if end_date else "",
        sort=sort, page=page, page_size=page_size,
        anchor_tid=anchor_tid,
    )


@app.get("/api/stats")
def stats():
    return database.get_stats()


@app.get("/api/forums")
def forums():
    return {"items": database.list_forums()}
