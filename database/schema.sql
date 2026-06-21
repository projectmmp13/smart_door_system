                                                                                                                                                               -- Smart Door Security System - Database Schema
-- Fully normalized SQL tables for the IoT security system

-- Admin table for web dashboard authentication
CREATE TABLE IF NOT EXISTS admin (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    full_name VARCHAR(100) NOT NULL,
    is_active BOOLEAN DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_login DATETIME NULL
);

-- Users table for people who can access the door
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id VARCHAR(50) UNIQUE NOT NULL,
    first_name VARCHAR(50) NOT NULL,
    last_name VARCHAR(50) NOT NULL,
    email VARCHAR(100) UNIQUE,
    phone VARCHAR(20),
    department VARCHAR(100),
    designation VARCHAR(100),
    is_active BOOLEAN DEFAULT 1,
    face_enrolled BOOLEAN DEFAULT 0,
    fingerprint_enrolled BOOLEAN DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_by INTEGER,
    FOREIGN KEY (created_by) REFERENCES admin(id)
);

-- Face encodings table - stores encoded face data (NOT raw images)
CREATE TABLE IF NOT EXISTS face_encodings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    encoding_data BLOB NOT NULL,  -- Numpy array serialized as bytes
    encoding_hash VARCHAR(64) NOT NULL,  -- SHA256 hash for integrity
    num_samples INTEGER DEFAULT 1,
    quality_score FLOAT DEFAULT 0.0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Fingerprint data table
CREATE TABLE IF NOT EXISTS fingerprint_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    fingerprint_id INTEGER NOT NULL,  -- ID stored in fingerprint sensor
    template_hash VARCHAR(64) NOT NULL,  -- Hash for verification
    finger_position VARCHAR(20) DEFAULT 'right_index',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Access logs table - records all entry/exit attempts
CREATE TABLE IF NOT EXISTS access_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,  -- NULL if unknown person
    employee_id VARCHAR(50),  -- Stored separately for historical reference
    user_name VARCHAR(100),  -- Stored separately for historical reference
    event_type VARCHAR(10) NOT NULL CHECK(event_type IN ('ENTRY', 'EXIT')),
    access_date DATE NOT NULL,
    access_time TIME NOT NULL,
    result VARCHAR(20) NOT NULL CHECK(result IN ('SUCCESS', 'FAILED', 'DENIED')),
    face_match BOOLEAN DEFAULT 0,
    fingerprint_match BOOLEAN DEFAULT 0,
    failure_reason VARCHAR(255),
    confidence_score FLOAT,
    ip_address VARCHAR(45),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);

-- System logs table - for system events and errors
CREATE TABLE IF NOT EXISTS system_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_level VARCHAR(20) NOT NULL,
    module VARCHAR(50) NOT NULL,
    message TEXT NOT NULL,
    details TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Session table for admin web sessions
CREATE TABLE IF NOT EXISTS admin_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    session_token VARCHAR(255) UNIQUE NOT NULL,
    ip_address VARCHAR(45),
    user_agent TEXT,
    expires_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (admin_id) REFERENCES admin(id) ON DELETE CASCADE
);

-- Create indexes for better query performance
CREATE INDEX IF NOT EXISTS idx_users_employee_id ON users(employee_id);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active);
CREATE INDEX IF NOT EXISTS idx_access_logs_user ON access_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_access_logs_date ON access_logs(access_date);
CREATE INDEX IF NOT EXISTS idx_access_logs_result ON access_logs(result);
CREATE INDEX IF NOT EXISTS idx_system_logs_level ON system_logs(log_level);
CREATE INDEX IF NOT EXISTS idx_system_logs_module ON system_logs(module);

-- Insert default admin user (password: admin12)
-- Password hash generated using bcrypt for password "admin12"
INSERT OR IGNORE INTO admin (username, password_hash, email, full_name)
VALUES (
    'admin',
    '$2b$12$KIXyv.6qLQqLQqLQqLQqLQqLQqLQqLQqLQqLQqLQqLQqLQqLQqLQq',
    'admin@smartdoor.local',
    'System Administrator'
);
