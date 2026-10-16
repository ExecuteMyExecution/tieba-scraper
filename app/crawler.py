from __future__ import annotations

import asyncio
import random
from datetime import datetime, time, timedelta
from typing import Any, Awaitable, Callable

import aiotieba
from aiotieba import ThreadSortType

from . import database
from .models import CrawlRequest
from .portrait_detector import (
    DETECTOR_VERSION,
    classify_portrait_images,
    filter_portrait_images,
)


class DetailFetchError(RuntimeError):
    def __init__(
        self, error: Exception, retry_count: int, *, skipped: bool = False
    ) -> None:
        super().__init__(str(error))
        self.retry_count = retry_count
        self.skipped = skipped


def _timestamp_bounds(request: CrawlRequest) -> tuple[int | None, int | None]:
    start = (
        int(datetime.combine(request.start_date, time.min).timestamp())
        if request.start_date
        else None
    )
    end = (
        int(datetime.combine(request.end_date + timedelta(days=1), time.min).timestamp())
        if request.end_date
        else None
    )
    return start, end


def _image_urls(contents: Any) -> list[str]:
    return [display_url for display_url, _ in _image_entries(contents)]


def _image_entries(contents: Any) -> list[tuple[str, str]]:
    result: list[str] = []
    entries: list[tuple[str, str]] = []
    for image in getattr(contents, "imgs", []) or []:
        display_url = (
            getattr(image, "origin_src", "")
            or getattr(image, "big_src", "")
            or getattr(image, "src", "")
        )
        # Portrait detection needs the original pixels; medium thumbnails can
        # shrink faces in collages below the detector's usable size.
        detection_url = display_url
        if display_url and display_url not in result:
            result.append(display_url)
            entries.append((display_url, detection_url))
    return entries


def _author_name(user: Any) -> str:
    if not user:
        return "未知用户"
    return (
        getattr(user, "show_name", "")
        or getattr(user, "nick_name_new", "")
        or getattr(user, "user_name", "")
        or "未知用户"
    )


def _matches_image_filter(record: dict[str, Any], request: CrawlRequest) -> bool:
    condition = request.first_floor_image_filter
    if condition == "with_images":
        return bool(record["has_images"])
    if condition == "without_images":
        return not record["has_images"]
    if condition == "portrait_images":
        return bool(record.get("is_portrait"))
    return True


