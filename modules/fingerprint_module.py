"""
Smart Door Security System - Fingerprint Module
Handles fingerprint sensor integration, capture, and matching.
Supports common fingerprint sensors like R307, R305, GT-511C3.
"""

import threading
import logging
import time
import hashlib
from typing import Optional, Tuple, Dict, Callable
from dataclasses import dataclass
from enum import Enum
import sys
from pathlib import Path
import queue
import serial
import serial.tools.list_ports
from collections import deque
import weakref
import requests
import json

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import FINGERPRINT_PORT, FINGERPRINT_BAUD_RATE, FINGERPRINT_TIMEOUT, API_BASE_URL
from database.db_manager import FingerprintRepository, UserRepository, SystemLogRepository

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Try to import serial library (for real sensor)
try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False
    logger.warning("PySerial not installed. Running in simulation mode.")


class FingerprintStatus(Enum):
    """Fingerprint sensor/scan status."""
    WAITING = "Waiting for Fingerprint"
    SCANNING = "Scanning..."
    CAPTURED = "Fingerprint Captured"
    MATCHED = "Fingerprint Matched"
    NOT_MATCHED = "Fingerprint Failed"
    SENSOR_ERROR = "Sensor Error"
    TIMEOUT = "Scan Timeout"
    NO_FINGER = "No Finger Detected"
    SENSOR_DISCONNECTED = "Sensor Disconnected"


@dataclass
class FingerprintResult:
    """Result of fingerprint scan/match operation."""
    status: FingerprintStatus
    user_id: Optional[int] = None
    user_name: Optional[str] = None
    employee_id: Optional[str] = None
    fingerprint_id: Optional[int] = None
    confidence: float = 0.0
    message: str = ""


