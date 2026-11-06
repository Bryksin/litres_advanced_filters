/* Sidebar UI: "Жанры | Фильтры" tabs, genre search, mobile drawer,
 * active-filter badge, and state persistence across auto-submit reloads.
 *
 * Loaded at the end of <body> (DOM is ready) before the inline catalog script,
 * which calls window.SidebarUI.persist() / markGenreNavigation() / isSearching().
 * Form fields and URL building are owned by the inline script; this file never
 * touches field names or values.
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'laf.sidebar';
  var REOPEN_WINDOW_MS = 30000; // re-open the drawer only right after an auto-submit
  var DRAWER_MQ = window.matchMedia('(max-width: 1023px)');

  var sidebar = document.getElementById('sidebar');
  if (!sidebar) return;

  var scrim = document.getElementById('drawer-scrim');
  var mainContent = document.getElementById('main-content');
  var closeBtn = sidebar.querySelector('.sidebar__drawer-close');
  var tabs = {
    genres: document.getElementById('tab-genres'),
    filters: document.getElementById('tab-filters'),
  };
  var panels = {
    genres: document.getElementById('panel-genres'),
    filters: document.getElementById('filter-section'),
  };
  var tree = sidebar.querySelector('.genre-tree');
  var searchInput = document.getElementById('genre-search');
  var searchEmpty = document.getElementById('genre-search-empty');
  var lastOpener = null;
  var genreNavigation = false;

  // ── Storage (per-viewer convenience only; every access may throw) ──
  function readState() {
    try { return JSON.parse(sessionStorage.getItem(STORAGE_KEY) || '{}') || {}; }
    catch (e) { return {}; }
  }
  function writeState(state) {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }
    catch (e) { /* ignore */ }
  }

  // ── Tabs ──
  function currentTab() {
    return sidebar.getAttribute('data-tab') === 'filters' ? 'filters' : 'genres';
  }

  function setTab(name, focusTab) {
    if (!tabs[name]) return;
    sidebar.setAttribute('data-tab', name);
    Object.keys(tabs).forEach(function (key) {
      var on = key === name;
      if (tabs[key]) {
        tabs[key].setAttribute('aria-selected', on ? 'true' : 'false');
        tabs[key].tabIndex = on ? 0 : -1;
      }
    });
    if (focusTab && tabs[name]) tabs[name].focus();
    var st = readState();
    st.tab = name;
    writeState(st);
  }

  Object.keys(tabs).forEach(function (key) {
    var tab = tabs[key];
    if (!tab) return;
    tab.addEventListener('click', function () { setTab(key); });
    tab.addEventListener('keydown', function (e) {
      // WAI-ARIA tabs pattern: arrows / Home / End move between tabs
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].indexOf(e.key) === -1) return;
      e.preventDefault();
      var next = (e.key === 'ArrowLeft' || e.key === 'Home') ? 'genres' : 'filters';
      setTab(next, true);
    });
  });

  // Elements with data-sidebar-tab switch tabs (skip link, current-genre row)
  sidebar.addEventListener('click', function (e) {
    var el = e.target.closest('[data-sidebar-tab]');
    if (!el || !sidebar.contains(el)) return;
    e.preventDefault();
    var name = el.getAttribute('data-sidebar-tab');
    setTab(name);
    if (panels[name]) {
      panels[name].scrollTop = 0;
      if (name === 'genres' && searchInput && !DRAWER_MQ.matches) searchInput.focus();
      else panels[name].focus({ preventScroll: true });
    }
  });

  // ── Current genre name (from the server-rendered active tree item) ──
  function activeGenreEl() {
    return tree ? tree.querySelector('.is-active[data-genre-id]') : null;
  }
  (function fillGenreName() {
    var el = activeGenreEl();
    var name = el ? el.textContent.trim() : '';
    if (!name) return;
    document.querySelectorAll('[data-current-genre-name]').forEach(function (n) {
      n.textContent = name;
    });
  })();

  // ── Active filter count badge ──
  function isChecked(id) {
    var el = document.getElementById(id);
    return !!(el && el.checked && !el.disabled);
  }
  function hasValue(id) {
    var el = document.getElementById(id);
    return !!(el && el.value !== '');
  }
  function countActiveFilters() {
    var n = 0;
    ['f-only-series', 'f-only-standalones', 'f-exclude-narrators', 'f-exclude-authors',
     'f-hide-listened', 'f-incomplete-series'].forEach(function (id) { if (isChecked(id)) n++; });
    ['f-rating-min', 'f-rating-max', 'f-rating-count-min'].forEach(function (id) { if (hasValue(id)) n++; });
    return n;
  }
  function updateBadges() {
    var n = countActiveFilters();
    document.querySelectorAll('[data-active-filter-count]').forEach(function (b) {
      b.textContent = n ? String(n) : '';
      b.hidden = !n;
    });
    var menuBtn = document.querySelector('.mobile-bar__menu');
    if (menuBtn) {
      var label = n ? 'Открыть фильтры (активно: ' + n + ')' : 'Открыть фильтры';
      if (menuBtn.querySelector('.auth-alert-dot')) label += '. Сессия ЛитРес истекла';
      menuBtn.setAttribute('aria-label', label);
    }
  }
  var form = document.getElementById('filter-form');
  if (form) {
    form.addEventListener('change', updateBadges);
    form.addEventListener('input', updateBadges);
  }
  updateBadges();

  // ── Genre search (client-side filter of the tree) ──
  function norm(s) {
    return (s || '').toLowerCase().replace(/ё/g, 'е').trim();
  }
  function itemLabel(li) {
    var own = li.querySelector(':scope > .genre-tree__link, :scope > .genre-tree__details > .genre-tree__summary');
    return own ? own.textContent : '';
  }
  // Shows/hides <li> for query q; returns true if it or a descendant matches.
  // Children of a matching parent stay visible (collapsed unless they match too);
  // branches are expanded only towards genuine descendant matches.
  function filterItem(li, q, parentMatched) {
    var selfMatch = !!q && norm(itemLabel(li)).indexOf(q) !== -1;
    var details = li.querySelector(':scope > .genre-tree__details');
    var childMatch = false;
    if (details) {
      var kids = details.querySelectorAll(':scope > .genre-tree__list > .genre-tree__item');
      for (var i = 0; i < kids.length; i++) {
        if (filterItem(kids[i], q, parentMatched || selfMatch)) childMatch = true;
      }
      if (q) details.open = childMatch;
    }
    li.hidden = !(!q || parentMatched || selfMatch || childMatch);
    li.classList.toggle('is-match', selfMatch);
    return selfMatch || childMatch;
  }

  var searching = false;
  var savedOpen = null;

  function runSearch() {
    if (!tree || !searchInput) return;
    var q = norm(searchInput.value);
    if (q && !searching) {
      // Snapshot accordion state to restore it when the search is cleared
      savedOpen = [];
      tree.querySelectorAll('details.genre-tree__details').forEach(function (d) {
        savedOpen.push([d, d.open]);
      });
    }
    searching = !!q;
    var roots = tree.querySelectorAll(':scope > .genre-tree__list > .genre-tree__item');
    var any = false;
    for (var i = 0; i < roots.length; i++) {
      if (filterItem(roots[i], q)) any = true;
    }
    if (!q && savedOpen) {
      savedOpen.forEach(function (pair) { pair[0].open = pair[1]; });
      savedOpen = null;
    }
    tree.classList.toggle('is-filtered', searching);
    if (searchEmpty) searchEmpty.hidden = !q || any;
  }

  if (searchInput) {
    searchInput.addEventListener('input', runSearch);
    searchInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        // Never submit the filter form from the search box
        e.preventDefault();
        var first = tree && tree.querySelector('.genre-tree__item:not([hidden]) .genre-tree__link, .genre-tree__item:not([hidden]) .genre-tree__summary');
        if (first) first.focus();
      } else if (e.key === 'Escape' && searchInput.value) {
        e.preventDefault();
        e.stopPropagation(); // keep the drawer open
        searchInput.value = '';
        runSearch();
      }
    });
  }

  // ── Mobile drawer ──
  function isDrawerOpen() {
    return sidebar.classList.contains('is-open');
  }

  function openDrawer(tabName, opener, instant) {
    if (tabName) setTab(tabName);
    if (instant) sidebar.classList.add('no-anim');
    sidebar.classList.add('is-open');
    if (scrim) scrim.classList.add('is-visible');
    document.body.classList.add('drawer-open');
    document.querySelectorAll('[aria-controls="sidebar"]').forEach(function (b) {
      b.setAttribute('aria-expanded', 'true');
    });
    if (mainContent) mainContent.setAttribute('inert', '');
    lastOpener = opener || document.querySelector('.mobile-bar__menu');
    if (instant) {
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { sidebar.classList.remove('no-anim'); });
      });
    }
    if (closeBtn && !instant) closeBtn.focus();
  }

  function closeDrawer() {
    sidebar.classList.remove('is-open');
    if (scrim) scrim.classList.remove('is-visible');
    document.body.classList.remove('drawer-open');
    document.querySelectorAll('[aria-controls="sidebar"]').forEach(function (b) {
      b.setAttribute('aria-expanded', 'false');
    });
    if (mainContent) mainContent.removeAttribute('inert');
    var st = readState();
    st.open = false;
    writeState(st);
    if (lastOpener) lastOpener.focus();
  }

  document.querySelectorAll('[data-open-tab]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      openDrawer(btn.getAttribute('data-open-tab'), btn);
    });
  });
  if (closeBtn) closeBtn.addEventListener('click', closeDrawer);
  if (scrim) scrim.addEventListener('click', closeDrawer);
  var showResults = document.getElementById('sidebar-show-results');
  if (showResults) {
    showResults.addEventListener('click', function () {
      closeDrawer();
      window.scrollTo(0, 0);
    });
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && isDrawerOpen()) {
      var modal = document.getElementById('auth-modal');
      if (modal && modal.hasAttribute('open')) return; // auth modal closes first
      closeDrawer();
    }
  });
  // Leaving drawer mode (rotate / resize to desktop) must not leave the page inert
  DRAWER_MQ.addEventListener('change', function (e) {
    if (!e.matches && isDrawerOpen()) closeDrawer();
  });

  // ── Persistence across auto-submit reloads ──
  function persist() {
    var st = readState();
    st.tab = currentTab();
    st.scroll = {
      genres: panels.genres ? panels.genres.scrollTop : 0,
      filters: panels.filters ? panels.filters.scrollTop : 0,
    };
    // Re-open the drawer after a filter change, but not after picking a genre
    st.open = DRAWER_MQ.matches && isDrawerOpen() && !genreNavigation;
    st.ts = Date.now();
    writeState(st);
  }

  (function restore() {
    var st = readState();
    setTab(st.tab === 'filters' ? 'filters' : 'genres');
    var fresh = st.ts && (Date.now() - st.ts) < REOPEN_WINDOW_MS;
    if (fresh && st.scroll) {
      if (panels.genres) panels.genres.scrollTop = st.scroll.genres || 0;
      if (panels.filters) panels.filters.scrollTop = st.scroll.filters || 0;
    }
    // Make sure the selected genre is visible in the tree
    var active = activeGenreEl();
    if (active && panels.genres && !(fresh && st.scroll)) {
      var pr = panels.genres.getBoundingClientRect();
      var ar = active.getBoundingClientRect();
      if (pr.height && (ar.top < pr.top || ar.bottom > pr.bottom)) {
        panels.genres.scrollTop += ar.top - pr.top - pr.height / 3;
      }
    }
    if (fresh && st.open && DRAWER_MQ.matches) openDrawer(null, null, true);
    st.open = false;
    writeState(st);
  })();

  window.SidebarUI = {
    persist: persist,
    isSearching: function () { return searching; },
    markGenreNavigation: function () { genreNavigation = true; },
    setTab: setTab,
    open: openDrawer,
    close: closeDrawer,
  };
})();
