import asyncio
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

from app import crawler, database, main
from app.models import CrawlRequest
from app.portrait_detector import _looks_photographic_face
from aiotieba.exception import HTTPStatusError


def test_request_normalizes_forum_and_validates_dates():
    request = CrawlRequest(forum_name=" 摄影吧 ", pages=2)
    assert request.forum_name == "摄影"
    assert request.pages == 2
    assert CrawlRequest(forum_name="摄影", pages=100).pages == 100
    with pytest.raises(ValueError):
        CrawlRequest(forum_name="摄影", pages=101)


def test_timestamp_bounds_end_is_exclusive_next_day():
    request = CrawlRequest(
        forum_name="摄影", start_date=date(2026, 8, 1), end_date=date(2026, 8, 1)
    )
    start, end = crawler._timestamp_bounds(request)
    assert end - start == 86400


def test_image_urls_prefers_original_and_deduplicates():
    contents = SimpleNamespace(
        imgs=[
            SimpleNamespace(origin_src="original.jpg", big_src="big.jpg", src="small.jpg"),
            SimpleNamespace(origin_src="original.jpg", big_src="", src=""),
            SimpleNamespace(origin_src="", big_src="second.jpg", src="small2.jpg"),
        ]
    )
    assert crawler._image_urls(contents) == ["original.jpg", "second.jpg"]
    assert crawler._image_entries(contents) == [
        ("original.jpg", "small.jpg"),
        ("second.jpg", "small2.jpg"),
    ]


def test_first_floor_image_condition_controls_which_records_are_saved():
    with_image = {"has_images": True, "is_portrait": False}
    portrait = {"has_images": True, "is_portrait": True}
    without_image = {"has_images": False, "is_portrait": False}
    assert crawler._matches_image_filter(
        with_image,
        CrawlRequest(forum_name="摄影", first_floor_image_filter="with_images"),
    )
    assert not crawler._matches_image_filter(
        without_image,
        CrawlRequest(forum_name="摄影", first_floor_image_filter="with_images"),
    )
    assert crawler._matches_image_filter(
        without_image,
        CrawlRequest(forum_name="摄影", first_floor_image_filter="without_images"),
    )
    assert crawler._matches_image_filter(
        with_image, CrawlRequest(forum_name="摄影", first_floor_image_filter="all")
    )
    assert crawler._matches_image_filter(
        portrait,
        CrawlRequest(forum_name="摄影", first_floor_image_filter="portrait_images"),
    )
    assert not crawler._matches_image_filter(
        with_image,
        CrawlRequest(forum_name="摄影", first_floor_image_filter="portrait_images"),
    )


def test_borderline_face_photo_check_rejects_flat_grayscale_memes():
    import numpy as np

    grayscale = np.full((80, 80, 3), 180, dtype=np.uint8)
    colorful_texture = np.random.default_rng(7).integers(
        0, 255, size=(80, 80, 3), dtype=np.uint8
    )
    tall_screenshot = np.random.default_rng(9).integers(
        0, 255, size=(200, 80, 3), dtype=np.uint8
    )
    face = np.array([10, 10, 60, 60, *([0] * 10), 0.85], dtype=np.float32)
    assert not _looks_photographic_face(grayscale, face)
    assert _looks_photographic_face(colorful_texture, face)
    assert not _looks_photographic_face(tall_screenshot, face)


