/* geoqc web map: Leaflet 1.9 + Leaflet.VectorGrid (MVT from /tiles), list/card from /discrepancies.
 * Vector path chosen: MVT (all three layers come from one tile request); the JSON list endpoint is
 * used only for the top-N panel and the feature card. No bundler, no npm. */
(function () {
  "use strict";

  var CLASS_COLORS = {
    missing_in_map: "#e63946",
    missing_on_image: "#f4a261",
    geometry_mismatch: "#9b5de5"
  };
  var CLASS_LABELS = {
    missing_in_map: "Нет на карте",
    missing_on_image: "Нет на снимке",
    geometry_mismatch: "Геометрия"
  };
  var STATUS_LABELS = { new: "новое", confirmed: "подтверждено", rejected: "отклонено" };

  var state = {
    cfg: null,
    layers: { buildings_ref: true, buildings_det: true, discrepancies: true },
    classes: { missing_in_map: true, missing_on_image: true, geometry_mismatch: true },
    statuses: { new: true, confirmed: true, rejected: false },
    minConf: 0,
    topN: 50,
    features: {},      // id -> Feature (from the list or GET /discrepancies/{id})
    selectedId: null,
    map: null,
    vg: null,
    highlight: null
  };

  var $ = function (sel) { return document.querySelector(sel); };
  var $$ = function (sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); };

  function fetchJSON(url, opts) {
    return fetch(url, opts).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) { throw new Error("HTTP " + r.status + " " + url + ": " + t.slice(0, 200)); });
      }
      return r.json();
    });
  }

  function showNote(text) {
    var el = $("#note");
    if (!text) { el.classList.add("hidden"); return; }
    el.textContent = text;
    el.classList.remove("hidden");
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---------------------------------------------------------------- filters -> query string
  function activeKeys(obj) {
    return Object.keys(obj).filter(function (k) { return obj[k]; });
  }

  function filterParams() {
    var p = new URLSearchParams();
    var cls = activeKeys(state.classes);
    var st = activeKeys(state.statuses);
    // Empty selection would mean "no filter" server-side, so send an impossible-but-valid filter.
    p.set("class", cls.length ? cls.join(",") : "missing_in_map");
    p.set("status", st.length ? st.join(",") : "new");
    if (state.minConf > 0) p.set("min_conf", state.minConf.toFixed(2));
    return { params: p, empty: !cls.length || !st.length };
  }

  function passesFilter(props) {
    return state.classes[props["class"]] && state.statuses[props.status] &&
      Number(props.confidence) >= state.minConf;
  }

  function viewBbox() {
    var b = state.map.getBounds();
    var clamp = function (v, lo, hi) { return Math.min(hi, Math.max(lo, v)); };
    var minx = clamp(b.getWest(), -180, 180), maxx = clamp(b.getEast(), -180, 180);
    var miny = clamp(b.getSouth(), -90, 90), maxy = clamp(b.getNorth(), -90, 90);
    if (minx >= maxx || miny >= maxy) return null;
    return [minx, miny, maxx, maxy].map(function (v) { return v.toFixed(6); }).join(",");
  }

  // ---------------------------------------------------------------- vector tile styling
  function discrepancyStyle(props) {
    var color = CLASS_COLORS[props["class"]] || "#333";
    var style = {
      fill: true, fillColor: color, fillOpacity: 0.35, color: color, weight: 2, opacity: 0.9
    };
    if (props.status === "confirmed") { style.weight = 3; style.fillOpacity = 0.55; }
    if (props.status === "rejected") { style.dashArray = "4 4"; style.fillOpacity = 0.08; style.opacity = 0.6; }
    return style;
  }

  var tileStyles = {
    buildings_ref: function () {
      if (!state.layers.buildings_ref) return [];
      return { fill: false, color: "#1d4ed8", weight: 1.5, opacity: 0.9 };
    },
    buildings_det: function () {
      if (!state.layers.buildings_det) return [];
      return { fill: false, color: "#16a34a", weight: 1.5, opacity: 0.9, dashArray: "3 3" };
    },
    discrepancies: function (props) {
      if (!state.layers.discrepancies || !passesFilter(props)) return [];
      return discrepancyStyle(props);
    }
  };

  function featureKey(props) {
    return (props.layer || "x") + ":" + props.id;
  }

  function refreshTiles() {
    if (!state.vg) return;
    state.vg.options.rev = Date.now();
    state.vg.redraw();
  }

  // ---------------------------------------------------------------- list
  var listTimer = null;
  function scheduleList() {
    clearTimeout(listTimer);
    listTimer = setTimeout(loadList, 250);
  }

  function loadList() {
    var bbox = viewBbox();
    var f = filterParams();
    var listEl = $("#list");
    if (f.empty || !bbox) {
      listEl.innerHTML = "";
      $("#list-count").textContent = "0";
      return Promise.resolve();
    }
    f.params.set("bbox", bbox);
    f.params.set("limit", String(state.topN));
    return fetchJSON("/discrepancies?" + f.params.toString()).then(function (fc) {
      fc.features.forEach(function (feat) { state.features[feat.id] = feat; });
      $("#list-count").textContent = fc.numberReturned + " / " + fc.numberMatched;
      listEl.innerHTML = fc.features.map(function (feat, i) {
        var p = feat.properties;
        return '<li data-id="' + feat.id + '" class="status-' + p.status + (feat.id === state.selectedId ? " active" : "") + '">' +
          '<span class="bar" style="background:' + CLASS_COLORS[p["class"]] + '"></span>' +
          '<span><div class="title">#' + (i + 1) + " · " + escapeHtml(CLASS_LABELS[p["class"]] || p["class"]) + '</div>' +
          '<div class="sub">id ' + feat.id + (p.iou != null ? " · IoU " + Number(p.iou).toFixed(2) : "") +
          (p.ref_osm_id ? " · osm " + p.ref_osm_id : "") + '</div></span>' +
          '<span class="conf">' + (Number(p.confidence) * 100).toFixed(0) + "%</span></li>";
      }).join("");
    }).catch(function (err) { showNote(err.message); });
  }

  $("#list").addEventListener("click", function (ev) {
    var li = ev.target.closest("li[data-id]");
    if (!li) return;
    selectFeature(Number(li.getAttribute("data-id")), { zoom: true });
  });

  // ---------------------------------------------------------------- card
  function osmLink(osmId) {
    // buildings_ref.osm_id has no type column; buildings are ways in the vast majority of cases.
    var url = "https://www.openstreetmap.org/way/" + osmId;
    return '<a href="' + url + '" target="_blank" rel="noopener">' + osmId + "</a>";
  }

  // Scoring inputs from discrepancy_features (DECISIONS D-012), present only on GET /discrepancies/{id}.
  function scoringRows(f) {
    if (!f) return "";
    var fmt = function (v, d) { return v == null ? "—" : (typeof v === "number" ? v.toFixed(d) : String(v)); };
    return "<dt>Площадь, м²</dt><dd>" + fmt(f.area_m2, 1) + "</dd>" +
      "<dt>2-й источник</dt><dd>" + (f.has_secondary ? "да, покрытие " + fmt(f.secondary_coverage, 2) : "нет") + "</dd>" +
      "<dt>max IoU</dt><dd>" + fmt(f.max_iou_any, 3) + "</dd>" +
      "<dt>Край AOI</dt><dd>" + (f.aoi_edge ? "да" : "нет") + "</dd>" +
      "<dt>Score модели</dt><dd>" + fmt(f.score, 3) + "</dd>";
  }

  function renderCard(feat) {
    var body = $("#card-body");
    var idEl = $("#card-id");
    var buttons = ["#btn-accept", "#btn-reject", "#btn-reset"].map(function (s) { return $(s); });
    if (!feat) {
      idEl.textContent = "";
      body.className = "muted";
      body.textContent = "Выберите объект на карте или в списке.";
      buttons.forEach(function (b) { b.disabled = true; });
      return;
    }
    var p = feat.properties;
    idEl.textContent = "#" + feat.id;
    body.className = "";
    body.innerHTML = "<dl>" +
      "<dt>Класс</dt><dd><span class='badge " + p["class"] + "'>" + p["class"] + "</span> " + escapeHtml(CLASS_LABELS[p["class"]] || "") + "</dd>" +
      "<dt>Confidence</dt><dd>" + Number(p.confidence).toFixed(3) + "</dd>" +
      "<dt>IoU</dt><dd>" + (p.iou != null ? Number(p.iou).toFixed(3) : "—") + "</dd>" +
      "<dt>Статус</dt><dd><span class='badge " + p.status + "'>" + escapeHtml(STATUS_LABELS[p.status] || p.status) + "</span></dd>" +
      "<dt>OSM</dt><dd>" + (p.ref_osm_id ? osmLink(p.ref_osm_id) : "—") + "</dd>" +
      "<dt>ref_id / det_id</dt><dd>" + (p.ref_id != null ? p.ref_id : "—") + " / " + (p.det_id != null ? p.det_id : "—") + "</dd>" +
      "<dt>Модель</dt><dd>" + escapeHtml(p.model_version || "—") + "</dd>" +
      "<dt>Обновлено</dt><dd>" + escapeHtml((p.updated_at || "").replace("T", " ").slice(0, 19)) + "</dd>" +
      scoringRows(p.features) +
      "</dl>";
    $("#btn-accept").disabled = p.status === "confirmed";
    $("#btn-reject").disabled = p.status === "rejected";
    $("#btn-reset").disabled = p.status === "new";
  }

  function setHighlight(feat) {
    if (state.highlight) { state.map.removeLayer(state.highlight); state.highlight = null; }
    if (!feat) return;
    state.highlight = L.geoJSON(feat, {
      style: { color: "#111", weight: 4, opacity: 0.9, fill: false, dashArray: "6 3" },
      interactive: false
    }).addTo(state.map);
  }

  function selectFeature(id, opts) {
    opts = opts || {};
    state.selectedId = id;
    $$("#list li").forEach(function (li) {
      li.classList.toggle("active", Number(li.getAttribute("data-id")) === id);
    });
    var cached = state.features[id];
    var promise = (cached && "features" in cached.properties)
      ? Promise.resolve(cached) : fetchJSON("/discrepancies/" + id);
    return promise.then(function (feat) {
      state.features[id] = feat;
      renderCard(feat);
      setHighlight(feat);
      if (opts.zoom && state.highlight) {
        state.map.fitBounds(state.highlight.getBounds(), { maxZoom: 19, padding: [40, 40] });
      }
      var li = $('#list li[data-id="' + id + '"]');
      if (li && opts.scrollList !== false) li.scrollIntoView({ block: "nearest" });
    }).catch(function (err) { showNote(err.message); });
  }

  function patchStatus(status) {
    var id = state.selectedId;
    if (id == null) return;
    return fetchJSON("/discrepancies/" + id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: status })
    }).then(function (feat) {
      state.features[id] = feat;
      renderCard(feat);
      if (state.vg) {
        var props = Object.assign({}, feat.properties, { layer: "discrepancies" });
        var style = (state.layers.discrepancies && passesFilter(props)) ? discrepancyStyle(props) : [];
        state.vg.setFeatureStyle(featureKey(props), style);
      }
      loadList();
      loadStats();
    }).catch(function (err) { showNote(err.message); });
  }

  $("#btn-accept").addEventListener("click", function () { patchStatus("confirmed"); });
  $("#btn-reject").addEventListener("click", function () { patchStatus("rejected"); });
  $("#btn-reset").addEventListener("click", function () { patchStatus("new"); });

  // ---------------------------------------------------------------- stats / export / controls
  function loadStats() {
    return fetchJSON("/stats").then(function (s) {
      $("#stats").textContent = "всего " + s.total + " · new " + s.by_status.new +
        " · confirmed " + s.by_status.confirmed + " · rejected " + s.by_status.rejected;
    }).catch(function () { $("#stats").textContent = "статистика недоступна (БД?)"; });
  }

  $("#btn-export").addEventListener("click", function () {
    var f = filterParams();
    var a = document.createElement("a");
    a.href = "/export?" + f.params.toString();
    a.download = "discrepancies.geojson";
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  $("#btn-refresh").addEventListener("click", function () {
    refreshTiles();
    loadList();
    loadStats();
  });

  $$("input[data-layer]").forEach(function (cb) {
    cb.addEventListener("change", function () {
      state.layers[cb.getAttribute("data-layer")] = cb.checked;
      refreshTiles();
    });
  });
  $$("input[data-class]").forEach(function (cb) {
    cb.addEventListener("change", function () {
      state.classes[cb.getAttribute("data-class")] = cb.checked;
      refreshTiles();
      loadList();
    });
  });
  $$("input[data-status]").forEach(function (cb) {
    cb.addEventListener("change", function () {
      state.statuses[cb.getAttribute("data-status")] = cb.checked;
      refreshTiles();
      loadList();
    });
  });
  var confTimer = null;
  $("#min-conf").addEventListener("input", function (ev) {
    state.minConf = Number(ev.target.value);
    $("#min-conf-out").textContent = state.minConf.toFixed(2);
    clearTimeout(confTimer);
    confTimer = setTimeout(function () { refreshTiles(); loadList(); }, 300);
  });
  $("#top-n").addEventListener("change", function (ev) {
    state.topN = Number(ev.target.value);
    loadList();
  });

  // ---------------------------------------------------------------- map bootstrap
  function initMap(cfg) {
    state.cfg = cfg;
    if (cfg.aoi && cfg.aoi.name) $("#aoi-name").textContent = cfg.aoi.name;
    var map = L.map("map", { maxZoom: 22, zoomControl: true });
    state.map = map;
    map.attributionControl.setPrefix('<a href="/docs" target="_blank" rel="noopener">geoqc API</a>');

    var basemaps = {};
    var first = null, imagery = null;
    cfg.basemaps.forEach(function (b) {
      var layer = L.tileLayer(b.url, {
        attribution: b.attribution, maxZoom: 22, maxNativeZoom: b.max_zoom, crossOrigin: false
      });
      basemaps[b.title] = layer;
      if (!first) first = layer;
      if (b.key === "imagery") imagery = layer;
    });
    (imagery || first).addTo(map);
    L.control.layers(basemaps, null, { position: "topright", collapsed: false }).addTo(map);
    L.control.scale({ imperial: false }).addTo(map);

    // Data-derived layers are OSM (ODbL) and detections on the open image: attribute both always.
    if (cfg.attribution.osm) map.attributionControl.addAttribution(cfg.attribution.osm);
    if (cfg.attribution.image) {
      map.attributionControl.addAttribution("Снимок: " + cfg.attribution.image);
      $("#legal-image").textContent = "Снимок: " + cfg.attribution.image.replace(/<[^>]+>/g, "") + ".";
    }
    if (cfg.esri_enabled && cfg.attribution.esri) {
      $("#legal-esri").textContent = "Esri World Imagery (" + cfg.attribution.esri + ") — только подложка, D-002.";
    }

    if (cfg.aoi && cfg.aoi.bbox) {
      var bb = cfg.aoi.bbox;
      map.fitBounds([[bb[1], bb[0]], [bb[3], bb[2]]]);
    } else {
      map.setView([0, 0], 2);
    }
    if (cfg.note) showNote(cfg.note);

    if (L.vectorGrid && L.vectorGrid.protobuf) {
      // Leaflet >= 1.8 removed L.DomEvent.fakeStop used by VectorGrid's canvas renderer.
      if (!L.DomEvent.fakeStop) L.DomEvent.fakeStop = function () { return true; };
      var minZoom = Math.min.apply(null, Object.keys(cfg.mvt_layers).map(function (k) { return cfg.mvt_layers[k]; }));
      state.vg = L.vectorGrid.protobuf("/tiles/{z}/{x}/{y}.mvt?rev={rev}", {
        rev: Date.now(),
        minZoom: isFinite(minZoom) ? minZoom : 0,
        maxNativeZoom: 20,
        maxZoom: 22,
        interactive: true,
        vectorTileLayerStyles: tileStyles,
        getFeatureId: featureKey,
        pane: "overlayPane"
      }).addTo(map);
      state.vg.on("click", function (e) {
        var p = e.layer && e.layer.properties;
        if (!p || p.layer !== "discrepancies") return;
        L.DomEvent.stop(e);
        selectFeature(Number(p.id), { zoom: false });
      });
    } else {
      showNote("Leaflet.VectorGrid не загрузился (CDN недоступен): слои MVT выключены, список и карточка работают.");
    }

    map.on("moveend", scheduleList);
    loadList();
    loadStats();
  }

  fetchJSON("/config/map").then(initMap).catch(function (err) {
    showNote("Не удалось загрузить /config/map: " + err.message);
    initMap({ aoi: {}, basemaps: [{ key: "osm", title: "OSM Standard",
      url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      attribution: "&copy; OpenStreetMap contributors, ODbL", max_zoom: 19 }],
      attribution: {}, mvt_layers: {}, esri_enabled: false });
  });
})();
