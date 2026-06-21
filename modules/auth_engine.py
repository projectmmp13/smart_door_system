"""
Smart Door Security System - Authentication Engine
Implements multi-factor authentication requiring BOTH face AND fingerprint.
"""

import threading
import logging
import time
from typing import Optional, Callable, Tuple
from enum import Enum
from dataclasses import dataclass, field
import sys
from pathlib import Path
import queue
import weakref
from collections import deque
import gc

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import AUTH_TIMEOUT
from database.db_manager import AccessLogRepository, UserRepository, SystemLogRepository

from modules.face_recognition_module import (
    FaceRecognitionEngine, FaceResult, FaceStatus
)
from modules.fingerprint_module import (
    FingerprintManager, FingerprintResult, FingerprintStatus
)
from modules.door_control import DoorController, DoorState

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class AuthState(Enum):
    """Authentication state machine states."""
    IDLE = "Waiting for Authentication"
    FACE_PENDING = "Face Verification Pending"
    FACE_MATCHED = "Face Verified - Awaiting Fingerprint"
    FINGERPRINT_PENDING = "Fingerprint Verification Pending"
    VERIFYING = "Verifying Identity..."
    ACCESS_GRANTED = "ACCESS GRANTED"
    ACCESS_DENIED = "ACCESS DENIED"
    TIMEOUT = "Authentication Timeout"
    ERROR = "Authentication Error"


@dataclass
class AuthSession:
    """Represents an authentication session."""
    state: AuthState = AuthState.IDLE
    face_result: Optional[FaceResult] = None
    fingerprint_result: Optional[FingerprintResult] = None
    face_user_id: Optional[int] = None
    fingerprint_user_id: Optional[int] = None
    matched_user_id: Optional[int] = None
    matched_user_name: Optional[str] = None
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    failure_reason: Optional[str] = None
    confidence: float = 0.0


