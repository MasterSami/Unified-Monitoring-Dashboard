/* Service / group combobox — the filter shared by Capacity, Agents and Alerts.
 *
 * A text box with a menu we draw ourselves. The native <datalist> was dropped
 * because Firefox shows it as an unstyled system popup, and because the list
 * has to follow the platform tab and instance the user has picked: pick the
 * Dynatrace tab and only Dynatrace host groups are offered.
 *
 * Scope comes from the toolbar the box sits in: `[name=platform]` (the hidden
 * mirror behind the tabs) and `[name=instance]` (the select). A page without
 * them — Alerts — gets every name.
 *
 * Choosing an item writes the value and fires `input`, which is what the
 * HTMX trigger on the box listens for, so a pick and a keystroke reach the
 * server the same way.
 */
(function () {
  "use strict";

  var MAX_ITEMS = 80;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function setup(root) {
    var input = root.querySelector("input");
    var menu = root.querySelector(".svc-menu");
    var dataEl = root.querySelector("[data-svc-catalog]");
    if (!input || !menu || !dataEl) return;

    var catalog;
    try { catalog = JSON.parse(dataEl.textContent || "{}"); } catch (e) { catalog = {}; }

    var toolbar = root.closest(".toolbar") || document;
    var platformEl = toolbar.querySelector("[name=platform]");
    var instanceEl = toolbar.querySelector("[name=instance]");
    var active = -1;
    var items = [];

    function scope() {
      var p = platformEl && platformEl.value ? platformEl.value : "all";
      var i = instanceEl && instanceEl.value ? instanceEl.value : "all";
      return { platform: p, instance: i };
    }

    /* Names in scope, each with the platforms it was seen on (so the "All"
       view can say which tool a group belongs to). */
    function names() {
      var sc = scope(), seen = {}, out = [];
      Object.keys(catalog).forEach(function (inst) {
        var entry = catalog[inst] || {};
        if (sc.instance !== "all" && inst !== sc.instance) return;
        if (sc.platform !== "all" && entry.platform !== sc.platform) return;
        (entry.names || []).forEach(function (n) {
          var k = n.toLowerCase();
          if (!seen[k]) { seen[k] = { name: n, platforms: {} }; out.push(seen[k]); }
          seen[k].platforms[entry.platform] = true;
        });
      });
      out.sort(function (a, b) { return a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1; });
      return out;
    }

    function label(p) {
      return { zabbix: "Zabbix", dynatrace: "Dynatrace", nnmi: "NNMi",
               sitescope: "SiteScope", digitalview: "DigitalView" }[p] || p;
    }

    function render() {
      var q = input.value.trim().toLowerCase();
      var sc = scope();
      var all = names();
      items = all.filter(function (it) { return !q || it.name.toLowerCase().indexOf(q) !== -1; });
      var shown = items.slice(0, MAX_ITEMS);
      var html = "";
      if (!shown.length) {
        var where = sc.instance !== "all" ? sc.instance : (sc.platform !== "all" ? label(sc.platform) : "any source");
        html = '<li class="svc-empty">' +
          (q ? "No service in " + esc(where) + " matches “" + esc(input.value.trim()) + "”"
             : "No service names known for " + esc(where)) + "</li>";
      } else {
        html = shown.map(function (it, idx) {
          var tags = sc.platform === "all"
            ? Object.keys(it.platforms).map(function (p) {
                return '<span class="svc-tag svc-tag-' + esc(p) + '">' + esc(label(p)) + "</span>";
              }).join("")
            : "";
          var name = esc(it.name);
          if (q) {
            var at = it.name.toLowerCase().indexOf(q);
            name = esc(it.name.slice(0, at)) + "<mark>" + esc(it.name.slice(at, at + q.length)) + "</mark>" + esc(it.name.slice(at + q.length));
          }
          return '<li role="option" data-idx="' + idx + '"' + (idx === active ? ' class="is-active"' : "") + ">" +
            '<span class="svc-name">' + name + "</span>" + tags + "</li>";
        }).join("");
        if (items.length > MAX_ITEMS) {
          html += '<li class="svc-more">' + (items.length - MAX_ITEMS) + " more — keep typing to narrow</li>";
        }
      }
      menu.innerHTML = html;
    }

    function open() {
      render();
      menu.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }
    function close() {
      menu.hidden = true;
      active = -1;
      input.setAttribute("aria-expanded", "false");
    }
    function choose(idx) {
      var it = items[idx];
      if (!it) return;
      input.value = it.name;
      close();
      // The box still has focus (mouse picks prevent the mousedown default,
      // keyboard picks never left it), so no focus() here — calling it would
      // fire the focus handler and pop the menu straight back open.
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    function move(delta) {
      var n = Math.min(items.length, MAX_ITEMS);
      if (!n) return;
      active = (active + delta + n) % n;
      render();
      var el = menu.querySelector(".is-active");
      if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
    }

    input.addEventListener("focus", open);
    input.addEventListener("click", function () { if (menu.hidden) open(); });
    input.addEventListener("input", function () { active = -1; open(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); if (menu.hidden) open(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") { if (!menu.hidden && active >= 0) { e.preventDefault(); choose(active); } else close(); }
      else if (e.key === "Escape") { close(); }
      else if (e.key === "Tab") { close(); }
    });
    menu.addEventListener("mousedown", function (e) {
      var li = e.target.closest("li[data-idx]");
      if (!li) return;
      e.preventDefault(); // keep focus on the input
      choose(Number(li.dataset.idx));
    });
    menu.addEventListener("mousemove", function (e) {
      var li = e.target.closest("li[data-idx]");
      if (!li) return;
      var idx = Number(li.dataset.idx);
      if (idx !== active) { active = idx; render(); }
    });
    document.addEventListener("mousedown", function (e) {
      if (!root.contains(e.target)) close();
    });

    /* Re-scope when the platform tab or instance changes. The tab buttons
       update the hidden mirror in their own onclick, so listen for clicks on
       them as well as for `change` on the select. */
    if (instanceEl) instanceEl.addEventListener("change", function () { if (!menu.hidden) render(); });
    toolbar.querySelectorAll(".tabs button[data-platform]").forEach(function (b) {
      b.addEventListener("click", function () { if (!menu.hidden) render(); });
    });
  }

  function init() {
    document.querySelectorAll("[data-svcbox]").forEach(function (root) {
      if (root.dataset.svcReady) return;
      root.dataset.svcReady = "1";
      setup(root);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
  // The toolbar is outside the HTMX-swapped region, but be safe for any page
  // that ever swaps it in.
  document.addEventListener("htmx:afterSettle", init);
})();
