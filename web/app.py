"""
Smart Door Security System - Web Application
Flask-based admin dashboard and REST API.
"""

import os
import sys
import secrets
import hashlib
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
import logging
import threading
from collections import defaultdict
import re

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, session, g
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import bcrypt

from config.settings import WEB_HOST, WEB_PORT, SECRET_KEY, RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW
from database.db_manager import (
    AdminRepository, UserRepository, FaceEncodingRepository,
    FingerprintRepository, AccessLogRepository, SystemLogRepository
)

# Create Flask app
app = Flask(__name__, 
            template_folder='templates',
            static_folder='static')
app.secret_key = SECRET_KEY
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

# Initialize repositories
admin_repo = AdminRepository()
user_repo = UserRepository()
face_repo = FaceEncodingRepository()
fingerprint_repo = FingerprintRepository()
access_log_repo = AccessLogRepository()
system_log = SystemLogRepository()


# =====================
# Authentication Helpers
# =====================

def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verify_password(password: str, hashed: str) -> bool:
    """Verify a password against its hash."""
    try:
        return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False


def login_required(f):
    """Decorator to require login for a route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        
        # Verify session is still valid
        admin = admin_repo.get_by_id(session['admin_id'])
        if not admin or not admin.get('is_active', False):
            session.clear()
            flash('Your session has expired.', 'warning')
            return redirect(url_for('login'))
        
        g.admin = admin
        return f(*args, **kwargs)
    return decorated_function


def api_login_required(f):
    """Decorator to require login for API endpoints."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check for session auth
        if 'admin_id' in session:
            admin = admin_repo.get_by_id(session['admin_id'])
            if admin and admin.get('is_active', False):
                g.admin = admin
                return f(*args, **kwargs)
        
        # Check for token auth (for API clients)
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if token:
            sess = admin_repo.get_session(token)
            if sess:
                g.admin = {'id': sess['admin_id'], 'username': sess['username']}
                return f(*args, **kwargs)
        
        return jsonify({'error': 'Unauthorized'}), 401
    return decorated_function


# =====================
# Web Routes
# =====================

@app.route('/')
def index():
    """Redirect to login or dashboard."""
    if 'admin_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    """Admin login page."""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        admin = admin_repo.get_by_username(username)
        
        if not admin:
            flash('Invalid username or password.', 'error')
            return render_template('login.html')
        
        
        # Verify password
        if not verify_password(password, admin['password_hash']):
            flash('Invalid username or password.', 'error')
            return render_template('login.html')
        
        # Successful login
        admin_repo.update_last_login(admin['id'])
        
        session.permanent = True
        session['admin_id'] = admin['id']
        session['admin_username'] = admin['username']
        session['admin_name'] = admin['full_name']
        
        system_log.info('WebAuth', f"Admin login: {admin['username']}")
        
        flash(f'Welcome, {admin["full_name"]}!', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('login.html')


@app.route('/logout')
def logout():
    """Logout the admin."""
    if 'admin_username' in session:
        system_log.info('WebAuth', f"Admin logout: {session['admin_username']}")
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    """Main admin dashboard."""
    # Get statistics
    stats = access_log_repo.get_stats(days=7)
    recent_logs = access_log_repo.get_recent_logs(limit=10)
    total_users = len(user_repo.get_all())
    active_users = len(user_repo.get_all(active_only=True))
    
    return render_template('dashboard.html',
                          stats=stats,
                          recent_logs=recent_logs,
                          total_users=total_users,
                          active_users=active_users)


@app.route('/users')
@login_required
def users():
    """User management page."""
    all_users = user_repo.get_all()
    return render_template('users.html', users=all_users)


@app.route('/users/add', methods=['GET', 'POST'])
@login_required
def add_user():
    """Add new user page."""
    if request.method == 'POST':
        try:
            employee_id = request.form.get('employee_id', '').strip()
            first_name = request.form.get('first_name', '').strip()
            last_name = request.form.get('last_name', '').strip()
            email = request.form.get('email', '').strip() or None
            phone = request.form.get('phone', '').strip() or None
            department = request.form.get('department', '').strip() or None
            designation = request.form.get('designation', '').strip() or None
            
            if not employee_id or not first_name or not last_name:
                flash('Employee ID, First Name, and Last Name are required.', 'error')
                return render_template('user_form.html', user=None, action='add')
            
            # Check if employee_id exists
            if user_repo.get_by_employee_id(employee_id):
                flash('An employee with this ID already exists.', 'error')
                return render_template('user_form.html', user=None, action='add')
            
            user_id = user_repo.create(
                employee_id=employee_id,
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=phone,
                department=department,
                designation=designation,
                created_by=session['admin_id']
            )
            
            system_log.info('UserManagement', 
                          f"User created: {first_name} {last_name} ({employee_id})")
            
            flash(f'User {first_name} {last_name} created successfully.', 'success')
            return redirect(url_for('users'))
            
        except Exception as e:
            flash(f'Error creating user: {str(e)}', 'error')
    
    return render_template('user_form.html', user=None, action='add')


@app.route('/users/edit/<int:user_id>', methods=['GET', 'POST'])
@login_required
def edit_user(user_id):
    """Edit user page."""
    user = user_repo.get_by_id(user_id)
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('users'))
    
    if request.method == 'POST':
        try:
            user_repo.update(
                user_id=user_id,
                first_name=request.form.get('first_name', '').strip(),
                last_name=request.form.get('last_name', '').strip(),
                email=request.form.get('email', '').strip() or None,
                phone=request.form.get('phone', '').strip() or None,
                department=request.form.get('department', '').strip() or None,
                designation=request.form.get('designation', '').strip() or None
            )
            
            system_log.info('UserManagement', f"User updated: {user['employee_id']}")
            
            flash('User updated successfully.', 'success')
            return redirect(url_for('users'))
            
        except Exception as e:
            flash(f'Error updating user: {str(e)}', 'error')
    
    return render_template('user_form.html', user=user, action='edit')


