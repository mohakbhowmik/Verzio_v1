/**
 * script.js — Verzio Studio dashboard shell behavior
 * Handles: sidebar collapse (desktop), sidebar drawer (mobile),
 * active nav highlighting, and small UI affordances.
 * No framework — vanilla JS only, matches the server-rendered Jinja shell.
 */

(function () {
  "use strict";

  const SIDEBAR_STATE_KEY = "verzio.sidebarCollapsed";
  const appShell = document.querySelector(".app-shell");
  const collapseBtn = document.querySelector("[data-action='toggle-sidebar']");
  const mobileToggleBtn = document.querySelector("[data-action='toggle-sidebar-mobile']");
  const scrim = document.querySelector(".sidebar-scrim");

  if (!appShell) return;

  /* ---------------------------------------------------------------------
   * Desktop collapse (persisted)
   * ------------------------------------------------------------------- */
  function applyStoredSidebarState() {
    try {
      const collapsed = window.localStorage.getItem(SIDEBAR_STATE_KEY) === "1";
      appShell.classList.toggle("sidebar-collapsed", collapsed);
    } catch (e) {
      /* localStorage unavailable — default to expanded */
    }
  }

  function toggleDesktopSidebar() {
    const collapsed = appShell.classList.toggle("sidebar-collapsed");
    try {
      window.localStorage.setItem(SIDEBAR_STATE_KEY, collapsed ? "1" : "0");
    } catch (e) {
      /* ignore persistence errors */
    }
  }

  if (collapseBtn) {
    collapseBtn.addEventListener("click", toggleDesktopSidebar);
  }

  applyStoredSidebarState();

  /* ---------------------------------------------------------------------
   * Mobile drawer
   * ------------------------------------------------------------------- */
  function openMobileSidebar() {
    appShell.classList.add("sidebar-mobile-open");
  }

  function closeMobileSidebar() {
    appShell.classList.remove("sidebar-mobile-open");
  }

  if (mobileToggleBtn) {
    mobileToggleBtn.addEventListener("click", openMobileSidebar);
  }

  if (scrim) {
    scrim.addEventListener("click", closeMobileSidebar);
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeMobileSidebar();
  });

  // Close mobile drawer when a nav link is tapped
  document.querySelectorAll(".sidebar .nav-item").forEach(function (link) {
    link.addEventListener("click", closeMobileSidebar);
  });

  /* ---------------------------------------------------------------------
   * Active nav highlighting based on current path
   * (Server also sets this via Jinja, this is a client-side fallback)
   * ------------------------------------------------------------------- */
  const currentPath = window.location.pathname.replace(/\/$/, "") || "/";
  document.querySelectorAll(".sidebar .nav-item[href]").forEach(function (link) {
    const href = link.getAttribute("href").replace(/\/$/, "") || "/";
    if (href === currentPath) {
      link.classList.add("active");
    }
  });

  /* ---------------------------------------------------------------------
   * Simple client-side table filter (search box on list pages)
   * Looks for [data-table-search] input + [data-table-target] table body
   * ------------------------------------------------------------------- */
  document.querySelectorAll("[data-table-search]").forEach(function (input) {
    const targetSelector = input.getAttribute("data-table-search");
    const rows = document.querySelectorAll(targetSelector + " tbody tr");

    input.addEventListener("input", function () {
      const term = input.value.trim().toLowerCase();
      rows.forEach(function (row) {
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(term) ? "" : "none";
      });
    });
  });

  /* ---------------------------------------------------------------------
   * Filter pills (status filters on appointments/businesses pages)
   * ------------------------------------------------------------------- */
  document.querySelectorAll("[data-filter-group]").forEach(function (group) {
    const pills = group.querySelectorAll(".filter-pill");
    const targetSelector = group.getAttribute("data-filter-group");
    const rows = document.querySelectorAll(targetSelector + " tbody tr");

    pills.forEach(function (pill) {
      pill.addEventListener("click", function () {
        pills.forEach((p) => p.classList.remove("active"));
        pill.classList.add("active");

        const status = pill.getAttribute("data-filter-value");
        rows.forEach(function (row) {
          if (status === "all" || row.getAttribute("data-status") === status) {
            row.style.display = "";
          } else {
            row.style.display = "none";
          }
        });
      });
    });
  });

  /* ---------------------------------------------------------------------
   * Toasts for settings "Save changes" affordance (dummy — no backend call)
   * ------------------------------------------------------------------- */
  document.querySelectorAll("[data-action='save-settings']").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const original = btn.textContent;
      btn.textContent = "Saved";
      btn.disabled = true;
      setTimeout(function () {
        btn.textContent = original;
        btn.disabled = false;
      }, 1400);
    });
  });
})();
