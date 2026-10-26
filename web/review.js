/* geoqc tester tools: manual verification of the discrepancy sample (reports/verification.csv).
 * Loaded by app.js only when /config/map says review_tools=true (config/api.yaml review.enabled
 * or `geoqc api serve --review`). Uses the small window.geoqc surface, never app.js internals. */
(function () {
  "use strict";

  var g = window.geoqc;
  if (!g) return;
  var assetQuery = (document.currentScript && document.currentScript.src.split("?")[1]) || "";

  var VERDICTS = [
    { key: "map_error", hotkey: "1", label: "ошибка карты", title: "Ошибка OSM: здание есть/нет, контур неверен" },
    { key: "model_error", hotkey: "2", label: "ошибка модели", title: "Ошибка детектора" },
    { key: "image_issue", hotkey: "3", label: "снимок", title: "Тень, крона, дата съёмки, смещение" },
    { key: "unsure", hotkey: "4", label: "не ясно", title: "По снимку не решить" }
  ];
  var LABELS = {};
  VERDICTS.forEach(function (v) { LABELS[v.key] = v.label; });

  var items = [];
  var byId = {};

  var css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "/review.css" + (assetQuery ? "?" + assetQuery : "");
  document.head.appendChild(css);

  // Own tab next to the product ones: verdict buttons on top, the sample list scrolls below.
  var panel = document.createElement("section");
  panel.className = "panel review list-tab";
  panel.innerHTML =
    '<h2><span class="tag">тестирование</span> <span id="review-progress" class="muted"></span>' +
    '<button id="review-next" class="btn ghost" title="Следующее без вердикта">Следующее</button></h2>' +
    '<div id="review-verdict" class="review-verdict">' +
    '<div class="muted small" id="review-hint">Выберите расхождение из списка.</div>' +
    '<div class="review-buttons">' + VERDICTS.map(function (v) {
      return '<button class="btn" disabled data-verdict="' + v.key + '" title="' + v.title + '">' +
        v.hotkey + " " + v.key + "</button>";
    }).join("") + "</div>" +
    '<input id="review-comment" type="text" maxlength="500" placeholder="Комментарий (необязательно)" disabled>' +
    '<button id="review-clear" class="btn ghost" disabled>Снять вердикт</button>' +
    "</div>" +
    '<ol id="review-list" class="list review-list"></ol>';

  var $ = function (sel) { return panel.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(panel.querySelectorAll(sel)); };

  function apply(data) {
    items = data.items;
    byId = {};
    items.forEach(function (it) { byId[it.id] = it; });
    if (!panel.id) g.addTab("review", "Верификация", panel);
    $("#review-progress").textContent = data.done + " / " + data.total;
    var selected = g.selectedId();
    $("#review-list").innerHTML = items.map(function (it) {
      var v = it.verdict
        ? '<span class="v">' + g.escapeHtml(LABELS[it.verdict] || it.verdict) + "</span>"
        : '<span class="v none">—</span>';
      return '<li data-id="' + it.id + '"' + (it.id === selected ? ' class="active"' : "") + ">" +
        '<span class="n">' + it.n + "</span>" +
        '<span class="bar" style="background:' + (g.CLASS_COLORS[it["class"]] || "#999") + '"></span>' +
        '<span class="title">' + g.escapeHtml(g.CLASS_LABELS[it["class"]] || it["class"]) +
        ' <span class="sub">#' + it.id + "</span></span>" + v + "</li>";
    }).join("");
    render(selected);
  }

  function render(id) {
    var it = byId[id];
    $("#review-hint").textContent = it
      ? "№ " + it.n + " · #" + it.id + ": клавиши 1–4, затем следующее"
      : (id != null ? "#" + id + " не входит в выборку" : "Выберите расхождение из списка.");
    $$("[data-verdict], #review-comment, #review-clear").forEach(function (el) { el.disabled = !it; });
    $$("[data-verdict]").forEach(function (b) {
      b.classList.toggle("selected", !!it && b.getAttribute("data-verdict") === it.verdict);
    });
    if (document.activeElement !== $("#review-comment")) $("#review-comment").value = (it && it.comment) || "";
    $$("#review-list li").forEach(function (li) {
      var on = Number(li.getAttribute("data-id")) === id;
      li.classList.toggle("active", on);
      if (on) g.scrollWithin($("#review-list"), li);
    });
  }

  function nextUnverified(afterId) {
    var start = items.findIndex(function (it) { return it.id === afterId; });
    for (var k = 1; k <= items.length; k++) {
      var it = items[(start + k + items.length) % items.length];
      if (!it.verdict) return it.id;
    }
    return null;
  }

  function save(verdict, advance) {
    var id = g.selectedId();
    if (id == null || !byId[id]) return;
    return g.fetchJSON("/verification/" + id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verdict: verdict, comment: $("#review-comment").value })
    }).then(function (data) {
      apply(data);
      if (!advance) return;
      var next = nextUnverified(id);
      if (next != null) g.selectFeature(next, { zoom: true });
      else g.showNote("Выборка проверена: geoqc eval discrepancies --verification reports/verification.csv");
    }).catch(function (err) { g.showNote(err.message); });
  }

  g.onSelect(function (feat) { render(feat.id); });

  $$("[data-verdict]").forEach(function (b) {
    b.addEventListener("click", function () { save(b.getAttribute("data-verdict"), true); });
  });
  $("#review-clear").addEventListener("click", function () { save(null, false); });
  $("#review-comment").addEventListener("change", function () {
    var it = byId[g.selectedId()];
    if (it) save(it.verdict, false);
  });
  $("#review-list").addEventListener("click", function (ev) {
    var li = ev.target.closest("li[data-id]");
    if (li) g.selectFeature(Number(li.getAttribute("data-id")), { zoom: true });
  });
  $("#review-next").addEventListener("click", function () {
    var next = nextUnverified(g.selectedId());
    if (next != null) g.selectFeature(next, { zoom: true });
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) return;
    var v = VERDICTS.filter(function (x) { return x.hotkey === ev.key; })[0];
    if (v && byId[g.selectedId()]) save(v.key, true);
  });

  // 404 = no sample yet (run `geoqc eval verification-sample`): no tab is added.
  fetch("/verification").then(function (r) {
    if (r.status === 404) return null;
    if (!r.ok) throw new Error("HTTP " + r.status + " /verification");
    return r.json();
  }).then(function (data) { if (data) apply(data); })
    .catch(function (err) { g.showNote(err.message); });
})();
