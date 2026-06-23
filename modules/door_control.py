"""
Smart Door Security System - Door Control Module
Controls door mechanism via servo motor with auto-return functionality.
"""

import threading
import time
import logging
from typing import Optional, Callable
from dataclasses import dataclass
from enum import Enum
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import configuration
try:
    from config.settings import (
        DOOR_SERVO_PIN, DOOR_SERVO_OPEN_ANGLE, DOOR_SERVO_CLOSED_ANGLE,
        DOOR_UNLOCK_DURATION, DOOR_SERVO_PWM_FREQ
    )
    from database.db_manager import SystemLogRepository
except ImportError:
    import sys as _sys
    _sys.path.insert(0, str(PROJECT_ROOT))
    from config.settings import (
        DOOR_SERVO_PIN, DOOR_SERVO_OPEN_ANGLE, DOOR_SERVO_CLOSED_ANGLE,
        DOOR_UNLOCK_DURATION, DOOR_SERVO_PWM_FREQ
    )
    from database.db_manager import SystemLogRepository

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Try to import servo control library
try:
    from gpiozero import AngularServo
    from gpiozero.pins.pigpio import PiGPIOFactory
    import gpiozero
    GPIOZERO_AVAILABLE = True
except ImportError:
    GPIOZERO_AVAILABLE = False

# Try RPi.GPIO as fallback for PWM servo control
try:
    import RPi.GPIO as GPIO
    RPI_GPIO_AVAILABLE = True
except ImportError:
    RPI_GPIO_AVAILABLE = False


class DoorState(Enum):
    """Door lock state."""
    LOCKED = "Door Locked"
    UNLOCKED = "Door Unlocked"
    UNLOCKING = "Unlocking..."
    LOCKING = "Locking..."
    ERROR = "Door Error"


@dataclass
class DoorStatus:
    """Current door status."""
    state: DoorState
    time_until_lock: float = 0.0
    last_unlock_time: Optional[float] = None
    message: str = ""


class ServoController:
    """
    Abstracts servo motor hardware behind a simple rotate-to-angle interface.
    Uses gpiozero.AngularServo when available (smooth, built-in PWM).
    Falls back to RPi.GPIO software PWM.
    Runs in simulation mode when neither library is present.
    """

    def __init__(self, pin: int, open_angle: float, closed_angle: float,
                 pwm_freq: float = 50.0, simulation: bool = False):
        self.pin = pin
        self.open_angle = open_angle
        self.closed_angle = closed_angle
        self.pwm_freq = pwm_freq
        self.simulation = simulation or not (GPIOZERO_AVAILABLE or RPI_GPIO_AVAILABLE)
        self._servo = None
        self._pwm = None
        self._current_angle: Optional[float] = None

        if not self.simulation:
            self._init_servo()
        else:
            self._current_angle = closed_angle

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_servo(self):
        """Attach to the servo hardware."""
        try:
            if GPIOZERO_AVAILABLE:
                try:
                    factory = PiGPIOFactory()
                    self._servo = AngularServo(
                        self.pin,
                        min_angle=self.closed_angle,
                        max_angle=self.open_angle,
                        initial_angle=self.closed_angle,
                        pin_factory=factory,
                    )
                except Exception:
                    self._servo = AngularServo(
                        self.pin,
                        min_angle=self.closed_angle,
                        max_angle=self.open_angle,
                        initial_angle=self.closed_angle,
                    )
                self._current_angle = self.closed_angle
                logger.info(f"Servo initialised on GPIO {self.pin} via gpiozero")

            elif RPI_GPIO_AVAILABLE:
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self.pin, GPIO.OUT)
                self._pwm = GPIO.PWM(self.pin, self.pwm_freq)
                self._pwm.start(0)
                self._current_angle = self.closed_angle
                self._write_angle(self.closed_angle)
                logger.info(f"Servo initialised on GPIO {self.pin} via RPi.GPIO PWM")

        except Exception as e:
            logger.error(f"Servo initialisation failed: {e}")
            self.simulation = True

    # ------------------------------------------------------------------
    # Angle ↔ duty-cycle conversion (SG90 / MG90S standard)
    # 0.5 ms pulse → 0°, 2.5 ms pulse → 180°  (at 50 Hz)
    # ------------------------------------------------------------------

    @staticmethod
    def _angle_to_duty(angle: float, freq: float) -> float:
        """Convert 0-180° angle to PWM duty-cycle percentage."""
        # pulse_width (ms) = 0.5 + (angle / 180) * 2.0
        pulse_ms = 0.5 + (angle / 180.0) * 2.0
        return (pulse_ms / (1000.0 / freq)) * 100.0

    def _write_angle(self, angle: float):
        """Send a raw PWM duty-cycle to the servo (RPi.GPIO path)."""
        if self._pwm is not None:
            duty = self._angle_to_duty(angle, self.pwm_freq)
            self._pwm.ChangeDutyCycle(duty)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def rotate_to(self, angle: float, step: float = 5.0, delay: float = 0.01):
        """
        Move the servo to *angle* degrees.

        *angle* is clamped to [closed_angle, open_angle].
        The servo steps in increments of *step* degrees so the motion is
        smooth and does not slam the mechanism.
        """
        angle = max(min(angle, self.open_angle), self.closed_angle)
        start = self._current_angle if self._current_angle is not None else angle
        self._current_angle = angle

        if self.simulation:
            logger.info(f"[SIM] Servo → {angle:.0f}°  (from {start:.0f}°)")
            return

        try:
            if GPIOZERO_AVAILABLE and self._servo is not None:
                self._servo.angle = angle
                time.sleep(0.4)   # give the servo time to reach position
            elif RPI_GPIO_AVAILABLE and self._pwm is not None:
                # Step-wise movement for smoother motion
                direction = 1 if angle >= start else -1
                for a in range(int(start), int(angle) + 1, int(step) * direction):
                    self._write_angle(a)
                    time.sleep(delay)
        except Exception as e:
            logger.error(f"Servo move error: {e}")

    def open(self):
        """Rotate servo to the open (unlock) angle."""
        self.rotate_to(self.open_angle)

    def close(self):
        """Rotate servo back to the closed (lock) angle."""
        self.rotate_to(self.closed_angle)

    def cleanup(self):
        """Release hardware resources."""
        try:
            if GPIOZERO_AVAILABLE and self._servo is not None:
                self._servo.close()
                self._servo = None
            if RPI_GPIO_AVAILABLE:
                if self._pwm is not None:
                    self._pwm.stop()
                    self._pwm = None
                GPIO.cleanup(self.pin)
            logger.info("Servo cleaned up")
        except Exception as e:
            logger.error(f"Servo cleanup error: {e}")


