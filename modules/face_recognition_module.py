"""
Smart Door Security System - Face Recognition Module
Handles face detection, encoding, and matching using face_recognition library.
"""

import cv2
import numpy as np
import face_recognition
import threading
import logging
import gc
import requests
import time
import sys
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any, Union
from dataclasses import dataclass
from enum import Enum

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (
    CAMERA_INDEX, CAMERA_WIDTH, CAMERA_HEIGHT,
    CAMERA_FPS,
    CAMERA_BUFFER_SIZE,
    FACE_RECOGNITION_TOLERANCE, FACE_DETECTION_MODEL, FACE_ENCODING_JITTERS,
    API_BASE_URL,
    CONFIDENCE_THRESHOLD
)
from database.db_manager import FaceEncodingRepository, UserRepository, SystemLogRepository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class FaceStatus(Enum):
    NO_FACE = "No Face Detected"
    FACE_DETECTED = "Face Detected"
    FACE_MATCHED = "Face Matched"
    UNKNOWN_FACE = "Unknown Face"
    MULTIPLE_FACES = "Multiple Faces Detected"
    CAMERA_ERROR = "Camera Error"


@dataclass
class FaceResult:
    status: FaceStatus
    user_id: Optional[int] = None
    user_name: Optional[str] = None
    employee_id: Optional[str] = None
    confidence: float = 0.0
    face_location: Optional[Tuple[int, int, int, int]] = None
    frame: Optional[np.ndarray] = None