@app.route('/users/delete/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    """Delete a user."""
    user = user_repo.get_by_id(user_id)
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('users'))
    
    try:
        user_repo.delete(user_id)
        system_log.info('UserManagement', 
                       f"User deleted: {user['first_name']} {user['last_name']}")
        flash('User deleted successfully.', 'success')
    except Exception as e:
        flash(f'Error deleting user: {str(e)}', 'error')
    
    return redirect(url_for('users'))


@app.route('/users/toggle/<int:user_id>', methods=['POST'])
@login_required
def toggle_user(user_id):
    """Enable/disable a user."""
    user = user_repo.get_by_id(user_id)
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('users'))
    
    new_status = not user['is_active']
    user_repo.set_active(user_id, new_status)
    
    status_text = 'enabled' if new_status else 'disabled'
    system_log.info('UserManagement', 
                   f"User {status_text}: {user['first_name']} {user['last_name']}")
    
    flash(f'User {status_text} successfully.', 'success')
    return redirect(url_for('users'))


@app.route('/users/enable/<int:user_id>', methods=['POST'])
@login_required
def enable_user(user_id):
    """Enable a disabled user."""
    user = user_repo.get_by_id(user_id)
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('users'))
    
    if user['is_active']:
        flash('User is already enabled.', 'info')
        return redirect(url_for('users'))
    
    user_repo.set_active(user_id, True)
    
    system_log.info('UserManagement', 
                   f"User enabled: {user['first_name']} {user['last_name']}")
    
    flash('User enabled successfully.', 'success')
    return redirect(url_for('users'))


@app.route('/users/disable/<int:user_id>', methods=['POST'])
@login_required
def disable_user(user_id):
    """Disable an active user."""
    user = user_repo.get_by_id(user_id)
    if not user:
        flash('User not found.', 'error')
        return redirect(url_for('users'))
    
    if not user['is_active']:
        flash('User is already disabled.', 'info')
        return redirect(url_for('users'))
    
    user_repo.set_active(user_id, False)
    
    system_log.info('UserManagement', 
                   f"User disabled: {user['first_name']} {user['last_name']}")
    
    flash('User disabled successfully.', 'success')
    return redirect(url_for('users'))


