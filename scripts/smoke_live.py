"""Small opt-in network check for the upstream Tieba client."""

import asyncio

import aiotieba
from aiotieba import ThreadSortType

from app.crawler import TiebaCrawler
from app.models import CrawlRequest


async def main() -> None:
    async with aiotieba.Client() as client:
        items = await client.get_threads(
            "python", pn=1, rn=2, sort=ThreadSortType.CREATE
        )
        print(f"error={items.err!r} count={len(items)}")
        for item in items[:2]:
            print(item.tid, item.create_time, item.title)
        if items:
            posts = await client.get_posts(items[0].tid, pn=1, rn=30)
            first = next((post for post in posts if post.floor == 1), None)
            print(
                f"posts_error={posts.err!r} posts_count={len(posts)} "
                f"first_floor={bool(first)}"
            )
            record = await TiebaCrawler()._thread_record(
                client, items[0], CrawlRequest(forum_name="python"), job_id=0
            )
            print(
                f"record_tid={record['tid']} content_len={len(record['content'])} "
                f"images={len(record['images'])}"
            )


if __name__ == "__main__":
    asyncio.run(main())