def test_database_upsert_and_search(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    database.initialize()
    job_id = database.create_job({
        "forum_name": "摄影", "pages": 1, "first_floor_image_filter": "with_images",
        "start_date": None, "end_date": None,
    })
    database.upsert_thread({
        "tid": 123, "forum_name": "摄影", "title": "雨天散步",
        "content": "城市里的倒影", "author_name": "旅人", "author_id": 7,
        "created_at": 1_780_000_000, "last_reply_at": 1_780_000_100,
        "reply_count": 12, "view_count": 300, "agree_count": 5,
        "has_images": True, "images": ["https://example.com/a.jpg"],
        "image_thumbs": ["https://example.com/a-thumb.jpg"],
        "source_url": "https://tieba.baidu.com/p/123", "job_id": job_id,
    })
    result = database.search_threads(query="倒影", has_images="yes")
    assert result["total"] == 1
    assert result["items"][0]["title"] == "雨天散步"
    assert result["items"][0]["images"] == ["https://example.com/a.jpg"]
    assert result["items"][0]["image_thumbs"] == [
        "https://example.com/a-thumb.jpg"
    ]
    database.upsert_thread({
        "tid": 124, "forum_name": "摄影", "title": "晴天散步",
        "content": "城市里的阳光", "author_name": "路人", "author_id": 8,
        "created_at": 1_780_000_001, "last_reply_at": 1_780_000_100,
        "reply_count": 0, "view_count": 0, "agree_count": 0,
        "has_images": False, "images": [],
        "source_url": "https://tieba.baidu.com/p/124", "job_id": job_id,
    })
    excluded = database.search_threads(exclude_authors=["旅人"])
    assert excluded["total"] == 1
    assert excluded["items"][0]["author_name"] == "路人"


def test_anchor_tid_keeps_position_after_excluding_author(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "anchor.db")
    database.initialize()
    job_id = database.create_job({
        "forum_name": "摄影", "pages": 1, "first_floor_image_filter": "all",
        "start_date": None, "end_date": None,
    })
    for tid in range(1, 11):
        database.upsert_thread({
            "tid": tid, "forum_name": "摄影", "title": f"主题{tid}",
            "content": "正文",
            "author_name": "隐藏作者" if tid in (1, 2) else f"作者{tid}",
            "author_id": tid, "created_at": tid, "last_reply_at": 0,
            "reply_count": 0, "view_count": 0, "agree_count": 0,
            "has_images": False, "images": [],
            "source_url": f"https://tieba.baidu.com/p/{tid}", "job_id": job_id,
        })

    anchored = database.search_threads(
        sort="oldest", page=1, page_size=3, anchor_tid=8
    )
    assert anchored["page"] == 3
    assert [item["tid"] for item in anchored["items"]] == [7, 8, 9]

    # Two earlier posts are filtered out, so the same anchor moves to page 2.
    filtered = database.search_threads(
        sort="oldest", page=1, page_size=3,
        exclude_authors=["隐藏作者"], anchor_tid=8,
    )
    assert filtered["page"] == 2
    assert [item["tid"] for item in filtered["items"]] == [6, 7, 8]

    # An anchor that is itself filtered out falls back to the requested page.
    missing = database.search_threads(
        sort="oldest", page=1, page_size=3,
        exclude_authors=["隐藏作者"], anchor_tid=2,
    )
    assert missing["page"] == 1
    assert [item["tid"] for item in missing["items"]] == [3, 4, 5]


def test_publish_preserves_failed_threads_and_creates_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "preserve.db")
    database.initialize()
    old_job = database.create_job({
        "forum_name": "摄影", "pages": 1, "first_floor_image_filter": "all",
        "start_date": None, "end_date": None,
    })
    for tid in (1, 2):
        database.upsert_thread({
            "tid": tid, "forum_name": "摄影", "title": f"旧主题{tid}",
            "content": "旧正文", "author_name": "作者", "author_id": 1,
            "created_at": tid, "last_reply_at": 0, "reply_count": 0,
            "view_count": 0, "agree_count": 0, "has_images": False,
            "images": [], "source_url": f"https://tieba.baidu.com/p/{tid}",
            "job_id": old_job,
        })

    new_job = database.create_job({
        "forum_name": "摄影", "pages": 1, "first_floor_image_filter": "all",
        "start_date": None, "end_date": None,
    })
    database.stage_threads(new_job, [{
        "tid": 1, "forum_name": "摄影", "title": "新主题1",
        "content": "新正文", "author_name": "作者", "author_id": 1,
        "created_at": 1, "last_reply_at": 0, "reply_count": 0,
        "view_count": 0, "agree_count": 0, "has_images": False,
        "images": [], "source_url": "https://tieba.baidu.com/p/1",
        "job_id": new_job,
    }])

    assert database.publish_job_threads(new_job, "all", preserve_tids={2}) == 1
    visible = {item["tid"]: item for item in database.search_threads()["items"]}
    assert set(visible) == {1, 2}
    assert visible[1]["title"] == "新主题1"
    assert visible[2]["title"] == "旧主题2"
    backups = list((tmp_path / "backups").glob("auto-preserve-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup_db:
        backup_title = backup_db.execute(
            "SELECT title FROM threads WHERE tid=1"
        ).fetchone()[0]
    assert backup_title == "旧主题1"


def test_publish_refuses_to_replace_existing_data_with_empty_filter(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "empty-filter.db")
    database.initialize()
    old_job = database.create_job({
        "forum_name": "摄影", "pages": 1, "first_floor_image_filter": "all",
        "start_date": None, "end_date": None,
    })
    database.upsert_thread({
        "tid": 1, "forum_name": "摄影", "title": "旧主题", "content": "旧正文",
        "author_name": "作者", "author_id": 1, "created_at": 1,
        "last_reply_at": 0, "reply_count": 0, "view_count": 0, "agree_count": 0,
        "has_images": True, "images": ["https://example.com/a.jpg"],
        "source_url": "https://tieba.baidu.com/p/1", "job_id": old_job,
    })
    new_job = database.create_job({
        "forum_name": "摄影", "pages": 1, "first_floor_image_filter": "with_images",
        "start_date": None, "end_date": None,
    })
    database.stage_threads(new_job, [{
        "tid": 2, "forum_name": "摄影", "title": "无图主题", "content": "正文",
        "author_name": "作者", "author_id": 1, "created_at": 2,
        "last_reply_at": 0, "reply_count": 0, "view_count": 0, "agree_count": 0,
        "has_images": False, "images": [],
        "source_url": "https://tieba.baidu.com/p/2", "job_id": new_job,
    }])

    with pytest.raises(ValueError, match="没有可发布"):
        database.publish_job_threads(new_job, "with_images")
    visible = database.search_threads(forum="摄影")
    assert visible["total"] == 1
    assert visible["items"][0]["tid"] == 1


def test_forum_filter_is_exact_and_uses_indexed_equality(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "forum-exact.db")
    database.initialize()
    job_id = database.create_job({
        "forum_name": "摄影", "pages": 1, "first_floor_image_filter": "all",
        "start_date": None, "end_date": None,
    })
    for tid, forum in [(1, "摄影"), (2, "摄影技术")]:
        database.upsert_thread({
            "tid": tid, "forum_name": forum, "title": f"主题{tid}",
            "content": "正文", "author_name": "作者", "author_id": 1,
            "created_at": tid, "last_reply_at": 0, "reply_count": 0,
            "view_count": 0, "agree_count": 0, "has_images": False,
            "images": [], "source_url": f"https://tieba.baidu.com/p/{tid}",
            "job_id": job_id,
        })
    result = database.search_threads(forum="摄影")
    assert [item["tid"] for item in result["items"]] == [1]


def test_pagination_is_stable_when_posts_share_the_same_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "stable-pages.db")
    database.initialize()
    job_id = database.create_job({
        "forum_name": "摄影", "pages": 1, "first_floor_image_filter": "all",
        "start_date": None, "end_date": None,
    })
    for tid in range(1, 14):
        database.upsert_thread({
            "tid": tid, "forum_name": "摄影", "title": f"主题{tid}",
            "content": "正文", "author_name": "作者", "author_id": 1,
            "created_at": 100, "last_reply_at": 0, "reply_count": 0,
            "view_count": 0, "agree_count": 0, "has_images": False,
            "images": [], "source_url": f"https://tieba.baidu.com/p/{tid}",
            "job_id": job_id,
        })

    page_one = database.search_threads(page=1, page_size=12)
    page_two = database.search_threads(page=2, page_size=12)
    first_ids = {item["tid"] for item in page_one["items"]}
    second_ids = {item["tid"] for item in page_two["items"]}
    assert len(first_ids) == 12
    assert second_ids == {1}
    assert first_ids.isdisjoint(second_ids)


def test_latest_successful_job_replaces_previous_content(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "latest.db")
    database.initialize()
    payload = {
        "forum_name": "摄影", "pages": 1, "first_floor_image_filter": "all",
        "start_date": None, "end_date": None,
    }
    old_job = database.create_job(payload)
    new_job = database.create_job(payload)

    def record(tid, job_id):
        return {
            "tid": tid, "forum_name": "摄影", "title": f"主题{tid}",
            "content": "正文", "author_name": "作者", "author_id": 1,
            "created_at": 1_780_000_000 + tid, "last_reply_at": 0,
            "reply_count": 0, "view_count": 0, "agree_count": 0,
            "has_images": False, "images": [],
            "source_url": f"https://tieba.baidu.com/p/{tid}", "job_id": job_id,
        }

    database.upsert_thread(record(1, old_job))
    database.upsert_thread(record(2, new_job))
    assert database.retain_threads_for_job(new_job) == 1
    result = database.search_threads()
    assert result["total"] == 1
    assert result["items"][0]["tid"] == 2


def test_portrait_publish_uses_the_same_unfiltered_job_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "snapshot.db")
    database.initialize()
    job_id = database.create_job({
        "forum_name": "摄影", "pages": 1,
        "first_floor_image_filter": "portrait_images",
        "start_date": None, "end_date": None,
    })

    def record(tid, images):
        return {
            "tid": tid, "forum_name": "摄影", "title": f"主题{tid}",
            "content": "正文", "author_name": "作者", "author_id": 1,
            "created_at": 1_780_000_000 + tid, "last_reply_at": 0,
            "reply_count": 0, "view_count": 0, "agree_count": 0,
            "has_images": bool(images), "images": images,
            "source_url": f"https://tieba.baidu.com/p/{tid}", "job_id": job_id,
        }

    database.stage_threads(job_id, [
        record(10, ["https://example.com/a.jpg", "https://example.com/b.jpg"]),
        record(11, ["https://example.com/not-a-portrait.jpg"]),
    ])
    database.set_portrait_matches(
        job_id, {10: ["https://example.com/b.jpg"], 11: []}
    )

    assert database.publish_job_threads(job_id, "portrait_images") == 1
    visible = database.search_threads()
    assert visible["total"] == 1
    assert visible["items"][0]["tid"] == 10
    assert visible["items"][0]["images"] == ["https://example.com/b.jpg"]
    assert visible["items"][0]["image_thumbs"] == ["https://example.com/b.jpg"]

    with database.connect() as db:
        snapshot = db.execute(
            "SELECT images_json, portrait_images_json, portrait_thumbs_json "
            "FROM job_threads WHERE job_id=? AND tid=10", (job_id,),
        ).fetchone()
    assert snapshot["images_json"] == (
        '["https://example.com/a.jpg", "https://example.com/b.jpg"]'
    )
    assert snapshot["portrait_images_json"] == '["https://example.com/b.jpg"]'
    assert snapshot["portrait_thumbs_json"] == '["https://example.com/b.jpg"]'


