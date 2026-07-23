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



/* ==========================================================
   VERZIO TABLE SEARCH + FILTER ENGINE
========================================================== */

document.addEventListener("DOMContentLoaded", () => {

    document.querySelectorAll("[data-table-search]").forEach(searchInput => {

        const table = document.querySelector(searchInput.dataset.tableSearch);
        if (!table) return;

        const rows = table.querySelectorAll("tbody tr");

        let currentFilter = "all";

        function applyFilters() {

            const search = searchInput.value.toLowerCase();

            rows.forEach(row => {

                const matchesSearch =
                    row.innerText.toLowerCase().includes(search);

                const matchesStatus =
                    currentFilter === "all" ||
                    row.dataset.status === currentFilter;

                row.style.display =
                    matchesSearch && matchesStatus ? "" : "none";
            });
        }

        searchInput.addEventListener("input", applyFilters);

        const filterGroup = document.querySelector(
            `[data-filter-group="${searchInput.dataset.tableSearch}"]`
        );

        if (!filterGroup) return;

        filterGroup
            .querySelectorAll(".filter-pill")
            .forEach(btn => {

                btn.addEventListener("click", () => {

                    filterGroup
                        .querySelectorAll(".filter-pill")
                        .forEach(b => b.classList.remove("active"));

                    btn.classList.add("active");

                    currentFilter = btn.dataset.filterValue;

                    applyFilters();

                });

            });

    });

});