"""

Smart Door Security System - Configuration Settings
All system-wide configurations are defined here.
"""

import os
from pathlib import Path

# Base directory
BASE_DIR = Path(__file__).resolve().parent.parent

# Database settings
DATABASE_PATH = BASE_DIR / "database" / "smart_door.db"

# Camera settings
# Use 0 for the default webcam or '/dev/video0' on Raspberry Pi systems.
CAMERA_INDEX = 0  # Default camera index or device path for Raspberry Pi
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
CAMERA_FPS = 30

# Face recognition settings
FACE_RECOGNITION_TOLERANCE = 0.6  # Lower = stricter matching
FACE_DETECTION_MODEL = "hog"  # "hog" for CPU, "cnn" for GPU
FACE_ENCODING_JITTERS = 1  # Higher = more accurate but slower

# Fingerprint sensor settings
FINGERPRINT_PORT = "COM3"  # Change based on your system
FINGERPRINT_BAUD_RATE = 57600
FINGERPRINT_TIMEOUT = 5  # seconds

# Door control settings
DOOR_UNLOCK_DURATION = 10  # seconds before auto-lock (changed from 5 to 10)
DOOR_RELAY_PIN = 17  # GPIO pin for relay (Raspberry Pi)

# Web server settings
WEB_HOST = "127.0.0.1"
WEB_PORT = 5000
SECRET_KEY = os.environ.get("SECRET_KEY", "your-secret-key-change-in-production-123!")

# API settings
API_BASE_URL = f"http://{WEB_HOST}:{WEB_PORT}/api"

# Logging settings
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "system.log"
LOG_LEVEL = "INFO"

# Enrollment settings
ENROLLMENT_DIR = BASE_DIR / "enrollments"
MAX_FACE_SAMPLES = 5  # Number of face samples during enrollment

# Security settings
PASSWORD_MIN_LENGTH = 8
MAX_LOGIN_ATTEMPTS = 5  # Re-added for security
LOCKOUT_DURATION = 300  # seconds
RATE_LIMIT_REQUESTS = 10  # API rate limiting
RATE_LIMIT_WINDOW = 60  # seconds

# Threading settings
THREAD_TIMEOUT = 30  # seconds for thread operations
MAX_CONCURRENT_THREADS = 10

# Camera settings (optimized)
CAMERA_BUFFER_SIZE = 1  # Reduce buffer for faster processing
CAMERA_FRAME_TIMEOUT = 5  # seconds
CAMERA_RETRY_ATTEMPTS = 3

# Sensor settings (optimized)
SENSOR_RETRY_ATTEMPTS = 3
SENSOR_RETRY_DELAY = 1  # seconds
SENSOR_CONNECTION_TIMEOUT = 10  # seconds

# Authentication settings
AUTH_RETRY_ATTEMPTS = 3
AUTH_RETRY_DELAY = 2  # seconds
CONFIDENCE_THRESHOLD = 0.6  # Minimum confidence for face match

# Door settings
DOOR_STATE_CHECK_INTERVAL = 1  # seconds
DOOR_EMERGENCY_TIMEOUT = 60  # seconds max unlock time
DOOR_RETRY_ATTEMPTS = 3

# Memory management
MAX_FRAME_HISTORY = 100  # Limit frame cache
GC_THRESHOLD = 100  # Force garbage collection every N frames

# GUI settings
GUI_UPDATE_INTERVAL = 50  # milliseconds
GUI_WINDOW_WIDTH = 1200
GUI_WINDOW_HEIGHT = 800

# Authentication timeout
AUTH_TIMEOUT = 30  # seconds to complete both authentications
