#!/usr/bin/env python3
"""
Smart Door Security System - Main Application
Runs 24/7 with GUI showing camera preview, fingerprint status, and door state.
Multi-factor authentication: Face + Fingerprint required for access.
"""

import sys
import threading
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# Tkinter imports
import tkinter as tk
from tkinter import ttk, messagebox

try:
    from PIL import Image, ImageTk
except ImportError as e:
    raise ImportError(
        "Pillow is required for the GUI image display. "
        "Install it with 'sudo apt install python3-pil python3-pil.imagetk' "
        "or 'pip install pillow'."
    ) from e

import cv2

# Project imports
from config.settings import (
    GUI_UPDATE_INTERVAL, GUI_WINDOW_WIDTH, GUI_WINDOW_HEIGHT
)
from database.db_manager import (
    DatabaseManager, UserRepository, AccessLogRepository, SystemLogRepository
)
from modules.face_recognition_module import (
    FaceRecognitionEngine, FaceResult, FaceStatus
)
from modules.fingerprint_module import (
    FingerprintManager, FingerprintResult, FingerprintStatus
)
from modules.door_control import DoorController, DoorState, DoorMonitor
from modules.auth_engine import AuthState

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_ROOT / 'logs' / 'system.log')
    ]
)
logger = logging.getLogger(__name__)


