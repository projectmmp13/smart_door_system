"""Face recognition diagnostic utility.

Usage:
  python diagnostics/face_diagnostic.py --image path/to/image.jpg
  python diagnostics/face_diagnostic.py --camera 0

The script runs detection, computes an embedding, prints top-5 distances
to enrolled users, and saves debug images to diagnostics/output/.
"""

import argparse
import os
from pathlib import Path
import cv2
import numpy as np
import face_recognition
import logging

from modules.face_recognition_module import FaceRecognitionEngine, CameraManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("face_diagnostic")


def ensure_output_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def annotate_and_save(frame, location, label, out_path: Path):
    top, right, bottom, left = location
    frame_copy = frame.copy()
    cv2.rectangle(frame_copy, (left, top), (right, bottom), (0, 255, 0), 2)
    cv2.putText(frame_copy, label, (left + 6, bottom - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
    cv2.imwrite(str(out_path), frame_copy)


def run_on_image(engine: FaceRecognitionEngine, image_path: Path, out_dir: Path):
    img = cv2.imread(str(image_path))
    if img is None:
        logger.error("Failed to read image: %s", image_path)
        return

    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    small = cv2.resize(rgb, (0, 0), fx=0.25, fy=0.25)

    locations = face_recognition.face_locations(small)

    if not locations:
        logger.info("No faces found in image")
        return

    # Use first face
    loc = locations[0]
    encodings = face_recognition.face_encodings(small, [loc], num_jitters=0)
    if not encodings:
        logger.info("Failed to compute encoding")
        return

    encoding = encodings[0]

    # Ensure cache is fresh
    engine.refresh_cache()

    known = engine._known_encodings
    users = engine._known_user_data

    if not known:
        logger.info("No known encodings in database")
        return

    distances = face_recognition.face_distance(known, encoding)
    order = np.argsort(distances)[:5]

    logger.info("Top matches (index, user, distance):")
    for idx in order:
        user = users[idx]
        logger.info("  %d: %s (employee=%s) dist=%.4f", idx, user.get('name'), user.get('employee_id'), float(distances[idx]))

    best_idx = int(np.argmin(distances))
    best_dist = float(distances[best_idx])
    logger.info("Best match idx=%d dist=%.4f", best_idx, best_dist)

    # Save annotated output
    scaled_location = tuple(int(coord * 4) for coord in loc)
    out_img_path = out_dir / f"annotated_{image_path.name}"
    annotate_and_save(img, scaled_location, f"dist={best_dist:.4f}", out_img_path)
    logger.info("Saved annotated image to %s", out_img_path)


def run_on_camera(engine: FaceRecognitionEngine, camera_index: int, out_dir: Path):
    cam = CameraManager()
    if not cam.start():
        logger.error("Failed to start camera")
        return

    # grab single frame
    frame = None
    for _ in range(30):
        frame = cam.get_frame()
        if frame is not None:
            break
    if frame is None:
        logger.error("No frame captured from camera")
        cam.stop()
        return

    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    small = cv2.resize(rgb, (0, 0), fx=0.25, fy=0.25)
    locations = face_recognition.face_locations(small)
    if not locations:
        logger.info("No faces found in camera frame")
        cam.stop()
        return

    loc = locations[0]
    encodings = face_recognition.face_encodings(small, [loc], num_jitters=0)
    if not encodings:
        logger.info("Failed to compute encoding from camera frame")
        cam.stop()
        return

    encoding = encodings[0]
    engine.refresh_cache()
    known = engine._known_encodings
    users = engine._known_user_data
    if not known:
        logger.info("No known encodings in database")
        cam.stop()
        return

    distances = face_recognition.face_distance(known, encoding)
    order = np.argsort(distances)[:5]

    logger.info("Top matches (index, user, distance):")
    for idx in order:
        user = users[idx]
        logger.info("  %d: %s (employee=%s) dist=%.4f", idx, user.get('name'), user.get('employee_id'), float(distances[idx]))

    best_idx = int(np.argmin(distances))
    best_dist = float(distances[best_idx])
    logger.info("Best match idx=%d dist=%.4f", best_idx, best_dist)

    scaled_location = tuple(int(coord * 4) for coord in loc)
    out_img_path = out_dir / "annotated_camera.jpg"
    annotate_and_save(frame, scaled_location, f"dist={best_dist:.4f}", out_img_path)
    logger.info("Saved annotated camera image to %s", out_img_path)

    cam.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', type=str, help='Path to image file')
    parser.add_argument('--camera', type=int, help='Camera index to use')
    parser.add_argument('--out', type=str, default='diagnostics/output', help='Output directory')
    args = parser.parse_args()

    out_dir = Path(args.out)
    ensure_output_dir(out_dir)

    engine = FaceRecognitionEngine()

    # load known faces into cache
    engine.refresh_cache()

    if args.image:
        run_on_image(engine, Path(args.image), out_dir)
    elif args.camera is not None:
        run_on_camera(engine, args.camera, out_dir)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
