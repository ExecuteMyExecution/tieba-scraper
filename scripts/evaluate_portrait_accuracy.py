"""Build contact sheets from one stored portrait-crawl snapshot."""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import argparse
from pathlib import Path

import cv2
import httpx
import numpy as np


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import database


DB_PATH = ROOT / "data" / "tieba.db"
OUTPUT_ROOT = ROOT / "data" / "portrait_eval"
COLS = 4
TILE_WIDTH = 300
TILE_HEIGHT = 250
IMAGE_HEIGHT = 205


def _fit(image: np.ndarray, width: int, height: int) -> np.ndarray:
    canvas = np.full((height, width, 3), 238, dtype=np.uint8)
    source_height, source_width = image.shape[:2]
    scale = min(width / source_width, height / source_height)
    resized = cv2.resize(
        image,
        (max(1, int(source_width * scale)), max(1, int(source_height * scale))),
        interpolation=cv2.INTER_AREA,
    )
    y = (height - resized.shape[0]) // 2
    x = (width - resized.shape[1]) // 2
    canvas[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
    return canvas


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", type=int, help="要复核的人像抓取任务编号")
    args = parser.parse_args()

    database.initialize()
    connection = sqlite3.connect(DB_PATH)
    if args.job_id is None:
        job = connection.execute(
            """
            SELECT jobs.id, jobs.forum_name
            FROM jobs
            WHERE jobs.status IN ('completed', 'partial')
              AND jobs.image_filter='portrait_images'
              AND EXISTS (
                  SELECT 1 FROM job_threads WHERE job_threads.job_id=jobs.id
              )
            ORDER BY jobs.id DESC LIMIT 1
            """
        ).fetchone()
    else:
        job = connection.execute(
            """
            SELECT jobs.id, jobs.forum_name FROM jobs
            WHERE jobs.id=? AND jobs.status IN ('completed', 'partial')
              AND jobs.image_filter='portrait_images'
              AND EXISTS (
                  SELECT 1 FROM job_threads WHERE job_threads.job_id=jobs.id
              )
            """,
            (args.job_id,),
        ).fetchone()
    if job is None:
        raise SystemExit("没有可复核的同批次人像任务，请先从页面运行一次人像采集。")
    job_id, forum_name = job
    output_dir = OUTPUT_ROOT / f"job_{job_id}"
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = connection.execute(
        """
        SELECT tid, title, images_json, portrait_images_json
        FROM job_threads WHERE job_id=? ORDER BY created_at DESC
        """,
        (job_id,),
    ).fetchall()

    records: list[dict] = []
    seen: set[str] = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://tieba.baidu.com/",
    }
    with httpx.Client(headers=headers, follow_redirects=True, timeout=12.0) as client:
        for tid, title, raw_images, raw_portrait_images in rows:
            portrait_urls = set(json.loads(raw_portrait_images))
            for url in json.loads(raw_images):
                if url in seen:
                    continue
                seen.add(url)
                record = {
                    "id": len(records) + 1,
                    "tid": tid,
                    "title": title,
                    "url": url,
                    "job_id": job_id,
                    "predicted_portrait": url in portrait_urls,
                    "loaded": False,
                }
                try:
                    response = client.get(url)
                    response.raise_for_status()
                    image = cv2.imdecode(
                        np.frombuffer(response.content, dtype=np.uint8), cv2.IMREAD_COLOR
                    )
                    if image is not None:
                        record["loaded"] = True
                        local_path = output_dir / f"image_{record['id']:02d}.jpg"
                        cv2.imwrite(str(local_path), image, [cv2.IMWRITE_JPEG_QUALITY, 94])
                        record["local_path"] = str(local_path)
                        record["image"] = image
                except (httpx.HTTPError, ValueError):
                    pass
                records.append(record)

    rows_per_sheet = 5
    per_sheet = COLS * rows_per_sheet
    for sheet_index in range(math.ceil(len(records) / per_sheet)):
        subset = records[sheet_index * per_sheet:(sheet_index + 1) * per_sheet]
        sheet = np.full(
            (rows_per_sheet * TILE_HEIGHT, COLS * TILE_WIDTH, 3), 28, dtype=np.uint8
        )
        for index, record in enumerate(subset):
            row, col = divmod(index, COLS)
            x, y = col * TILE_WIDTH, row * TILE_HEIGHT
            image = record.pop("image", None)
            tile = (
                _fit(image, TILE_WIDTH - 10, IMAGE_HEIGHT)
                if image is not None
                else np.full((IMAGE_HEIGHT, TILE_WIDTH - 10, 3), 90, dtype=np.uint8)
            )
            sheet[y + 5:y + 5 + IMAGE_HEIGHT, x + 5:x + TILE_WIDTH - 5] = tile
            predicted = record["predicted_portrait"]
            color = (70, 205, 100) if predicted else (145, 145, 145)
            cv2.rectangle(sheet, (x + 3, y + 3), (x + TILE_WIDTH - 4, y + TILE_HEIGHT - 4), color, 3)
            label = f"#{record['id']:02d}  {'PREDICTED' if predicted else 'negative'}  tid {record['tid']}"
            cv2.putText(
                sheet, label, (x + 9, y + 232), cv2.FONT_HERSHEY_SIMPLEX,
                0.48, (245, 245, 245), 1, cv2.LINE_AA,
            )
        output = output_dir / f"contact_sheet_{sheet_index + 1:02d}.jpg"
        cv2.imwrite(str(output), sheet, [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(output)

    with (output_dir / "records.json").open("w", encoding="utf-8") as handle:
        json.dump(records, handle, ensure_ascii=False, indent=2)
    print(
        f"job={job_id} forum={forum_name} unique={len(records)} "
        f"loaded={sum(r['loaded'] for r in records)} "
        f"predicted={sum(r['predicted_portrait'] for r in records)}"
    )


if __name__ == "__main__":
    main()