class AuthenticationEngine:
    """
    Multi-factor authentication engine.
    Access granted ONLY when:
    1. Face matches a registered user
    2. Fingerprint matches the SAME user
    3. User is active in the database
    """
    
    def __init__(self, simulation: bool = False):
        self.simulation = simulation
        
        # Initialize components
        self.face_engine = FaceRecognitionEngine()
        self.fingerprint_manager = FingerprintManager(simulation=simulation)
        self.door_controller = DoorController(simulation=simulation)
        
        # Repositories
        self.access_log = AccessLogRepository()
        self.user_repo = UserRepository()
        self.system_log = SystemLogRepository()
        
        # Authentication state
        self._current_session: Optional[AuthSession] = None
        self._session_lock = threading.Lock()
        self._running = False
        self._auth_thread: Optional[threading.Thread] = None
        
        # Callbacks
        self._state_callbacks: list = []
        self._result_callbacks: list = []
        
        # Configuration
        self.auth_timeout = AUTH_TIMEOUT
    
    def add_state_callback(self, callback: Callable[[AuthSession], None]):
        """Add callback for authentication state changes."""
        if callback not in self._state_callbacks:
            self._state_callbacks.append(callback)
    
    def remove_state_callback(self, callback: Callable[[AuthSession], None]):
        """Remove state callback."""
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)
    
    def add_result_callback(self, callback: Callable[[AuthSession], None]):
        """Add callback for authentication results (success/failure)."""
        if callback not in self._result_callbacks:
            self._result_callbacks.append(callback)
    
    def _notify_state_change(self, session: AuthSession):
        """Notify all state callbacks."""
        for callback in self._state_callbacks:
            try:
                callback(session)
            except Exception as e:
                logger.error(f"State callback error: {e}")
    
    def _notify_result(self, session: AuthSession):
        """Notify all result callbacks."""
        for callback in self._result_callbacks:
            try:
                callback(session)
            except Exception as e:
                logger.error(f"Result callback error: {e}")
    
    def start(self) -> bool:
        """Start the authentication engine."""
        logger.info("Starting authentication engine...")
        
        # Start face recognition
        if not self.face_engine.start():
            logger.error("Failed to start face recognition")
            self.system_log.error("AuthEngine", "Failed to start face recognition")
            return False
        
        # Start fingerprint sensor
        if not self.fingerprint_manager.start():
            logger.warning("Fingerprint sensor not available - may be in simulation mode")
        
        self._running = True
        self._current_session = AuthSession()
        
        # Start authentication loop
        self._auth_thread = threading.Thread(target=self._auth_loop, daemon=True)
        self._auth_thread.start()
        
        logger.info("Authentication engine started")
        self.system_log.info("AuthEngine", "Authentication engine started")
        return True
    
    def stop(self):
        """Stop the authentication engine."""
        self._running = False
        
        if self._auth_thread:
            self._auth_thread.join(timeout=3.0)
        
        self.face_engine.stop()
        self.fingerprint_manager.stop()
        self.door_controller.cleanup()
        
        logger.info("Authentication engine stopped")
        self.system_log.info("AuthEngine", "Authentication engine stopped")
    
    def _auth_loop(self):
        """Main authentication loop."""
        while self._running:
            try:
                with self._session_lock:
                    if self._current_session is None:
                        self._current_session = AuthSession()
                    
                    session = self._current_session
                
                # Check for timeout
                if session.state not in [AuthState.IDLE, AuthState.ACCESS_GRANTED, AuthState.ACCESS_DENIED]:
                    elapsed = time.time() - session.start_time
                    if elapsed > self.auth_timeout:
                        self._handle_timeout(session)
                        continue
                
                # State machine
                if session.state == AuthState.IDLE:
                    self._process_idle_state(session)
                
                elif session.state == AuthState.FACE_MATCHED:
                    self._process_fingerprint_verification(session)
                
                elif session.state in [AuthState.ACCESS_GRANTED, AuthState.ACCESS_DENIED]:
                    # Wait before resetting
                    time.sleep(3)
                    self._reset_session()
                
                time.sleep(0.05)  # Small delay to prevent CPU spinning
                
            except Exception as e:
                logger.error(f"Auth loop error: {e}")
                self.system_log.error("AuthEngine", f"Auth loop error: {str(e)}")
                time.sleep(1)
    
    def _process_idle_state(self, session: AuthSession):
        """Process authentication when in idle state - look for faces."""
        face_result = self.face_engine.process_frame()
        
        if face_result.status == FaceStatus.FACE_MATCHED:
            # Face matched - verify user is active
            user = self.user_repo.get_by_id(face_result.user_id)
            
            if user and user.get('is_active', False):
                session.state = AuthState.FACE_MATCHED
                session.face_result = face_result
                session.face_user_id = face_result.user_id
                session.start_time = time.time()
                
                logger.info(f"Face matched: {face_result.user_name}")
                self._notify_state_change(session)
            else:
                # User not active
                logger.warning(f"Face matched but user inactive: {face_result.user_name}")
    
    def _process_fingerprint_verification(self, session: AuthSession):
        """Process fingerprint after face is matched."""
        fp_result = self.fingerprint_manager.scan_once(timeout=2.0)
        
        if fp_result.status == FingerprintStatus.MATCHED:
            session.fingerprint_result = fp_result
            session.fingerprint_user_id = fp_result.user_id
            
            # Critical check: SAME USER for both?
            if session.face_user_id == session.fingerprint_user_id:
                # Double verification: check user is still active
                user = self.user_repo.get_by_id(session.face_user_id)
                
                if user and user.get('is_active', False):
                    self._grant_access(session, user)
                else:
                    self._deny_access(session, "User account is disabled")
            else:
                # Different users for face and fingerprint
                self._deny_access(
                    session, 
                    "Face and fingerprint belong to different users"
                )
        
        elif fp_result.status == FingerprintStatus.NOT_MATCHED:
            session.fingerprint_result = fp_result
            self._deny_access(session, "Fingerprint not recognized")
        
        elif fp_result.status in [FingerprintStatus.TIMEOUT, FingerprintStatus.NO_FINGER]:
            # Still waiting for fingerprint
            pass
        
        elif fp_result.status == FingerprintStatus.SENSOR_ERROR:
            self._deny_access(session, "Fingerprint sensor error")
    
    def _grant_access(self, session: AuthSession, user: dict):
        """Grant access to authenticated user."""
        session.state = AuthState.ACCESS_GRANTED
        session.matched_user_id = user['id']
        session.matched_user_name = f"{user['first_name']} {user['last_name']}"
        session.end_time = time.time()
        session.confidence = (
            (session.face_result.confidence if session.face_result else 0) +
            (session.fingerprint_result.confidence if session.fingerprint_result else 0)
        ) / 2
        
        # Unlock door
        self.door_controller.unlock(
            reason=f"Authenticated: {session.matched_user_name}"
        )
        
        # Log access
        self.access_log.log_access(
            user_id=session.matched_user_id,
            event_type='ENTRY',
            result='SUCCESS',
            face_match=True,
            fingerprint_match=True,
            confidence_score=session.confidence
        )
        
        logger.info(f"ACCESS GRANTED: {session.matched_user_name}")
        self.system_log.info(
            "AuthEngine",
            f"Access granted to {session.matched_user_name}",
            f"Confidence: {session.confidence:.2f}"
        )
        
        self._notify_state_change(session)
        self._notify_result(session)
    
    def _deny_access(self, session: AuthSession, reason: str):
        """Deny access."""
        session.state = AuthState.ACCESS_DENIED
        session.failure_reason = reason
        session.end_time = time.time()
        
        # Ensure door is locked
        self.door_controller.lock(reason="Access denied")
        
        # Log failure
        self.access_log.log_access(
            user_id=session.face_user_id,
            event_type='ENTRY',
            result='DENIED',
            face_match=session.face_result is not None and 
                       session.face_result.status == FaceStatus.FACE_MATCHED,
            fingerprint_match=session.fingerprint_result is not None and
                              session.fingerprint_result.status == FingerprintStatus.MATCHED,
            failure_reason=reason
        )
        
        logger.warning(f"ACCESS DENIED: {reason}")
        self.system_log.warning("AuthEngine", f"Access denied: {reason}")
        
        self._notify_state_change(session)
        self._notify_result(session)
    
    def _handle_timeout(self, session: AuthSession):
        """Handle authentication timeout."""
        session.state = AuthState.TIMEOUT
        session.failure_reason = "Authentication timeout"
        session.end_time = time.time()
        
        # Log timeout
        self.access_log.log_access(
            user_id=session.face_user_id,
            event_type='ENTRY',
            result='FAILED',
            face_match=session.face_result is not None,
            fingerprint_match=False,
            failure_reason="Timeout"
        )
        
        logger.warning("Authentication timeout")
        self.system_log.warning("AuthEngine", "Authentication timeout")
        
        self._notify_state_change(session)
        self._notify_result(session)
        
        # Reset after brief delay
        time.sleep(2)
        self._reset_session()
    
    def _reset_session(self):
        """Reset authentication session."""
        with self._session_lock:
            self._current_session = AuthSession()
            self._notify_state_change(self._current_session)
    
    def get_current_session(self) -> AuthSession:
        """Get current authentication session."""
        with self._session_lock:
            if self._current_session is None:
                self._current_session = AuthSession()
            return self._current_session
    
    def get_face_frame(self):
        """Get current camera frame for display."""
        return self.face_engine.get_current_frame()
    
    def process_face(self) -> FaceResult:
        """Process a single frame for face detection."""
        return self.face_engine.process_frame()
    
    def cancel_authentication(self):
        """Cancel current authentication attempt."""
        with self._session_lock:
            if self._current_session and self._current_session.state not in [
                AuthState.IDLE, AuthState.ACCESS_GRANTED, AuthState.ACCESS_DENIED
            ]:
                self._current_session.state = AuthState.ACCESS_DENIED
                self._current_session.failure_reason = "Cancelled"
                self._notify_state_change(self._current_session)
        
        self._reset_session()


# Convenience function
def get_auth_engine(simulation: bool = False) -> AuthenticationEngine:
    """Get or create the authentication engine."""
    return AuthenticationEngine(simulation=simulation)
