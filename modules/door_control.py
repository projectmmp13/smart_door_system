"""
Smart Door Security System - Door Control Module
Handles door lock/unlock via relay or servo motor with auto-lock functionality.
"""

import threading
import time
import logging
from typing import Optional, Callable
from dataclasses import dataclass
from enum import Enum
import sys
from pathlib import Path
import queue
import weakref
from collections import deque

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import configuration
try:
    from config.settings import DOOR_UNLOCK_DURATION, DOOR_RELAY_PIN
    from database.db_manager import SystemLogRepository
except ImportError:
    # Fallback for when running from different directory
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    from config.settings import DOOR_UNLOCK_DURATION, DOOR_RELAY_PIN
    from database.db_manager import SystemLogRepository

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Try to import GPIO library (for Raspberry Pi)
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    logger.info("RPi.GPIO not available. Running in simulation mode.")


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
    time_until_lock: float = 0.0  # seconds until auto-lock
    last_unlock_time: Optional[float] = None
    message: str = ""


class DoorController:
    """
    Controls door lock mechanism via relay or servo.
    Supports GPIO (Raspberry Pi) and simulation mode.
    """
    
    _instance = None
    _lock = threading.Lock()
    _init_simulation = False
    _init_relay_pin = None
    
    def __new__(cls, relay_pin: int = None, simulation: bool = False):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
                    cls._init_simulation = simulation
                    cls._init_relay_pin = relay_pin
        return cls._instance
    
    def __init__(self, relay_pin: int = None, simulation: bool = False):
        if self._initialized:
            return
        
        self.relay_pin = self._init_relay_pin or relay_pin or DOOR_RELAY_PIN
        self.simulation = self._init_simulation or simulation or not GPIO_AVAILABLE
        self.unlock_duration = DOOR_UNLOCK_DURATION
        
        self._state = DoorState.LOCKED
        self._state_lock = threading.RLock()
        self._auto_lock_timer: Optional[threading.Timer] = None
        self._last_unlock_time: Optional[float] = None
        self._callbacks: list = []
        
        self.system_log = SystemLogRepository()
        self._initialized = True
        
        if self.simulation:
            logger.info("Door controller running in SIMULATION mode")
        else:
            self._init_gpio()
    
    def _init_gpio(self):
        """Initialize GPIO for relay control."""
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.relay_pin, GPIO.OUT)
            GPIO.output(self.relay_pin, GPIO.LOW)  # Start locked
            logger.info(f"GPIO initialized on pin {self.relay_pin}")
            self.system_log.info("DoorController", f"GPIO initialized on pin {self.relay_pin}")
        except Exception as e:
            logger.error(f"GPIO initialization failed: {e}")
            self.simulation = True
    
    def add_state_callback(self, callback: Callable[[DoorStatus], None]):
        """Add a callback to be notified of state changes."""
        if callback not in self._callbacks:
            self._callbacks.append(callback)
    
    def remove_state_callback(self, callback: Callable[[DoorStatus], None]):
        """Remove a state change callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)
    
    def _notify_callbacks(self):
        """Notify all registered callbacks of state change."""
        status = self.get_status()
        for callback in self._callbacks:
            try:
                callback(status)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    def get_status(self) -> DoorStatus:
        """Get current door status."""
        with self._state_lock:
            time_until_lock = 0.0
            if self._state == DoorState.UNLOCKED and self._last_unlock_time:
                elapsed = time.time() - self._last_unlock_time
                time_until_lock = max(0, self.unlock_duration - elapsed)
            
            return DoorStatus(
                state=self._state,
                time_until_lock=time_until_lock,
                last_unlock_time=self._last_unlock_time,
                message=self._state.value
            )
    
    def get_state(self) -> DoorState:
        """Get current door state."""
        with self._state_lock:
            return self._state
    
    def is_locked(self) -> bool:
        """Check if door is locked."""
        return self.get_state() == DoorState.LOCKED
    
    def is_unlocked(self) -> bool:
        """Check if door is unlocked."""
        return self.get_state() == DoorState.UNLOCKED
    
    def unlock(self, duration: float = None, reason: str = "Manual") -> bool:
        """
        Unlock the door.
        
        Args:
            duration: How long to keep unlocked (seconds). Uses default if None.
            reason: Reason for unlocking (for logging)
            
        Returns:
            True if unlock successful
        """
        duration = duration or self.unlock_duration
        
        with self._state_lock:
            # Cancel any pending auto-lock
            if self._auto_lock_timer:
                self._auto_lock_timer.cancel()
            
            self._state = DoorState.UNLOCKING
            self._notify_callbacks()
            
            try:
                if not self.simulation:
                    GPIO.output(self.relay_pin, GPIO.HIGH)  # Activate relay
                
                self._state = DoorState.UNLOCKED
                self._last_unlock_time = time.time()
                
                logger.info(f"Door unlocked: {reason}")
                self.system_log.info("DoorController", f"Door unlocked: {reason}")
                
                # Schedule auto-lock (changed from 5 seconds to 10 seconds)
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
    
    def lock(self, reason: str = "Manual") -> bool:
        """
        Lock the door immediately.
        
        Args:
            reason: Reason for locking (for logging)
            
        Returns:
            True if lock successful
        """
        with self._state_lock:
            # Cancel any pending auto-lock
            if self._auto_lock_timer:
                self._auto_lock_timer.cancel()
                self._auto_lock_timer = None
            
            self._state = DoorState.LOCKING
            self._notify_callbacks()
            
            try:
                if not self.simulation:
                    GPIO.output(self.relay_pin, GPIO.LOW)  # Deactivate relay
                
                self._state = DoorState.LOCKED
                self._last_unlock_time = None
                
                logger.info(f"Door locked: {reason}")
                self.system_log.info("DoorController", f"Door locked: {reason}")
                
                self._notify_callbacks()
                return True
                
            except Exception as e:
                logger.error(f"Lock failed: {e}")
                self._state = DoorState.ERROR
                self.system_log.error("DoorController", f"Lock failed: {str(e)}")
                self._notify_callbacks()
                return False
    
    def _auto_lock(self):
        """Auto-lock callback for timer."""
        self.lock(reason="Auto-lock timer")
    
    def set_unlock_duration(self, duration: float):
        """Set the default unlock duration."""
        if duration > 0:
            self.unlock_duration = duration
    
    def emergency_lock(self) -> bool:
        """Emergency lock - immediate lock without logging extensively."""
        with self._state_lock:
            if self._auto_lock_timer:
                self._auto_lock_timer.cancel()
                self._auto_lock_timer = None
            
            try:
                if not self.simulation:
                    GPIO.output(self.relay_pin, GPIO.LOW)
                
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
        """Clean up resources."""
        # Cancel any pending timer
        if self._auto_lock_timer:
            self._auto_lock_timer.cancel()
        
        # Ensure door is locked
        self.lock(reason="System shutdown")
        
        # Clean up GPIO
        if not self.simulation and GPIO_AVAILABLE:
            try:
                GPIO.cleanup(self.relay_pin)
            except Exception as e:
                logger.error(f"GPIO cleanup error: {e}")
    
    def __del__(self):
        """Destructor - ensure cleanup."""
        try:
            self.cleanup()
        except:
            pass


class DoorMonitor:
    """
    Monitors door state and provides countdown updates.
    Useful for GUI updates showing time until auto-lock.
    """
    
    def __init__(self, controller: DoorController, update_interval: float = 0.5):
        self.controller = controller
        self.update_interval = update_interval
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._callbacks: list = []
    
    def add_callback(self, callback: Callable[[DoorStatus], None]):
        """Add a callback for periodic status updates."""
        if callback not in self._callbacks:
            self._callbacks.append(callback)
    
    def remove_callback(self, callback: Callable[[DoorStatus], None]):
        """Remove a callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)
    
    def start(self):
        """Start monitoring."""
        if self._running:
            return
        
        self._running = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
    
    def stop(self):
        """Stop monitoring."""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2.0)
    
    def _monitor_loop(self):
        """Monitoring loop."""
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
    """Get or create the door controller singleton."""
    controller = DoorController(simulation=simulation)
    return controller
