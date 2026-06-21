"""
Smart Door Security System - Face Recognition Module
Handles face detection, encoding, and matching using face_recognition library.
"""

import cv2
import numpy as np
import face_recognition
import threading
import logging
from typing import Optional, Tuple, List, Dict, Any, Union
from dataclasses import dataclass
from enum import Enum
import time
import sys
from pathlib import Path
import queue
import weakref
from collections import deque
import gc
import requests
import json

# Optional Raspberry Pi camera support
try:
    from picamera2 import Picamera2
    PICAMERA2_AVAILABLE = True
except ImportError:
    PICAMERA2_AVAILABLE = False

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import (
    CAMERA_INDEX, CAMERA_WIDTH, CAMERA_HEIGHT,
    CAMERA_FPS,
    CAMERA_BUFFER_SIZE,
    FACE_RECOGNITION_TOLERANCE, FACE_DETECTION_MODEL, FACE_ENCODING_JITTERS,
    API_BASE_URL
)
from database.db_manager import FaceEncodingRepository, UserRepository, SystemLogRepository

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class FaceStatus(Enum):
    """Face detection/recognition status."""
    NO_FACE = "No Face Detected"
    FACE_DETECTED = "Face Detected"
    FACE_MATCHED = "Face Matched"
    UNKNOWN_FACE = "Unknown Face"
    MULTIPLE_FACES = "Multiple Faces Detected"
    CAMERA_ERROR = "Camera Error"


@dataclass
class FaceResult:
    """Result of face recognition operation."""
    status: FaceStatus
    user_id: Optional[int] = None
    user_name: Optional[str] = None
    employee_id: Optional[str] = None
    confidence: float = 0.0
    face_location: Optional[Tuple[int, int, int, int]] = None
    frame: Optional[np.ndarray] = None