class CameraManager:
    """Manages USB webcam access with thread safety."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._camera = None
        self._frame_lock = threading.Lock()
        self._current_frame = None
        self._running = False
        self._capture_thread = None
        self._initialized = True
        self.system_log = SystemLogRepository()

    def start(self) -> bool:
        """Start camera capture — works for USB webcam, Raspberry Pi camera, or both."""
        if self._running:
            return True

        try:
            # Phase 1: USB webcam (works on desktop AND Raspberry Pi)
            logger.info("Trying USB webcam...")
            self._camera = self._open_usb_camera()
            if self._camera is not None and self._camera.isOpened():
                self._running = True
                self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
                self._capture_thread.start()
                self._wait_for_first_frame()
                logger.info("Camera started (USB webcam)")
                self.system_log.info("CameraManager", "Camera started  USB webcam")
                return True
            logger.warning("USB camera not available, trying GStreamer...")

            # Phase 2: GStreamer (handles cameras that resist V4L2)
            self._camera = self._open_gstreamer_camera()
            if self._camera is not None and self._camera.isOpened():
                self._running = True
                self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
                self._capture_thread.start()
                self._wait_for_first_frame()
                logger.info("Camera started (GStreamer)")
                self.system_log.info("CameraManager", "Camera started via GStreamer")
                return True
            logger.warning("GStreamer default not available, trying pipeline...")
            self._camera = self._open_gstreamer_pipeline()
            if self._camera is not None and self._camera.isOpened():
                self._running = True
                self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
                self._capture_thread.start()
                self._wait_for_first_frame()
                logger.info("Camera started (GStreamer pipeline)")
                self.system_log.info("CameraManager", "Camera started via GStreamer pipeline")
                return True

            # Phase 3: Raspberry Pi camera hardware (last resort, hardware-specific)
            logger.warning("No USB camera found, trying Raspberry Pi camera...")
            picam = self._open_picamera2_source()
            if picam is not None:
                self._picamera2 = picam
                self._running = True
                self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
                self._capture_thread.start()
                self._wait_for_first_frame()
                logger.info("Camera started via Picamera2")
                self.system_log.info("CameraManager", "Camera started via Picamera2")
                return True

            logger.error("All camera backends failed — no camera available")
            self.system_log.error("CameraManager", "All camera backends failed")
            return False

        except Exception as e:
            logger.error(f"Camera start error: {e}")
            self.system_log.error("CameraManager", f"Camera start error: {str(e)}")
            self.stop()
            return False

    def _open_usb_camera(self) -> Optional[cv2.VideoCapture]:
        """Open USB webcam with multiple backends and a retry-and-release strategy."""
        backends = []
        v4l2 = getattr(cv2, 'CAP_V4L2', None)
        if v4l2 is not None:
            backends.append(v4l2)
        backends.append(cv2.CAP_ANY)

        for candidate in [CAMERA_INDEX, '/dev/video0', '/dev/video1']:
            for backend in backends:
                for attempt in range(2):
                    logger.info(f"Trying USB camera candidate={candidate} backend={backend} (attempt {attempt+1})")
                    try:
                        cap = cv2.VideoCapture(candidate, backend)
                        if not cap.isOpened():
                            logger.warning(f"  Failed to open: candidate={candidate} backend={backend}")
                            continue

                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, CAMERA_BUFFER_SIZE)

                        validated = self._validate(cap)
                        if validated:
                            logger.info(f"USB camera confirmed: candidate={candidate} backend={backend}")
                            return cap

                        logger.warning(f"  Read validation failed: candidate={candidate} backend={backend} — releasing and retrying")
                        cap.release()
                        del cap

                    except Exception as e:
                        logger.warning(f"  USB open attempt failed for {candidate}/{backend}: {e}")

        return None

    def _open_gstreamer_camera(self) -> Optional[cv2.VideoCapture]:
        """Try GStreamer with auto-detected defaults (no explicit pipeline)."""
        if not hasattr(cv2, 'CAP_GSTREAMER'):
            return None
        try:
            cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_GSTREAMER)
            if cap.isOpened() and self._validate(cap):
                logger.info(f"GStreamer camera opened with default backend")
                return cap
            cap.release()
        except Exception as e:
            logger.warning(f"GStreamer default open failed: {e}")
        return None

    def _open_gstreamer_pipeline(self) -> Optional[cv2.VideoCapture]:
        """Try a set of explicitly-defined GStreamer pipelines for the USB camera."""
        if not hasattr(cv2, 'CAP_GSTREAMER'):
            return None

        pipelines = [
            # libcamera (default on Raspberry Pi OS Bookworm)
            f"libcamerasrc ! video/x-raw,width={CAMERA_WIDTH},height={CAMERA_HEIGHT},framerate={CAMERA_FPS}/1 ! videoconvert ! appsink",
            # V4L2 BGR (most generic USB camera pipeline)
            f"v4l2src device=/dev/video0 ! video/x-raw,format=BGR,width={CAMERA_WIDTH},height={CAMERA_HEIGHT},framerate={CAMERA_FPS}/1 ! videoconvert ! appsink",
            # V4L2 YUY2 (common uncompressed USB format)
            f"v4l2src device=/dev/video0 ! video/x-raw,format=YUY2,width={CAMERA_WIDTH},height={CAMERA_HEIGHT},framerate={CAMERA_FPS}/1 ! videoconvert ! appsink",
            # V4L2 explicitly pointing at device node
            f"v4l2src device={CAMERA_INDEX} ! video/x-raw,format=BGR,width={CAMERA_WIDTH},height={CAMERA_HEIGHT},framerate={CAMERA_FPS}/1 ! videoconvert ! appsink",
        ]

        for pipeline in pipelines:
            try:
                cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
                if cap.isOpened() and self._validate(cap):
                    logger.info(f"GStreamer pipeline confirmed: {pipeline[:60]}...")
                    return cap
                cap.release()
            except Exception as e:
                logger.warning(f"GStreamer pipeline failed ({pipeline[:50]}...): {e}")

        return None

    def _open_camera(self, source: Union[int, str]) -> Optional[cv2.VideoCapture]:
        """Open USB webcam using OpenCV VideoCapture (legacy, kept for compatibility)."""
        candidates = []
        if isinstance(source, str):
            candidates.append(source)
            try:
                candidates.append(int(source))
            except ValueError:
                pass
        else:
            candidates.extend([source, '/dev/video0'])

        for candidate in candidates:
            for backend in [cv2.CAP_ANY]:
                try:
                    cap = cv2.VideoCapture(candidate, backend)
                    if cap.isOpened() and self._validate(cap):
                        logger.info(f"Opened USB camera at index/device={candidate}")
                        return cap
                    cap.release()
                    del cap
                    gc.collect()
                except Exception as e:
                    logger.warning(f"Camera open failed for {candidate}: {e}")

        return None

    def _validate(self, cap: cv2.VideoCapture) -> bool:
        """Validate that the camera can produce a frame."""
        for _ in range(5):
            ret, frame = cap.read()
            if ret and frame is not None:
                return True
            time.sleep(0.15)
        return False

    def _wait_for_first_frame(self, timeout: float = 2.0) -> bool:
        """Block up to timeout seconds until the capture thread has written a frame."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._frame_lock:
                if self._current_frame is not None:
                    return True
            time.sleep(0.05)
        logger.warning("Timed out waiting for first camera frame")
        return False

    def stop(self):
        """Stop camera capture."""
        self._running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=2.0)
        if self._camera:
            self._camera.release()
            self._camera = None
        logger.info("USB camera stopped")

    def is_running(self) -> bool:
        return self._running

    def _capture_loop(self):
        """Continuous frame capture loop."""
        while self._running:
            try:
                if getattr(self, '_picamera2', None) is not None:
                    frame = self._picamera2.capture_array()
                    if frame is not None:
                        if frame.ndim == 3:
                            if frame.shape[2] == 3:
                                frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                            elif frame.shape[2] == 4:
                                frame = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
                        with self._frame_lock:
                            self._current_frame = frame.copy()
                    else:
                        time.sleep(0.01)
                    continue

                if self._camera is None:
                    time.sleep(0.1)
                    continue

                ret, frame = self._camera.read()
                if ret and frame is not None:
                    with self._frame_lock:
                        self._current_frame = frame.copy()
                else:
                    time.sleep(0.01)
            except Exception as e:
                logger.error(f"Capture error: {e}")
                time.sleep(0.1)

    def get_frame(self) -> Optional[np.ndarray]:
        """Get the current frame."""
        with self._frame_lock:
            if self._current_frame is not None:
                return self._current_frame.copy()
        return None


