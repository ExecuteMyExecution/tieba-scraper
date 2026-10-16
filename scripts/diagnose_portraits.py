"""Print YuNet face confidence scores for images in the current local database."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import cv2
import httpx
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.portrait_detector import MAX_DETECTION_EDGE, MODEL_PATH, _detectors


DB_PATH = ROOT / "data" / "tieba.db"


def scores(url: str) -> tuple[list[float], tuple[int, int]]:
    response = httpx.get(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://tieba.baidu.com/"},
        follow_redirects=True,
        timeout=12.0,
    )
    response.raise_for_status()
    image = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
    height, width = image.shape[:2]
    scale = min(1.0, MAX_DETECTION_EDGE / max(height, width))
    if scale < 1:
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    height, width = image.shape[:2]
    detector = cv2.FaceDetectorYN.create(str(MODEL_PATH), "", (width, height), 0.5, 0.3, 1000)
    _, faces = detector.detect(image)
    face_scores = [] if faces is None else [round(float(face[-1]), 4) for face in faces]
    return face_scores, (height, width)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    connection = sqlite3.connect(DB_PATH)
    rows = connection.execute(
        "SELECT tid, title, images_json FROM threads ORDER BY created_at DESC"
    ).fetchall()
    seen: set[str] = set()
    for tid, title, raw_images in rows:
        for url in json.loads(raw_images):
            if url in seen:
                continue
            seen.add(url)
            try:
                print(tid, title, scores(url))
            except Exception as exc:
                print(tid, title, f"ERROR: {exc}")


if __name__ == "__main__":
    main()