@app.route('/logs')
@login_required
def logs():
    """Access logs page."""
    # Get filter parameters
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    result_filter = request.args.get('result')
    user_search = request.args.get('user_search', '').strip()
    page = int(request.args.get('page', 1))
    per_page = 50

    # Parse dates
    start = None
    end = None
    if start_date:
        try:
            start = datetime.strptime(start_date, '%Y-%m-%d').date()
        except:
            pass
    if end_date:
        try:
            end = datetime.strptime(end_date, '%Y-%m-%d').date()
        except:
            pass

    # Handle user search - find matching user IDs
    user_ids = None
    if user_search:
        users = user_repo.get_all(active_only=True)
        matching_users = []
        search_lower = user_search.lower()

        for user in users:
            full_name = f"{user['first_name']} {user['last_name']}".lower()
            employee_id = user['employee_id'].lower()

            if search_lower in full_name or search_lower in employee_id:
                matching_users.append(user['id'])

        if matching_users:
            user_ids = matching_users
        else:
            # No matches found, return empty logs
            user_ids = []

    # Get logs with user filtering
    all_logs = access_log_repo.get_logs(
        start_date=start,
        end_date=end,
        result=result_filter if result_filter else None,
        user_id=user_ids[0] if user_ids and len(user_ids) == 1 else None,
        limit=per_page,
        offset=(page - 1) * per_page
    )

    # If we have multiple user IDs, we need to filter the results
    if user_ids and len(user_ids) > 1:
        filtered_logs = [log for log in all_logs if log.get('user_id') in user_ids]
        all_logs = filtered_logs

    return render_template('logs.html',
                          logs=all_logs,
                          page=page,
                          start_date=start_date,
                          end_date=end_date,
                          result_filter=result_filter,
                          user_search=user_search)


@app.route('/analytics')
@login_required
def analytics():
    """Analytics dashboard page."""
    # Get analytics data
    daily_stats = access_log_repo.get_daily_stats(days=30)
    hourly_stats = access_log_repo.get_hourly_stats(days=7)
    user_activity = access_log_repo.get_user_activity(days=30)
    peak_hours = access_log_repo.get_peak_hours(days=30)

    return render_template('analytics.html',
                          daily_stats=daily_stats,
                          hourly_stats=hourly_stats,
                          user_activity=user_activity,
                          peak_hours=peak_hours)


# =====================
# REST API Endpoints
# =====================

@app.route('/api/users', methods=['GET'])
@api_login_required
def api_get_users():
    """Get all users."""
    users = user_repo.get_all()
    return jsonify({'users': users})


