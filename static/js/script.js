/**
 * VERZIO STUDIO — ADMIN SHELL LOGIC
 * Minimal interactions for sidebar management.
 */

document.addEventListener('DOMContentLoaded', () => {
    const sidebar = document.querySelector('.sidebar');
    const toggleBtns = document.querySelectorAll('[data-action="toggle-sidebar-mobile"]');
    
    // Toggle sidebar for mobile views
    toggleBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            if (sidebar) {
                sidebar.classList.toggle('open');
            }
        });
    });

    // Close sidebar when clicking outside on mobile
    document.addEventListener('click', (e) => {
        if (window.innerWidth <= 992) {
            if (sidebar && sidebar.classList.contains('open')) {
                if (!sidebar.contains(e.target) && !e.target.closest('[data-action="toggle-sidebar-mobile"]')) {
                    sidebar.classList.remove('open');
                }
            }
        }
    });

    // Optional: Search keyboard shortcut (⌘K or Ctrl+K)
    document.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
            e.preventDefault();
            const searchInput = document.querySelector('.topbar-search input');
            if (searchInput) searchInput.focus();
        }
    });
});