#!/usr/bin/env python3
"""
Smart Door Security System - Main Application
Runs 24/7 with GUI showing camera preview, door state,
and ultrasonic proximity sensing.
Face recognition authentication for access.
"""

import sys
import threading
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# Tkinter imports
import tkinter as tk
from tkinter import ttk, messagebox

try:
    from PIL import Image, ImageTk
except ImportError as exc:
    raise ImportError(
        "Pillow is required for the GUI image display. "
        "Install it with 'sudo apt install python3-pil python3-pil.imagetk' "
        "or 'pip install pillow'."
    ) from exc

import cv2

# Project imports
from config.settings import (
    GUI_UPDATE_INTERVAL, GUI_WINDOW_WIDTH, GUI_WINDOW_HEIGHT,
    ULTRASONIC_THRESHOLD, AUTO_LOCK_DELAY,
)
from database.db_manager import (
    DatabaseManager, UserRepository,
    AccessLogRepository, SystemLogRepository,
)
from modules.face_recognition_module import (
    FaceRecognitionEngine, FaceResult, FaceStatus,
)
from modules.door_control import (
    DoorController, DoorState, DoorMonitor,
    UltrasonicSensorMonitor, SensorEvent,
)
from modules.auth_engine import AuthState

# ─────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_ROOT / "logs" / "system.log"),
    ],
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# GLOBAL SINGLETON SENSOR MONITOR  (created once, shared)
# ══════════════════════════════════════════════════════════════════════════
_sensor_monitor: Optional[UltrasonicSensorMonitor] = None


# ══════════════════════════════════════════════════════════════════════════
# GUI
# ══════════════════════════════════════════════════════════════════════════