class FaceRecognitionEngine:
    """Face recognition engine for detection and matching."""

    def __init__(self):
        self.camera = CameraManager()
        self.face_repo = FaceEncodingRepository()
        self.user_repo = UserRepository()
        self.system_log = SystemLogRepository()

        self._known_encodings: List[np.ndarray] = []
        self._known_user_data: List[Dict] = []
        self._cache_lock = threading.Lock()
        self._last_cache_update = 0
        self._cache_ttl = 30

        self._current_result: Optional[FaceResult] = None
        self._result_lock = threading.Lock()

    def start(self) -> bool:
        if not self.camera.start():
            return False
        self._refresh_known_faces()
        return True

    def stop(self):
        self.camera.stop()

    def _refresh_known_faces(self):
        try:
            with self._cache_lock:
                encodings_data = self.face_repo.get_all_encodings()
                self._known_encodings = []
                self._known_user_data = []

                for data in encodings_data:
                    self._known_encodings.append(data['encoding'])
                    self._known_user_data.append({
                        'user_id': data['user_id'],
                        'name': data['name'],
                        'employee_id': data['employee_id']
                    })

                self._last_cache_update = time.time()
                logger.info(f"Loaded {len(self._known_encodings)} known faces")

        except Exception as e:
            logger.error(f"Error refreshing known faces: {e}")
            self.system_log.error("FaceRecognition", f"Cache refresh error: {str(e)}")

    def _check_cache_freshness(self):
        if time.time() - self._last_cache_update > self._cache_ttl:
            self._refresh_known_faces()

    def process_frame(self) -> FaceResult:
        frame = self.camera.get_frame()

        if frame is None:
            return FaceResult(status=FaceStatus.CAMERA_ERROR)

        try:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            small_frame = cv2.resize(rgb_frame, (0, 0), fx=0.25, fy=0.25)

            face_locations = face_recognition.face_locations(
                small_frame,
                model=FACE_DETECTION_MODEL
            )

            if not face_locations:
                return FaceResult(status=FaceStatus.NO_FACE, frame=frame)

            if len(face_locations) > 1:
                frame_with_boxes = self._draw_face_boxes(frame, face_locations, scale=4)
                return FaceResult(
                    status=FaceStatus.MULTIPLE_FACES,
                    frame=frame_with_boxes
                )

            face_location = face_locations[0]

            face_encodings = face_recognition.face_encodings(
                small_frame,
                [face_location],
                num_jitters=FACE_ENCODING_JITTERS
            )

            if not face_encodings:
                scaled_location = tuple(coord * 4 for coord in face_location)
                frame_with_box = self._draw_face_box(
                    frame, scaled_location, "Face Detected", (255, 255, 0)
                )
                return FaceResult(
                    status=FaceStatus.FACE_DETECTED,
                    face_location=scaled_location,
                    frame=frame_with_box
                )

            face_encoding = face_encodings[0]
            self._check_cache_freshness()

            with self._cache_lock:
                if not self._known_encodings:
                    scaled_location = tuple(coord * 4 for coord in face_location)
                    frame_with_box = self._draw_face_box(
                        frame, scaled_location, "Unknown Face", (0, 0, 255)
                    )
                    return FaceResult(
                        status=FaceStatus.UNKNOWN_FACE,
                        face_location=scaled_location,
                        frame=frame_with_box
                    )

                face_distances = face_recognition.face_distance(
                    self._known_encodings,
                    face_encoding
                )

                best_match_idx = np.argmin(face_distances)
                best_distance = face_distances[best_match_idx]
                confidence = 1.0 - best_distance

                # Dual-gate: accept only if BOTH the raw Euclidean distance is within
                # the strict tolerance AND the derived confidence meets the minimum.
                # This two-layer check blocks near-miss false positives even when the
                # closest known face happens to be only moderately similar.
                if (best_distance <= FACE_RECOGNITION_TOLERANCE
                        and confidence >= CONFIDENCE_THRESHOLD):
                    user_data = self._known_user_data[best_match_idx]
                    scaled_location = tuple(coord * 4 for coord in face_location)

                    label = f"{user_data['name']} ({confidence*100:.1f}%)"
                    frame_with_box = self._draw_face_box(
                        frame, scaled_location, label, (0, 255, 0)
                    )

                    return FaceResult(
                        status=FaceStatus.FACE_MATCHED,
                        user_id=user_data['user_id'],
                        user_name=user_data['name'],
                        employee_id=user_data['employee_id'],
                        confidence=confidence,
                        face_location=scaled_location,
                        frame=frame_with_box
                    )
                else:
                    # Log low-confidence / out-of-tolerance attempts for audit
                    logger.info(
                        "FaceRecognition: rejected match "
                        f"distance={best_distance:.4f} "
                        f"confidence={confidence:.4f} "
                        f"(tolerance={FACE_RECOGNITION_TOLERANCE}, "
                        f"conf_threshold={CONFIDENCE_THRESHOLD})"
                    )
                    scaled_location = tuple(coord * 4 for coord in face_location)
                    frame_with_box = self._draw_face_box(
                        frame, scaled_location, "Unknown Face", (0, 0, 255)
                    )
                    return FaceResult(
                        status=FaceStatus.UNKNOWN_FACE,
                        face_location=scaled_location,
                        frame=frame_with_box
                    )

                best_match_idx = np.argmin(face_distances)
                best_distance = face_distances[best_match_idx]

                if best_distance <= FACE_RECOGNITION_TOLERANCE:
                    user_data = self._known_user_data[best_match_idx]
                    confidence = 1.0 - best_distance
                    scaled_location = tuple(coord * 4 for coord in face_location)

                    label = f"{user_data['name']} ({confidence*100:.1f}%)"
                    frame_with_box = self._draw_face_box(
                        frame, scaled_location, label, (0, 255, 0)
                    )

                    return FaceResult(
                        status=FaceStatus.FACE_MATCHED,
                        user_id=user_data['user_id'],
                        user_name=user_data['name'],
                        employee_id=user_data['employee_id'],
                        confidence=confidence,
                        face_location=scaled_location,
                        frame=frame_with_box
                    )
                else:
                    scaled_location = tuple(coord * 4 for coord in face_location)
                    frame_with_box = self._draw_face_box(
                        frame, scaled_location, "Unknown Face", (0, 0, 255)
                    )
                    return FaceResult(
                        status=FaceStatus.UNKNOWN_FACE,
                        face_location=scaled_location,
                        frame=frame_with_box
                    )

        except Exception as e:
            logger.error(f"Face processing error: {e}")
            self.system_log.error("FaceRecognition", f"Processing error: {str(e)}")
            return FaceResult(status=FaceStatus.CAMERA_ERROR, frame=frame)

    def _draw_face_box(self, frame: np.ndarray, location: Tuple[int, int, int, int],
                       label: str, color: Tuple[int, int, int]) -> np.ndarray:
        frame_copy = frame.copy()
        top, right, bottom, left = location

        cv2.rectangle(frame_copy, (left, top), (right, bottom), color, 2)
        cv2.rectangle(frame_copy, (left, bottom - 35), (right, bottom), color, cv2.FILLED)
        cv2.putText(
            frame_copy, label, (left + 6, bottom - 10),
            cv2.FONT_HERSHEY_DUPLEX, 0.6, (255, 255, 255), 1
        )

        return frame_copy

    def _draw_face_boxes(self, frame: np.ndarray,
                         locations: List[Tuple[int, int, int, int]],
                         scale: int = 1) -> np.ndarray:
        frame_copy = frame.copy()
        for location in locations:
            top, right, bottom, left = [coord * scale for coord in location]
            cv2.rectangle(frame_copy, (left, top), (right, bottom), (255, 255, 0), 2)
        return frame_copy

    def get_current_frame(self) -> Optional[np.ndarray]:
        return self.camera.get_frame()

    def refresh_cache(self):
        self._refresh_known_faces()