def test_publish_keeps_latest_snapshot_per_forum(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "per-forum.db")
    database.initialize()

    def create_job(forum):
        return database.create_job({
            "forum_name": forum, "pages": 1, "first_floor_image_filter": "all",
            "start_date": None, "end_date": None,
        })

    def record(tid, forum, job_id):
        return {
            "tid": tid, "forum_name": forum, "title": f"{forum}-{tid}",
            "content": "正文", "author_name": "作者", "author_id": 1,
            "created_at": tid, "last_reply_at": 0, "reply_count": 0,
            "view_count": 0, "agree_count": 0, "has_images": False,
            "images": [], "source_url": f"https://tieba.baidu.com/p/{tid}",
            "job_id": job_id,
        }

    luosu_old = create_job("落俗")
    beauty = create_job("美女")
    luosu_new = create_job("落俗")
    database.stage_threads(luosu_old, [record(1, "落俗", luosu_old)])
    database.stage_threads(beauty, [record(2, "美女", beauty)])
    database.stage_threads(luosu_new, [record(3, "落俗", luosu_new)])

    assert database.publish_job_threads(luosu_old, "all") == 1
    assert database.publish_job_threads(beauty, "all") == 1
    assert {item["tid"] for item in database.search_threads()["items"]} == {1, 2}

    assert database.publish_job_threads(luosu_new, "all") == 1
    combined = database.search_threads(page_size=10)
    assert combined["total"] == 2
    assert combined["forum_count"] == 2
    assert combined["job_id"] is None
    assert {item["tid"] for item in combined["items"]} == {2, 3}

    luosu = database.search_threads(forum="落俗")
    assert luosu["total"] == 1
    assert luosu["job_id"] == luosu_new
    assert luosu["items"][0]["tid"] == 3
    assert {item["forum_name"] for item in database.list_forums()} == {"落俗", "美女"}