class CameraManager:
    """Manages webcam access with thread safety."""
    
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
        self._picamera2 = None
        self._frame_lock = threading.Lock()
        self._current_frame = None
        self._running = False
        self._capture_thread = None
        self._initialized = True
        self.system_log = SystemLogRepository()
    
    def start(self) -> bool:
        """Start camera capture."""
        if self._running:
            return True

        try:
            if PICAMERA2_AVAILABLE:
                logger.info("Attempting Picamera2 first for Raspberry Pi camera")
                self._picamera2 = self._open_picamera2_source()
                if self._picamera2 is not None:
                    self._running = True
                    self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
                    self._capture_thread.start()
                    self.system_log.info("CameraManager", "Camera started via Picamera2")
                    return True
                logger.warning("Picamera2 fallback failed, trying OpenCV capture")

            self._camera = self._open_camera_source(CAMERA_INDEX)
            if not self._camera or not self._camera.isOpened():
                logger.warning("Primary camera open failed, trying gstreamer fallback")
                self._camera = self._open_gstreamer_source()

            if not self._camera or not self._camera.isOpened():
                logger.error("Failed to open camera")
                self.system_log.error("CameraManager", "Failed to open camera")
                return False

            # Set camera properties
            self._camera.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
            self._camera.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
            self._camera.set(cv2.CAP_PROP_BUFFERSIZE, CAMERA_BUFFER_SIZE)

            # Verify we can read an initial frame
            success = False
            for attempt in range(5):
                ret, frame = self._camera.read()
                if ret and frame is not None:
                    success = True
                    break
                time.sleep(0.2)

            if not success:
                logger.error("Camera opened but failed to read initial frame")
                self.system_log.error("CameraManager", "Camera opened but failed to read initial frame")
                self._camera.release()
                self._camera = None
                return False

            self._running = True
            self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
            self._capture_thread.start()

            logger.info("Camera started successfully")
            self.system_log.info("CameraManager", "Camera started")
            return True

        except Exception as e:
            logger.error(f"Camera start error: {e}")
            self.system_log.error("CameraManager", f"Camera start error: {str(e)}")
            return False

    def _open_camera_source(self, source: Union[int, str]) -> Optional[cv2.VideoCapture]:
        """Try opening the camera source with candidate device paths and backends."""
        candidates = []
        if isinstance(source, str):
            candidates.append(source)
            try:
                numeric = int(source)
                candidates.append(numeric)
            except ValueError:
                pass
        else:
            candidates.extend([source, '/dev/video0', '/dev/video1'])

        v4l_backend = getattr(cv2, 'CAP_V4L2', None)
        backends = [backend for backend in [v4l_backend, cv2.CAP_ANY] if backend is not None]

        for candidate in candidates:
            for backend in backends:
                try:
                    cap = cv2.VideoCapture(candidate, backend)
                    if cap.isOpened():
                        if self._validate_capture_source(cap):
                            logger.info(f"Opened camera candidate={candidate} backend={backend}")
                            return cap
                        logger.warning(f"Camera candidate opened but failed validation: {candidate} backend={backend}")
                        cap.release()
                        del cap
                        gc.collect()
                        continue
                    cap.release()
                    del cap
                    gc.collect()
                except Exception as e:
                    logger.warning(f"Camera open attempt failed for {candidate} backend={backend}: {e}")

        logger.warning("No direct camera source opened; trying gstreamer fallback")
        return None

    def _validate_capture_source(self, cap: cv2.VideoCapture) -> bool:
        """Validate that the capture source can produce at least one frame."""
        for _ in range(5):
            ret, frame = cap.read()
            if ret and frame is not None:
                return True
            time.sleep(0.15)
        return False

    def _open_gstreamer_source(self) -> Optional[cv2.VideoCapture]:
        """Try opening the Raspberry Pi camera with a GStreamer pipeline."""
        if not hasattr(cv2, 'CAP_GSTREAMER'):
            return None

        pipelines = [
            f"libcamerasrc ! video/x-raw,width={CAMERA_WIDTH},height={CAMERA_HEIGHT},framerate={CAMERA_FPS}/1 ! videoconvert ! appsink",
            f"v4l2src device=/dev/video0 ! video/x-raw,format=BGR,width={CAMERA_WIDTH},height={CAMERA_HEIGHT},framerate={CAMERA_FPS}/1 ! videoconvert ! appsink",
            f"v4l2src device=/dev/video0 ! video/x-raw,format=YUY2,width={CAMERA_WIDTH},height={CAMERA_HEIGHT},framerate={CAMERA_FPS}/1 ! videoconvert ! appsink"
        ]

        for pipeline in pipelines:
            try:
                cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
                if cap.isOpened():
                    logger.info(f"Opened camera via GStreamer pipeline: {pipeline}")
                    return cap
                cap.release()
            except Exception as e:
                logger.warning(f"GStreamer camera open failed for pipeline='{pipeline}': {e}")
        return None

    def _open_picamera2_source(self):
        """Try opening the Raspberry Pi camera with Picamera2."""
        if not PICAMERA2_AVAILABLE:
            return None

        try:
            picam = Picamera2()
            config = picam.create_preview_configuration(main={"size": (CAMERA_WIDTH, CAMERA_HEIGHT)})
            picam.configure(config)
            picam.start()
            frame = picam.capture_array()
            if frame is None:
                picam.stop()
                picam.close()
                return None
            logger.info("Opened camera via Picamera2")
            return picam
        except Exception as e:
            logger.warning(f"Picamera2 camera open failed: {e}")
            gc.collect()
            return None

    def _capture_loop(self):
        """Continuous frame capture loop."""
        while self._running:
            try:
                if self._picamera2 is not None:
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
    
    def stop(self):
        """Stop camera capture."""
        self._running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=2.0)
        if self._camera:
            self._camera.release()
            self._camera = None
        if self._picamera2 is not None:
            try:
                self._picamera2.stop()
            except Exception:
                pass
            try:
                self._picamera2.close()
            except Exception:
                pass
            self._picamera2 = None
        logger.info("Camera stopped")
    
    def is_running(self) -> bool:
        return self._running


