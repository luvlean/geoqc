/* geoqc: building detection on an uploaded image (POST /detect).
 * Loaded by app.js when /config/map says detect_tools=true (config/api.yaml detect.enabled).
 * Own tab "Снимок": drag & drop anywhere on the page or a file picker; the result opens in a
 * viewer over the map (Leaflet CRS.Simple, image pixels). Georeferenced results can also be put
 * on the main map and downloaded as GeoJSON (EPSG:4326). Uses only the window.geoqc surface. */
(function () {
  "use strict";

  var g = window.geoqc;
  if (!g) return;
  var assetQuery = (document.currentScript && document.currentScript.src.split("?")[1]) || "";
  var css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "/detect.css" + (assetQuery ? "?" + assetQuery : "");
  document.head.appendChild(css);

  var esc = g.escapeHtml;
  var cfg = { conf_default: 0.5, min_conf: 0.1, default_gsd_m: 0.3, max_upload_mb: 300, max_side_px: 6144 };
  var file = null;       // last chosen File (kept to re-run with other settings)
  var result = null;     // last /detect response
  var viewer = null;     // { map, overlay, polys }
  var onMap = null;      // L.GeoJSON on the main map
  var busy = false;
  var rerun = false;    // a file was chosen while the previous one was still running

  var panel = document.createElement("section");
  panel.className = "panel detect list-tab";
  panel.innerHTML =
    '<div id="detect-drop" class="detect-drop" tabindex="0">' +
    "<b>Перетащите снимок сюда</b><br><span class=\"muted small\">GeoTIFF, PNG, JPEG · или</span><br>" +
    '<button type="button" class="btn" id="detect-pick">Выбрать файл</button>' +
    '<input type="file" id="detect-file" accept="image/*,.tif,.tiff,.jp2,.webp" hidden>' +
    "</div>" +
    '<div class="detect-form">' +
    '<label title="Размер пикселя снимка на местности. Пусто — из привязки GeoTIFF, иначе значение по умолчанию">' +
    'Пиксель, м <input type="number" id="detect-gsd" min="0.01" max="10" step="0.01" placeholder="авто"></label>' +
    '<label title="Порог уверенности модели">Порог <input type="range" id="detect-conf" min="0.1" max="0.95" step="0.05">' +
    '<output id="detect-conf-out"></output></label>' +
    "</div>" +
    '<div class="detect-actions">' +
    '<button type="button" class="btn ok" id="detect-run" disabled>Распознать</button>' +
    '<button type="button" class="btn" id="detect-view-btn" disabled>Просмотр</button>' +
    '<button type="button" class="btn" id="detect-onmap" disabled title="Только для снимков с привязкой">На карту</button>' +
    '<button type="button" class="btn ghost" id="detect-save" disabled>GeoJSON</button>' +
    "</div>" +
    '<div id="detect-status" class="muted small">Модель обучена на аэрофото 0,3 м/пикс: лучше всего работает на снимках 0,1–0,5 м.</div>' +
    '<ol id="detect-list" class="list detect-list"></ol>';
  g.addTab("detect", "Снимок", panel);

  var drop = document.getElementById("detect-drop");
  var input = document.getElementById("detect-file");
  var gsdInput = document.getElementById("detect-gsd");
  var conf = document.getElementById("detect-conf");
  var confOut = document.getElementById("detect-conf-out");
  var status = document.getElementById("detect-status");
  var list = document.getElementById("detect-list");
  var btnRun = document.getElementById("detect-run");
  var btnView = document.getElementById("detect-view-btn");
  var btnMap = document.getElementById("detect-onmap");
  var btnSave = document.getElementById("detect-save");

  function setConf(v) { conf.value = v; confOut.textContent = Number(v).toFixed(2); }
  conf.addEventListener("input", function () { setConf(conf.value); });
  g.fetchJSON("/detect/config").then(function (c) {
    cfg = c;
    conf.min = c.min_conf;
    setConf(c.conf_default);
    gsdInput.placeholder = "авто (" + c.default_gsd_m + ")";
  }).catch(function () { setConf(cfg.conf_default); });

  function setStatus(html, cls) {
    status.className = "small " + (cls || "muted");
    status.innerHTML = html;
  }

  // ---------------------------------------------------------------- choosing a file
  function choose(f) {
    if (!f) return;
    if (f.size > cfg.max_upload_mb * 1024 * 1024) {
      setStatus("Файл " + esc(f.name) + " больше " + cfg.max_upload_mb + " МБ.", "error");
      return;
    }
    file = f;
    btnRun.disabled = false;
    g.showTab("detect");
    run();
  }
  document.getElementById("detect-pick").addEventListener("click", function () { input.click(); });
  drop.addEventListener("click", function (ev) { if (ev.target === drop) input.click(); });
  drop.addEventListener("keydown", function (ev) { if (ev.key === "Enter" || ev.key === " ") input.click(); });
  input.addEventListener("change", function () { choose(input.files[0]); input.value = ""; });
  btnRun.addEventListener("click", run);

  // Drag & drop anywhere on the page: a full-window hint while a file is dragged over.
  var hint = document.createElement("div");
  hint.className = "detect-hint hidden";
  hint.textContent = "Отпустите файл — модель найдёт здания на снимке";
  document.body.appendChild(hint);
  var depth = 0;
  function hasFiles(ev) {
    return ev.dataTransfer && Array.prototype.indexOf.call(ev.dataTransfer.types || [], "Files") >= 0;
  }
  window.addEventListener("dragenter", function (ev) {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    depth += 1;
    hint.classList.remove("hidden");
  });
  window.addEventListener("dragover", function (ev) {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "copy";
  });
  window.addEventListener("dragleave", function (ev) {
    if (!hasFiles(ev)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) hint.classList.add("hidden");
  });
  window.addEventListener("drop", function (ev) {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    depth = 0;
    hint.classList.add("hidden");
    choose(ev.dataTransfer.files[0]);
  });

  // ---------------------------------------------------------------- request
  function run() {
    if (!file) return;
    if (busy) { rerun = true; return; }
    busy = true;
    btnRun.disabled = true;
    var t0 = Date.now();
    var name = file.name;
    setStatus("<b>" + esc(name) + "</b>: загрузка и распознавание…", "busy");
    var form = new FormData();
    form.append("file", file, name);
    if (gsdInput.value) form.append("gsd_m", gsdInput.value);
    form.append("conf", conf.value);
    fetch("/detect", { method: "POST", body: form }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (r.ok) return j;
        var d = j.detail;
        // FastAPI validation errors come as a list of {loc, msg}
        if (Array.isArray(d)) d = d.map(function (e) { return (e.loc || []).slice(-1)[0] + ": " + e.msg; }).join("; ");
        throw new Error(typeof d === "string" && d ? d : "HTTP " + r.status);
      });
    }).then(function (j) {
      result = j;
      showResult((Date.now() - t0) / 1000);
    }).catch(function (err) {
      setStatus("<b>" + esc(name) + "</b>: " + esc(err.message), "error");
    }).then(function () {
      busy = false;
      btnRun.disabled = !file;
      if (rerun) { rerun = false; run(); }
    });
  }

  var GSD_FROM = { georeference: "из привязки", user: "задан вручную", "default": "по умолчанию — задайте, если знаете" };

  function showResult(wall) {
    var j = result;
    var feats = j.features.features;
    var total = feats.reduce(function (s, f) { return s + f.properties.area_m2; }, 0);
    setStatus(
      "<b>" + esc(j.filename) + "</b>: " + feats.length + " зданий, " + Math.round(total).toLocaleString("ru") + " м²<br>" +
      j.source_size[0] + "×" + j.source_size[1] + " px, пиксель " + String(j.source_gsd_m).replace(".", ",") + " м (" +
      GSD_FROM[j.gsd_from] + ") → " + j.width + "×" + j.height + " px при " + String(j.gsd_m).replace(".", ",") +
      " м · порог " + j.conf.toFixed(2).replace(".", ",") + " · " + j.n_tiles + " тайлов · " + wall.toFixed(1) + " с",
      j.gsd_from === "default" ? "warn" : "muted"
    );
    list.innerHTML = feats.map(function (f) {
      var p = f.properties;
      return '<li data-id="' + p.id + '"><span class="bar"></span>' +
        '<span><span class="title">#' + p.id + "</span> <span class=\"sub\">" + Math.round(p.area_m2) + " м²</span></span>" +
        '<span class="conf">' + p.score.toFixed(2) + "</span></li>";
    }).join("");
    btnView.disabled = false;
    btnSave.disabled = false;
    btnMap.disabled = !j.georeferenced;
    if (onMap) { g.map().removeLayer(onMap); onMap = null; btnMap.textContent = "На карту"; }
    openViewer();
  }

  // ---------------------------------------------------------------- viewer over the map
  var view = document.createElement("div");
  view.id = "detect-view";
  view.className = "hidden";
  view.innerHTML = '<div id="detect-map"></div>' +
    '<div class="detect-tools">' +
    '<div class="detect-mode" title="Переключение: клавиша H">' +
    '<button type="button" class="btn selected" data-mode="det">Детекция</button>' +
    '<button type="button" class="btn" data-mode="src">Исходник</button>' +
    "</div>" +
    '<button type="button" class="btn" id="detect-close" title="Вернуться к карте очереди">✕ Закрыть просмотр</button>' +
    "</div>";
  document.body.appendChild(view);
  document.getElementById("detect-close").addEventListener("click", closeViewer);
  var modeBtns = view.querySelectorAll(".detect-mode button");
  var showPolys = true;
  function setPolys(on) {
    showPolys = on;
    Array.prototype.forEach.call(modeBtns, function (b) {
      b.classList.toggle("selected", (b.getAttribute("data-mode") === "det") === on);
    });
    if (!viewer || !viewer.polys) return;
    if (on) viewer.polys.addTo(viewer.map);
    else viewer.map.removeLayer(viewer.polys);
  }
  Array.prototype.forEach.call(modeBtns, function (b) {
    b.addEventListener("click", function () { setPolys(b.getAttribute("data-mode") === "det"); });
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.code !== "KeyH" || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (view.classList.contains("hidden") || /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName)) return;
    setPolys(!showPolys);
  });
  btnView.addEventListener("click", function () { if (result) openViewer(); });

  var STYLE = { color: "#22c55e", weight: 2, opacity: 1, fillOpacity: 0.12 };
  var STYLE_ACTIVE = { color: "#00e5ff", weight: 3, opacity: 1, fillOpacity: 0.25 };

  function openViewer() {
    view.classList.remove("hidden");
    if (!viewer) {
      var m = L.map("detect-map", {
        crs: L.CRS.Simple, minZoom: -6, maxZoom: 4, zoomSnap: 0, zoomDelta: 0.5,
        maxBoundsViscosity: 1, attributionControl: false
      });
      viewer = { map: m, overlay: null, polys: null, byId: {}, bounds: null };
      m.on("resize", function () { fillView(false); });
    }
    var j = result;
    var m2 = viewer.map;
    m2.invalidateSize();
    if (viewer.overlay) m2.removeLayer(viewer.overlay);
    if (viewer.polys) m2.removeLayer(viewer.polys);
    // CRS.Simple: lat = -row, lng = column, so the picture is upright.
    var bounds = [[-j.height, 0], [0, j.width]];
    viewer.overlay = L.imageOverlay(j.image, bounds).addTo(m2);
    viewer.byId = {};
    viewer.polys = L.geoJSON(j.features, {
      style: function () { return STYLE; },
      coordsToLatLng: function (c) { return L.latLng(-c[1], c[0]); },
      onEachFeature: function (f, layer) {
        var p = f.properties;
        viewer.byId[p.id] = layer;
        layer.bindTooltip("#" + p.id + " · " + Math.round(p.area_m2) + " м² · " + p.score.toFixed(2), { sticky: true });
        layer.on("click", function () { activate(p.id, false); });
      }
    });
    if (showPolys) viewer.polys.addTo(m2);
    viewer.bounds = L.latLngBounds(bounds);
    m2.setMaxBounds(viewer.bounds);
    fillView(true);
  }

  // The picture fills the whole viewer (no empty margins): the smallest zoom is the one at which
  // the image still covers the view, and panning stops at the image edges.
  function fillView(reset) {
    if (!viewer || !viewer.bounds) return;
    var m = viewer.map;
    m.setMinZoom(-6);
    var cover = m.getBoundsZoom(viewer.bounds, true);
    m.setMinZoom(cover);
    if (reset || !m._loaded || m.getZoom() < cover) m.setView(viewer.bounds.getCenter(), cover, { animate: false });
  }

  function closeViewer() { view.classList.add("hidden"); }

  var active = null;
  function activate(id, zoom) {
    if (!viewer || !result) return;
    if (zoom) {
      // reopening rebuilds the layers, so look the polygon up afterwards
      if (view.classList.contains("hidden")) openViewer();
      if (!showPolys) setPolys(true);
    }
    if (active && viewer.byId[active]) viewer.byId[active].setStyle(STYLE);
    active = id;
    var layer = viewer.byId[id];
    if (!layer) return;
    layer.setStyle(STYLE_ACTIVE);
    if (showPolys) layer.bringToFront();
    if (zoom) viewer.map.fitBounds(layer.getBounds(), { maxZoom: 2, padding: [80, 80] });
    Array.prototype.forEach.call(list.children, function (li) {
      li.classList.toggle("active", Number(li.getAttribute("data-id")) === id);
    });
    var li = list.querySelector('li[data-id="' + id + '"]');
    if (li) g.scrollWithin(list, li);
  }
  list.addEventListener("click", function (ev) {
    var li = ev.target.closest("li[data-id]");
    if (li) activate(Number(li.getAttribute("data-id")), true);
  });

  // ---------------------------------------------------------------- main map and download
  btnMap.addEventListener("click", function () {
    if (!result || !result.features_wgs84) return;
    var map = g.map();
    if (onMap) {
      map.removeLayer(onMap);
      onMap = null;
      btnMap.textContent = "На карту";
      return;
    }
    onMap = L.geoJSON(result.features_wgs84, {
      style: function () { return { color: "#22c55e", weight: 2, fillOpacity: 0.15 }; },
      onEachFeature: function (f, layer) {
        var p = f.properties;
        layer.bindTooltip("Снимок: #" + p.id + " · " + Math.round(p.area_m2) + " м² · " + p.score.toFixed(2), { sticky: true });
      }
    }).addTo(map);
    btnMap.textContent = "Убрать с карты";
    closeViewer();
    if (onMap.getLayers().length) map.fitBounds(onMap.getBounds(), { padding: [40, 40] });
  });

  btnSave.addEventListener("click", function () {
    if (!result) return;
    var fc = result.features_wgs84 || result.features;
    var blob = new Blob([JSON.stringify(fc)], { type: "application/geo+json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = result.filename.replace(/\.[^.]+$/, "") + (result.features_wgs84 ? "_buildings.geojson" : "_buildings_px.geojson");
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  });
})();