class FaceEnrollment:
    """Handles face enrollment for new users."""

    def __init__(self):
        self.camera = CameraManager()
        self.face_repo = FaceEncodingRepository()
        self.user_repo = UserRepository()
        self.system_log = SystemLogRepository()

    def enroll_face(self, user_id: int, num_samples: int = 5,
                    callback=None) -> Tuple[bool, str]:
        user = self.user_repo.get_by_id(user_id)
        if not user:
            return False, "User not found"

        if not self.camera.is_running():
            if not self.camera.start():
                return False, "Failed to start camera"

        encodings = []
        samples_captured = 0
        max_attempts = num_samples * 10
        attempts = 0

        logger.info(f"Starting face enrollment for user {user_id}")

        while samples_captured < num_samples and attempts < max_attempts:
            attempts += 1
            frame = self.camera.get_frame()

            if frame is None:
                time.sleep(0.1)
                continue

            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            face_locations = face_recognition.face_locations(rgb_frame, model=FACE_DETECTION_MODEL)

            if len(face_locations) != 1:
                time.sleep(0.1)
                continue

            face_encodings = face_recognition.face_encodings(
                rgb_frame,
                face_locations,
                num_jitters=FACE_ENCODING_JITTERS
            )

            if face_encodings:
                encodings.append(face_encodings[0])
                samples_captured += 1

                if callback:
                    callback(samples_captured, num_samples)

                logger.info(f"Captured sample {samples_captured}/{num_samples}")
                time.sleep(0.3)

        if samples_captured < num_samples:
            return False, f"Only captured {samples_captured}/{num_samples} samples"

        average_encoding = np.mean(encodings, axis=0)

        distances = [
            face_recognition.face_distance([average_encoding], enc)[0]
            for enc in encodings
        ]
        quality_score = 1.0 - np.mean(distances)

        try:
            self.face_repo.save_encoding(
                user_id=user_id,
                encoding_array=average_encoding,
                num_samples=num_samples,
                quality_score=quality_score
            )

            self.user_repo.update(user_id, face_enrolled=True)
            self._update_enrollment_status_api(user_id, 'face', True)

            self.system_log.info(
                "FaceEnrollment",
                f"Face enrolled for user {user['first_name']} {user['last_name']}",
                f"Quality score: {quality_score:.2f}"
            )

            return True, f"Face enrolled successfully (Quality: {quality_score*100:.1f}%)"

        except Exception as e:
            logger.error(f"Error saving face encoding: {e}")
            self.system_log.error("FaceEnrollment", f"Save error: {str(e)}")
            return False, f"Error saving face data: {str(e)}"

    def _update_enrollment_status_api(self, user_id: int, biometric_type: str, enrolled: bool):
        try:
            url = f"{API_BASE_URL}/users/{user_id}/enrollment"
            data = {
                'biometric_type': biometric_type,
                'enrolled': enrolled
            }

            response = requests.post(url, json=data, timeout=5)

            if response.status_code == 200:
                logger.info(f"API enrollment status update successful for user {user_id}")
            else:
                logger.warning(f"API enrollment status update failed: {response.status_code}")

        except requests.RequestException as e:
            logger.warning(f"API enrollment status update failed: {e}")
        except Exception as e:
            logger.error(f"Error updating enrollment status via API: {e}")


def get_face_recognition_engine() -> FaceRecognitionEngine:
    return FaceRecognitionEngine()