class SmartDoorGUI:
    """Main GUI application for the Smart Door Security System."""

    def __init__(self, simulation: bool = True):
        self.simulation = simulation

        self.root = tk.Tk()
        self.root.title("Smart Door Security System")
        self.root.geometry(f"{GUI_WINDOW_WIDTH}x{GUI_WINDOW_HEIGHT}")
        self.root.configure(bg="#1a1a2e")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.db               = DatabaseManager()
        self.user_repo        = UserRepository()
        self.access_log_repo  = AccessLogRepository()
        self.system_log       = SystemLogRepository()

        self.face_engine        = FaceRecognitionEngine()
        self.door_controller    = DoorController(simulation=simulation)
        self.door_monitor       = DoorMonitor(self.door_controller)

        # Pull the ultrasonic monitor inside DoorController (it creates one)
        global _sensor_monitor
        _sensor_monitor = self.door_controller._ultrasonic

        self._running         = False
        self._current_face_result : Optional[FaceResult]         = None
        self._auth_state          = AuthState.IDLE
        self._matched_face_user_id          = None
        self._auth_start_time               = None
        self._proximity_distance_cm: float   = -1.0
        self._proximity_status              = "Sensor idle"

        # Tkinter string variables
        self.camera_image        = None
        self.face_status_var     = tk.StringVar(value="Initializing…")
        self.auth_result_var     = tk.StringVar(value="WAITING")
        self.door_status_var     = tk.StringVar(value="Door Locked")
        self.door_timer_var      = tk.StringVar(value="")
        self.sensor_dist_var     = tk.StringVar(value="— cm")
        self.sensor_status_var   = tk.StringVar(value="Sensor idle")
        self.current_time_var    = tk.StringVar()

        self._build_gui()
        self._start_systems()

    # ─── Build GUI ───────────────────────────────────────────────────────

    def _build_gui(self):
        self.style = ttk.Style()
        self.style.theme_use("clam")

        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        self._build_header(main_frame)

        content_frame = ttk.Frame(main_frame)
        content_frame.pack(fill=tk.BOTH, expand=True, pady=10)

        left_frame  = ttk.Frame(content_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        self._build_camera_panel(left_frame)

        right_frame = ttk.Frame(content_frame)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        self._build_auth_result_panel(right_frame)
        self._build_door_panel(right_frame)
        self._build_sensor_panel(right_frame)

        self._build_footer(main_frame)

    def _build_header(self, parent):
        header_frame = ttk.Frame(parent)
        header_frame.pack(fill=tk.X, pady=(0, 10))

        tk.Label(
            header_frame,
            text="SMART DOOR SECURITY SYSTEM",
            font=("Helvetica", 24, "bold"),
            fg="#00ff88", bg="#1a1a2e",
        ).pack(side=tk.LEFT)

        tk.Label(
            header_frame,
            textvariable=self.current_time_var,
            font=("Helvetica", 14),
            fg="#ffffff", bg="#1a1a2e",
        ).pack(side=tk.RIGHT)
        self._update_time()

    def _build_camera_panel(self, parent):
        camera_frame = tk.LabelFrame(
            parent, text="Camera Preview",
            font=("Helvetica", 12, "bold"),
            fg="#00d4ff", bg="#16213e", padx=10, pady=10,
        )
        camera_frame.pack(fill=tk.BOTH, expand=True)

        self.camera_canvas = tk.Canvas(
            camera_frame, width=640, height=480,
            bg="#0f0f0f", highlightthickness=0,
        )
        self.camera_canvas.pack(pady=10)

        face_status_frame = tk.Frame(camera_frame, bg="#16213e")
        face_status_frame.pack(fill=tk.X)
        tk.Label(
            face_status_frame, text="Face Status: ",
            font=("Helvetica", 11),
            fg="#ffffff", bg="#16213e",
        ).pack(side=tk.LEFT)

        self.face_status_label = tk.Label(
            face_status_frame,
            textvariable=self.face_status_var,
            font=("Helvetica", 11, "bold"),
            fg="#ffcc00", bg="#16213e",
        )
        self.face_status_label.pack(side=tk.LEFT)

    def _build_auth_result_panel(self, parent):
        auth_frame = tk.LabelFrame(
            parent, text="Authentication Result",
            font=("Helvetica", 12, "bold"),
            fg="#00d4ff", bg="#16213e", padx=15, pady=15,
        )
        auth_frame.pack(fill=tk.X, pady=(0, 10))

        self.auth_result_label = tk.Label(
            auth_frame,
            textvariable=self.auth_result_var,
            font=("Helvetica", 24, "bold"),
            fg="#ffffff", bg="#333333",
            padx=20, pady=20,
        )
        self.auth_result_label.pack(fill=tk.X, pady=10)

    def _build_door_panel(self, parent):
        door_frame = tk.LabelFrame(
            parent, text="Door Status",
            font=("Helvetica", 12, "bold"),
            fg="#00d4ff", bg="#16213e", padx=15, pady=15,
        )
        door_frame.pack(fill=tk.X)

        self.door_status_label = tk.Label(
            door_frame,
            textvariable=self.door_status_var,
            font=("Helvetica", 18, "bold"),
            fg="#ff4444", bg="#16213e",
        )
        self.door_status_label.pack(pady=10)

        self.door_timer_label = tk.Label(
            door_frame,
            textvariable=self.door_timer_var,
            font=("Helvetica", 12),
            fg="#888888", bg="#16213e",
        )
        self.door_timer_label.pack()

        self.door_canvas = tk.Canvas(
            door_frame, width=80, height=120,
            bg="#16213e", highlightthickness=0,
        )
        self.door_canvas.pack(pady=10)
        self._draw_door_icon(locked=True)

    def _build_sensor_panel(self, parent):
        """HC-SR04 ultrasonic proximity sensor display panel."""
        sensor_frame = tk.LabelFrame(
            parent, text="Proximity Sensor (HC-SR04)",
            font=("Helvetica", 12, "bold"),
            fg="#00d4ff", bg="#16213e", padx=15, pady=15,
        )
        sensor_frame.pack(fill=tk.X, pady=(0, 10))

        # Top row: distance readout + unlocked-via-ultrasonic icon
        top_row = tk.Frame(sensor_frame, bg="#16213e")
        top_row.pack(fill=tk.X, pady=(0, 5))

        self.sensor_dist_label = tk.Label(
            top_row,
            textvariable=self.sensor_dist_var,
            font=("Helvetica", 20, "bold"),
            fg="#00ff88", bg="#16213e",
        )
        self.sensor_dist_label.pack(side=tk.LEFT)
        tk.Label(
            top_row, text=f"cm (threshold {ULTRASONIC_THRESHOLD:.0f} cm)",
            font=("Helvetica", 10),
            fg="#888888", bg="#16213e",
        ).pack(side=tk.LEFT, padx=(6, 0), anchor=tk.W)

        # ── Unlocked-via-ultrasonic lock icon (λ/2 seismic pattern) ─────
        icon_frame = tk.Frame(top_row, bg="#16213e")
        icon_frame.pack(side=tk.RIGHT)

        self.sensor_icon_canvas = tk.Canvas(
            icon_frame, width=44, height=44,
            bg="#16213e", highlightthickness=0,
        )
        self.sensor_icon_canvas.pack()
        self._draw_sensor_lock_icon(locked=True)

        # Status / trigger label
        self.sensor_status_label = tk.Label(
            sensor_frame,
            textvariable=self.sensor_status_var,
            font=("Helvetica", 11, "bold"),
            fg="#ffcc00", bg="#16213e",
        )
        self.sensor_status_label.pack(pady=4)

        # Auto-lock rule reminder
        tk.Label(
            sensor_frame,
            text=(
                "Object <= %.0f cm → SILENT UNLOCK\n"
                "Auto-relock after %.0f s"
            ) % (ULTRASONIC_THRESHOLD, AUTO_LOCK_DELAY),
            font=("Helvetica", 9),
            fg="#cccccc", bg="#16213e", justify=tk.LEFT,
        ).pack(pady=(4, 0))

    def _draw_sensor_lock_icon(self, locked=True):
        """
        Draws a lock-or-unlocked icon on the sensor canvas.
        locked=True  → red padlock (door safe, sensor armed)
        locked=False → green λ/2 wave  (door UNLOCKED via ultrasonic)
        """
        c = self.sensor_icon_canvas
        c.delete("all")
        if locked:
            # Red padlock with closed shackle
            c.create_rectangle(12, 18, 32, 30, fill="#ff4444", outline="#cc0000", width=1)
            c.create_arc(14, 8, 30, 22, start=0, extent=180,
                         outline="#ff4444", width=2, style=tk.ARC)
        else:
            # λ/2 seismic wave — green radiating arcs (door open via ultrasonic)
            cx, cy = 22, 22
            for r, w in [(8, 3), (13, 2), (18, 1)]:
                c.create_arc(cx - r, cy - 2, cx + r, cy + 2,
                             start=0, extent=120,
                             outline="#00ff88", width=w)
            c.create_line(cx, cy - 6, cx, cy + 6, fill="#00ff88", width=1)

    def _build_footer(self, parent):
        footer_frame = tk.LabelFrame(
            parent, text="Recent Activity",
            font=("Helvetica", 10, "bold"),
            fg="#00d4ff", bg="#16213e", padx=10, pady=5,
        )
        footer_frame.pack(fill=tk.X, pady=(10, 0))

        self.activity_text = tk.Text(
            footer_frame, height=4,
            font=("Consolas", 9),
            bg="#0f0f0f", fg="#00ff88",
            state=tk.DISABLED,
        )
        self.activity_text.pack(fill=tk.X, pady=5)

    # ─── Icon helpers ────────────────────────────────────────────────────

    def _draw_door_icon(self, locked=True):
        self.door_canvas.delete("all")
        color = "#ff4444" if locked else "#00ff88"
        self.door_canvas.create_rectangle(
            10, 10, 70, 110, outline=color, width=3)
        self.door_canvas.create_oval(55, 55, 65, 65, fill=color, outline=color)
        if locked:
            self.door_canvas.create_rectangle(
                30, 45, 50, 65, outline=color, width=2)
            self.door_canvas.create_arc(
                30, 35, 50, 55, start=0, extent=180,
                outline=color, width=2, style=tk.ARC)

    # ─── Time ────────────────────────────────────────────────────────────

    def _update_time(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.current_time_var.set(now)
        self.root.after(1000, self._update_time)

    # ─── System startup ──────────────────────────────────────────────────

    def _start_systems(self):
        try:
            # Camera / face recognition
            if self.face_engine.start():
                self.face_status_var.set("Camera Ready")
                self._log_activity("Face recognition system started")
            else:
                self.face_status_var.set("Camera Error")
                self._log_activity("ERROR: Face recognition failed to start")

            # Door door + ultrasonic are both inside DoorController.__init__
            self.door_controller.add_state_callback(self._on_door_status_change)
            self.door_monitor.start()

            # Ultrasonic sensor monitor
            if not self.simulation:
                self._log_activity(
                    "HC-SR04 proximity sensor started — "
                    f"threshold={ULTRASONIC_THRESHOLD:.1f} cm")

            self._running = True
            self._process_loop()
            self.system_log.info("MainGUI", "System started successfully")

        except Exception as exc:
            logger.error("Failed to start systems: %s", exc)
            messagebox.showerror("Error", f"Failed to start systems: {exc}")

    # ─── Main processing loop ────────────────────────────────────────────

    def _process_loop(self):
        if not self._running:
            return
        try:
            face_result = self.face_engine.process_frame()
            self._update_face_display(face_result)
            self._process_authentication(face_result)
            self._update_sensor_display()
        except Exception as exc:
            logger.error("Process loop error: %s", exc)
        self.root.after(GUI_UPDATE_INTERVAL, self._process_loop)

    def _update_face_display(self, face_result: FaceResult):
        if face_result.frame is not None:
            frame = cv2.cvtColor(face_result.frame, cv2.COLOR_BGR2RGB)
            frame = cv2.resize(frame, (640, 480))
            img   = Image.fromarray(frame)
            self.camera_image = ImageTk.PhotoImage(image=img)
            self.camera_canvas.create_image(0, 0, anchor=tk.NW, image=self.camera_image)

        status_text = face_result.status.value
        if face_result.status == FaceStatus.FACE_MATCHED:
            status_text = f"Face Matched: {face_result.user_name}"
            self.face_status_label.config(fg="#00ff88")
        elif face_result.status == FaceStatus.UNKNOWN_FACE:
            self.face_status_label.config(fg="#ff4444")
        elif face_result.status == FaceStatus.FACE_DETECTED:
            self.face_status_label.config(fg="#ffcc00")
        else:
            self.face_status_label.config(fg="#888888")
        self.face_status_var.set(status_text)

    def _update_sensor_display(self):
        """Update the ultrasonic sensor panel with the latest reading."""
        global _sensor_monitor
        if _sensor_monitor is None:
            return
        self._sensor_poll()

    def _sensor_poll(self):
        """Called by the GUI timer; updates the sensor display fields."""
        dist = self._proximity_distance_cm
        if dist < 0:
            self.sensor_dist_var.set("—")
        else:
            self.sensor_dist_var.set(f"{dist:.1f}")
        self.sensor_status_var.set(self._proximity_status)

    def _process_authentication(self, face_result: FaceResult):
        if self._auth_state in (AuthState.ACCESS_GRANTED,
                                AuthState.ACCESS_DENIED,
                                AuthState.TIMEOUT):
            if time.time() - self._auth_start_time > 5:
                self._reset_auth_state()
            return

        if self.door_controller.is_unlocked():
            return

        if self._auth_state == AuthState.IDLE:
            if face_result.status == FaceStatus.FACE_MATCHED:
                user = self.user_repo.get_by_id(face_result.user_id)
                if user and user.get("is_active", False):
                    self._auth_state = AuthState.FACE_MATCHED
                    self._matched_face_user_id = face_result.user_id
                    self._auth_start_time = time.time()
                    self._current_face_result = face_result
                    self._grant_access(user)

        elif self._auth_state == AuthState.FACE_MATCHED:
            if time.time() - self._auth_start_time > AUTH_TIMEOUT:
                self._handle_auth_failure("Authentication timeout")

    def _grant_access(self, user: dict):
        self._auth_state = AuthState.ACCESS_GRANTED
        self._auth_start_time = time.time()
        user_name = f"{user['first_name']} {user['last_name']}"
        self.auth_result_var.set(f"ACCESS GRANTED\n{user_name}")
        self.auth_result_label.config(bg="#004400", fg="#00ff88")
        self.door_controller.unlock(reason=f"Authenticated: {user_name}")
        self.access_log_repo.log_access(
            user_id=user["id"], event_type="ENTRY", result="SUCCESS",
            face_match=True,
            confidence_score=(
                self._current_face_result.confidence
                if self._current_face_result else 0),
        )
        self._log_activity(f"ACCESS GRANTED: {user_name}")
        logger.info("Access granted to %s", user_name)

    def _handle_auth_failure(self, reason: str):
        if self.door_controller.is_unlocked():
            return
        self._auth_state = AuthState.ACCESS_DENIED
        self._auth_start_time = time.time()
        self.auth_result_var.set(f"ACCESS DENIED\n{reason}")
        self.auth_result_label.config(bg="#440000", fg="#ff4444")
        self.door_controller.lock(reason="Access denied")
        self.access_log_repo.log_access(
            user_id=self._matched_face_user_id,
            event_type="ENTRY", result="DENIED",
            face_match=(self._current_face_result is not None
                        and self._current_face_result.status
                        == FaceStatus.FACE_MATCHED),
            failure_reason=reason,
        )
        self._log_activity(f"ACCESS DENIED: {reason}")
        logger.warning("Access denied: %s", reason)

    def _reset_auth_state(self):
        self._auth_state             = AuthState.IDLE
        self._matched_face_user_id   = None
        self._current_face_result    = None
        self._auth_start_time        = None
        self.auth_result_var.set("WAITING")
        self.auth_result_label.config(bg="#333333", fg="#ffffff")

    # ─── Door status ─────────────────────────────────────────────────────

    def _on_door_status_change(self, status):
        self.root.after(0, lambda: self._update_door_display(status))

    def _update_door_display(self, status):
        if status.state == DoorState.LOCKED:
            self.door_status_var.set("Door Locked")
            self.door_status_label.config(fg="#ff4444")
            self.door_timer_var.set("")
            self._draw_door_icon(locked=True)
        elif status.state == DoorState.UNLOCKED:
            self.door_status_var.set("Door Unlocked")
            self.door_status_label.config(fg="#00ff88")
            if status.time_until_lock > 0:
                self.door_timer_var.set(
                    f"Auto-lock in {status.time_until_lock:.1f} s")
            self._draw_door_icon(locked=False)
        elif status.state == DoorState.UNLOCKING:
            self.door_status_var.set("Unlocking…")
            self.door_status_label.config(fg="#ffcc00")
            self.door_timer_var.set("")
        elif status.state == DoorState.LOCKING:
            self.door_status_var.set("Locking…")
            self.door_status_label.config(fg="#ffcc00")
            self.door_timer_var.set("")

    # ─── Sensor callbacks ─────────────────────────────────────────────────

    def _on_ultrasonic_event(self, event: SensorEvent, distance_cm: float):
        """Called from the ultrasonic background thread — queue onto Tk thread."""
        self.root.after(0, lambda: self._handle_ultrasonic_event(event, distance_cm))

    def _handle_ultrasonic_event(self, event: SensorEvent, distance_cm: float):
        self._proximity_distance_cm = distance_cm
        if event == SensorEvent.PROXIMITY:
            self._proximity_status = (
                f"Object detected {distance_cm:.1f} cm "
                f"(<= {ULTRASONIC_THRESHOLD:.0f} cm) — SILENT UNLOCK")
            self.sensor_status_label.config(fg="#ff4444")
            self.sensor_icon_canvas.itemconfig("lock_icon", fill="#00ff88")
            self._draw_sensor_lock_icon(locked=False)
            self._log_activity(
                f"[{datetime.now().strftime('%H:%M:%S')}] PROXIMITY UNLOCK: "
                f"{distance_cm:.1f} cm detected — silent-open started")
        elif event == SensorEvent.NONE:
            self._proximity_status = "No object in range"
            self.sensor_status_label.config(fg="#00ff88")
            self._draw_sensor_lock_icon(locked=True)

    # ─── Activity log ─────────────────────────────────────────────────────

    def _log_activity(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {message}\n"
        self.activity_text.config(state=tk.NORMAL)
        self.activity_text.insert(tk.END, log_entry)
        self.activity_text.see(tk.END)
        lines = self.activity_text.get("1.0", tk.END).split("\n")
        if len(lines) > 100:
            self.activity_text.delete("1.0", f"{len(lines) - 100}.0")
        self.activity_text.config(state=tk.DISABLED)

    # ─── Shutdown ─────────────────────────────────────────────────────────

    def on_closing(self):
        if messagebox.askokcancel("Quit", "Are you sure you want to exit?"):
            self._running = False
            self.door_monitor.stop()
            self.door_controller.cleanup()
            self.face_engine.stop()
            self.system_log.info("MainGUI", "System shutdown")
            logger.info("System shutdown")
            self.root.destroy()

    def run(self):
        logger.info("Starting Smart Door Security System GUI…")
        self._log_activity("System started")
        self.root.mainloop()


# ══════════════════════════════════════════════════════════════════════════
# Entry-point
# ══════════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Smart Door Security System")
    parser.add_argument(
        "--simulation", "-s",
        action="store_true",
        help="Run in simulation mode (no real hardware)",
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    app = SmartDoorGUI(simulation=args.simulation)
    app.run()


if __name__ == "__main__":
    main()