def test_portrait_crawl_classifies_one_fixed_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "crawler-snapshot.db")
    database.initialize()
    request = CrawlRequest(
        forum_name="摄影", pages=1, first_floor_image_filter="portrait_images"
    )
    job_id = database.create_job(request.model_dump(mode="json"))

    def thread(tid, title):
        return SimpleNamespace(
            tid=tid, fname="摄影", title=title, create_time=1_780_000_000 + tid,
            last_time=0, reply_num=0, view_num=0, agree=0, author_id=tid,
            user=SimpleNamespace(show_name=f"作者{tid}"), text="",
        )

    source_threads = [thread(20, "有人像"), thread(21, "无人像")]
    image_urls = {
        20: ["https://example.com/a.jpg", "https://example.com/b.jpg"],
        21: ["https://example.com/c.jpg"],
    }

    class Result(list):
        err = None

    class FakeClient:
        get_threads_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get_threads(self, *_args, **_kwargs):
            self.get_threads_calls += 1
            return Result(source_threads)

        async def get_posts(self, tid, **_kwargs):
            images = [
                SimpleNamespace(
                    origin_src=url, big_src="", src=url.replace(".jpg", "-thumb.jpg")
                )
                for url in image_urls[tid]
            ]
            post = SimpleNamespace(
                floor=1, contents=SimpleNamespace(imgs=images, text="正文"),
                text="正文", user=SimpleNamespace(show_name=f"作者{tid}"),
            )
            result = Result([post])
            result.thread = source_threads[tid - 20]
            return result

    portrait_calls = []

    async def portrait_filter(urls):
        portrait_calls.append(urls)
        return ["https://example.com/b.jpg"]

    client = FakeClient()
    worker = crawler.TiebaCrawler(
        client_factory=lambda: client, portrait_filter=portrait_filter
    )
    asyncio.run(worker.run(job_id, request))

    assert client.get_threads_calls == 1
    assert portrait_calls == [[
        "https://example.com/a.jpg",
        "https://example.com/b.jpg",
        "https://example.com/c.jpg",
    ]]
    visible = database.search_threads()
    assert visible["total"] == 1
    assert visible["items"][0]["tid"] == 20
    assert visible["items"][0]["images"] == ["https://example.com/b.jpg"]
    assert visible["items"][0]["image_thumbs"] == [
        "https://example.com/b-thumb.jpg"
    ]
    with database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM job_threads WHERE job_id=?", (job_id,)
        ).fetchone()[0] == 2