class FaceRecognitionEngine:
    """Face recognition engine for detection and matching."""
    
    def __init__(self):
        self.camera = CameraManager()
        self.face_repo = FaceEncodingRepository()
        self.user_repo = UserRepository()
        self.system_log = SystemLogRepository()
        
        # Cache for known face encodings
        self._known_encodings: List[np.ndarray] = []
        self._known_user_data: List[Dict] = []
        self._cache_lock = threading.Lock()
        self._last_cache_update = 0
        self._cache_ttl = 30  # seconds
        
        # Recognition state
        self._current_result: Optional[FaceResult] = None
        self._result_lock = threading.Lock()
    
    def start(self) -> bool:
        """Start the face recognition engine."""
        if not self.camera.start():
            return False
        self._refresh_known_faces()
        return True
    
    def stop(self):
        """Stop the face recognition engine."""
        self.camera.stop()
    
    def _refresh_known_faces(self):
        """Refresh the cache of known face encodings."""
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
        """Check if cache needs refresh."""
        if time.time() - self._last_cache_update > self._cache_ttl:
            self._refresh_known_faces()
    
    def process_frame(self) -> FaceResult:
        """Process current camera frame for face recognition."""
        frame = self.camera.get_frame()
        
        if frame is None:
            return FaceResult(status=FaceStatus.CAMERA_ERROR)
        
        try:
            # Convert BGR to RGB for face_recognition
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            
            # Resize for faster processing
            small_frame = cv2.resize(rgb_frame, (0, 0), fx=0.25, fy=0.25)
            
            # Detect faces
            face_locations = face_recognition.face_locations(
                small_frame, 
                model=FACE_DETECTION_MODEL
            )
            
            if not face_locations:
                return FaceResult(status=FaceStatus.NO_FACE, frame=frame)
            
            if len(face_locations) > 1:
                # Draw rectangles for multiple faces
                frame_with_boxes = self._draw_face_boxes(frame, face_locations, scale=4)
                return FaceResult(
                    status=FaceStatus.MULTIPLE_FACES,
                    frame=frame_with_boxes
                )
            
            # Single face detected - proceed with recognition
            face_location = face_locations[0]
            
            # Get face encoding
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
            
            # Refresh cache if needed
            self._check_cache_freshness()
            
            # Compare with known faces
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
                
                # Calculate face distances
                face_distances = face_recognition.face_distance(
                    self._known_encodings, 
                    face_encoding
                )
                
                best_match_idx = np.argmin(face_distances)
                best_distance = face_distances[best_match_idx]
                
                # Check if match is within tolerance
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
        """Draw a rectangle around a detected face."""
        frame_copy = frame.copy()
        top, right, bottom, left = location
        
        # Draw rectangle
        cv2.rectangle(frame_copy, (left, top), (right, bottom), color, 2)
        
        # Draw label background
        cv2.rectangle(frame_copy, (left, bottom - 35), (right, bottom), color, cv2.FILLED)
        
        # Draw label text
        cv2.putText(
            frame_copy, label, (left + 6, bottom - 10),
            cv2.FONT_HERSHEY_DUPLEX, 0.6, (255, 255, 255), 1
        )
        
        return frame_copy
    
    def _draw_face_boxes(self, frame: np.ndarray, 
                         locations: List[Tuple[int, int, int, int]],
                         scale: int = 1) -> np.ndarray:
        """Draw rectangles around multiple detected faces."""
        frame_copy = frame.copy()
        for location in locations:
            top, right, bottom, left = [coord * scale for coord in location]
            cv2.rectangle(frame_copy, (left, top), (right, bottom), (255, 255, 0), 2)
        return frame_copy
    
    def get_current_frame(self) -> Optional[np.ndarray]:
        """Get current camera frame without processing."""
        return self.camera.get_frame()
    
    def refresh_cache(self):
        """Force refresh of known faces cache."""
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
        """
        Enroll a face for a user.
        
        Args:
            user_id: The user ID to enroll
            num_samples: Number of face samples to capture
            callback: Optional callback for progress updates (samples_captured, total)
        
        Returns:
            Tuple of (success, message)
        """
        # Verify user exists
        user = self.user_repo.get_by_id(user_id)
        if not user:
            return False, "User not found"
        
        # Start camera if not running
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
            
            # Convert to RGB
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            
            # Detect faces
            face_locations = face_recognition.face_locations(rgb_frame, model=FACE_DETECTION_MODEL)
            
            if len(face_locations) != 1:
                time.sleep(0.1)
                continue
            
            # Get encoding
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
                time.sleep(0.3)  # Brief pause between captures
        
        if samples_captured < num_samples:
            return False, f"Only captured {samples_captured}/{num_samples} samples"
        
        # Calculate average encoding
        average_encoding = np.mean(encodings, axis=0)
        
        # Calculate quality score (consistency of encodings)
        distances = [
            face_recognition.face_distance([average_encoding], enc)[0]
            for enc in encodings
        ]
        quality_score = 1.0 - np.mean(distances)
        
        # Save to database
        try:
            self.face_repo.save_encoding(
                user_id=user_id,
                encoding_array=average_encoding,
                num_samples=num_samples,
                quality_score=quality_score
            )
            
            # Update user's face_enrolled status in database
            self.user_repo.update(user_id, face_enrolled=True)
            
            # Call backend API to update enrollment status
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
        """
        Call backend API to update enrollment status.
        
        Args:
            user_id: User ID
            biometric_type: 'face' or 'fingerprint'
            enrolled: True if enrolled, False if not
        """
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


# Convenience function for external use
def get_face_recognition_engine() -> FaceRecognitionEngine:
    """Get or create the face recognition engine singleton."""
    return FaceRecognitionEngine()