class DoorController:
    """
    Controls door mechanism via servo motor with auto-lock functionality.

    On face-match / authentication success:
        unlock() → servo rotates 90° clockwise  (door opens)
        after DOOR_UNLOCK_DURATION seconds → servo rotates 90° anticlockwise (door closes)
    """

    _instance = None
    _lock = threading.Lock()
    _init_simulation = False
    _init_servo_pin = None

    def __new__(cls, servo_pin: int = None, simulation: bool = False):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
                    cls._init_simulation = simulation
                    cls._init_servo_pin = servo_pin
        return cls._instance

    def __init__(self, servo_pin: int = None, simulation: bool = False):
        if self._initialized:
            return

        self.servo_pin = self._init_servo_pin or servo_pin or DOOR_SERVO_PIN
        self.simulation = (
            self._init_simulation
            or simulation
            or not (GPIOZERO_AVAILABLE or RPI_GPIO_AVAILABLE)
        )
        self.unlock_duration = DOOR_UNLOCK_DURATION

        self._state = DoorState.LOCKED
        self._state_lock = threading.RLock()
        self._auto_lock_timer: Optional[threading.Timer] = None
        self._last_unlock_time: Optional[float] = None
        self._callbacks: list = []

        # Hardware layer — servo replaces relay entirely
        self._servo_ctrl = ServoController(
            pin=self.servo_pin,
            open_angle=DOOR_SERVO_OPEN_ANGLE,
            closed_angle=DOOR_SERVO_CLOSED_ANGLE,
            pwm_freq=DOOR_SERVO_PWM_FREQ,
            simulation=self.simulation,
        )

        self.system_log = SystemLogRepository()
        self._initialized = True

        if self.simulation:
            logger.info("Door controller running in SIMULATION mode")
        else:
            logger.info(
                f"Door servo configured — pin={self.servo_pin}  "
                f"open={DOOR_SERVO_OPEN_ANGLE}°  closed={DOOR_SERVO_CLOSED_ANGLE}°"
            )

    # ------------------------------------------------------------------
    # Callback helpers
    # ------------------------------------------------------------------

    def add_state_callback(self, callback: Callable[[DoorStatus], None]):
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def remove_state_callback(self, callback: Callable[[DoorStatus], None]):
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _notify_callbacks(self):
        status = self.get_status()
        for callback in self._callbacks:
            try:
                callback(status)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def get_status(self) -> DoorStatus:
        with self._state_lock:
            time_until_lock = 0.0
            if self._state == DoorState.UNLOCKED and self._last_unlock_time:
                elapsed = time.time() - self._last_unlock_time
                time_until_lock = max(0, self.unlock_duration - elapsed)
            return DoorStatus(
                state=self._state,
                time_until_lock=time_until_lock,
                last_unlock_time=self._last_unlock_time,
                message=self._state.value,
            )

    def get_state(self) -> DoorState:
        with self._state_lock:
            return self._state

    def is_locked(self) -> bool:
        return self.get_state() == DoorState.LOCKED

    def is_unlocked(self) -> bool:
        return self.get_state() == DoorState.UNLOCKED

    # ------------------------------------------------------------------
    # Core servo actions — triggered immediately on face detection
    # ------------------------------------------------------------------

    def unlock(self, duration: float = None, reason: str = "Face detected") -> bool:
        """
        Rotate servo clockwise to OPEN angle (door unlock).

        Called from the face-detection / auth-success path.
        After *duration* seconds the servo returns to CLOSED angle automatically.
        """
        duration = duration or self.unlock_duration

        with self._state_lock:
            if self._auto_lock_timer:
                self._auto_lock_timer.cancel()

            self._state = DoorState.UNLOCKING
            self._notify_callbacks()

            try:
                self._servo_ctrl.open()

                self._state = DoorState.UNLOCKED
                self._last_unlock_time = time.time()

                logger.info(
                    f"Door UNLOCKED (servo → {DOOR_SERVO_OPEN_ANGLE}°): {reason}"
                )
                self.system_log.info(
                    "DoorController",
                    f"Door unlocked (servo {DOOR_SERVO_OPEN_ANGLE}°): {reason}",
                )

                # Schedule auto-return to closed position
                self._auto_lock_timer = threading.Timer(duration, self._auto_lock)
                self._auto_lock_timer.daemon = True
                self._auto_lock_timer.start()

                self._notify_callbacks()
                return True

            except Exception as e:
                logger.error(f"Unlock failed: {e}")
                self._state = DoorState.ERROR
                self.system_log.error("DoorController", f"Unlock failed: {str(e)}")
                self._notify_callbacks()
                return False

    def lock(self, reason: str = "Auto-lock timer") -> bool:
        """
        Rotate servo anticlockwise back to CLOSED angle (door lock).
        """
        with self._state_lock:
            if self._auto_lock_timer:
                self._auto_lock_timer.cancel()
                self._auto_lock_timer = None

            self._state = DoorState.LOCKING
            self._notify_callbacks()

            try:
                self._servo_ctrl.close()

                self._state = DoorState.LOCKED
                self._last_unlock_time = None

                logger.info(
                    f"Door LOCKED (servo → {DOOR_SERVO_CLOSED_ANGLE}°): {reason}"
                )
                self.system_log.info(
                    "DoorController",
                    f"Door locked (servo {DOOR_SERVO_CLOSED_ANGLE}°): {reason}",
                )

                self._notify_callbacks()
                return True

            except Exception as e:
                logger.error(f"Lock failed: {e}")
                self._state = DoorState.ERROR
                self.system_log.error("DoorController", f"Lock failed: {str(e)}")
                self._notify_callbacks()
                return False

    def _auto_lock(self):
        """Auto-lock callback — closes the door after unlock duration."""
        self.lock(reason="Auto-lock timer")

    def set_unlock_duration(self, duration: float):
        if duration > 0:
            self.unlock_duration = duration

    def emergency_lock(self) -> bool:
        """Emergency immediate lock."""
        with self._state_lock:
            if self._auto_lock_timer:
                self._auto_lock_timer.cancel()
                self._auto_lock_timer = None

            try:
                self._servo_ctrl.close()
                self._state = DoorState.LOCKED
                self._last_unlock_time = None
                logger.warning("Emergency lock activated")
                self.system_log.warning("DoorController", "Emergency lock activated")
                self._notify_callbacks()
                return True
            except Exception as e:
                logger.error(f"Emergency lock failed: {e}")
                return False

    def cleanup(self):
        """Release resources and ensure door is locked."""
        if self._auto_lock_timer:
            self._auto_lock_timer.cancel()
            self._auto_lock_timer = None

        self._servo_ctrl.cleanup()

        if self.system_log:
            try:
                self.system_log.info("DoorController", "Servo controller cleaned up")
            except Exception:
                pass

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass


class DoorMonitor:
    """
    Monitors door state and fires periodic callbacks.
    Used by the GUI to show time-until-lock countdown.
    """

    def __init__(self, controller: DoorController, update_interval: float = 0.5):
        self.controller = controller
        self.update_interval = update_interval
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._callbacks: list = []

    def add_callback(self, callback: Callable[[DoorStatus], None]):
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[DoorStatus], None]):
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def start(self):
        if self._running:
            return
        self._running = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def stop(self):
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2.0)

    def _monitor_loop(self):
        while self._running:
            status = self.controller.get_status()
            for callback in self._callbacks:
                try:
                    callback(status)
                except Exception as e:
                    logger.error(f"Monitor callback error: {e}")
            time.sleep(self.update_interval)


# Convenience function
def get_door_controller(simulation: bool = False) -> DoorController:
    controller = DoorController(simulation=simulation)
    return controller