def test_detail_429_is_retried_with_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "retry.db")
    database.initialize()

    class Result(list):
        def __init__(self, values=(), error=None):
            super().__init__(values)
            self.err = error
            self.thread = None

    post = SimpleNamespace(
        floor=1, contents=SimpleNamespace(imgs=[], text="正文"), text="正文",
        user=SimpleNamespace(show_name="作者"),
    )

    class FakeClient:
        calls = 0

        async def get_posts(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls < 3:
                return Result(error=HTTPStatusError(429, "Too Many Requests"))
            return Result([post])

    thread = SimpleNamespace(
        tid=99, fname="摄影", title="测试", create_time=1,
        last_time=0, reply_num=0, view_num=0, agree=0, author_id=1,
        user=SimpleNamespace(show_name="作者"), text="",
    )
    client = FakeClient()
    worker = crawler.TiebaCrawler(
        detail_request_interval=0, retry_base_delay=0, recovery_delay=0
    )
    result = asyncio.run(worker._thread_record(client, thread, 1, "摄影"))
    assert client.calls == 3
    assert result["_detail_retries"] == 2


def test_unrecovered_detail_failure_marks_job_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "partial.db")
    database.initialize()
    old_job = database.create_job({
        "forum_name": "摄影", "pages": 1, "first_floor_image_filter": "all",
        "start_date": None, "end_date": None,
    })
    database.upsert_thread({
        "tid": 2, "forum_name": "摄影", "title": "旧数据",
        "content": "旧正文", "author_name": "作者2", "author_id": 2,
        "created_at": 2, "last_reply_at": 0, "reply_count": 0,
        "view_count": 0, "agree_count": 0, "has_images": False,
        "images": [], "source_url": "https://tieba.baidu.com/p/2",
        "job_id": old_job,
    })
    request = CrawlRequest(forum_name="摄影", pages=1)
    job_id = database.create_job(request.model_dump(mode="json"))

    def thread(tid):
        return SimpleNamespace(
            tid=tid, fname="摄影", title=f"主题{tid}", create_time=tid,
            last_time=0, reply_num=0, view_num=0, agree=0, author_id=tid,
            user=SimpleNamespace(show_name=f"作者{tid}"), text="",
        )

    class Result(list):
        def __init__(self, values=(), error=None):
            super().__init__(values)
            self.err = error
            self.thread = None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get_threads(self, *_args, **_kwargs):
            return Result([thread(1), thread(2)])

        async def get_posts(self, tid, **_kwargs):
            if tid == 2:
                return Result(error=HTTPStatusError(429, "Too Many Requests"))
            post = SimpleNamespace(
                floor=1, contents=SimpleNamespace(imgs=[], text="正文"),
                text="正文", user=SimpleNamespace(show_name="作者1"),
            )
            return Result([post])

    worker = crawler.TiebaCrawler(
        client_factory=FakeClient,
        detail_request_interval=0,
        detail_max_attempts=2,
        retry_base_delay=0,
        recovery_delay=0,
    )
    asyncio.run(worker.run(job_id, request))

    job = database.get_job(job_id)
    assert job["status"] == "partial"
    assert job["failed_count"] == 1
    assert job["retry_count"] == 2
    visible = {item["tid"]: item for item in database.search_threads()["items"]}
    assert set(visible) == {1, 2}
    assert visible[2]["title"] == "旧数据"


