#!/usr/bin/env python3
"""
Smart Door Security System - GUI User Enrollment Script
Provides a graphical interface for enrolling face and fingerprint biometrics.
"""

import sys
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from tkinter import font as tkfont
import threading
import time
import cv2
import numpy as np
import face_recognition
from PIL import Image, ImageTk
import logging
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from database.db_manager import UserRepository, DatabaseManager
from modules.face_recognition_module import FaceEnrollment, CameraManager
from modules.fingerprint_module import FingerprintManager
from config.settings import FACE_DETECTION_MODEL


class EnrollmentGUI:
    """Main GUI application for user enrollment."""
    
    def __init__(self, root):
        self.root = root
        self.root.title("Smart Door Security System - User Enrollment")
        self.root.geometry("1200x800")
        self.root.configure(bg='#f0f0f0')
        
        # Initialize components
        self.user_repo = UserRepository()
        self.camera = CameraManager()
        self.face_enrollment = FaceEnrollment()
        self.fingerprint_manager = FingerprintManager()
        
        # State variables
        self.selected_user_id = None
        self.camera_active = False
        self.enrollment_thread = None
        self.stop_enrollment = False

        # Face detection preview state (main-thread only)
        self.is_face_enrolling = False         # True while face enrollment is running
        self.face_detected_in_preview = False  # Whether 1 face is visible right now
        self.face_locations_preview = []       # Scaled bounding boxes [(top,right,bottom,left)]
        self._preview_frame_count = 0          # Frame counter for pre-enrollment throttling
        self.samples_captured_count = 0        # Samples captured so far
        self.total_samples_count = 5           # Total samples required
        # Timestamp of the last captured sample — used during enrollment to infer
        # face-detected state WITHOUT calling face_recognition concurrently.
        self.enrollment_last_sample_time = 0.0
        
        # Setup GUI
        self.setup_styles()
        self.setup_layout()
        self.setup_camera_preview()
        
        # Load initial data
        self.load_users()
        
        # Start camera preview
        self.start_camera_preview()
    
    def setup_styles(self):
        """Setup custom styles for the GUI."""
        style = ttk.Style()
        style.theme_use('clam')
        
        # Custom colors
        self.colors = {
            'primary': '#2c3e50',
            'secondary': '#3498db',
            'success': '#27ae60',
            'warning': '#f39c12',
            'danger': '#e74c3c',
            'light': '#ecf0f1',
            'dark': '#34495e'
        }
        
        # Configure styles
        style.configure('Header.TLabel', font=('Segoe UI', 16, 'bold'), 
                       foreground=self.colors['primary'])
        style.configure('SubHeader.TLabel', font=('Segoe UI', 12), 
                       foreground=self.colors['dark'])
        style.configure('Card.TFrame', background='white', relief='solid')
        style.configure('Status.TLabel', font=('Segoe UI', 10, 'bold'))
        
        # Custom button styles
        self.button_font = ('Segoe UI', 10, 'bold')
        
    def setup_layout(self):
        """Setup the main layout of the application."""
        # Main container
        main_container = ttk.Frame(self.root, style='Card.TFrame')
        main_container.pack(fill='both', expand=True, padx=20, pady=20)
        
        # Header
        header_frame = ttk.Frame(main_container)
        header_frame.pack(fill='x', padx=20, pady=(20, 10))
        
        ttk.Label(header_frame, text="User Biometric Enrollment", 
                 style='Header.TLabel').pack(side='left')
        
        # Status indicator
        self.status_label = ttk.Label(header_frame, text="System Ready", 
                                    style='Status.TLabel', foreground='green')
        self.status_label.pack(side='right')
        
        # Main content area
        content_frame = ttk.Frame(main_container)
        content_frame.pack(fill='both', expand=True, padx=20, pady=10)
        
        # Left panel - User selection and options
        left_panel = ttk.Frame(content_frame, style='Card.TFrame')
        left_panel.pack(side='left', fill='y', padx=(0, 10), ipadx=20, ipady=20)
        
        # User selection
        ttk.Label(left_panel, text="Select User", style='SubHeader.TLabel').pack(anchor='w', pady=(0, 5))

        # User list
        self.user_listbox = tk.Listbox(left_panel, height=15, width=40, font=('Segoe UI', 10))
        self.user_listbox.pack(fill='x', pady=(0, 5))
        self.user_listbox.bind('<<ListboxSelect>>', self.on_user_select)

        # Refresh button immediately below the list — it's a list utility, not an enrollment action
        self.refresh_btn = tk.Button(
            left_panel, text="⟳  Refresh Users",
            font=self.button_font, bg=self.colors['light'],
            fg=self.colors['dark'], command=self.load_users,
            relief='groove', cursor='hand2'
        )
        self.refresh_btn.pack(fill='x', pady=(0, 15))

        # Enrollment options
        ttk.Separator(left_panel, orient='horizontal').pack(fill='x', pady=(0, 10))
        ttk.Label(left_panel, text="Enrollment Options", style='SubHeader.TLabel').pack(anchor='w', pady=(0, 10))

        # Option buttons
        self.enroll_face_btn = tk.Button(left_panel, text="Enroll Face Only",
                                        font=self.button_font, bg=self.colors['secondary'],
                                        fg='white', command=self.enroll_face_only,
                                        cursor='hand2')
        self.enroll_face_btn.pack(fill='x', pady=5)

        self.enroll_fp_btn = tk.Button(left_panel, text="Enroll Fingerprint Only",
                                      font=self.button_font, bg=self.colors['warning'],
                                      fg='white', command=self.enroll_fingerprint_only,
                                      cursor='hand2')
        self.enroll_fp_btn.pack(fill='x', pady=5)

        self.enroll_both_btn = tk.Button(left_panel, text="Enroll Both (Recommended)",
                                        font=self.button_font, bg=self.colors['success'],
                                        fg='white', command=self.enroll_both,
                                        cursor='hand2')
        self.enroll_both_btn.pack(fill='x', pady=5)

        # Cancel button
        ttk.Separator(left_panel, orient='horizontal').pack(fill='x', pady=15)

        self.cancel_btn = tk.Button(left_panel, text="✕  Cancel Enrollment",
                                   font=self.button_font, bg=self.colors['danger'],
                                   fg='white', command=self.cancel_enrollment,
                                   cursor='hand2')
        self.cancel_btn.pack(fill='x', pady=5)
        
        # Right panel - Camera preview and status
        right_panel = ttk.Frame(content_frame, style='Card.TFrame')
        right_panel.pack(side='right', fill='both', expand=True, padx=(10, 0))
        
        # Camera preview section
        ttk.Label(right_panel, text="Camera Preview", style='SubHeader.TLabel').pack(anchor='w', padx=20, pady=(20, 10))
        
        # Camera frame
        self.camera_frame = ttk.Frame(right_panel, relief='sunken', width=640, height=480)
        self.camera_frame.pack(padx=20, pady=10, fill='both', expand=True)

        self.camera_canvas = tk.Canvas(self.camera_frame, width=640, height=480, bg='black')
        self.camera_canvas.pack(fill='both', expand=True)

        # Face-detection status label (shown below the live preview)
        self.face_status_label = ttk.Label(
            right_panel,
            text="",
            font=('Segoe UI', 10, 'bold'),
            foreground='gray'
        )
        self.face_status_label.pack(anchor='center', pady=(0, 5))

        # Status and progress section
        status_frame = ttk.Frame(right_panel, style='Card.TFrame')
        status_frame.pack(fill='x', padx=20, pady=(0, 20))

        # Overall enrollment progress bar
        ttk.Label(status_frame, text="Enrollment Progress", style='SubHeader.TLabel').pack(anchor='w', padx=20, pady=(10, 5))

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(status_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill='x', padx=20, pady=(0, 8))

        # Face sample capture indicator (5 dots)
        dot_row = ttk.Frame(status_frame)
        dot_row.pack(fill='x', padx=20, pady=(0, 8))
        ttk.Label(dot_row, text="Face Samples:", font=('Segoe UI', 9)).pack(side='left', padx=(0, 8))
        self.sample_dots_canvas = tk.Canvas(dot_row, height=28, bg='white', highlightthickness=0)
        self.sample_dots_canvas.pack(side='left', fill='x', expand=True)
        self.sample_dots_canvas.bind('<Configure>', lambda e: self._draw_sample_dots())

        # Status text log
        self.status_text = scrolledtext.ScrolledText(status_frame, height=8, width=60, font=('Consolas', 10))
        self.status_text.pack(fill='x', padx=20, pady=(0, 20))
        
        # Instructions
        instructions = """
Instructions:
1. Select a user from the list on the left
2. Choose your enrollment option (Face, Fingerprint, or Both)
3. Follow the on-screen instructions during enrollment
4. The camera preview will show your face during face enrollment
5. For fingerprint enrollment, place your finger on the sensor
        """
        
        ttk.Label(right_panel, text=instructions, font=('Segoe UI', 9), 
                 foreground=self.colors['dark']).pack(anchor='w', padx=20, pady=(0, 20))
    
    def setup_camera_preview(self):
        """Setup camera preview functionality."""
        self.camera_image = None
        self.camera_photo = None
        self.camera_update_id = None

    # ------------------------------------------------------------------
    # Face-sample dot indicator
    # ------------------------------------------------------------------

    def _draw_sample_dots(self):
        """Redraw the 5 capture-progress dots on the sample_dots_canvas."""
        canvas = self.sample_dots_canvas
        canvas.delete("all")

        total = self.total_samples_count
        captured = self.samples_captured_count
        diameter = 24
        gap = 8
        y0 = 2

        for i in range(total):
            x0 = i * (diameter + gap)
            x1 = x0 + diameter
            if i < captured:
                fill_col, outline_col, text_col = '#27ae60', '#1e8449', 'white'
                label = '✓'
            else:
                fill_col, outline_col, text_col = '#dfe6e9', '#b2bec3', '#7f8c8d'
                label = str(i + 1)
            canvas.create_oval(x0, y0, x1, y0 + diameter,
                               fill=fill_col, outline=outline_col, width=2)
            canvas.create_text(x0 + diameter // 2, y0 + diameter // 2,
                               text=label, fill=text_col,
                               font=('Segoe UI', 9, 'bold'))

    def update_sample_dots(self, captured: int, total: int = 5):
        """Thread-safe update of the capture-progress dots."""
        self.samples_captured_count = captured
        self.total_samples_count = total
        self.root.after(0, self._draw_sample_dots)
    
    def load_users(self):
        """Load and display all users in the listbox."""
        self.user_listbox.delete(0, tk.END)
        
        users = self.user_repo.get_all()
        
        if not users:
            self.user_listbox.insert(tk.END, "No users found. Please add users via web dashboard first.")
            self.selected_user_id = None
            self.update_status("No users available", "orange")
            return
        
        for user in users:
            name = f"{user['first_name']} {user['last_name']}"
            employee_id = user['employee_id']
            face_status = "✓" if user['face_enrolled'] else "✗"
            fp_status = "✓" if user['fingerprint_enrolled'] else "✗"
            
            display_text = f"{name} ({employee_id}) - Face: {face_status}, FP: {fp_status}"
            self.user_listbox.insert(tk.END, display_text)
            self.user_listbox.itemconfig(tk.END, {'foreground': '#2c3e50'})
        
        self.update_status(f"Loaded {len(users)} users", "green")
    
    def on_user_select(self, event):
        """Handle user selection from listbox."""
        selection = self.user_listbox.curselection()
        if selection:
            # Get the index and find corresponding user
            index = selection[0]
            users = self.user_repo.get_all()
            if index < len(users):
                self.selected_user_id = users[index]['id']
                self.update_status(f"Selected: {users[index]['first_name']} {users[index]['last_name']}", "blue")
            else:
                self.selected_user_id = None
        else:
            self.selected_user_id = None
    
    def update_status(self, message, color="black"):
        """Update the status label with a message and color."""
        self.status_label.config(text=message, foreground=color)
        self.log_message(message)
    
    def log_message(self, message):
        """Add a message to the status text area."""
        timestamp = time.strftime("%H:%M:%S")
        self.status_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.status_text.see(tk.END)
    
    def start_camera_preview(self):
        """Start the camera preview."""
        if not self.camera_active:
            self.camera_active = True
            self.camera_update_id = self.root.after(50, self.update_camera_preview)
    
    def stop_camera_preview(self):
        """Stop the camera preview."""
        self.camera_active = False
        if self.camera_update_id:
            self.root.after_cancel(self.camera_update_id)
            self.camera_update_id = None
    
    def update_camera_preview(self):
        """Update the camera preview frame, overlaying face-detection feedback during enrollment."""
        if not self.camera_active:
            return

        frame = self.camera.get_frame()
        if frame is not None:
            # Convert BGR to RGB
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            # ── Face-detection overlay ────────────────────────────────────────
            if self.is_face_enrolling:
                # IMPORTANT: we must NOT call face_recognition here while the
                # enrollment thread is already calling it — dlib is not
                # thread-safe and concurrent calls cause heap corruption.
                # Instead we infer face-detected state from how recently the
                # enrollment engine last successfully captured a sample.
                elapsed = time.time() - self.enrollment_last_sample_time
                face_active = (self.enrollment_last_sample_time > 0
                               and elapsed < 1.5)

                frame_rgb = frame_rgb.copy()
                h, w = frame_rgb.shape[:2]

                if face_active:
                    # Green banner — face is being captured
                    cv2.rectangle(frame_rgb, (0, 0), (w, 52), (39, 174, 96), cv2.FILLED)
                    cv2.putText(frame_rgb,
                                f"Face Detected  ({self.samples_captured_count}/{self.total_samples_count})",
                                (12, 36),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                                (255, 255, 255), 2, cv2.LINE_AA)
                    self.face_status_label.config(
                        text="✔  Face detected — hold still",
                        foreground='#27ae60'
                    )
                else:
                    # Red banner — engine is waiting for a face
                    cv2.rectangle(frame_rgb, (0, 0), (w, 52), (220, 53, 69), cv2.FILLED)
                    cv2.putText(frame_rgb, "No Face Detected",
                                (12, 36),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                                (255, 255, 255), 2, cv2.LINE_AA)
                    self.face_status_label.config(
                        text="⚠  No face detected — position your face in the frame",
                        foreground='#c0392b'
                    )

            elif not self.is_face_enrolling:
                # Pre-enrollment: lightweight face check so the user can
                # position themselves before clicking Enroll.
                # This path is safe because the enrollment thread is idle.
                self._preview_frame_count += 1
                if self._preview_frame_count % 5 == 0:
                    small = cv2.resize(frame_rgb, (0, 0), fx=0.25, fy=0.25)
                    locations = face_recognition.face_locations(
                        small, model=FACE_DETECTION_MODEL
                    )
                    self.face_locations_preview = [
                        (t * 4, r * 4, b * 4, l * 4) for (t, r, b, l) in locations
                    ]
                    self.face_detected_in_preview = (len(locations) == 1)

                if self.face_detected_in_preview and self.face_locations_preview:
                    frame_rgb = frame_rgb.copy()
                    for (top, right, bottom, left) in self.face_locations_preview:
                        cv2.rectangle(frame_rgb, (left, top), (right, bottom),
                                      (72, 187, 120), 2)
                    self.face_status_label.config(
                        text="✔  Face in frame — ready to enroll",
                        foreground='#27ae60'
                    )
                else:
                    self.face_status_label.config(text="", foreground='gray')
            # ─────────────────────────────────────────────────────────────────

            # Resize to fit canvas
            height, width = frame_rgb.shape[:2]
            canvas_width = self.camera_canvas.winfo_width()
            canvas_height = self.camera_canvas.winfo_height()

            if canvas_width > 1 and canvas_height > 1:
                # Maintain aspect ratio
                frame_ratio = width / height
                canvas_ratio = canvas_width / canvas_height

                if frame_ratio > canvas_ratio:
                    new_width = canvas_width
                    new_height = int(canvas_width / frame_ratio)
                else:
                    new_height = canvas_height
                    new_width = int(canvas_height * frame_ratio)

                resized_frame = cv2.resize(frame_rgb, (new_width, new_height))

                image = Image.fromarray(resized_frame)
                photo = ImageTk.PhotoImage(image=image)

                self.camera_canvas.delete("all")
                x = (canvas_width - new_width) // 2
                y = (canvas_height - new_height) // 2
                self.camera_canvas.create_image(x, y, anchor='nw', image=photo)

                # Keep reference to prevent garbage collection
                self.camera_photo = photo

        # Schedule next update
        if self.camera_active:
            self.camera_update_id = self.root.after(50, self.update_camera_preview)
    
    def validate_selection(self):
        """Validate that a user is selected."""
        if self.selected_user_id is None:
            messagebox.showwarning("Warning", "Please select a user first.")
            return False
        
        # Get user details
        user = self.user_repo.get_by_id(self.selected_user_id)
        if not user:
            messagebox.showerror("Error", "Selected user not found.")
            return False
        
        return True
    
    def enroll_face_only(self):
        """Start face-only enrollment."""
        if not self.validate_selection():
            return
        
        user = self.user_repo.get_by_id(self.selected_user_id)
        message = f"Start face enrollment for {user['first_name']} {user['last_name']}?"
        
        if messagebox.askyesno("Confirm Enrollment", message):
            self.start_enrollment_thread(self.enroll_face_process)
    
    def enroll_fingerprint_only(self):
        """Start fingerprint-only enrollment."""
        if not self.validate_selection():
            return
        
        user = self.user_repo.get_by_id(self.selected_user_id)
        message = f"Start fingerprint enrollment for {user['first_name']} {user['last_name']}?"
        
        if messagebox.askyesno("Confirm Enrollment", message):
            self.start_enrollment_thread(self.enroll_fingerprint_process)
    
    def enroll_both(self):
        """Start both face and fingerprint enrollment."""
        if not self.validate_selection():
            return
        
        user = self.user_repo.get_by_id(self.selected_user_id)
        message = f"Start full enrollment (face + fingerprint) for {user['first_name']} {user['last_name']}?"
        
        if messagebox.askyesno("Confirm Enrollment", message):
            self.start_enrollment_thread(self.enroll_both_process)
    
    def start_enrollment_thread(self, process_function):
        """Start enrollment in a separate thread."""
        if self.enrollment_thread and self.enrollment_thread.is_alive():
            messagebox.showwarning("Warning", "Enrollment already in progress.")
            return
        
        self.stop_enrollment = False
        self.progress_var.set(0)
        self.log_message("Starting enrollment process...")
        
        self.enrollment_thread = threading.Thread(target=process_function, daemon=True)
        self.enrollment_thread.start()
    
    def enroll_face_process(self):
        """Process for face enrollment."""
        # Reset dot indicator and activate face-detection overlay
        self.root.after(0, lambda: self.update_sample_dots(0, 5))
        self.is_face_enrolling = True
        self.enrollment_last_sample_time = 0.0   # reset — no sample captured yet
        self._preview_frame_count = 0
        self.face_detected_in_preview = False
        self.face_locations_preview = []

        try:
            self.root.after(0, lambda: self.update_status("Starting face enrollment...", "blue"))

            # Start camera if not running
            if not self.camera.is_running():
                if not self.camera.start():
                    self.root.after(0, lambda: self.update_status("Failed to start camera", "red"))
                    return

            self.log_message("Please look at the camera...")
            self.log_message("The system will capture 5 face samples.")
            self.log_message("Capture pauses automatically when no face is detected.")

            def progress_callback(captured, total):
                if self.stop_enrollment:
                    return
                self.enrollment_last_sample_time = time.time()
                self.progress_var.set((captured / total) * 100)
                self.root.after(0, lambda: self.update_sample_dots(captured, total))
                self.root.after(0, lambda: self.update_status(
                    f"Captured {captured}/{total} samples...", "blue"))

            success, message = self.face_enrollment.enroll_face(
                user_id=self.selected_user_id,
                num_samples=5,
                callback=progress_callback
            )

            if success:
                self.root.after(0, lambda: self.update_status("Face enrollment completed successfully!", "green"))
                self.progress_var.set(100)
                self.root.after(0, lambda: self.update_sample_dots(5, 5))
                self.root.after(0, lambda: self.log_message(f"✓ {message}"))
                self.root.after(0, lambda: messagebox.showinfo("Success", f"Face enrollment completed!\n{message}"))
            else:
                self.root.after(0, lambda: self.update_status("Face enrollment failed", "red"))
                self.root.after(0, lambda: self.log_message(f"✗ {message}"))
                self.root.after(0, lambda: messagebox.showerror("Error", f"Face enrollment failed:\n{message}"))

            self.root.after(0, self.load_users)

        except Exception as e:
            self.root.after(0, lambda: self.update_status(f"Enrollment error: {str(e)}", "red"))
            self.root.after(0, lambda: self.log_message(f"Error: {str(e)}"))
            self.root.after(0, lambda: messagebox.showerror("Error", f"An error occurred: {str(e)}"))

        finally:
            # Always deactivate the face-detection overlay
            self.is_face_enrolling = False
            self.face_detected_in_preview = False
            self.face_locations_preview = []
            self.root.after(0, lambda: self.face_status_label.config(text="", foreground='gray'))
    
    def enroll_fingerprint_process(self):
        """Process for fingerprint enrollment."""
        try:
            self.update_status("Starting fingerprint enrollment...", "blue")
            
            # Start fingerprint sensor
            if not self.fingerprint_manager.start():
                self.update_status("Failed to connect to fingerprint sensor", "red")
                messagebox.showerror("Error", "Failed to connect to fingerprint sensor")
                return
            
            self.log_message("Please place your finger on the sensor...")
            
            def progress_callback(message):
                if self.stop_enrollment:
                    return
                self.update_status(f"Fingerprint: {message}", "blue")
                self.log_message(f"  {message}")
                self.root.update()
            
            success, message, fp_id = self.fingerprint_manager.enroll(
                user_id=self.selected_user_id,
                finger_position='right_index',
                callback=progress_callback
            )
            
            if success:
                self.update_status("Fingerprint enrollment completed!", "green")
                self.progress_var.set(100)
                self.log_message(f"✓ {message}")
                self.log_message(f"  Fingerprint ID: {fp_id}")
                messagebox.showinfo("Success", f"Fingerprint enrollment completed!\n{message}\nFingerprint ID: {fp_id}")
            else:
                self.update_status("Fingerprint enrollment failed", "red")
                self.log_message(f"✗ {message}")
                messagebox.showerror("Error", f"Fingerprint enrollment failed:\n{message}")
            
            # Refresh user list to show updated status
            self.load_users()
            
        except Exception as e:
            self.update_status(f"Enrollment error: {str(e)}", "red")
            self.log_message(f"Error: {str(e)}")
            messagebox.showerror("Error", f"An error occurred: {str(e)}")
    
    def enroll_both_process(self):
        """Process for both face and fingerprint enrollment."""
        try:
            self.update_status("Starting full enrollment...", "blue")

            # ── Step 1: Face enrollment ───────────────────────────────────────
            self.log_message("=" * 50)
            self.log_message("FACE ENROLLMENT")
            self.log_message("=" * 50)

            # Activate face-detection overlay for the face phase
            self.root.after(0, lambda: self.update_sample_dots(0, 5))
            self.is_face_enrolling = True
            self.enrollment_last_sample_time = 0.0   # reset
            self._preview_frame_count = 0
            self.face_detected_in_preview = False
            self.face_locations_preview = []

            if not self.camera.is_running():
                if not self.camera.start():
                    self.is_face_enrolling = False
                    self.update_status("Failed to start camera", "red")
                    return

            self.log_message("Please look at the camera...")
            self.log_message("Capture pauses automatically when no face is detected.")

            def face_progress_callback(captured, total):
                if self.stop_enrollment:
                    return
                self.enrollment_last_sample_time = time.time()
                self.progress_var.set((captured / 10) * 100)  # Face phase = 50 % of total
                self.root.after(0, lambda: self.update_sample_dots(captured, total))
                self.root.after(0, lambda: self.update_status(
                    f"Face: {captured}/{total} samples...", "blue"))

            face_success, face_message = self.face_enrollment.enroll_face(
                user_id=self.selected_user_id,
                num_samples=5,
                callback=face_progress_callback
            )

            # Deactivate face-detection overlay after face phase
            self.is_face_enrolling = False
            self.face_detected_in_preview = False
            self.face_locations_preview = []
            self.root.after(0, lambda: self.face_status_label.config(text="", foreground='gray'))

            if face_success:
                self.root.after(0, lambda: self.log_message(f"✓ Face enrollment successful: {face_message}"))
                self.root.after(0, lambda: self.update_sample_dots(5, 5))
                self.progress_var.set(50)
            else:
                self.root.after(0, lambda: self.log_message(f"✗ Face enrollment failed: {face_message}"))
                self.root.after(0, lambda: self.update_status("Face enrollment failed", "red"))
            
            # Step 2: Fingerprint enrollment
            self.log_message("=" * 50)
            self.log_message("FINGERPRINT ENROLLMENT")
            self.log_message("=" * 50)
            
            if not self.fingerprint_manager.start():
                self.root.after(0, lambda: self.update_status("Failed to connect to fingerprint sensor", "red"))
                return
            
            def fp_progress_callback(message):
                if self.stop_enrollment:
                    return
                self.progress_var.set(50 + (self.progress_var.get() / 2))
                self.root.after(0, lambda: self.update_status(f"Fingerprint: {message}", "blue"))
                self.root.after(0, lambda: self.log_message(f"  {message}"))
            
            fp_success, fp_message, fp_id = self.fingerprint_manager.enroll(
                user_id=self.selected_user_id,
                finger_position='right_index',
                callback=fp_progress_callback
            )
            
            if fp_success:
                self.root.after(0, lambda: self.log_message(f"✓ Fingerprint enrollment successful: {fp_message}"))
                self.root.after(0, lambda: self.log_message(f"  Fingerprint ID: {fp_id}"))
                self.progress_var.set(100)
            else:
                self.root.after(0, lambda: self.log_message(f"✗ Fingerprint enrollment failed: {fp_message}"))
                self.root.after(0, lambda: self.update_status("Fingerprint enrollment failed", "red"))
            
            # Summary
            self.root.after(0, lambda: self.log_message("=" * 50))
            self.root.after(0, lambda: self.log_message("ENROLLMENT SUMMARY"))
            self.root.after(0, lambda: self.log_message("=" * 50))
            self.root.after(0, lambda: self.log_message(
                f"Face:        {'✓ Enrolled' if face_success else '✗ Failed'}"))
            self.root.after(0, lambda: self.log_message(
                f"Fingerprint: {'✓ Enrolled' if fp_success else '✗ Failed'}"))
            
            if face_success and fp_success:
                self.root.after(0, lambda: self.update_status("Full enrollment completed successfully!", "green"))
                self.root.after(0, lambda: messagebox.showinfo("Success", "Full enrollment completed successfully!\nUser is now fully enrolled."))
            else:
                self.root.after(0, lambda: self.update_status("Enrollment incomplete", "orange"))
                self.root.after(0, lambda: messagebox.showwarning("Warning", "Enrollment incomplete. Some biometrics may have failed."))
            
            self.root.after(0, self.load_users)
            
        except Exception as e:
            self.root.after(0, lambda: self.update_status(f"Enrollment error: {str(e)}", "red"))
            self.root.after(0, lambda: self.log_message(f"Error: {str(e)}"))
            self.root.after(0, lambda: messagebox.showerror("Error", f"An error occurred: {str(e)}"))

        finally:
            # Ensure face-detection overlay is always deactivated on exit
            self.is_face_enrolling = False
            self.face_detected_in_preview = False
            self.face_locations_preview = []
            self.root.after(0, lambda: self.face_status_label.config(text="", foreground='gray'))

    def cancel_enrollment(self):
        """Cancel the current enrollment process."""
        if self.enrollment_thread and self.enrollment_thread.is_alive():
            self.stop_enrollment = True
            self.update_status("Enrollment cancelled", "orange")
            self.log_message("Enrollment cancelled by user")
            messagebox.showinfo("Cancelled", "Enrollment process has been cancelled.")
        else:
            messagebox.showinfo("Info", "No enrollment in progress.")
    
    def cleanup(self):
        """Clean up resources before closing."""
        self.stop_camera_preview()
        self.camera.stop()
        self.fingerprint_manager.stop()
        self.root.destroy()


def main():
    """Main entry point for the GUI application."""
    root = tk.Tk()
    app = EnrollmentGUI(root)
    
    # Handle window close
    def on_closing():
        app.cleanup()
    
    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()