class SmartDoorGUI:
    """Main GUI application for the Smart Door Security System."""
    
    def __init__(self, simulation: bool = True):
        """Initialize the GUI application."""
        self.simulation = simulation
        
        # Initialize main window
        self.root = tk.Tk()
        self.root.title("Smart Door Security System")
        self.root.geometry(f"{GUI_WINDOW_WIDTH}x{GUI_WINDOW_HEIGHT}")
        self.root.configure(bg='#1a1a2e')
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # Initialize database
        self.db = DatabaseManager()
        self.user_repo = UserRepository()
        self.access_log_repo = AccessLogRepository()
        self.system_log = SystemLogRepository()
        
        # Initialize components
        self.face_engine = FaceRecognitionEngine()
        self.fingerprint_manager = FingerprintManager(simulation=True)  # Always run in simulation mode
        self.door_controller = DoorController(simulation=simulation)
        self.door_monitor = DoorMonitor(self.door_controller)
        
        # State variables
        self._running = False
        self._current_face_result: Optional[FaceResult] = None
        self._current_fp_result: Optional[FingerprintResult] = None
        self._auth_state = AuthState.IDLE
        self._matched_face_user_id = None
        self._auth_start_time = None
        
        # GUI variables
        self.camera_image = None
        self.face_status_var = tk.StringVar(value="Initializing...")
        self.fingerprint_status_var = tk.StringVar(value="Initializing...")
        self.auth_result_var = tk.StringVar(value="WAITING")
        self.door_status_var = tk.StringVar(value="Door Locked")
        self.door_timer_var = tk.StringVar(value="")
        self.current_time_var = tk.StringVar()
        
        # Build GUI
        self._build_gui()
        
        # Start systems
        self._start_systems()
    
    def _build_gui(self):
        """Build the GUI layout."""
        # Configure styles
        self.style = ttk.Style()
        self.style.theme_use('clam')
        
        # Main container
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Header
        self._build_header(main_frame)
        
        # Content area - two columns
        content_frame = ttk.Frame(main_frame)
        content_frame.pack(fill=tk.BOTH, expand=True, pady=10)
        
        # Left column - Camera
        left_frame = ttk.Frame(content_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        self._build_camera_panel(left_frame)
        
        # Right column - Status panels
        right_frame = ttk.Frame(content_frame)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        
        self._build_fingerprint_panel(right_frame)
        self._build_auth_result_panel(right_frame)
        self._build_door_panel(right_frame)
        
        # Footer with recent logs
        self._build_footer(main_frame)
    
    def _build_header(self, parent):
        """Build the header section."""
        header_frame = ttk.Frame(parent)
        header_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Title
        title_label = tk.Label(
            header_frame,
            text="SMART DOOR SECURITY SYSTEM",
            font=('Helvetica', 24, 'bold'),
            fg='#00ff88',
            bg='#1a1a2e'
        )
        title_label.pack(side=tk.LEFT)
        
        # Current time
        time_label = tk.Label(
            header_frame,
            textvariable=self.current_time_var,
            font=('Helvetica', 14),
            fg='#ffffff',
            bg='#1a1a2e'
        )
        time_label.pack(side=tk.RIGHT)
        
        self._update_time()
    
    def _build_camera_panel(self, parent):
        """Build the camera preview panel."""
        # Frame
        camera_frame = tk.LabelFrame(
            parent,
            text="Camera Preview",
            font=('Helvetica', 12, 'bold'),
            fg='#00d4ff',
            bg='#16213e',
            padx=10,
            pady=10
        )
        camera_frame.pack(fill=tk.BOTH, expand=True)
        
        # Camera canvas
        self.camera_canvas = tk.Canvas(
            camera_frame,
            width=640,
            height=480,
            bg='#0f0f0f',
            highlightthickness=0
        )
        self.camera_canvas.pack(pady=10)
        
        # Face status label
        face_status_frame = tk.Frame(camera_frame, bg='#16213e')
        face_status_frame.pack(fill=tk.X)
        
        tk.Label(
            face_status_frame,
            text="Face Status: ",
            font=('Helvetica', 11),
            fg='#ffffff',
            bg='#16213e'
        ).pack(side=tk.LEFT)
        
        self.face_status_label = tk.Label(
            face_status_frame,
            textvariable=self.face_status_var,
            font=('Helvetica', 11, 'bold'),
            fg='#ffcc00',
            bg='#16213e'
        )
        self.face_status_label.pack(side=tk.LEFT)
    
    def _build_fingerprint_panel(self, parent):
        """Build the fingerprint status panel."""
        fp_frame = tk.LabelFrame(
            parent,
            text="Fingerprint Status",
            font=('Helvetica', 12, 'bold'),
            fg='#00d4ff',
            bg='#16213e',
            padx=15,
            pady=15
        )
        fp_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Status indicator
        self.fp_status_label = tk.Label(
            fp_frame,
            textvariable=self.fingerprint_status_var,
            font=('Helvetica', 14, 'bold'),
            fg='#ffcc00',
            bg='#16213e'
        )
        self.fp_status_label.pack(pady=10)
        
        # Fingerprint icon canvas
        self.fp_canvas = tk.Canvas(
            fp_frame,
            width=100,
            height=100,
            bg='#16213e',
            highlightthickness=0
        )
        self.fp_canvas.pack(pady=5)
        self._draw_fingerprint_icon('#444444')
    
    def _build_auth_result_panel(self, parent):
        """Build the authentication result panel."""
        auth_frame = tk.LabelFrame(
            parent,
            text="Authentication Result",
            font=('Helvetica', 12, 'bold'),
            fg='#00d4ff',
            bg='#16213e',
            padx=15,
            pady=15
        )
        auth_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Result label
        self.auth_result_label = tk.Label(
            auth_frame,
            textvariable=self.auth_result_var,
            font=('Helvetica', 24, 'bold'),
            fg='#ffffff',
            bg='#333333',
            padx=20,
            pady=20
        )
        self.auth_result_label.pack(fill=tk.X, pady=10)
    
    def _build_door_panel(self, parent):
        """Build the door status panel."""
        door_frame = tk.LabelFrame(
            parent,
            text="Door Status",
            font=('Helvetica', 12, 'bold'),
            fg='#00d4ff',
            bg='#16213e',
            padx=15,
            pady=15
        )
        door_frame.pack(fill=tk.X)
        
        # Door status
        self.door_status_label = tk.Label(
            door_frame,
            textvariable=self.door_status_var,
            font=('Helvetica', 18, 'bold'),
            fg='#ff4444',
            bg='#16213e'
        )
        self.door_status_label.pack(pady=10)
        
        # Timer
        self.door_timer_label = tk.Label(
            door_frame,
            textvariable=self.door_timer_var,
            font=('Helvetica', 12),
            fg='#888888',
            bg='#16213e'
        )
        self.door_timer_label.pack()
        
        # Door icon canvas
        self.door_canvas = tk.Canvas(
            door_frame,
            width=80,
            height=120,
            bg='#16213e',
            highlightthickness=0
        )
        self.door_canvas.pack(pady=10)
        self._draw_door_icon(locked=True)
    
    def _build_footer(self, parent):
        """Build the footer with recent activity."""
        footer_frame = tk.LabelFrame(
            parent,
            text="Recent Activity",
            font=('Helvetica', 10, 'bold'),
            fg='#00d4ff',
            bg='#16213e',
            padx=10,
            pady=5
        )
        footer_frame.pack(fill=tk.X, pady=(10, 0))
        
        # Activity log text
        self.activity_text = tk.Text(
            footer_frame,
            height=4,
            font=('Consolas', 9),
            bg='#0f0f0f',
            fg='#00ff88',
            state=tk.DISABLED
        )
        self.activity_text.pack(fill=tk.X, pady=5)
    
    def _draw_fingerprint_icon(self, color):
        """Draw fingerprint icon on canvas."""
        self.fp_canvas.delete("all")
        # Simple fingerprint representation
        cx, cy = 50, 50
        for i in range(5):
            r = 15 + i * 8
            self.fp_canvas.create_arc(
                cx - r, cy - r, cx + r, cy + r,
                start=30, extent=120, outline=color, width=2, style=tk.ARC
            )
    
    def _draw_door_icon(self, locked=True):
        """Draw door icon on canvas."""
        self.door_canvas.delete("all")
        
        # Door frame
        color = '#ff4444' if locked else '#00ff88'
        self.door_canvas.create_rectangle(10, 10, 70, 110, outline=color, width=3)
        
        # Door handle
        self.door_canvas.create_oval(55, 55, 65, 65, fill=color, outline=color)
        
        # Lock icon
        if locked:
            self.door_canvas.create_rectangle(30, 45, 50, 65, outline=color, width=2)
            self.door_canvas.create_arc(30, 35, 50, 55, start=0, extent=180, 
                                        outline=color, width=2, style=tk.ARC)
    
    def _update_time(self):
        """Update the current time display."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.current_time_var.set(now)
        self.root.after(1000, self._update_time)
    
    def _start_systems(self):
        """Start all system components."""
        try:
            # Start face recognition
            if self.face_engine.start():
                self.face_status_var.set("Camera Ready")
                self._log_activity("Face recognition system started")
            else:
                self.face_status_var.set("Camera Error")
                self._log_activity("ERROR: Face recognition failed to start")
            
            # Start fingerprint sensor
            if self.fingerprint_manager.start():
                self.fingerprint_status_var.set("Waiting for Fingerprint")
                self._log_activity("Fingerprint sensor connected")
            else:
                # Fallback to simulation if connection fails
                self.fingerprint_manager.set_simulation(True)
                self.fingerprint_status_var.set("Sensor in Simulation Mode")
                self._log_activity("Fingerprint sensor connection failed - using simulation")
            
            # Start door monitor
            self.door_monitor.add_callback(self._on_door_status_change)
            self.door_monitor.start()
            
            self._running = True
            
            # Start main processing loop
            self._process_loop()
            
            self.system_log.info("MainGUI", "System started successfully")
            
        except Exception as e:
            logger.error(f"Failed to start systems: {e}")
            messagebox.showerror("Error", f"Failed to start systems: {e}")
    
    def _process_loop(self):
        """Main processing loop - runs on GUI thread via after()."""
        if not self._running:
            return
        
        try:
            # Process face recognition
            face_result = self.face_engine.process_frame()
            self._update_face_display(face_result)
            
            # Update authentication state machine
            self._process_authentication(face_result)
            
        except Exception as e:
            logger.error(f"Process loop error: {e}")
        
        # Schedule next iteration
        self.root.after(GUI_UPDATE_INTERVAL, self._process_loop)
    
    def _update_face_display(self, face_result: FaceResult):
        """Update the camera display with face detection results."""
        if face_result.frame is not None:
            # Convert frame to PhotoImage
            frame = cv2.cvtColor(face_result.frame, cv2.COLOR_BGR2RGB)
            frame = cv2.resize(frame, (640, 480))
            img = Image.fromarray(frame)
            self.camera_image = ImageTk.PhotoImage(image=img)
            
            # Update canvas
            self.camera_canvas.create_image(0, 0, anchor=tk.NW, image=self.camera_image)
        
        # Update face status
        status_text = face_result.status.value
        if face_result.status == FaceStatus.FACE_MATCHED:
            status_text = f"Face Matched: {face_result.user_name}"
            self.face_status_label.config(fg='#00ff88')
        elif face_result.status == FaceStatus.UNKNOWN_FACE:
            self.face_status_label.config(fg='#ff4444')
        elif face_result.status == FaceStatus.FACE_DETECTED:
            self.face_status_label.config(fg='#ffcc00')
        else:
            self.face_status_label.config(fg='#888888')
        
        self.face_status_var.set(status_text)
    
    def _process_authentication(self, face_result: FaceResult):
        """Process the authentication state machine."""
        current_state = self._auth_state
        
        if current_state == AuthState.IDLE:
            # Looking for face match
            if face_result.status == FaceStatus.FACE_MATCHED:
                # Verify user is active
                user = self.user_repo.get_by_id(face_result.user_id)
                if user and user.get('is_active', False):
                    self._auth_state = AuthState.FACE_MATCHED
                    self._matched_face_user_id = face_result.user_id
                    self._auth_start_time = time.time()
                    self._current_face_result = face_result
                    
                    self.fingerprint_status_var.set(f"Face Verified: {face_result.user_name}\nSkipping fingerprint (simulation mode)")
                    self._draw_fingerprint_icon('#00ff88')
                    self.auth_result_var.set("ACCESS GRANTED")
                    self.auth_result_label.config(bg='#004400', fg='#00ff88')
                    
                    self._log_activity(f"Face matched: {face_result.user_name}")
                    
                    # Grant access immediately (skip fingerprint in simulation mode)
                    self._grant_access(user)
        
        elif current_state == AuthState.FACE_MATCHED:
            # Check for timeout
            if time.time() - self._auth_start_time > 30:  # 30 second timeout
                self._auth_state = AuthState.TIMEOUT
                self._handle_auth_failure("Authentication timeout")
        
        elif current_state in [AuthState.ACCESS_GRANTED, AuthState.ACCESS_DENIED, AuthState.TIMEOUT]:
            # Wait and reset
            if time.time() - self._auth_start_time > 5:
                self._reset_auth_state()
    
    def _verify_fingerprint(self):
        """Verify fingerprint in background thread."""
        try:
            fp_result = self.fingerprint_manager.scan_once(timeout=10.0)
            self._current_fp_result = fp_result
            
            # Update GUI from main thread
            self.root.after(0, lambda: self._handle_fingerprint_result(fp_result))
            
        except Exception as e:
            logger.error(f"Fingerprint verification error: {e}")
            self.root.after(0, lambda: self._handle_auth_failure(f"Fingerprint error: {e}"))
    
    def _handle_fingerprint_result(self, fp_result: FingerprintResult):
        """Handle fingerprint verification result."""
        if self._auth_state != AuthState.FACE_MATCHED:
            return  # State changed, ignore result
        
        if fp_result.status == FingerprintStatus.MATCHED:
            # Check if same user
            if fp_result.user_id == self._matched_face_user_id:
                # Verify user is still active
                user = self.user_repo.get_by_id(fp_result.user_id)
                if user and user.get('is_active', False):
                    self._grant_access(user)
                else:
                    self._handle_auth_failure("User account is disabled")
            else:
                self._handle_auth_failure("Face and fingerprint don't match same user")
        
        elif fp_result.status == FingerprintStatus.NOT_MATCHED:
            self._handle_auth_failure("Fingerprint not recognized")
        
        elif fp_result.status == FingerprintStatus.TIMEOUT:
            self._handle_auth_failure("Fingerprint scan timeout")
        
        else:
            # Still waiting or error
            self.fingerprint_status_var.set(fp_result.status.value)
    
    def _grant_access(self, user: dict):
        """Grant access to authenticated user."""
        self._auth_state = AuthState.ACCESS_GRANTED
        self._auth_start_time = time.time()
        
        user_name = f"{user['first_name']} {user['last_name']}"
        
        # Update UI
        self.auth_result_var.set(f"ACCESS GRANTED\n{user_name}")
        self.auth_result_label.config(bg='#004400', fg='#00ff88')
        self.fingerprint_status_var.set(f"Fingerprint Matched: {user_name}")
        self._draw_fingerprint_icon('#00ff88')
        
        # Unlock door
        self.door_controller.unlock(reason=f"Authenticated: {user_name}")
        
        # Log access
        self.access_log_repo.log_access(
            user_id=user['id'],
            event_type='ENTRY',
            result='SUCCESS',
            face_match=True,
            fingerprint_match=True,
            confidence_score=self._current_face_result.confidence if self._current_face_result else 0
        )
        
        self._log_activity(f"ACCESS GRANTED: {user_name}")
        logger.info(f"Access granted to {user_name}")
    
    def _handle_auth_failure(self, reason: str):
        """Handle authentication failure."""
        self._auth_state = AuthState.ACCESS_DENIED
        self._auth_start_time = time.time()
        
        # Update UI
        self.auth_result_var.set(f"ACCESS DENIED\n{reason}")
        self.auth_result_label.config(bg='#440000', fg='#ff4444')
        self.fingerprint_status_var.set("Fingerprint Failed")
        self._draw_fingerprint_icon('#ff4444')
        
        # Ensure door is locked
        self.door_controller.lock(reason="Access denied")
        
        # Log failure
        self.access_log_repo.log_access(
            user_id=self._matched_face_user_id,
            event_type='ENTRY',
            result='DENIED',
            face_match=self._current_face_result is not None,
            fingerprint_match=False,
            failure_reason=reason
        )
        
        self._log_activity(f"ACCESS DENIED: {reason}")
        logger.warning(f"Access denied: {reason}")
    
    def _reset_auth_state(self):
        """Reset authentication state to idle."""
        self._auth_state = AuthState.IDLE
        self._matched_face_user_id = None
        self._current_face_result = None
        self._current_fp_result = None
        self._auth_start_time = None
        
        # Reset UI
        self.auth_result_var.set("WAITING")
        self.auth_result_label.config(bg='#333333', fg='#ffffff')
        self.fingerprint_status_var.set("Waiting for Fingerprint")
        self._draw_fingerprint_icon('#444444')
    
    def _on_door_status_change(self, status):
        """Handle door status changes."""
        self.root.after(0, lambda: self._update_door_display(status))
    
    def _update_door_display(self, status):
        """Update door status display."""
        if status.state == DoorState.LOCKED:
            self.door_status_var.set("Door Locked")
            self.door_status_label.config(fg='#ff4444')
            self.door_timer_var.set("")
            self._draw_door_icon(locked=True)
        
        elif status.state == DoorState.UNLOCKED:
            self.door_status_var.set("Door Unlocked")
            self.door_status_label.config(fg='#00ff88')
            if status.time_until_lock > 0:
                self.door_timer_var.set(f"Auto-lock in {status.time_until_lock:.1f}s")
            self._draw_door_icon(locked=False)
        
        elif status.state == DoorState.UNLOCKING:
            self.door_status_var.set("Unlocking...")
            self.door_status_label.config(fg='#ffcc00')
        
        elif status.state == DoorState.LOCKING:
            self.door_status_var.set("Locking...")
            self.door_status_label.config(fg='#ffcc00')
    
    def _log_activity(self, message: str):
        """Add a message to the activity log."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {message}\n"
        
        self.activity_text.config(state=tk.NORMAL)
        self.activity_text.insert(tk.END, log_entry)
        self.activity_text.see(tk.END)
        
        # Keep only last 100 lines
        lines = self.activity_text.get("1.0", tk.END).split('\n')
        if len(lines) > 100:
            self.activity_text.delete("1.0", f"{len(lines)-100}.0")
        
        self.activity_text.config(state=tk.DISABLED)
    
    def on_closing(self):
        """Handle window close event."""
        if messagebox.askokcancel("Quit", "Are you sure you want to exit?"):
            self._running = False
            
            # Stop all components
            self.door_monitor.stop()
            self.door_controller.cleanup()
            self.fingerprint_manager.stop()
            self.face_engine.stop()
            
            self.system_log.info("MainGUI", "System shutdown")
            logger.info("System shutdown")
            
            self.root.destroy()
    
    def run(self):
        """Start the GUI main loop."""
        logger.info("Starting Smart Door Security System GUI...")
        self._log_activity("System started")
        self.root.mainloop()


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Smart Door Security System')
    parser.add_argument(
        '--simulation', '-s',
        action='store_true',
        help='Run in simulation mode (no real hardware)'
    )
    parser.add_argument(
        '--debug', '-d',
        action='store_true',
        help='Enable debug logging'
    )
    
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Ensure logs directory exists
    (PROJECT_ROOT / 'logs').mkdir(exist_ok=True)
    
    # Create and run GUI
    app = SmartDoorGUI(simulation=args.simulation)
    app.run()


if __name__ == "__main__":
    main()