def test_recent_thread_detail_cache_avoids_network_request(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "detail-cache.db")
    database.initialize()
    payload = {
        "forum_name": "摄影", "pages": 1, "first_floor_image_filter": "all",
        "start_date": None, "end_date": None,
    }
    old_job = database.create_job(payload)
    database.stage_threads(old_job, [{
        "tid": 77, "forum_name": "摄影", "title": "旧标题",
        "content": "缓存中的完整首楼", "author_name": "缓存作者", "author_id": 7,
        "created_at": 1, "last_reply_at": 2, "reply_count": 3,
        "view_count": 4, "agree_count": 5, "has_images": True,
        "images": ["https://example.com/cached.jpg"],
        "source_url": "https://tieba.baidu.com/p/77", "job_id": old_job,
    }])

    class NoNetworkClient:
        async def get_posts(self, *_args, **_kwargs):
            raise AssertionError("缓存命中时不应请求首楼")

    current = SimpleNamespace(
        tid=77, fname="摄影", title="列表中的新标题", create_time=10,
        last_time=20, reply_num=30, view_num=40, agree=5, author_id=7,
        user=SimpleNamespace(show_name="列表作者"), text="截断正文",
    )
    worker = crawler.TiebaCrawler(detail_cache_hours=24)
    record = asyncio.run(
        worker._thread_record(NoNetworkClient(), current, old_job + 1, "摄影")
    )
    assert record["_detail_cache_hit"] == 1
    assert record["content"] == "缓存中的完整首楼"
    assert record["images"] == ["https://example.com/cached.jpg"]
    assert record["title"] == "列表中的新标题"
    assert record["reply_count"] == 30


def test_portrait_cache_only_classifies_new_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "portrait-cache.db")
    database.initialize()
    request = CrawlRequest(
        forum_name="摄影", pages=1, first_floor_image_filter="portrait_images"
    )
    job_id = database.create_job(request.model_dump(mode="json"))
    database.set_portrait_cache(
        {"https://example.com/a.jpg": True}, crawler.DETECTOR_VERSION
    )

    source = SimpleNamespace(
        tid=88, fname="摄影", title="测试缓存", create_time=100,
        last_time=0, reply_num=0, view_num=0, agree=0, author_id=8,
        user=SimpleNamespace(show_name="作者"), text="",
    )

    class Result(list):
        err = None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get_threads(self, *_args, **_kwargs):
            return Result([source])

        async def get_posts(self, *_args, **_kwargs):
            images = [
                SimpleNamespace(origin_src="https://example.com/a.jpg", big_src="", src=""),
                SimpleNamespace(origin_src="https://example.com/b.jpg", big_src="", src=""),
            ]
            return Result([SimpleNamespace(
                floor=1, contents=SimpleNamespace(imgs=images, text="正文"),
                text="正文", user=SimpleNamespace(show_name="作者"),
            )])

    calls = []

    async def portrait_classifier(urls):
        calls.append(urls)
        return {url: None for url in urls}

    worker = crawler.TiebaCrawler(
        client_factory=FakeClient, portrait_classifier=portrait_classifier,
        detail_cache_hours=0, detail_request_interval=0,
    )
    asyncio.run(worker.run(job_id, request))

    assert calls == [["https://example.com/b.jpg"]]
    job = database.get_job(job_id)
    assert job["status"] == "partial"
    assert job["image_cache_count"] == 1
    assert job["image_failed_count"] == 1
    assert database.get_portrait_cache(
        ["https://example.com/b.jpg"], crawler.DETECTOR_VERSION
    ) == {}
    assert database.search_threads()["items"][0]["images"] == [
        "https://example.com/a.jpg"
    ]