class FingerprintSensor:
    """
    Fingerprint sensor interface.
    Supports both real hardware and simulation mode.
    """
    
    # Command bytes for R307/R305 sensor
    HEADER = bytes([0xEF, 0x01])
    ADDRESS = bytes([0xFF, 0xFF, 0xFF, 0xFF])
    
    # Command codes
    CMD_GET_IMAGE = 0x01
    CMD_GEN_CHAR = 0x02
    CMD_MATCH = 0x03
    CMD_SEARCH = 0x04
    CMD_STORE_CHAR = 0x06
    CMD_LOAD_CHAR = 0x07
    CMD_DELETE_CHAR = 0x0C
    CMD_EMPTY = 0x0D
    CMD_READ_SYS_PARA = 0x0F
    CMD_VERIFY_PASSWORD = 0x13
    CMD_GEN_RANDOM = 0x14
    
    def __init__(self, port: str = None, baud_rate: int = None, simulation: bool = False):
        """
        Initialize fingerprint sensor.
        
        Args:
            port: Serial port (e.g., 'COM3' or '/dev/ttyUSB0')
            baud_rate: Baud rate for serial communication
            simulation: If True, run in simulation mode without hardware
        """
        self.port = port or FINGERPRINT_PORT
        self.baud_rate = baud_rate or FINGERPRINT_BAUD_RATE
        self.simulation = simulation or not SERIAL_AVAILABLE
        
        self._serial = None
        self._connected = False
        self._lock = threading.Lock()
        
        self.fingerprint_repo = FingerprintRepository()
        self.user_repo = UserRepository()
        self.system_log = SystemLogRepository()
        
        # Simulation data
        self._sim_fingerprints: Dict[int, int] = {}  # fingerprint_id -> user_id
        self._sim_next_id = 1
        
        if self.simulation:
            logger.info("Fingerprint sensor running in SIMULATION mode")
            self._load_simulation_data()
    
    def _load_simulation_data(self):
        """Load existing fingerprint mappings for simulation."""
        try:
            fingerprints = self.fingerprint_repo.get_all_fingerprints()
            for fp in fingerprints:
                self._sim_fingerprints[fp['fingerprint_id']] = fp['user_id']
                self._sim_next_id = max(self._sim_next_id, fp['fingerprint_id'] + 1)
            logger.info(f"Loaded {len(self._sim_fingerprints)} fingerprints for simulation")
        except Exception as e:
            logger.error(f"Error loading simulation data: {e}")
    
    def set_simulation(self, enabled: bool):
        """Enable or disable simulation mode."""
        self.simulation = enabled
        if enabled:
            self._load_simulation_data()
            self._connected = True
        else:
            self._connected = False
            if self._serial:
                try:
                    self._serial.close()
                except:
                    pass
                self._serial = None

    def connect(self) -> bool:
        """Connect to the fingerprint sensor."""
        if self.simulation:
            self._connected = True
            return True
        
        try:
            with self._lock:
                self._serial = serial.Serial(
                    port=self.port,
                    baudrate=self.baud_rate,
                    timeout=FINGERPRINT_TIMEOUT
                )
                
                # Verify connection by reading system parameters
                if self._verify_password():
                    self._connected = True
                    logger.info(f"Connected to fingerprint sensor on {self.port}")
                    self.system_log.info("FingerprintSensor", f"Connected on {self.port}")
                    return True
                else:
                    self._serial.close()
                    self._serial = None
                    return False
                    
        except (serial.SerialException, OSError) as e:
            logger.error(f"Failed to connect to sensor: {e}")
            self.system_log.error("FingerprintSensor", f"Connection failed: {str(e)}")
            return False
    
    def disconnect(self):
        """Disconnect from the sensor."""
        with self._lock:
            if self._serial and self._serial.is_open:
                self._serial.close()
            self._serial = None
            self._connected = False
        logger.info("Fingerprint sensor disconnected")
    
    def is_connected(self) -> bool:
        """Check if sensor is connected."""
        return self._connected
    
    def _send_command(self, command: int, data: bytes = b'') -> Tuple[int, bytes]:
        """Send a command to the sensor and receive response."""
        if self.simulation:
            return 0x00, b''
        
        if not self._serial or not self._serial.is_open:
            return 0xFF, b''
        
        # Build packet
        packet_data = bytes([command]) + data
        length = len(packet_data) + 2  # +2 for checksum
        
        packet = (
            self.HEADER +
            self.ADDRESS +
            bytes([0x01]) +  # Package identifier
            bytes([(length >> 8) & 0xFF, length & 0xFF]) +
            packet_data
        )
        
        # Calculate checksum
        checksum = sum(packet[6:]) & 0xFFFF
        packet += bytes([(checksum >> 8) & 0xFF, checksum & 0xFF])
        
        try:
            self._serial.write(packet)
            response = self._serial.read(12)  # Minimum response size
            
            if len(response) < 12:
                return 0xFF, b''
            
            # Parse response
            confirmation_code = response[9]
            return confirmation_code, response[10:-2] if len(response) > 12 else b''
            
        except serial.SerialException as e:
            logger.error(f"Serial communication error: {e}")
            return 0xFF, b''
    
    def _verify_password(self) -> bool:
        """Verify sensor password."""
        if self.simulation:
            return True
        
        data = bytes([0x00, 0x00, 0x00, 0x00])  # Default password
        code, _ = self._send_command(self.CMD_VERIFY_PASSWORD, data)
        return code == 0x00
    
    def capture_fingerprint(self, timeout: float = None) -> FingerprintResult:
        """
        Capture a fingerprint from the sensor.
        
        Args:
            timeout: Maximum time to wait for finger (seconds)
            
        Returns:
            FingerprintResult with capture status
        """
        timeout = timeout or FINGERPRINT_TIMEOUT
        
        if not self._connected:
            return FingerprintResult(
                status=FingerprintStatus.SENSOR_DISCONNECTED,
                message="Sensor not connected"
            )
        
        if self.simulation:
            # Simulation: wait a bit then return captured
            time.sleep(0.5)
            return FingerprintResult(
                status=FingerprintStatus.CAPTURED,
                message="Fingerprint captured (simulation)"
            )
        
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            # Get image
            code, _ = self._send_command(self.CMD_GET_IMAGE)
            
            if code == 0x00:
                # Image captured, generate character file
                code, _ = self._send_command(self.CMD_GEN_CHAR, bytes([0x01]))
                
                if code == 0x00:
                    return FingerprintResult(
                        status=FingerprintStatus.CAPTURED,
                        message="Fingerprint captured successfully"
                    )
                else:
                    return FingerprintResult(
                        status=FingerprintStatus.SENSOR_ERROR,
                        message="Failed to process fingerprint image"
                    )
            elif code == 0x02:
                # No finger detected
                time.sleep(0.1)
                continue
            else:
                return FingerprintResult(
                    status=FingerprintStatus.SENSOR_ERROR,
                    message=f"Sensor error code: {code}"
                )
        
        return FingerprintResult(
            status=FingerprintStatus.TIMEOUT,
            message="Fingerprint capture timed out"
        )
    
    def search_fingerprint(self, timeout: float = None) -> FingerprintResult:
        """
        Capture and search for a matching fingerprint.
        
        Args:
            timeout: Maximum time to wait for finger
            
        Returns:
            FingerprintResult with match status and user info if found
        """
        if not self._connected:
            return FingerprintResult(
                status=FingerprintStatus.SENSOR_DISCONNECTED,
                message="Sensor not connected"
            )
        
        # First capture the fingerprint
        capture_result = self.capture_fingerprint(timeout)
        
        if capture_result.status != FingerprintStatus.CAPTURED:
            return capture_result
        
        if self.simulation:
            # Simulation: randomly match with existing fingerprint
            if self._sim_fingerprints:
                # For demo, match the first registered fingerprint
                import random
                if random.random() < 0.8:  # 80% success rate in simulation
                    fp_id = list(self._sim_fingerprints.keys())[0]
                    user_data = self.fingerprint_repo.get_by_fingerprint_id(fp_id)
                    
                    if user_data and user_data.get('is_active', True):
                        return FingerprintResult(
                            status=FingerprintStatus.MATCHED,
                            user_id=user_data['user_id'],
                            user_name=f"{user_data['first_name']} {user_data['last_name']}",
                            employee_id=user_data['employee_id'],
                            fingerprint_id=fp_id,
                            confidence=0.95,
                            message="Fingerprint matched (simulation)"
                        )
            
            return FingerprintResult(
                status=FingerprintStatus.NOT_MATCHED,
                message="No matching fingerprint found (simulation)"
            )
        
        # Real sensor: search in database
        # Search in all stored fingerprints (0 to 162 for most sensors)
        code, data = self._send_command(
            self.CMD_SEARCH,
            bytes([0x01, 0x00, 0x00, 0x00, 0xA3])  # CharBuffer1, start=0, count=163
        )
        
        if code == 0x00 and len(data) >= 4:
            # Match found
            fp_id = (data[0] << 8) | data[1]
            match_score = (data[2] << 8) | data[3]
            
            # Look up user from database
            user_data = self.fingerprint_repo.get_by_fingerprint_id(fp_id)
            
            if user_data:
                if not user_data.get('is_active', True):
                    return FingerprintResult(
                        status=FingerprintStatus.NOT_MATCHED,
                        fingerprint_id=fp_id,
                        message="User account is disabled"
                    )
                
                return FingerprintResult(
                    status=FingerprintStatus.MATCHED,
                    user_id=user_data['user_id'],
                    user_name=f"{user_data['first_name']} {user_data['last_name']}",
                    employee_id=user_data['employee_id'],
                    fingerprint_id=fp_id,
                    confidence=match_score / 200.0,  # Normalize score
                    message="Fingerprint matched"
                )
            else:
                return FingerprintResult(
                    status=FingerprintStatus.NOT_MATCHED,
                    fingerprint_id=fp_id,
                    message="Fingerprint found but user not in database"
                )
        
        elif code == 0x09:
            return FingerprintResult(
                status=FingerprintStatus.NOT_MATCHED,
                message="No matching fingerprint found"
            )
        else:
            return FingerprintResult(
                status=FingerprintStatus.SENSOR_ERROR,
                message=f"Search error code: {code}"
            )
    
    def enroll_fingerprint(self, user_id: int, finger_position: str = 'right_index',
                           callback: Callable[[str], None] = None) -> Tuple[bool, str, int]:
        """
        Enroll a new fingerprint for a user.
        
        Args:
            user_id: User ID to associate with fingerprint
            finger_position: Which finger (e.g., 'right_index')
            callback: Optional callback for status updates
            
        Returns:
            Tuple of (success, message, fingerprint_id)
        """
        # Verify user exists
        user = self.user_repo.get_by_id(user_id)
        if not user:
            return False, "User not found", -1
        
        if not self._connected:
            return False, "Sensor not connected", -1
        
        if callback:
            callback("Place finger on sensor...")
        
        if self.simulation:
            # Simulation enrollment
            time.sleep(1)
            if callback:
                callback("First scan captured, lift finger...")
            time.sleep(0.5)
            if callback:
                callback("Place same finger again...")
            time.sleep(1)
            if callback:
                callback("Processing...")
            
            fp_id = self._sim_next_id
            self._sim_next_id += 1
            self._sim_fingerprints[fp_id] = user_id
            
            # Save to database
            template_hash = hashlib.sha256(f"sim_{user_id}_{fp_id}".encode()).hexdigest()
            self.fingerprint_repo.save_fingerprint(
                user_id=user_id,
                fingerprint_id=fp_id,
                template_hash=template_hash,
                finger_position=finger_position
            )
            
            self.system_log.info(
                "FingerprintEnrollment",
                f"Enrolled fingerprint for {user['first_name']} {user['last_name']}",
                f"Fingerprint ID: {fp_id}"
            )
            
            return True, "Fingerprint enrolled successfully (simulation)", fp_id
        
        # Real sensor enrollment
        # First capture
        result1 = self.capture_fingerprint()
        if result1.status != FingerprintStatus.CAPTURED:
            return False, f"First capture failed: {result1.message}", -1
        
        if callback:
            callback("First scan captured. Remove finger...")
        time.sleep(1)
        
        if callback:
            callback("Place the same finger again...")
        
        # Second capture to CharBuffer2
        start_time = time.time()
        while time.time() - start_time < FINGERPRINT_TIMEOUT:
            code, _ = self._send_command(self.CMD_GET_IMAGE)
            if code == 0x00:
                code, _ = self._send_command(self.CMD_GEN_CHAR, bytes([0x02]))
                if code == 0x00:
                    break
            time.sleep(0.1)
        else:
            return False, "Second capture timed out", -1
        
        # Generate template
        code, _ = self._send_command(0x05)  # RegModel
        if code != 0x00:
            return False, "Failed to create fingerprint template", -1
        
        # Find next available ID
        fp_id = self._get_next_fingerprint_id()
        
        # Store template
        code, _ = self._send_command(
            self.CMD_STORE_CHAR,
            bytes([0x01, (fp_id >> 8) & 0xFF, fp_id & 0xFF])
        )
        
        if code != 0x00:
            return False, f"Failed to store fingerprint (error: {code})", -1
        
        # Save mapping to database
        template_hash = hashlib.sha256(f"{user_id}_{fp_id}_{time.time()}".encode()).hexdigest()
        self.fingerprint_repo.save_fingerprint(
            user_id=user_id,
            fingerprint_id=fp_id,
            template_hash=template_hash,
            finger_position=finger_position
        )
        
        # Update user's fingerprint_enrolled status in database
        self.user_repo.update(user_id, fingerprint_enrolled=True)
        
        # Call backend API to update enrollment status
        self._update_enrollment_status_api(user_id, 'fingerprint', True)
        
        self.system_log.info(
            "FingerprintEnrollment",
            f"Enrolled fingerprint for {user['first_name']} {user['last_name']}",
            f"Fingerprint ID: {fp_id}"
        )
        
        return True, "Fingerprint enrolled successfully", fp_id
    
    def _get_next_fingerprint_id(self) -> int:
        """Get the next available fingerprint ID in the sensor."""
        # For simplicity, count existing fingerprints
        fingerprints = self.fingerprint_repo.get_all_fingerprints()
        if not fingerprints:
            return 1
        
        existing_ids = {fp['fingerprint_id'] for fp in fingerprints}
        for i in range(1, 163):  # Most sensors support up to 162 fingerprints
            if i not in existing_ids:
                return i
        
        raise Exception("Fingerprint storage full")
    
    def delete_fingerprint(self, user_id: int) -> bool:
        """Delete a user's fingerprint from sensor and database."""
        fp_data = self.fingerprint_repo.get_by_user_id(user_id)
        
        if not fp_data:
            return True  # Nothing to delete
        
        fp_id = fp_data['fingerprint_id']
        
        if not self.simulation and self._connected:
            # Delete from sensor
            code, _ = self._send_command(
                self.CMD_DELETE_CHAR,
                bytes([(fp_id >> 8) & 0xFF, fp_id & 0xFF, 0x00, 0x01])
            )
            
            if code != 0x00:
                logger.warning(f"Failed to delete fingerprint {fp_id} from sensor")
        
        # Remove from simulation cache
        if self.simulation and fp_id in self._sim_fingerprints:
            del self._sim_fingerprints[fp_id]
        
        # Delete from database
        self.fingerprint_repo.delete_fingerprint(user_id)
        
        self.system_log.info(
            "FingerprintDeletion",
            f"Deleted fingerprint for user {user_id}",
            f"Fingerprint ID: {fp_id}"
        )
        
        return True
    
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


