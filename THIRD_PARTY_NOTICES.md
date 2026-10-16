# Third-party notices

## aiotieba

This project uses [`aiotieba`](https://github.com/lumina37/aiotieba), an asynchronous Python client for Baidu Tieba.

- Version: 4.7.1
- Author/maintainer: lumina37 / Starry-OvO
- License: The Unlicense
- Documentation: <https://aiotieba.cc/>

No source code from `aiotieba` is vendored here; it is installed as a Python dependency.

## OpenCV

This project uses [`opencv-python-headless`](https://github.com/opencv/opencv-python) for local face and person detection.

- Version: 4.12.0.88
- License: Apache License 2.0
- Images are not sent to an external recognition service.

## YuNet

This project includes the `face_detection_yunet_2023mar.onnx` model from the official OpenCV Zoo for local face detection.

- Source: <https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet>
- License: MIT License
- A copy of the license is included at `app/assets/LICENSE.face_detection_yunet`.

## OpenCV Zoo person models

This project includes three models from the official OpenCV Zoo for local
body-aware portrait detection:

- `object_detection_yolox_2022nov_int8.onnx` ([source](https://github.com/opencv/opencv_zoo/tree/main/models/object_detection_yolox))
- `person_detection_mediapipe_2023mar.onnx` ([source](https://github.com/opencv/opencv_zoo/tree/main/models/person_detection_mediapipe))
- `human_segmentation_pphumanseg_2023mar.onnx` ([source](https://github.com/opencv/opencv_zoo/tree/main/models/human_segmentation_pphumanseg))

The three OpenCV Zoo model directories are licensed under the Apache License 2.0.
