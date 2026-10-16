from __future__ import annotations

import asyncio
import threading
from functools import lru_cache
from pathlib import Path

import httpx


# Bump this whenever models or thresholds change so cached decisions are
# automatically re-evaluated with the new detector.
DETECTOR_VERSION = "person-2026-08-30-v1"


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_DETECTION_EDGE = 720
ASSET_DIR = Path(__file__).parent / "assets"
FACE_MODEL_PATH = ASSET_DIR / "face_detection_yunet_2023mar.onnx"
BODY_MODEL_PATH = ASSET_DIR / "object_detection_yolox_2022nov_int8.onnx"
POSE_MODEL_PATH = ASSET_DIR / "person_detection_mediapipe_2023mar.onnx"
SEGMENTATION_MODEL_PATH = ASSET_DIR / "human_segmentation_pphumanseg_2023mar.onnx"
FACE_DETECTOR_THRESHOLD = 0.78
FACE_BALANCED_THRESHOLD = 0.80
FACE_STRICT_THRESHOLD = 0.91
BODY_PERSON_THRESHOLD = 0.10
POSE_PERSON_THRESHOLD = 0.65
SEGMENTATION_PERSON_AREA_THRESHOLD = 0.18
_detection_lock = threading.Lock()


@lru_cache(maxsize=1)
def _detectors():
    """Load the local face/body detectors once per worker process."""
    import cv2

    cv2.setNumThreads(1)
    model_paths = (
        FACE_MODEL_PATH,
        BODY_MODEL_PATH,
        POSE_MODEL_PATH,
        SEGMENTATION_MODEL_PATH,
    )
    missing = [path.name for path in model_paths if not path.exists()]
    if missing:
        raise RuntimeError(f"缺少人像识别模型：{', '.join(missing)}")
    face_detector = cv2.FaceDetectorYN.create(
        str(FACE_MODEL_PATH), "", (320, 320), FACE_DETECTOR_THRESHOLD, 0.3, 1000
    )
    body_detector = cv2.dnn.readNet(str(BODY_MODEL_PATH))
    pose_detector = cv2.dnn.readNet(str(POSE_MODEL_PATH))
    segmentation_detector = cv2.dnn.readNet(str(SEGMENTATION_MODEL_PATH))
    return cv2, face_detector, body_detector, pose_detector, segmentation_detector


def _letterbox(image, size: int, *, rgb: bool = True):
    """Resize without stretching and pad on the bottom/right."""
    import cv2
    import numpy as np

    if rgb:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    height, width = image.shape[:2]
    scale = min(size / height, size / width)
    resized = cv2.resize(
        image,
        (max(1, int(width * scale)), max(1, int(height * scale))),
        interpolation=cv2.INTER_LINEAR,
    ).astype(np.float32)
    canvas = np.full((size, size, 3), 114.0, dtype=np.float32)
    canvas[: resized.shape[0], : resized.shape[1]] = resized
    return canvas


def _body_person_score(cv2, detector, image) -> float:
    """Return YOLOX's strongest COCO `person` confidence."""
    import numpy as np

    input_image = _letterbox(image, 640)
    detector.setInput(np.transpose(input_image, (2, 0, 1))[None, ...])
    output = detector.forward()[0]
    return float(np.max(output[:, 4] * output[:, 5]))


def _pose_person_score(cv2, detector, image) -> float:
    """Return MediaPipe Pose's strongest upper/full-body confidence."""
    import numpy as np

    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    rgb = (rgb - 0.5) * 2.0
    height, width = rgb.shape[:2]
    scale = min(224 / height, 224 / width)
    resized = cv2.resize(
        rgb,
        (max(1, int(width * scale)), max(1, int(height * scale))),
        interpolation=cv2.INTER_LINEAR,
    )
    top = (224 - resized.shape[0]) // 2
    bottom = 224 - resized.shape[0] - top
    left = (224 - resized.shape[1]) // 2
    right = 224 - resized.shape[1] - left
    input_image = cv2.copyMakeBorder(
        resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(0, 0, 0)
    )
    detector.setInput(np.transpose(input_image, (2, 0, 1))[None, ...])
    outputs = detector.forward(detector.getUnconnectedOutLayersNames())
    logits = outputs[1][0, :, 0].astype(np.float64)
    probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -100, 100)))
    return float(np.max(probabilities))


