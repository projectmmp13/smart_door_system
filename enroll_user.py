#!/usr/bin/env python3
"""
Smart Door Security System - User Enrollment Script
Enrolls face and fingerprint biometrics for a user.
"""

import sys
import argparse
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from database.db_manager import UserRepository, DatabaseManager
from modules.face_recognition_module import FaceEnrollment, CameraManager
from modules.fingerprint_module import FingerprintManager


def list_users():
    """List all users in the database."""
    user_repo = UserRepository()
    users = user_repo.get_all()
    
    if not users:
        print("\nNo users found. Please add users via the web dashboard first.")
        return
    
    print("\n" + "=" * 70)
    print(f"{'ID':<5} {'Employee ID':<15} {'Name':<25} {'Face':<8} {'Fingerprint':<12}")
    print("=" * 70)
    
    for user in users:
        face_status = "✓" if user['face_enrolled'] else "✗"
        fp_status = "✓" if user['fingerprint_enrolled'] else "✗"
        name = f"{user['first_name']} {user['last_name']}"
        print(f"{user['id']:<5} {user['employee_id']:<15} {name:<25} {face_status:<8} {fp_status:<12}")
    
    print("=" * 70)
    print(f"Total: {len(users)} users\n")


def enroll_face(user_id: int):
    """Enroll face for a user."""
    user_repo = UserRepository()
    user = user_repo.get_by_id(user_id)
    
    if not user:
        print(f"Error: User with ID {user_id} not found.")
        return False
    
    print(f"\nEnrolling face for: {user['first_name']} {user['last_name']}")
    print("Please look at the camera...")
    print("The system will capture 5 face samples.\n")
    
    def progress_callback(captured, total):
        print(f"  Captured {captured}/{total} samples...")
    
    enrollment = FaceEnrollment()
    
    # Start camera
    camera = CameraManager()
    if not camera.start():
        print("Error: Failed to start camera.")
        return False
    
    try:
        success, message = enrollment.enroll_face(
            user_id=user_id,
            num_samples=5,
            callback=progress_callback
        )
        
        if success:
            print(f"\n✓ Success: {message}")
            return True
        else:
            print(f"\n✗ Failed: {message}")
            return False
    finally:
        camera.stop()


def enroll_fingerprint(user_id: int, simulation: bool = False):
    """Enroll fingerprint for a user."""
    user_repo = UserRepository()
    user = user_repo.get_by_id(user_id)
    
    if not user:
        print(f"Error: User with ID {user_id} not found.")
        return False
    
    print(f"\nEnrolling fingerprint for: {user['first_name']} {user['last_name']}")
    
    def progress_callback(message):
        print(f"  {message}")
    
    fp_manager = FingerprintManager(simulation=simulation)
    
    if not fp_manager.start():
        print("Error: Failed to connect to fingerprint sensor.")
        return False
    
    try:
        success, message, fp_id = fp_manager.enroll(
            user_id=user_id,
            finger_position='right_index',
            callback=progress_callback
        )
        
        if success:
            print(f"\n✓ Success: {message}")
            print(f"  Fingerprint ID: {fp_id}")
            return True
        else:
            print(f"\n✗ Failed: {message}")
            return False
    finally:
        fp_manager.stop()


def enroll_both(user_id: int, simulation: bool = False):
    """Enroll both face and fingerprint for a user."""
    print("\n" + "=" * 50)
    print("BIOMETRIC ENROLLMENT")
    print("=" * 50)
    
    # Enroll face
    print("\n[1/2] Face Enrollment")
    print("-" * 30)
    face_success = enroll_face(user_id)
    
    # Enroll fingerprint
    print("\n[2/2] Fingerprint Enrollment")
    print("-" * 30)
    fp_success = enroll_fingerprint(user_id, simulation)
    
    # Summary
    print("\n" + "=" * 50)
    print("ENROLLMENT SUMMARY")
    print("=" * 50)
    print(f"Face:        {'✓ Enrolled' if face_success else '✗ Failed'}")
    print(f"Fingerprint: {'✓ Enrolled' if fp_success else '✗ Failed'}")
    
    if face_success and fp_success:
        print("\n✓ User is fully enrolled and can now access the door.")
    else:
        print("\n✗ Enrollment incomplete. User cannot access the door.")
    
    return face_success and fp_success


def main():
    parser = argparse.ArgumentParser(
        description='Smart Door Security System - User Enrollment',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python enroll_user.py --list              # List all users
  python enroll_user.py --user 1            # Enroll both face and fingerprint for user ID 1
  python enroll_user.py --user 1 --face     # Enroll only face for user ID 1
  python enroll_user.py --user 1 --fp       # Enroll only fingerprint for user ID 1
  python enroll_user.py --user 1 --sim      # Enroll with fingerprint simulation
        """
    )
    
    parser.add_argument('--list', '-l', action='store_true',
                       help='List all users')
    parser.add_argument('--user', '-u', type=int,
                       help='User ID to enroll')
    parser.add_argument('--face', '-f', action='store_true',
                       help='Enroll only face')
    parser.add_argument('--fp', action='store_true',
                       help='Enroll only fingerprint')
    parser.add_argument('--sim', '-s', action='store_true',
                       help='Use fingerprint simulation mode')
    
    args = parser.parse_args()
    
    # Initialize database
    db = DatabaseManager()
    
    if args.list:
        list_users()
        return
    
    if not args.user:
        parser.print_help()
        print("\nError: Please specify a user ID with --user or use --list to see available users.")
        return
    
    # Determine what to enroll
    if args.face and not args.fp:
        enroll_face(args.user)
    elif args.fp and not args.face:
        enroll_fingerprint(args.user, args.sim)
    else:
        enroll_both(args.user, args.sim)


if __name__ == "__main__":
    main()