@app.route('/api/users/<int:user_id>', methods=['GET'])
@api_login_required
def api_get_user(user_id):
    """Get a specific user."""
    user = user_repo.get_by_id(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    return jsonify({'user': user})


@app.route('/api/users', methods=['POST'])
@api_login_required
def api_create_user():
    """Create a new user."""
    data = request.get_json()
    
    required = ['employee_id', 'first_name', 'last_name']
    if not all(k in data for k in required):
        return jsonify({'error': 'Missing required fields'}), 400
    
    try:
        user_id = user_repo.create(
            employee_id=data['employee_id'],
            first_name=data['first_name'],
            last_name=data['last_name'],
            email=data.get('email'),
            phone=data.get('phone'),
            department=data.get('department'),
            designation=data.get('designation'),
            created_by=g.admin['id']
        )
        
        user = user_repo.get_by_id(user_id)
        return jsonify({'user': user, 'message': 'User created'}), 201
        
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/users/<int:user_id>', methods=['PUT'])
@api_login_required
def api_update_user(user_id):
    """Update a user."""
    user = user_repo.get_by_id(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    data = request.get_json()
    
    try:
        user_repo.update(user_id, **data)
        user = user_repo.get_by_id(user_id)
        return jsonify({'user': user, 'message': 'User updated'})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@api_login_required
def api_delete_user(user_id):
    """Delete a user."""
    user = user_repo.get_by_id(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    try:
        user_repo.delete(user_id)
        return jsonify({'message': 'User deleted'})
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/users/<int:user_id>/toggle', methods=['POST'])
@api_login_required
def api_toggle_user(user_id):
    """Toggle user active status."""
    user = user_repo.get_by_id(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    new_status = not user['is_active']
    user_repo.set_active(user_id, new_status)
    
    return jsonify({
        'user_id': user_id,
        'is_active': new_status,
        'message': f"User {'enabled' if new_status else 'disabled'}"
    })


@app.route('/api/logs', methods=['GET'])
@api_login_required
def api_get_logs():
    """Get access logs."""
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    result = request.args.get('result')
    
    logs = access_log_repo.get_logs(result=result, limit=limit, offset=offset)
    return jsonify({'logs': logs})


@app.route('/api/logs/stats', methods=['GET'])
@api_login_required
def api_get_stats():
    """Get access statistics."""
    days = request.args.get('days', 7, type=int)
    stats = access_log_repo.get_stats(days=days)
    return jsonify({'stats': stats})


@app.route('/api/validate', methods=['POST'])
def api_validate_user():
    """
    Validate user for authentication (called by main.py).
    This is a special endpoint that doesn't require admin login.
    """
    data = request.get_json()
    user_id = data.get('user_id')
    
    if not user_id:
        return jsonify({'valid': False, 'error': 'No user_id provided'})
    
    user = user_repo.get_by_id(user_id)
    
    if not user:
        return jsonify({'valid': False, 'error': 'User not found'})
    
    if not user.get('is_active', False):
        return jsonify({'valid': False, 'error': 'User is disabled'})
    
    if not user.get('face_enrolled', False):
        return jsonify({'valid': False, 'error': 'Face not enrolled'})
    
    if not user.get('fingerprint_enrolled', False):
        return jsonify({'valid': False, 'error': 'Fingerprint not enrolled'})
    
    return jsonify({
        'valid': True,
        'user': {
            'id': user['id'],
            'employee_id': user['employee_id'],
            'name': f"{user['first_name']} {user['last_name']}"
        }
    })


@app.route('/api/log_access', methods=['POST'])
def api_log_access():
    """
    Log an access attempt (called by main.py).
    """
    data = request.get_json()
    
    try:
        log_id = access_log_repo.log_access(
            user_id=data.get('user_id'),
            event_type=data.get('event_type', 'ENTRY'),
            result=data.get('result', 'FAILED'),
            face_match=data.get('face_match', False),
            fingerprint_match=data.get('fingerprint_match', False),
            failure_reason=data.get('failure_reason'),
            confidence_score=data.get('confidence_score')
        )
        
        return jsonify({'success': True, 'log_id': log_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/users/<int:user_id>/enrollment', methods=['POST'])
@api_login_required
def api_update_enrollment_status(user_id):
    """
    Update biometric enrollment status for a user.
    Called by enrollment scripts after successful enrollment.
    """
    data = request.get_json()
    
    # Validate input
    biometric_type = data.get('biometric_type')
    enrolled = data.get('enrolled', False)
    
    if not biometric_type:
        return jsonify({'error': 'biometric_type is required'}), 400
    
    if biometric_type not in ['face', 'fingerprint']:
        return jsonify({'error': 'Invalid biometric_type. Must be "face" or "fingerprint"'}), 400
    
    if not isinstance(enrolled, bool):
        return jsonify({'error': 'enrolled must be a boolean'}), 400
    
    # Check if user exists
    user = user_repo.get_by_id(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    try:
        # Update enrollment status
        if biometric_type == 'face':
            user_repo.update(user_id, face_enrolled=enrolled)
        elif biometric_type == 'fingerprint':
            user_repo.update(user_id, fingerprint_enrolled=enrolled)
        
        # Log the enrollment status change
        system_log.info(
            'EnrollmentStatus',
            f"Updated {biometric_type} enrollment for user {user['employee_id']}: {enrolled}"
        )
        
        return jsonify({
            'success': True,
            'user_id': user_id,
            'biometric_type': biometric_type,
            'enrolled': enrolled,
            'message': f'{biometric_type.capitalize()} enrollment status updated'
        })
        
    except Exception as e:
        system_log.error('EnrollmentStatus', f"Failed to update enrollment status: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


# =====================
# Error Handlers
# =====================

@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors."""
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Not found'}), 404
    return render_template('error.html', error='Page not found'), 404


@app.errorhandler(500)
def server_error(e):
    """Handle 500 errors."""
    system_log.error('WebApp', f"Server error: {str(e)}")
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Internal server error'}), 500
    return render_template('error.html', error='Internal server error'), 500


# =====================
# Run Application
# =====================

def run_server(host=None, port=None, debug=False):
    """Run the Flask server."""
    host = host or WEB_HOST
    port = port or WEB_PORT
    
    system_log.info('WebApp', f"Starting web server on {host}:{port}")
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Smart Door Admin Dashboard')
    parser.add_argument('--host', default=WEB_HOST, help='Host to bind to')
    parser.add_argument('--port', type=int, default=WEB_PORT, help='Port to bind to')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    
    args = parser.parse_args()
    run_server(host=args.host, port=args.port, debug=args.debug)
