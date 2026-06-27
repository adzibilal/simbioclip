(function () {
    var layout = document.querySelector('.app-layout');
    var sidebar = document.getElementById('app-sidebar');
    var overlay = document.getElementById('app-sidebar-overlay');
    var mobileToggle = document.getElementById('sidebar-mobile-toggle');
    var collapseToggle = document.getElementById('sidebar-collapse-toggle');

    var collapsed = localStorage.getItem('sidebar-collapsed') === '1';
    if (layout && collapsed && layout.classList.contains('sidebar-collapsible')) {
        layout.classList.add('sidebar-collapsed');
    }

    if (collapseToggle && layout) {
        collapseToggle.addEventListener('click', function () {
            layout.classList.toggle('sidebar-collapsed');
            localStorage.setItem('sidebar-collapsed', layout.classList.contains('sidebar-collapsed') ? '1' : '0');
        });
    }

    function closeMobileSidebar() {
        if (sidebar) sidebar.classList.remove('mobile-open');
        if (overlay) overlay.classList.remove('open');
    }

    function openMobileSidebar() {
        if (sidebar) sidebar.classList.add('mobile-open');
        if (overlay) overlay.classList.add('open');
    }

    if (mobileToggle) {
        mobileToggle.addEventListener('click', function () {
            if (sidebar && sidebar.classList.contains('mobile-open')) closeMobileSidebar();
            else openMobileSidebar();
        });
    }

    if (overlay) overlay.addEventListener('click', closeMobileSidebar);

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') closeMobileSidebar();
    });
})();