class TiebaCrawler:
    def __init__(
        self,
        client_factory: Callable[[], Any] = aiotieba.Client,
        detail_concurrency: int = 2,
        portrait_filter: Callable[[list[str]], Awaitable[list[str]]] = filter_portrait_images,
        portrait_classifier: Callable[
            [list[str]], Awaitable[dict[str, bool | None]]
        ] | None = None,
        detail_request_interval: float = 0.55,
        detail_max_attempts: int = 4,
        retry_base_delay: float = 2.0,
        recovery_delay: float = 8.0,
        detail_cache_hours: int = 24,
        portrait_cache_days: int = 14,
        adaptive_success_window: int = 20,
        adaptive_max_interval: float = 4.0,
    ) -> None:
        self.client_factory = client_factory
        self.detail_concurrency = detail_concurrency
        self.portrait_filter = portrait_filter
        self.portrait_classifier = portrait_classifier
        if portrait_classifier is None and portrait_filter is filter_portrait_images:
            self.portrait_classifier = classify_portrait_images
        self.detail_request_interval = max(0.0, detail_request_interval)
        self.detail_max_attempts = max(1, detail_max_attempts)
        self.retry_base_delay = max(0.0, retry_base_delay)
        self.recovery_delay = max(0.0, recovery_delay)
        self.detail_cache_hours = max(0, detail_cache_hours)
        self.portrait_cache_days = max(0, portrait_cache_days)
        self.adaptive_success_window = max(1, adaptive_success_window)
        self.adaptive_max_interval = max(
            self.detail_request_interval, adaptive_max_interval
        )
        self._minimum_detail_interval = self.detail_request_interval * 0.75
        self._current_detail_interval = self.detail_request_interval
        self._consecutive_detail_successes = 0
        self._pace_lock = asyncio.Lock()
        self._next_detail_at = 0.0
        self._blocked_until = 0.0

    @staticmethod
    def _error_code(error: Exception) -> int | None:
        code = getattr(error, "code", None)
        if code is None and getattr(error, "args", None):
            code = error.args[0]
        try:
            return int(code)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _is_retryable(cls, error: Exception) -> bool:
        return cls._error_code(error) in {408, 429, 500, 502, 503, 504}

    @classmethod
    def _is_skippable(cls, error: Exception) -> bool:
        # aiotieba code 4 means the topic/post no longer exists. Retrying it
        # only creates noise and should not make an otherwise healthy job partial.
        return cls._error_code(error) == 4

    async def _wait_for_detail_slot(self) -> None:
        """Globally space detail requests so concurrent workers cannot burst."""
        loop = asyncio.get_running_loop()
        async with self._pace_lock:
            now = loop.time()
            wait = max(self._next_detail_at, self._blocked_until) - now
            if wait > 0:
                await asyncio.sleep(wait)
            interval = self._current_detail_interval
            jitter = random.uniform(0, interval * 0.25)
            self._next_detail_at = loop.time() + interval + jitter

    async def _apply_global_cooldown(self, delay: float) -> None:
        loop = asyncio.get_running_loop()
        async with self._pace_lock:
            self._blocked_until = max(self._blocked_until, loop.time() + delay)

    async def _record_detail_result(self, *, rate_limited: bool) -> None:
        """AIMD-style pacing: back off quickly, recover gradually."""
        async with self._pace_lock:
            if rate_limited:
                self._consecutive_detail_successes = 0
                self._current_detail_interval = min(
                    self.adaptive_max_interval,
                    max(
                        self._current_detail_interval * 1.5,
                        self._current_detail_interval + 0.20,
                    ),
                )
                return
            self._consecutive_detail_successes += 1
            if self._consecutive_detail_successes >= self.adaptive_success_window:
                self._current_detail_interval = max(
                    self._minimum_detail_interval,
                    self._current_detail_interval * 0.9,
                )
                self._consecutive_detail_successes = 0

    async def _get_posts_with_retry(self, client: Any, tid: int) -> tuple[Any, int]:
        retries = 0
        for attempt in range(self.detail_max_attempts):
            await self._wait_for_detail_slot()
            details = await client.get_posts(tid, pn=1, rn=30)
            error = getattr(details, "err", None)
            if error is None:
                await self._record_detail_result(rate_limited=False)
                return details, retries
            rate_limited = self._error_code(error) == 429
            if rate_limited:
                await self._record_detail_result(rate_limited=True)
            retryable = self._is_retryable(error)
            if not retryable or attempt + 1 >= self.detail_max_attempts:
                raise DetailFetchError(
                    error, retries, skipped=self._is_skippable(error)
                )
            retries += 1
            delay = self.retry_base_delay * (2 ** attempt)
            delay += random.uniform(0, self.retry_base_delay * 0.35)
            await self._apply_global_cooldown(delay)
        raise RuntimeError("首楼请求重试状态异常")

    async def _get_threads_with_retry(
        self, client: Any, request: CrawlRequest, page_number: int
    ) -> tuple[Any, int]:
        retries = 0
        for attempt in range(self.detail_max_attempts):
            threads = await client.get_threads(
                request.forum_name,
                pn=page_number,
                rn=30,
                sort=ThreadSortType.CREATE,
            )
            error = getattr(threads, "err", None)
            if error is None:
                return threads, retries
            if not self._is_retryable(error) or attempt + 1 >= self.detail_max_attempts:
                raise DetailFetchError(error, retries)
            retries += 1
            delay = self.retry_base_delay * (2 ** attempt)
            delay += random.uniform(0, self.retry_base_delay * 0.35)
            await self._apply_global_cooldown(delay)
            if delay > 0:
                await asyncio.sleep(delay)
        raise RuntimeError("列表请求重试状态异常")

    async def _enrich_batch(
        self,
        client: Any,
        threads: list[Any],
        job_id: int,
        forum_name: str,
    ) -> tuple[list[dict[str, Any]], dict[int, Any], set[int], int, int]:
        semaphore = asyncio.Semaphore(self.detail_concurrency)
        cached_threads = database.get_cached_threads(
            [int(thread.tid) for thread in threads],
            forum_name,
            self.detail_cache_hours,
        )

        async def enrich(thread: Any) -> dict[str, Any]:
            async with semaphore:
                return await self._thread_record(
                    client, thread, job_id, forum_name, cached_threads
                )

        results = await asyncio.gather(
            *(enrich(thread) for thread in threads), return_exceptions=True
        )
        records: list[dict[str, Any]] = []
        failures: dict[int, Any] = {}
        skipped: set[int] = set()
        retries = 0
        cache_hits = 0
        for thread, result in zip(threads, results):
            if isinstance(result, Exception):
                tid = int(thread.tid)
                if bool(getattr(result, "skipped", False)):
                    skipped.add(tid)
                else:
                    failures[tid] = thread
                retries += int(getattr(result, "retry_count", 0))
                continue
            retries += int(result.pop("_detail_retries", 0))
            cache_hits += int(result.pop("_detail_cache_hit", 0))
            records.append(result)
        return records, failures, skipped, retries, cache_hits

    async def _classify_portrait_urls(
        self, urls: list[str]
    ) -> dict[str, bool | None]:
        if self.portrait_classifier is not None:
            return await self.portrait_classifier(urls)
        matches = set(await self.portrait_filter(urls))
        return {url: url in matches for url in urls}

    async def run(self, job_id: int, request: CrawlRequest) -> None:
        database.update_job(job_id, status="running", message="正在连接贴吧…")
        saved = 0
        found = 0
        retry_count = 0
        detail_cache_count = 0
        image_cache_count = 0
        image_failed_count = 0
        snapshot: dict[int, dict[str, Any]] = {}
        failed_threads: dict[int, Any] = {}
        skipped_threads: set[int] = set()
        seen_tids: set[int] = set()
        start_ts, end_ts = _timestamp_bounds(request)
        try:
            async with self.client_factory() as client:
                for page_number in range(1, request.pages + 1):
                    crawl_share = 85 if request.first_floor_image_filter == "portrait_images" else 98
                    database.update_job(
                        job_id,
                        progress=int((page_number - 1) / request.pages * crawl_share),
                        message=f"正在读取第 {page_number}/{request.pages} 页",
                    )
                    threads, page_list_retries = await self._get_threads_with_retry(
                        client, request, page_number
                    )
                    retry_count += page_list_retries

                    candidates = []
                    regular_thread_times = []
                    for thread in threads:
                        tid = int(thread.tid)
                        if tid in seen_tids:
                            continue
                        seen_tids.add(tid)
                        created_at = int(getattr(thread, "create_time", 0) or 0)
                        if not bool(getattr(thread, "is_top", False)):
                            regular_thread_times.append(created_at)
                        if start_ts is not None and created_at < start_ts:
                            continue
                        if end_ts is not None and created_at >= end_ts:
                            continue
                        candidates.append(thread)
                    found += len(candidates)
                    reached_start_boundary = bool(
                        start_ts is not None
                        and regular_thread_times
                        and all(
                            0 < created_at < start_ts
                            for created_at in regular_thread_times
                        )
                    )

                    (
                        page_records,
                        page_failures,
                        page_skipped,
                        page_retries,
                        page_cache_hits,
                    ) = await self._enrich_batch(
                        client, candidates, job_id, request.forum_name
                    )
                    retry_count += page_retries
                    detail_cache_count += page_cache_hits
                    skipped_threads.update(page_skipped)
                    for record in page_records:
                        snapshot[record["tid"]] = record
                        failed_threads.pop(record["tid"], None)
                        skipped_threads.discard(record["tid"])
                    for tid in page_skipped:
                        failed_threads.pop(tid, None)
                    failed_threads.update(page_failures)
                    database.stage_threads(job_id, page_records)
                    if request.first_floor_image_filter != "portrait_images":
                        saved = sum(
                            _matches_image_filter(record, request)
                            for record in snapshot.values()
                        )

                    database.update_job(
                        job_id,
                        found_count=found,
                        saved_count=saved,
                        retry_count=retry_count,
                        failed_count=len(failed_threads),
                        skipped_count=len(skipped_threads),
                        detail_cache_count=detail_cache_count,
                        progress=int(page_number / request.pages * crawl_share),
                        message=(
                            f"第 {page_number} 页完成，扫描 {found} 条，"
                            f"首楼成功 {len(snapshot)} 条，缓存 {detail_cache_count} 条，"
                            f"跳过 {len(skipped_threads)} 条，待补抓 {len(failed_threads)} 条"
                            + (
                                "，已到达开始日期，后续页面无需读取"
                                if reached_start_boundary else ""
                            )
                        ),
                    )
                    if reached_start_boundary:
                        break
                    if page_number < request.pages:
                        await asyncio.sleep(0.45)

                if failed_threads:
                    pending = list(failed_threads.values())
                    database.update_job(
                        job_id,
                        retry_count=retry_count,
                        failed_count=len(failed_threads),
                        message=f"首轮结束，正在补抓 {len(pending)} 条失败主题…",
                    )
                    if self.recovery_delay > 0:
                        await asyncio.sleep(self.recovery_delay)
                    for offset in range(0, len(pending), 30):
                        batch = pending[offset:offset + 30]
                        (
                            records,
                            failures,
                            skipped,
                            retries,
                            cache_hits,
                        ) = await self._enrich_batch(
                            client, batch, job_id, request.forum_name
                        )
                        retry_count += retries
                        detail_cache_count += cache_hits
                        skipped_threads.update(skipped)
                        for record in records:
                            snapshot[record["tid"]] = record
                            failed_threads.pop(record["tid"], None)
                            skipped_threads.discard(record["tid"])
                        for thread in batch:
                            tid = int(thread.tid)
                            if tid not in failures:
                                failed_threads.pop(tid, None)
                        failed_threads.update(failures)
                        database.stage_threads(job_id, records)
                        completed = min(offset + len(batch), len(pending))
                        database.update_job(
                            job_id,
                            retry_count=retry_count,
                            failed_count=len(failed_threads),
                            skipped_count=len(skipped_threads),
                            detail_cache_count=detail_cache_count,
                            message=(
                                f"正在补抓失败主题 {completed}/{len(pending)}，"
                                f"仍失败 {len(failed_threads)} 条"
                            ),
                        )

            final_failed = len(failed_threads)
            if found and not snapshot and final_failed:
                raise RuntimeError("所有主题首楼均请求失败，已保留上一次页面数据")

            if request.first_floor_image_filter == "portrait_images":
                image_urls = list(dict.fromkeys(
                    url
                    for record in snapshot.values()
                    for url in record["images"]
                ))
                cached_portraits = database.get_portrait_cache(
                    image_urls, DETECTOR_VERSION, self.portrait_cache_days
                )
                image_cache_count = len(cached_portraits)
                uncached_urls = [url for url in image_urls if url not in cached_portraits]
                database.update_job(
                    job_id,
                    progress=87,
                    image_cache_count=image_cache_count,
                    message=(
                        f"快照已固定，图片缓存命中 {image_cache_count} 张，"
                        f"正在识别 {len(uncached_urls)} 张新图片…"
                    ),
                )
                new_outcomes = await self._classify_portrait_urls(uncached_urls)
                image_failed_urls = {
                    url for url, outcome in new_outcomes.items() if outcome is None
                }
                image_failed_count = len(image_failed_urls)
                new_matches = {
                    url for url, outcome in new_outcomes.items() if outcome is True
                }
                database.set_portrait_cache(
                    {
                        url: outcome
                        for url, outcome in new_outcomes.items()
                        if outcome is not None
                    },
                    DETECTOR_VERSION,
                )
                matched_urls = {
                    url for url, is_portrait in cached_portraits.items() if is_portrait
                } | new_matches
                portrait_matches: dict[int, list[str]] = {}
                for tid, record in snapshot.items():
                    matched = [url for url in record["images"] if url in matched_urls]
                    portrait_matches[tid] = matched
                    record["is_portrait"] = bool(matched)
                database.set_portrait_matches(job_id, portrait_matches)
                database.update_job(
                    job_id,
                    progress=98,
                    image_failed_count=image_failed_count,
                    message=(
                        f"人像识别完成，{image_failed_count} 张图片暂未识别，"
                        "正在发布同批次结果…"
                    ),
                )

            saved = database.publish_job_threads(
                job_id, request.first_floor_image_filter
            )
            has_incomplete_results = bool(final_failed or image_failed_count)
            final_status = "partial" if has_incomplete_results else "completed"
            completion = "部分完成" if has_incomplete_results else "抓取完成"
            database.update_job(
                job_id,
                status=final_status,
                progress=100,
                found_count=found,
                saved_count=saved,
                retry_count=retry_count,
                failed_count=final_failed,
                skipped_count=len(skipped_threads),
                detail_cache_count=detail_cache_count,
                image_cache_count=image_cache_count,
                image_failed_count=image_failed_count,
                message=(
                    f"{completion}，扫描 {found} 条，首楼成功 {len(snapshot)} 条，"
                    f"缓存复用 {detail_cache_count} 条，跳过已删除 {len(skipped_threads)} 条，"
                    f"主题失败 {final_failed} 条，图片未识别 {image_failed_count} 张，"
                    f"符合条件 {saved} 条"
                ),
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        except asyncio.CancelledError:
            database.update_job(
                job_id,
                status="cancelled",
                retry_count=retry_count,
                failed_count=len(failed_threads),
                skipped_count=len(skipped_threads),
                detail_cache_count=detail_cache_count,
                image_cache_count=image_cache_count,
                image_failed_count=image_failed_count,
                message="任务已取消，已保留该贴吧上一次发布的数据",
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            raise
        except Exception as exc:
            database.update_job(
                job_id,
                status="failed",
                retry_count=retry_count + int(getattr(exc, "retry_count", 0)),
                failed_count=len(failed_threads),
                skipped_count=len(skipped_threads),
                detail_cache_count=detail_cache_count,
                image_cache_count=image_cache_count,
                image_failed_count=image_failed_count,
                message=f"抓取失败：{exc}",
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )

    async def _thread_record(
        self,
        client: Any,
        thread: Any,
        job_id: int,
        forum_name: str,
        cached_threads: dict[int, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        tid = int(thread.tid)
        current_forum = getattr(thread, "fname", "") or forum_name
        cached = (
            cached_threads.get(tid)
            if cached_threads is not None
            else database.get_cached_thread(tid, current_forum, self.detail_cache_hours)
        )
        if cached is not None:
            content = cached["content"]
            author_name = cached["author_name"]
            all_images = cached["images"]
            retries = 0
            cache_hit = 1
        else:
            # 列表接口的正文可能被截断，额外请求第一页以取得完整首楼。
            details, retries = await self._get_posts_with_retry(client, tid)
            first_floor = next(
                (post for post in details if int(getattr(post, "floor", 0)) == 1),
                None,
            )
            detail_thread = getattr(details, "thread", None)
            content_source = first_floor or detail_thread or thread
            contents = getattr(content_source, "contents", None)
            all_images = _image_urls(contents)
            content = (
                getattr(content_source, "text", "")
                or getattr(contents, "text", "")
                or getattr(thread, "text", "")
            )
            user = getattr(first_floor, "user", None) or getattr(thread, "user", None)
            author_name = _author_name(user)
            cache_hit = 0
        return {
            "tid": tid,
            "forum_name": current_forum,
            "title": getattr(thread, "title", "") or "无标题",
            "content": content.strip(),
            "author_name": author_name,
            "author_id": int(getattr(thread, "author_id", 0) or 0),
            "created_at": int(getattr(thread, "create_time", 0) or 0),
            "last_reply_at": int(getattr(thread, "last_time", 0) or 0),
            "reply_count": int(getattr(thread, "reply_num", 0) or 0),
            "view_count": int(getattr(thread, "view_num", 0) or 0),
            "agree_count": int(getattr(thread, "agree", 0) or 0),
            "has_images": bool(all_images),
            "is_portrait": False,
            "images": all_images,
            "source_url": f"https://tieba.baidu.com/p/{tid}",
            "job_id": job_id,
            "_detail_retries": retries,
            "_detail_cache_hit": cache_hit,
        }