def test_deleted_topic_is_skipped_without_recovery_or_partial_status(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "deleted.db")
    database.initialize()
    request = CrawlRequest(forum_name="摄影", pages=1)
    job_id = database.create_job(request.model_dump(mode="json"))

    def thread(tid):
        return SimpleNamespace(
            tid=tid, fname="摄影", title=f"主题{tid}", create_time=tid,
            last_time=0, reply_num=0, view_num=0, agree=0, author_id=tid,
            user=SimpleNamespace(show_name="作者"), text="",
        )

    class Result(list):
        def __init__(self, values=(), error=None):
            super().__init__(values)
            self.err = error
            self.thread = None

    class FakeClient:
        deleted_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get_threads(self, *_args, **_kwargs):
            return Result([thread(1), thread(2)])

        async def get_posts(self, tid, **_kwargs):
            if tid == 2:
                self.deleted_calls += 1
                return Result(error=HTTPStatusError(4, "Post deleted"))
            return Result([SimpleNamespace(
                floor=1, contents=SimpleNamespace(imgs=[], text="正文"),
                text="正文", user=SimpleNamespace(show_name="作者"),
            )])

    client = FakeClient()
    worker = crawler.TiebaCrawler(
        client_factory=lambda: client, detail_cache_hours=0,
        detail_request_interval=0, retry_base_delay=0, recovery_delay=0,
    )
    asyncio.run(worker.run(job_id, request))

    job = database.get_job(job_id)
    assert job["status"] == "completed"
    assert job["skipped_count"] == 1
    assert job["failed_count"] == 0
    assert client.deleted_calls == 1
    assert database.search_threads()["total"] == 1


def test_adaptive_detail_pacing_backs_off_and_recovers():
    worker = crawler.TiebaCrawler(
        detail_request_interval=0.4,
        adaptive_success_window=2,
        adaptive_max_interval=2,
    )

    async def exercise():
        await worker._record_detail_result(rate_limited=True)
        backed_off = worker._current_detail_interval
        await worker._record_detail_result(rate_limited=False)
        await worker._record_detail_result(rate_limited=False)
        return backed_off, worker._current_detail_interval

    backed_off, recovered = asyncio.run(exercise())
    assert backed_off == pytest.approx(0.6)
    assert recovered == pytest.approx(0.54)


def test_api_rejects_second_concurrent_crawl():
    async def exercise():
        blocker = asyncio.Event()
        active = asyncio.create_task(blocker.wait())
        main.running_tasks[999] = active
        try:
            with pytest.raises(main.HTTPException) as exc_info:
                await main.start_crawl(CrawlRequest(forum_name="摄影", pages=1))
            assert exc_info.value.status_code == 409
        finally:
            active.cancel()
            main.running_tasks.pop(999, None)
            with pytest.raises(asyncio.CancelledError):
                await active

    asyncio.run(exercise())


def test_enrich_batch_uses_one_bulk_cache_lookup(monkeypatch):
    lookups = []

    def bulk_lookup(tids, forum_name, max_age_hours):
        lookups.append((tids, forum_name, max_age_hours))
        return {
            tid: {
                "content": f"缓存正文{tid}", "author_name": "缓存作者",
                "has_images": False, "images": [], "fetched_at": "now",
            }
            for tid in tids
        }

    monkeypatch.setattr(database, "get_cached_threads", bulk_lookup)

    def thread(tid):
        return SimpleNamespace(
            tid=tid, fname="摄影", title=f"主题{tid}", create_time=tid,
            last_time=0, reply_num=0, view_num=0, agree=0, author_id=tid,
            user=SimpleNamespace(show_name="作者"), text="",
        )

    class NoNetworkClient:
        async def get_posts(self, *_args, **_kwargs):
            raise AssertionError("批量缓存命中时不应请求网络")

    worker = crawler.TiebaCrawler(detail_cache_hours=24)
    records, failures, skipped, retries, cache_hits = asyncio.run(
        worker._enrich_batch(NoNetworkClient(), [thread(1), thread(2)], 1, "摄影")
    )
    assert lookups == [([1, 2], "摄影", 24)]
    assert len(records) == 2
    assert failures == {}
    assert skipped == set()
    assert retries == 0
    assert cache_hits == 2


