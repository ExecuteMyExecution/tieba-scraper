"""Re-run portrait detection for one stored crawl snapshot without recrawling Tieba."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import database
from app.portrait_detector import filter_portrait_images


async def reclassify(job_id: int) -> int:
    database.initialize()
    with database.connect() as db:
        job = db.execute(
            "SELECT id FROM jobs WHERE id=? AND image_filter='portrait_images'",
            (job_id,),
        ).fetchone()
        if job is None:
            raise SystemExit(f"任务 #{job_id} 不存在，或不是人像任务。")
        rows = db.execute(
            "SELECT tid, images_json FROM job_threads WHERE job_id=? ORDER BY tid",
            (job_id,),
        ).fetchall()
    if not rows:
        raise SystemExit(f"任务 #{job_id} 没有可重新识别的快照。")

    images_by_tid = {
        int(row["tid"]): list(dict.fromkeys(json.loads(row["images_json"])))
        for row in rows
    }
    all_urls = list(
        dict.fromkeys(url for urls in images_by_tid.values() for url in urls)
    )
    matched_urls = set(await filter_portrait_images(all_urls))
    matches = {
        tid: [url for url in urls if url in matched_urls]
        for tid, urls in images_by_tid.items()
    }
    database.set_portrait_matches(job_id, matches)
    saved_count = database.publish_job_threads(job_id, "portrait_images")
    database.update_job(
        job_id,
        saved_count=saved_count,
        message=f"已用新版人物检测重新识别并发布 {saved_count} 条",
    )
    print(
        f"job={job_id} images={len(all_urls)} matched={len(matched_urls)} "
        f"published={saved_count}"
    )
    return saved_count


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", type=int, required=True, help="人像任务编号")
    args = parser.parse_args()
    asyncio.run(reclassify(args.job_id))


if __name__ == "__main__":
    main()