def _segmented_person_area(cv2, detector, image) -> float:
    """Return the largest connected human-mask area as a fraction of the image."""
    import numpy as np

    resized = cv2.resize(image, (192, 192), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    normalized = (rgb - 0.5) / 0.5
    detector.setInput(cv2.dnn.blobFromImage(normalized))
    output = detector.forward()[0]
    mask = np.argmax(output, axis=0).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if count <= 1:
        return 0.0
    largest = int(np.max(stats[1:, cv2.CC_STAT_AREA]))
    return largest / float(mask.size)


def _looks_photographic_face(image, face) -> bool:
    """Reject flat/grayscale meme faces for borderline YuNet detections."""
    import cv2
    import numpy as np

    height, width = image.shape[:2]
    # Borderline detections inside very tall phone/chat screenshots are a major
    # source of meme, avatar, and animal-face false positives.
    if max(height, width) / max(1, min(height, width)) > 1.95:
        return False
    x, y, face_width, face_height = (float(value) for value in face[:4])
    margin_x, margin_y = face_width * 0.18, face_height * 0.18
    left = max(0, int(x - margin_x))
    top = max(0, int(y - margin_y))
    right = min(width, int(x + face_width + margin_x))
    bottom = min(height, int(y + face_height + margin_y))
    crop = image[top:bottom, left:right]
    if crop.size == 0 or min(crop.shape[:2]) < 14:
        return False

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    histogram = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    probabilities = histogram[histogram > 0] / histogram.sum()
    entropy = float(-(probabilities * np.log2(probabilities)).sum())
    color_distance = float(
        np.mean(crop.max(axis=2).astype(np.float32) - crop.min(axis=2))
    )
    return entropy >= 4.6 and color_distance >= 7.0


async def _download_image(client: httpx.AsyncClient, url: str) -> bytes:
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        declared_size = int(response.headers.get("content-length", "0") or 0)
        if declared_size > MAX_IMAGE_BYTES:
            raise ValueError("图片超过识别大小限制")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise ValueError("图片超过识别大小限制")
            chunks.append(chunk)
        return b"".join(chunks)


def _contains_person(data: bytes) -> bool:
    import numpy as np

    cv2, face_detector, body_detector, pose_detector, segmentation_detector = _detectors()
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError("无法解码图片")

    height, width = image.shape[:2]
    scale = min(1.0, MAX_DETECTION_EDGE / max(height, width))
    if scale < 1:
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    with _detection_lock:
        height, width = image.shape[:2]
        face_detector.setInputSize((width, height))
        _, faces = face_detector.detect(image)
        if faces is not None:
            image_area = height * width
            for face in faces:
                face_width = float(face[2])
                face_height = float(face[3])
                score = float(face[-1])
                # Reject tiny face-like texture fragments that occur in memes.
                large_enough = (
                    min(face_width, face_height) >= 14
                    and face_width * face_height / image_area >= 0.0005
                )
                if not large_enough:
                    continue
                if score >= FACE_STRICT_THRESHOLD:
                    return True
                if score >= FACE_BALANCED_THRESHOLD and _looks_photographic_face(image, face):
                    return True

        # A face-only detector misses backs, phone-covered faces, full-body
        # portraits and tight torso crops. Use progressively more specialized
        # person models only when the faster face check did not match.
        if _body_person_score(cv2, body_detector, image) >= BODY_PERSON_THRESHOLD:
            return True
        if _pose_person_score(cv2, pose_detector, image) >= POSE_PERSON_THRESHOLD:
            return True
        if (
            _segmented_person_area(cv2, segmentation_detector, image)
            >= SEGMENTATION_PERSON_AREA_THRESHOLD
        ):
            return True

    return False


async def classify_portrait_images(urls: list[str]) -> dict[str, bool | None]:
    """Return True/False for classified images and None for download failures."""
    if not urls:
        return {}

    limits = httpx.Limits(max_connections=3, max_keepalive_connections=3)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://tieba.baidu.com/",
    }
    semaphore = asyncio.Semaphore(3)
    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=12.0, limits=limits
    ) as client:
        async def inspect(url: str) -> tuple[str, bool | None]:
            for attempt in range(3):
                try:
                    async with semaphore:
                        data = await asyncio.wait_for(
                            _download_image(client, url), timeout=10
                        )
                    result = await asyncio.to_thread(_contains_person, data)
                    return url, result
                except (httpx.HTTPError, ValueError, TimeoutError):
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (2 ** attempt))
            return url, None

        outcomes = await asyncio.gather(*(inspect(url) for url in urls))
    return dict(outcomes)


async def filter_portrait_images(urls: list[str]) -> list[str]:
    """Backward-compatible helper returning only positively classified URLs."""
    outcomes = await classify_portrait_images(urls)
    return [url for url, is_portrait in outcomes.items() if is_portrait is True]
