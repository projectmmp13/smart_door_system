/**
 * Smart Door Security System - Admin Dashboard JavaScript
 */

document.addEventListener('DOMContentLoaded', function() {
    // Auto-hide flash messages after 5 seconds
    const flashMessages = document.querySelectorAll('.flash-message');
    flashMessages.forEach(function(message) {
        setTimeout(function() {
            message.style.opacity = '0';
            message.style.transform = 'translateY(-10px)';
            setTimeout(function() {
                message.remove();
            }, 300);
        }, 5000);
    });

    // Confirm delete actions
    const deleteButtons = document.querySelectorAll('[data-confirm]');
    deleteButtons.forEach(function(button) {
        button.addEventListener('click', function(e) {
            const message = this.getAttribute('data-confirm') || 'Are you sure?';
            if (!confirm(message)) {
                e.preventDefault();
            }
        });
    });

    // Auto-refresh dashboard stats every 30 seconds
    if (document.querySelector('.stats-grid')) {
        setInterval(function() {
            // Only refresh if user is on the page
            if (!document.hidden) {
                refreshStats();
            }
        }, 30000);
    }

    // Add loading state to forms
    const forms = document.querySelectorAll('form');
    forms.forEach(function(form) {
        form.addEventListener('submit', function() {
            const submitBtn = form.querySelector('button[type="submit"]');
            if (submitBtn) {
                submitBtn.disabled = true;
                submitBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Processing...';
            }
        });
    });
});

/**
 * Refresh dashboard statistics via API
 */
function refreshStats() {
    fetch('/api/logs/stats?days=7')
        .then(response => response.json())
        .then(data => {
            if (data.stats) {
                // Update stat cards if they exist
                updateStatCard('successful', data.stats.successful || 0);
                updateStatCard('denied', data.stats.denied || 0);
            }
        })
        .catch(err => console.error('Failed to refresh stats:', err));
}

/**
 * Update a stat card value
 */
function updateStatCard(type, value) {
    const cards = document.querySelectorAll('.stat-card');
    cards.forEach(card => {
        const label = card.querySelector('.stat-info p');
        if (label && label.textContent.toLowerCase().includes(type)) {
            const valueEl = card.querySelector('.stat-info h3');
            if (valueEl) {
                valueEl.textContent = value;
            }
        }
    });
}

/**
 * Toggle user status via API
 */
function toggleUser(userId) {
    fetch(`/api/users/${userId}/toggle`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.message) {
            location.reload();
        }
    })
    .catch(err => {
        console.error('Failed to toggle user:', err);
        alert('Failed to update user status');
    });
}

/**
 * Format date for display
 */
function formatDate(dateStr) {
    const date = new Date(dateStr);
    return date.toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric'
    });
}

/**
 * Format time for display
 */
function formatTime(timeStr) {
    if (!timeStr) return '';
    const parts = timeStr.split(':');
    const hours = parseInt(parts[0]);
    const minutes = parts[1];
    const ampm = hours >= 12 ? 'PM' : 'AM';
    const displayHours = hours % 12 || 12;
    return `${displayHours}:${minutes} ${ampm}`;
}
