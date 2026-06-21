"""
Smart Door Security System - Database Manager
Handles all database operations with connection pooling and thread safety.
"""

import sqlite3
import threading
import hashlib
import pickle
from datetime import datetime, date, time
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
import logging
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DATABASE_PATH

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class DatabaseManager:
    """Thread-safe database manager with connection pooling."""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """Singleton pattern for database manager."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialize the database manager."""
        if self._initialized:
            return
        
        self._local = threading.local()
        self.db_path = DATABASE_PATH
        self._initialized = True
        self._init_database()
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
            )
            self._local.connection.row_factory = sqlite3.Row
            self._local.connection.execute("PRAGMA foreign_keys = ON")
        return self._local.connection
    
    def _init_database(self):
        """Initialize database with schema."""
        # Ensure database directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Read and execute schema
        schema_path = Path(__file__).parent / "schema.sql"
        if schema_path.exists():
            with open(schema_path, 'r') as f:
                schema = f.read()
            
            conn = self._get_connection()
            conn.executescript(schema)
            conn.commit()
            logger.info("Database initialized successfully")
        else:
            logger.error(f"Schema file not found: {schema_path}")
    
    def execute(self, query: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a query and return cursor."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(query, params)
        return cursor
    
    def execute_many(self, query: str, params_list: List[tuple]) -> None:
        """Execute a query with multiple parameter sets."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.executemany(query, params_list)
        conn.commit()
    
    def commit(self):
        """Commit current transaction."""
        conn = self._get_connection()
        conn.commit()
    
    def rollback(self):
        """Rollback current transaction."""
        conn = self._get_connection()
        conn.rollback()
    
    def close(self):
        """Close thread-local connection."""
        if hasattr(self._local, 'connection') and self._local.connection:
            self._local.connection.close()
            self._local.connection = None


class AdminRepository:
    """Repository for admin-related database operations."""
    
    def __init__(self):
        self.db = DatabaseManager()
    
    def get_by_username(self, username: str) -> Optional[Dict]:
        """Get admin by username."""
        cursor = self.db.execute(
            "SELECT * FROM admin WHERE username = ?",
            (username,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def get_by_id(self, admin_id: int) -> Optional[Dict]:
        """Get admin by ID."""
        cursor = self.db.execute(
            "SELECT * FROM admin WHERE id = ?",
            (admin_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    
    # Lockout functionality removed - login attempts no longer tracked
    
    def update_last_login(self, admin_id: int):
        """Update last login timestamp."""
        self.db.execute(
            "UPDATE admin SET last_login = ?, updated_at = ? WHERE id = ?",
            (datetime.now(), datetime.now(), admin_id)
        )
        self.db.commit()
    
    def create_session(self, admin_id: int, token: str, expires_at: datetime, 
                       ip_address: str = None, user_agent: str = None) -> int:
        """Create a new admin session."""
        cursor = self.db.execute(
            """INSERT INTO admin_sessions (admin_id, session_token, ip_address, user_agent, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (admin_id, token, ip_address, user_agent, expires_at)
        )
        self.db.commit()
        return cursor.lastrowid
    
    def get_session(self, token: str) -> Optional[Dict]:
        """Get session by token."""
        cursor = self.db.execute(
            """SELECT s.*, a.username, a.full_name 
               FROM admin_sessions s
               JOIN admin a ON s.admin_id = a.id
               WHERE s.session_token = ? AND s.expires_at > ?""",
            (token, datetime.now())
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def delete_session(self, token: str):
        """Delete a session."""
        self.db.execute("DELETE FROM admin_sessions WHERE session_token = ?", (token,))
        self.db.commit()


class UserRepository:
    """Repository for user-related database operations."""
    
    def __init__(self):
        self.db = DatabaseManager()
    
    def create(self, employee_id: str, first_name: str, last_name: str,
               email: str = None, phone: str = None, department: str = None,
               designation: str = None, created_by: int = None) -> int:
        """Create a new user."""
        cursor = self.db.execute(
            """INSERT INTO users (employee_id, first_name, last_name, email, phone, 
                                  department, designation, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (employee_id, first_name, last_name, email, phone, department, designation, created_by)
        )
        self.db.commit()
        return cursor.lastrowid
    
    def get_by_id(self, user_id: int) -> Optional[Dict]:
        """Get user by ID."""
        cursor = self.db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def get_by_employee_id(self, employee_id: str) -> Optional[Dict]:
        """Get user by employee ID."""
        cursor = self.db.execute(
            "SELECT * FROM users WHERE employee_id = ?",
            (employee_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def get_all(self, active_only: bool = False) -> List[Dict]:
        """Get all users."""
        query = "SELECT * FROM users"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY first_name, last_name"
        
        cursor = self.db.execute(query)
        return [dict(row) for row in cursor.fetchall()]
    
    def get_active_enrolled_users(self) -> List[Dict]:
        """Get all active users with both face and fingerprint enrolled."""
        cursor = self.db.execute(
            """SELECT * FROM users 
               WHERE is_active = 1 AND face_enrolled = 1 AND fingerprint_enrolled = 1"""
        )
        return [dict(row) for row in cursor.fetchall()]
    
    def update(self, user_id: int, **kwargs) -> bool:
        """Update user fields."""
        if not kwargs:
            return False
        
        allowed_fields = ['first_name', 'last_name', 'email', 'phone', 
                          'department', 'designation', 'is_active',
                          'face_enrolled', 'fingerprint_enrolled']
        
        updates = []
        values = []
        for key, value in kwargs.items():
            if key in allowed_fields:
                updates.append(f"{key} = ?")
                values.append(value)
        
        if not updates:
            return False
        
        updates.append("updated_at = ?")
        values.append(datetime.now())
        values.append(user_id)
        
        query = f"UPDATE users SET {', '.join(updates)} WHERE id = ?"
        self.db.execute(query, tuple(values))
        self.db.commit()
        return True
    
    def delete(self, user_id: int) -> bool:
        """Delete a user (cascades to face and fingerprint data)."""
        self.db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self.db.commit()
        return True
    
    def set_active(self, user_id: int, is_active: bool) -> bool:
        """Enable or disable a user."""
        return self.update(user_id, is_active=is_active)


class FaceEncodingRepository:
    """Repository for face encoding operations."""
    
    def __init__(self):
        self.db = DatabaseManager()
    
    def save_encoding(self, user_id: int, encoding_array, num_samples: int = 1,
                      quality_score: float = 0.0) -> int:
        """Save face encoding for a user."""
        # Serialize numpy array
        encoding_bytes = pickle.dumps(encoding_array)
        encoding_hash = hashlib.sha256(encoding_bytes).hexdigest()
        
        # Check if encoding exists
        cursor = self.db.execute(
            "SELECT id FROM face_encodings WHERE user_id = ?",
            (user_id,)
        )
        existing = cursor.fetchone()
        
        if existing:
            # Update existing
            self.db.execute(
                """UPDATE face_encodings 
                   SET encoding_data = ?, encoding_hash = ?, num_samples = ?, 
                       quality_score = ?, updated_at = ?
                   WHERE user_id = ?""",
                (encoding_bytes, encoding_hash, num_samples, quality_score, 
                 datetime.now(), user_id)
            )
            result_id = existing[0]
        else:
            # Insert new
            cursor = self.db.execute(
                """INSERT INTO face_encodings (user_id, encoding_data, encoding_hash, 
                                               num_samples, quality_score)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, encoding_bytes, encoding_hash, num_samples, quality_score)
            )
            result_id = cursor.lastrowid
        
        # Update user's face_enrolled status
        self.db.execute(
            "UPDATE users SET face_enrolled = 1, updated_at = ? WHERE id = ?",
            (datetime.now(), user_id)
        )
        self.db.commit()
        return result_id
    
    def get_encoding(self, user_id: int) -> Optional[Any]:
        """Get face encoding for a user."""
        cursor = self.db.execute(
            "SELECT encoding_data FROM face_encodings WHERE user_id = ?",
            (user_id,)
        )
        row = cursor.fetchone()
        if row:
            return pickle.loads(row[0])
        return None
    
    def get_all_encodings(self) -> List[Dict]:
        """Get all face encodings with user IDs."""
        cursor = self.db.execute(
            """SELECT fe.user_id, fe.encoding_data, u.first_name, u.last_name, u.employee_id
               FROM face_encodings fe
               JOIN users u ON fe.user_id = u.id
               WHERE u.is_active = 1"""
        )
        results = []
        for row in cursor.fetchall():
            encoding = pickle.loads(row[1])
            results.append({
                'user_id': row[0],
                'encoding': encoding,
                'name': f"{row[2]} {row[3]}",
                'employee_id': row[4]
            })
        return results
    
    def delete_encoding(self, user_id: int) -> bool:
        """Delete face encoding for a user."""
        self.db.execute("DELETE FROM face_encodings WHERE user_id = ?", (user_id,))
        self.db.execute(
            "UPDATE users SET face_enrolled = 0, updated_at = ? WHERE id = ?",
            (datetime.now(), user_id)
        )
        self.db.commit()
        return True


class FingerprintRepository:
    """Repository for fingerprint data operations."""
    
    def __init__(self):
        self.db = DatabaseManager()
    
    def save_fingerprint(self, user_id: int, fingerprint_id: int,
                         template_hash: str, finger_position: str = 'right_index') -> int:
        """Save fingerprint data for a user."""
        # Check if exists
        cursor = self.db.execute(
            "SELECT id FROM fingerprint_data WHERE user_id = ?",
            (user_id,)
        )
        existing = cursor.fetchone()
        
        if existing:
            self.db.execute(
                """UPDATE fingerprint_data 
                   SET fingerprint_id = ?, template_hash = ?, finger_position = ?, updated_at = ?
                   WHERE user_id = ?""",
                (fingerprint_id, template_hash, finger_position, datetime.now(), user_id)
            )
            result_id = existing[0]
        else:
            cursor = self.db.execute(
                """INSERT INTO fingerprint_data (user_id, fingerprint_id, template_hash, finger_position)
                   VALUES (?, ?, ?, ?)""",
                (user_id, fingerprint_id, template_hash, finger_position)
            )
            result_id = cursor.lastrowid
        
        # Update user's fingerprint_enrolled status
        self.db.execute(
            "UPDATE users SET fingerprint_enrolled = 1, updated_at = ? WHERE id = ?",
            (datetime.now(), user_id)
        )
        self.db.commit()
        return result_id
    
    def get_by_fingerprint_id(self, fingerprint_id: int) -> Optional[Dict]:
        """Get user by fingerprint sensor ID."""
        cursor = self.db.execute(
            """SELECT fd.*, u.first_name, u.last_name, u.employee_id, u.is_active
               FROM fingerprint_data fd
               JOIN users u ON fd.user_id = u.id
               WHERE fd.fingerprint_id = ?""",
            (fingerprint_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def get_by_user_id(self, user_id: int) -> Optional[Dict]:
        """Get fingerprint data by user ID."""
        cursor = self.db.execute(
            "SELECT * FROM fingerprint_data WHERE user_id = ?",
            (user_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def get_all_fingerprints(self) -> List[Dict]:
        """Get all fingerprint mappings."""
        cursor = self.db.execute(
            """SELECT fd.fingerprint_id, fd.user_id, u.first_name, u.last_name, u.employee_id
               FROM fingerprint_data fd
               JOIN users u ON fd.user_id = u.id
               WHERE u.is_active = 1"""
        )
        return [dict(row) for row in cursor.fetchall()]
    
    def delete_fingerprint(self, user_id: int) -> bool:
        """Delete fingerprint data for a user."""
        self.db.execute("DELETE FROM fingerprint_data WHERE user_id = ?", (user_id,))
        self.db.execute(
            "UPDATE users SET fingerprint_enrolled = 0, updated_at = ? WHERE id = ?",
            (datetime.now(), user_id)
        )
        self.db.commit()
        return True


class AccessLogRepository:
    """Repository for access log operations."""
    
    def __init__(self):
        self.db = DatabaseManager()
    
    def log_access(self, user_id: Optional[int], event_type: str, result: str,
                   face_match: bool = False, fingerprint_match: bool = False,
                   failure_reason: str = None, confidence_score: float = None,
                   ip_address: str = None) -> int:
        """Log an access attempt."""
        now = datetime.now()
        
        # Get user info if available
        employee_id = None
        user_name = None
        if user_id:
            user_repo = UserRepository()
            user = user_repo.get_by_id(user_id)
            if user:
                employee_id = user['employee_id']
                user_name = f"{user['first_name']} {user['last_name']}"

        cursor = self.db.execute(
            """INSERT INTO access_logs
               (user_id, employee_id, user_name, event_type, access_date, access_time,
                result, face_match, fingerprint_match, failure_reason, confidence_score, ip_address)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, employee_id, user_name, event_type, now.date(), now.strftime('%H:%M:%S'),
             result, face_match, fingerprint_match, failure_reason, confidence_score, ip_address)
        )
        self.db.commit()
        return cursor.lastrowid
    
    def get_logs(self, start_date: date = None, end_date: date = None,
                 user_id: int = None, result: str = None, 
                 limit: int = 100, offset: int = 0) -> List[Dict]:
        """Get access logs with filters."""
        query = "SELECT * FROM access_logs WHERE 1=1"
        params = []
        
        if start_date:
            query += " AND access_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND access_date <= ?"
            params.append(end_date)
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        if result:
            query += " AND result = ?"
            params.append(result)
        
        query += " ORDER BY access_date DESC, access_time DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        cursor = self.db.execute(query, tuple(params))
        return [dict(row) for row in cursor.fetchall()]
    
    def get_recent_logs(self, limit: int = 50) -> List[Dict]:
        """Get most recent logs."""
        cursor = self.db.execute(
            """SELECT * FROM access_logs 
               ORDER BY created_at DESC LIMIT ?""",
            (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]
    
    def get_stats(self, days: int = 7) -> Dict:
        """Get access statistics for the last N days."""
        cursor = self.db.execute(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN result = 'SUCCESS' THEN 1 ELSE 0 END) as successful,
                SUM(CASE WHEN result = 'FAILED' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN result = 'DENIED' THEN 1 ELSE 0 END) as denied
               FROM access_logs
               WHERE access_date >= date('now', ?)""",
            (f'-{days} days',)
        )
        row = cursor.fetchone()
        return dict(row) if row else {'total': 0, 'successful': 0, 'failed': 0, 'denied': 0}

    def get_daily_stats(self, days: int = 30) -> List[Dict]:
        """Get daily access statistics for the last N days."""
        cursor = self.db.execute(
            """SELECT
                access_date,
                COUNT(*) as total,
                SUM(CASE WHEN result = 'SUCCESS' THEN 1 ELSE 0 END) as successful,
                SUM(CASE WHEN result = 'FAILED' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN result = 'DENIED' THEN 1 ELSE 0 END) as denied
               FROM access_logs
               WHERE access_date >= date('now', ?)
               GROUP BY access_date
               ORDER BY access_date""",
            (f'-{days} days',)
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_hourly_stats(self, days: int = 7) -> List[Dict]:
        """Get hourly access statistics for the last N days."""
        cursor = self.db.execute(
            """SELECT
                CAST(strftime('%H', access_time) AS INTEGER) as hour,
                COUNT(*) as total,
                SUM(CASE WHEN result = 'SUCCESS' THEN 1 ELSE 0 END) as successful,
                SUM(CASE WHEN result = 'FAILED' THEN 1 ELSE 0 END) as failed
               FROM access_logs
               WHERE access_date >= date('now', ?)
               GROUP BY hour
               ORDER BY hour""",
            (f'-{days} days',)
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_user_activity(self, days: int = 30) -> List[Dict]:
        """Get user activity statistics for the last N days."""
        cursor = self.db.execute(
            """SELECT
                u.first_name || ' ' || u.last_name as user_name,
                u.employee_id,
                COUNT(al.id) as access_count,
                SUM(CASE WHEN al.result = 'SUCCESS' THEN 1 ELSE 0 END) as successful_access
               FROM users u
               LEFT JOIN access_logs al ON u.id = al.user_id AND al.access_date >= date('now', ?)
               GROUP BY u.id, u.first_name, u.last_name, u.employee_id
               ORDER BY access_count DESC
               LIMIT 20""",
            (f'-{days} days',)
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_peak_hours(self, days: int = 30) -> List[Dict]:
        """Get peak access hours for the last N days."""
        cursor = self.db.execute(
            """SELECT
                CAST(strftime('%H', access_time) AS INTEGER) as hour,
                COUNT(*) as total_access
               FROM access_logs
               WHERE access_date >= date('now', ?) AND result = 'SUCCESS'
               GROUP BY hour
               ORDER BY total_access DESC
               LIMIT 5""",
            (f'-{days} days',)
        )
        return [dict(row) for row in cursor.fetchall()]


class SystemLogRepository:
    """Repository for system log operations."""
    
    def __init__(self):
        self.db = DatabaseManager()
    
    def log(self, level: str, module: str, message: str, details: str = None):
        """Log a system event."""
        self.db.execute(
            "INSERT INTO system_logs (log_level, module, message, details) VALUES (?, ?, ?, ?)",
            (level, module, message, details)
        )
        self.db.commit()
    
    def info(self, module: str, message: str, details: str = None):
        self.log('INFO', module, message, details)
    
    def warning(self, module: str, message: str, details: str = None):
        self.log('WARNING', module, message, details)
    
    def error(self, module: str, message: str, details: str = None):
        self.log('ERROR', module, message, details)
    
    def get_logs(self, level: str = None, module: str = None, 
                 limit: int = 100) -> List[Dict]:
        """Get system logs with filters."""
        query = "SELECT * FROM system_logs WHERE 1=1"
        params = []
        
        if level:
            query += " AND log_level = ?"
            params.append(level)
        if module:
            query += " AND module = ?"
            params.append(module)
        
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        
        cursor = self.db.execute(query, tuple(params))
        return [dict(row) for row in cursor.fetchall()]


# Initialize database on import
if __name__ == "__main__":
    db = DatabaseManager()
    print(f"Database initialized at: {DATABASE_PATH}")