class FingerprintManager:
    """
    High-level manager for fingerprint authentication.
    Provides async scanning with callbacks.
    """
    
    _instance = None
    _lock = threading.Lock()
    _simulation = False
    
    def __new__(cls, simulation: bool = False):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
                    cls._simulation = simulation
        return cls._instance
    
    def __init__(self, simulation: bool = False):
        if self._initialized:
            return
        
        self.sensor = FingerprintSensor(simulation=self._simulation or simulation)
        self._scanning = False
        self._scan_thread = None
        self._callback = None
        self._result_lock = threading.Lock()
        self._last_result: Optional[FingerprintResult] = None
        self._initialized = True
    
    def set_simulation(self, enabled: bool):
        """Enable or disable simulation mode."""
        self.sensor.set_simulation(enabled)

    def start(self) -> bool:
        """Initialize and connect to the sensor."""
        return self.sensor.connect()
    
    def stop(self):
        """Stop scanning and disconnect."""
        self._scanning = False
        if self._scan_thread:
            self._scan_thread.join(timeout=2.0)
        self.sensor.disconnect()
    
    def is_connected(self) -> bool:
        """Check if sensor is ready."""
        return self.sensor.is_connected()
    
    def scan_once(self, timeout: float = None) -> FingerprintResult:
        """
        Perform a single fingerprint scan and match.
        
        Args:
            timeout: Maximum time to wait for finger
            
        Returns:
            FingerprintResult with match status
        """
        result = self.sensor.search_fingerprint(timeout)
        with self._result_lock:
            self._last_result = result
        return result
    
    def start_continuous_scan(self, callback: Callable[[FingerprintResult], None],
                               interval: float = 1.0):
        """
        Start continuous scanning in background.
        
        Args:
            callback: Function called with each scan result
            interval: Time between scans (seconds)
        """
        if self._scanning:
            return
        
        self._callback = callback
        self._scanning = True
        self._scan_thread = threading.Thread(
            target=self._continuous_scan_loop,
            args=(interval,),
            daemon=True
        )
        self._scan_thread.start()
    
    def _continuous_scan_loop(self, interval: float):
        """Background scanning loop."""
        while self._scanning:
            result = self.sensor.search_fingerprint(timeout=interval)
            
            with self._result_lock:
                self._last_result = result
            
            if self._callback:
                self._callback(result)
            
            time.sleep(0.1)
    
    def stop_continuous_scan(self):
        """Stop continuous scanning."""
        self._scanning = False
        if self._scan_thread:
            self._scan_thread.join(timeout=2.0)
    
    def get_last_result(self) -> Optional[FingerprintResult]:
        """Get the most recent scan result."""
        with self._result_lock:
            return self._last_result
    
    def enroll(self, user_id: int, finger_position: str = 'right_index',
               callback: Callable[[str], None] = None) -> Tuple[bool, str, int]:
        """
        Enroll a fingerprint for a user.
        
        Args:
            user_id: User to enroll
            finger_position: Which finger
            callback: Progress callback
            
        Returns:
            Tuple of (success, message, fingerprint_id)
        """
        # Stop continuous scanning during enrollment
        was_scanning = self._scanning
        if was_scanning:
            self.stop_continuous_scan()
        
        try:
            return self.sensor.enroll_fingerprint(user_id, finger_position, callback)
        finally:
            if was_scanning and self._callback:
                self.start_continuous_scan(self._callback)
    
    def delete(self, user_id: int) -> bool:
        """Delete a user's fingerprint."""
        return self.sensor.delete_fingerprint(user_id)


# Convenience function
def get_fingerprint_manager(simulation: bool = False) -> FingerprintManager:
    """Get or create the fingerprint manager singleton."""
    manager = FingerprintManager(simulation=simulation)
    return manager