def test_crawl_deduplicates_pages_and_stops_after_start_date(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "dedupe-boundary.db")
    database.initialize()
    request = CrawlRequest(forum_name="摄影", pages=5, start_date=date(2026, 8, 30))
    job_id = database.create_job(request.model_dump(mode="json"))
    start_ts, _ = crawler._timestamp_bounds(request)

    def thread(tid, created_at):
        return SimpleNamespace(
            tid=tid, fname="摄影", title=f"主题{tid}", create_time=created_at,
            last_time=0, reply_num=0, view_num=0, agree=0, author_id=tid,
            user=SimpleNamespace(show_name="作者"), text="", is_top=False,
        )

    recent_one = thread(1, start_ts + 200)
    recent_two = thread(2, start_ts + 100)
    older_one = thread(3, start_ts - 100)
    older_two = thread(4, start_ts - 200)

    class Result(list):
        err = None
        thread = None

    class FakeClient:
        page_calls = 0
        post_calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get_threads(self, *_args, **kwargs):
            self.page_calls += 1
            if kwargs["pn"] == 1:
                return Result([recent_one, recent_two])
            return Result([recent_one, older_one, older_two])

        async def get_posts(self, tid, **_kwargs):
            self.post_calls.append(tid)
            return Result([SimpleNamespace(
                floor=1, contents=SimpleNamespace(imgs=[], text=f"正文{tid}"),
                text=f"正文{tid}", user=SimpleNamespace(show_name="作者"),
            )])

    client = FakeClient()
    worker = crawler.TiebaCrawler(
        client_factory=lambda: client, detail_cache_hours=0,
        detail_request_interval=0, retry_base_delay=0, recovery_delay=0,
    )
    asyncio.run(worker.run(job_id, request))

    assert client.page_calls == 2
    assert client.post_calls == [1, 2]
    job = database.get_job(job_id)
    assert job["status"] == "completed"
    assert job["found_count"] == 2
    assert database.search_threads()["total"] == 2


def test_empty_crawl_preserves_previous_forum_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "empty-crawl.db")
    database.initialize()
    old_job = database.create_job({
        "forum_name": "摄影", "pages": 1, "first_floor_image_filter": "all",
        "start_date": None, "end_date": None,
    })
    database.upsert_thread({
        "tid": 99, "forum_name": "摄影", "title": "上一次结果",
        "content": "仍应保留", "author_name": "作者", "author_id": 1,
        "created_at": 1, "last_reply_at": 1, "reply_count": 0,
        "view_count": 0, "agree_count": 0, "has_images": False,
        "images": [], "source_url": "https://tieba.baidu.com/p/99",
        "job_id": old_job,
    })

    request = CrawlRequest(forum_name="摄影", pages=1)
    job_id = database.create_job(request.model_dump(mode="json"))

    class Result(list):
        err = None

    class EmptyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get_threads(self, *_args, **_kwargs):
            return Result()

    worker = crawler.TiebaCrawler(
        client_factory=EmptyClient, detail_request_interval=0
    )
    asyncio.run(worker.run(job_id, request))

    job = database.get_job(job_id)
    assert job["status"] == "failed"
    assert "保留" in job["message"]
    visible = database.search_threads(forum="摄影")
    assert visible["total"] == 1
    assert visible["items"][0]["tid"] == 99


def test_cancelling_crawl_preserves_previous_forum_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "cancel.db")
    database.initialize()
    previous_job = database.create_job({
        "forum_name": "摄影", "pages": 1, "first_floor_image_filter": "all",
        "start_date": None, "end_date": None,
    })
    database.stage_threads(previous_job, [{
        "tid": 99, "forum_name": "摄影", "title": "上一次结果",
        "content": "仍应保留", "author_name": "作者", "author_id": 1,
        "created_at": 1, "last_reply_at": 1, "reply_count": 0,
        "view_count": 0, "agree_count": 0, "has_images": False,
        "images": [], "source_url": "https://tieba.baidu.com/p/99",
    }])
    database.publish_job_threads(previous_job, "all")

    request = CrawlRequest(forum_name="摄影", pages=2)
    job_id = database.create_job(request.model_dump(mode="json"))
    entered_request = asyncio.Event()

    class BlockingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get_threads(self, *_args, **_kwargs):
            entered_request.set()
            await asyncio.Event().wait()

    worker = crawler.TiebaCrawler(client_factory=BlockingClient)

    async def exercise():
        task = asyncio.create_task(worker.run(job_id, request))
        main.running_tasks[job_id] = task
        await entered_request.wait()
        try:
            result = await main.cancel_job(job_id)
            assert result["status"] == "cancelled"
        finally:
            main.running_tasks.pop(job_id, None)

    asyncio.run(exercise())

    job = database.get_job(job_id)
    assert job["status"] == "cancelled"
    assert job["finished_at"] is not None
    visible = database.search_threads(forum="摄影")
    assert visible["total"] == 1
    assert visible["items"][0]["tid"] == 99
