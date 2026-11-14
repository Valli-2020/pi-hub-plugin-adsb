// Flights tab: live map, aircraft table, charts, records and links.
// Runs in Pi Hub's sandboxed frame: no network, no popups, no storage.
// Everything comes through ph.call() from this plugin's own routes, map tiles
// included (as data: URLs). Data from the receiver is only ever set as
// textContent or drawn on a canvas, never parsed as markup.
(function () {
  'use strict';

  var STATS_MS = 60000, LINKS_MS = 30000, TILE = 256, MIN_Z = 2;
  var MAX_INFLIGHT = 3, MIN_GAP_MS = 130, TILE_INFLIGHT = 2, TILE_LRU = 320;
  var NS = 'http://www.w3.org/2000/svg';

  var st = {
    live: null, stats: null, links: null, sel: null, hover: null,
    sort: { key: 'dist', dir: 1 }, range: 'day', visible: true,
    timers: { live: 0, stats: 0, links: 0 }
  };

  // ── Small helpers ──────────────────────────────────────────────────────
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function css(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  function isDark() { return !!(ph.theme && ph.theme.mode === 'dark'); }
  function dash(v) { return v == null || v === '' ? '–' : v; }
  function fmtInt(v) { return v == null ? '–' : Math.round(v).toLocaleString('en-US'); }
  function fmt1(v) { return v == null ? '–' : (Math.round(v * 10) / 10).toLocaleString('en-US'); }
  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function hhmm(t) { var d = new Date(t * 1000); return pad(d.getHours()) + ':' + pad(d.getMinutes()); }
  function ago(t) {
    if (!t) return 'never';
    var s = Math.max(0, Math.round(Date.now() / 1000 - t));
    if (s < 60) return s + ' s ago';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    return Math.round(s / 3600) + ' h ago';
  }
  function compass(deg) {
    if (deg == null) return '';
    return ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][Math.round(deg / 45) % 8];
  }
  function altText(ac) {
    if (ac.ground) return 'ground';
    return ac.alt == null ? '–' : fmtInt(ac.alt) + ' ft';
  }

  // ── Request queue (the bridge allows few calls in flight) ──────────────
  var queue = [], inflight = 0, lastSend = 0, pumpTimer = 0;
  function call(route) {
    return new Promise(function (resolve, reject) {
      queue.push({ route: route, resolve: resolve, reject: reject });
      pump();
    });
  }
  function pump() {
    if (pumpTimer || !queue.length || inflight >= MAX_INFLIGHT) return;
    var wait = lastSend + MIN_GAP_MS - Date.now();
    if (wait > 0) { pumpTimer = setTimeout(function () { pumpTimer = 0; pump(); }, wait); return; }
    var job = queue.shift();
    inflight++; lastSend = Date.now();
    ph.call(job.route).then(job.resolve, job.reject).then(function () { inflight--; pump(); });
    pump();
  }

  // ── Skeleton ───────────────────────────────────────────────────────────
  var root = el('div', 'adsb');
  document.body.appendChild(root);

  var statusLine = el('div', 'statusline');
  var statusDot = el('span', 'dot'), statusMsg = el('span', 'msg'), statusGrow = el('span', 'grow');
  var statusRight = el('span', 'small');
  statusLine.appendChild(statusDot); statusLine.appendChild(statusMsg);
  statusLine.appendChild(statusGrow); statusLine.appendChild(statusRight);
  root.appendChild(statusLine);

  var tiles = el('div', 'tiles');
  root.appendChild(tiles);
  var TILE_DEFS = [
    ['total', 'Aircraft now', ''], ['with_pos', 'With position', ''], ['mlat', 'MLAT', ''],
    ['rate', 'Messages', '/s'], ['range', 'Max range today', 'km'], ['unique', 'Unique today', '']
  ];
  var tileVals = {};
  TILE_DEFS.forEach(function (d) {
    var t = el('div', 'tile');
    t.appendChild(el('div', 'label', d[1]));
    var v = el('div', 'value', '–');
    t.appendChild(v);
    tileVals[d[0]] = { v: v, unit: d[2] };
    tiles.appendChild(t);
  });
  function setTile(key, value) {
    var t = tileVals[key];
    t.v.textContent = value;
    if (t.unit && value !== '–') t.v.appendChild(el('span', 'unit', t.unit));
  }

  // Map card
  var mapCard = el('div', 'card');
  var mapHead = el('div', 'cardhead');
  mapHead.appendChild(el('h3', null, 'Map'));
  var centerBtn = el('button', 'btn', 'Center on receiver');
  centerBtn.type = 'button';
  mapHead.appendChild(centerBtn);
  mapCard.appendChild(mapHead);
  var wrap = el('div', 'mapwrap');
  var canvas = el('canvas');
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', 'Map of aircraft around the receiver');
  wrap.appendChild(canvas);
  var ctl = el('div', 'mapctl');
  var zin = el('button', 'btn icon', '+'), zout = el('button', 'btn icon', '−');
  zin.type = zout.type = 'button';
  zin.setAttribute('aria-label', 'Zoom in'); zout.setAttribute('aria-label', 'Zoom out');
  ctl.appendChild(zin); ctl.appendChild(zout);
  wrap.appendChild(ctl);
  var legend = el('div', 'legend');
  wrap.appendChild(legend);
  var attrib = el('div', 'attrib');
  wrap.appendChild(attrib);
  var note = el('div', 'mapnote');
  note.hidden = true;
  wrap.appendChild(note);
  var tip = el('div', 'tip');
  tip.hidden = true;
  wrap.appendChild(tip);
  mapCard.appendChild(wrap);
  root.appendChild(mapCard);

  // Aircraft table
  var tableCard = el('div', 'card');
  var tableHead = el('div', 'cardhead');
  var tableTitle = el('h3', null, 'Aircraft');
  tableHead.appendChild(tableTitle);
  tableCard.appendChild(tableHead);
  var tableWrap = el('div', 'tablewrap');
  var table = el('table');
  var thead = el('thead'), tbody = el('tbody');
  table.appendChild(thead); table.appendChild(tbody);
  tableWrap.appendChild(table);
  tableCard.appendChild(tableWrap);
  root.appendChild(tableCard);

  // Charts
  var chartCard = el('div', 'card');
  var chartHead = el('div', 'cardhead');
  chartHead.appendChild(el('h3', null, 'Statistics'));
  var chips = el('div', 'chips');
  var RANGES = [['today', 'Today'], ['day', '24 h'], ['week', '7 days']];
  RANGES.forEach(function (r) {
    var b = el('button', 'btn chip', r[1]);
    b.type = 'button';
    b.dataset.range = r[0];
    b.addEventListener('click', function () { st.range = r[0]; syncChips(); drawCharts(); });
    chips.appendChild(b);
  });
  function syncChips() {
    Array.prototype.forEach.call(chips.children, function (b) {
      b.setAttribute('aria-pressed', b.dataset.range === st.range ? 'true' : 'false');
    });
  }
  syncChips();
  chartHead.appendChild(chips);
  chartCard.appendChild(chartHead);
  var charts = el('div', 'charts');
  chartCard.appendChild(charts);
  function makeChart(title, keys) {
    var box = el('div');
    box.appendChild(el('div', 'small muted', title));
    var k = el('div', 'keys');
    keys.forEach(function (key) {
      var s = el('span');
      var i = el('i');
      i.dataset.color = key[1];
      s.appendChild(i);
      s.appendChild(document.createTextNode(key[0]));
      k.appendChild(s);
    });
    box.appendChild(k);
    var c = el('div', 'chart');
    var cv = el('canvas');
    var ct = el('div', 'ctip');
    ct.hidden = true;
    c.appendChild(cv); c.appendChild(ct);
    box.appendChild(c);
    charts.appendChild(box);
    return { box: c, canvas: cv, tip: ct, keys: k, hover: null };
  }
  var chartTraffic = makeChart('Aircraft and messages', [['Aircraft', '--accent'], ['Messages/s', '--ok']]);
  var chartRange = makeChart('Max range', [['Range (km)', '--accent']]);
  root.appendChild(chartCard);

  // Records + receiver
  var infoGrid = el('div', 'grid2');
  var recCard = el('div', 'card');
  recCard.appendChild(el('h3', null, 'Records'));
  var recBody = el('div');
  recCard.appendChild(recBody);
  var rxCard = el('div', 'card');
  rxCard.appendChild(el('h3', null, 'Receiver'));
  var rxBody = el('div');
  rxCard.appendChild(rxBody);
  infoGrid.appendChild(recCard); infoGrid.appendChild(rxCard);
  root.appendChild(infoGrid);

  // Links
  var linkCard = el('div', 'card');
  linkCard.appendChild(el('h3', null, 'Links'));
  var linkNote = el('p', 'small muted', '');
  linkNote.style.margin = '0 0 8px';
  linkNote.setAttribute('role', 'status');
  linkNote.setAttribute('aria-live', 'polite');
  linkCard.appendChild(linkNote);
  var linkGrid = el('div', 'links');
  linkCard.appendChild(linkGrid);
  linkCard.hidden = true;
  root.appendChild(linkCard);

  // ── Altitude colours ───────────────────────────────────────────────────
  var ALT_STOPS = [[0, 18], [2000, 32], [4000, 48], [8000, 85], [12000, 140], [20000, 190], [30000, 240], [40000, 290]];
  function altHue(alt) {
    if (alt <= ALT_STOPS[0][0]) return ALT_STOPS[0][1];
    for (var i = 1; i < ALT_STOPS.length; i++) {
      if (alt <= ALT_STOPS[i][0]) {
        var a = ALT_STOPS[i - 1], b = ALT_STOPS[i];
        return a[1] + (b[1] - a[1]) * (alt - a[0]) / (b[0] - a[0]);
      }
    }
    return ALT_STOPS[ALT_STOPS.length - 1][1];
  }
  function altColor(alt, ground) {
    if (ground) return isDark() ? '#9aa0a6' : '#6b7075';
    if (alt == null) return isDark() ? '#c4c7ca' : '#55595d';
    return 'hsl(' + Math.round(altHue(alt)) + ',80%,' + (isDark() ? '60%' : '42%') + ')';
  }
  function buildLegend() {
    clear(legend);
    [['Ground', null, true], ['0', 0], ['4k', 4000], ['8k', 8000], ['12k', 12000], ['20k', 20000],
     ['30k', 30000], ['40k ft', 40000]].forEach(function (l) {
      var s = el('span');
      var i = el('i');
      i.style.background = altColor(l[1], l[2]);
      s.appendChild(i);
      s.appendChild(document.createTextNode(l[0]));
      legend.appendChild(s);
    });
  }

  // ── Map ────────────────────────────────────────────────────────────────
  var map = { z: 8, cx: 0, cy: 0, w: 0, h: 0, dpr: 1, inited: false, maxZoom: 18,
              source: '', attribution: '', drawn: [], raf: 0, autoTheme: false };
  var tileImgs = new Map(), tilePending = {}, tileFailed = {}, tileInflight = 0;

  function worldSize(z) { return TILE * Math.pow(2, z); }
  function project(lat, lon, z) {
    var s = worldSize(z);
    lat = Math.max(-85.0511, Math.min(85.0511, lat));
    var r = lat * Math.PI / 180;
    return [(lon + 180) / 360 * s, (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * s];
  }
  function toScreen(lat, lon) {
    var p = project(lat, lon, map.z), s = worldSize(map.z), dx = p[0] - map.cx;
    if (dx > s / 2) dx -= s; else if (dx < -s / 2) dx += s;
    return [map.w / 2 + dx, map.h / 2 + (p[1] - map.cy)];
  }
  function clampView() {
    var s = worldSize(map.z);
    map.cx = ((map.cx % s) + s) % s;
    map.cy = Math.max(0, Math.min(s, map.cy));
  }
  function centerOn(lat, lon, z) {
    if (z != null) map.z = z;
    var p = project(lat, lon, map.z);
    map.cx = p[0]; map.cy = p[1];
    clampView();
  }
  function zoomAt(z, sx, sy) {
    z = Math.max(MIN_Z, Math.min(map.maxZoom, z));
    if (z === map.z) return;
    if (sx == null) { sx = map.w / 2; sy = map.h / 2; }
    var f = Math.pow(2, z - map.z);
    var wx = map.cx + (sx - map.w / 2), wy = map.cy + (sy - map.h / 2);
    map.cx = wx * f - (sx - map.w / 2);
    map.cy = wy * f - (sy - map.h / 2);
    map.z = z;
    clampView();
    hideTip();
    draw();
  }
  function home() { return st.live && st.live.home; }
  function centerHome() {
    var h = home();
    if (h) centerOn(h.lat, h.lon, st.live.start_zoom || 8);
    draw();
  }

  function sizeMap() {
    var w = wrap.clientWidth || 300;
    var h = Math.round(Math.max(320, Math.min(620, w * 0.62)));
    if (wrap.style.height !== h + 'px') wrap.style.height = h + 'px';
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (w !== map.w || h !== map.h || dpr !== map.dpr) {
      map.w = w; map.h = h; map.dpr = dpr;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      draw();
    }
  }

  // Tiles
  function themeKey() { return map.autoTheme ? (isDark() ? 'dark' : 'light') : ''; }
  function tileKey(z, x, y) { return themeKey() + '/' + z + '/' + x + '/' + y; }
  function getTile(key) {
    var t = tileImgs.get(key);
    if (t) { tileImgs.delete(key); tileImgs.set(key, t); }
    return t && t.complete && t.naturalWidth ? t : null;
  }
  function putTile(key, img) {
    tileImgs.set(key, img);
    while (tileImgs.size > TILE_LRU) tileImgs.delete(tileImgs.keys().next().value);
  }
  function requestTiles(missing, z) {
    if (!missing.length || tileInflight >= TILE_INFLIGHT || !st.live || !st.live.map.available) return;
    // Bounding box of what is missing, cut into rectangles of at most 6 x 5.
    var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    missing.forEach(function (m) {
      minX = Math.min(minX, m[0]); maxX = Math.max(maxX, m[0]);
      minY = Math.min(minY, m[1]); maxY = Math.max(maxY, m[1]);
    });
    var n = Math.pow(2, z);
    for (var y0 = minY; y0 <= maxY && tileInflight < TILE_INFLIGHT; y0 += 5) {
      for (var x0 = minX; x0 <= maxX && tileInflight < TILE_INFLIGHT; x0 += 6) {
        var w = Math.min(6, maxX - x0 + 1), h = Math.min(5, maxY - y0 + 1), keys = [], need = false;
        for (var dy = 0; dy < h; dy++) {
          for (var dx = 0; dx < w; dx++) {
            var k = tileKey(z, (((x0 + dx) % n) + n) % n, y0 + dy);
            keys.push(k);
            if (!tilePending[k] && !tileImgs.has(k)) need = true;
          }
        }
        if (!need) continue;
        keys.forEach(function (k) { tilePending[k] = true; });
        fetchTiles(z, (((x0 % n) + n) % n), y0, w, h, keys);
      }
    }
  }
  function fetchTiles(z, x, y, w, h, keys) {
    tileInflight++;
    var theme = isDark() ? 'dark' : 'light', tk = themeKey();
    call('tiles?z=' + z + '&x=' + x + '&y=' + y + '&w=' + w + '&h=' + h + '&theme=' + theme).then(function (d) {
      if (map.source && d.source !== map.source) { tileImgs.clear(); tileFailed = {}; }
      map.source = d.source;
      map.attribution = d.attribution || '';
      map.maxZoom = Math.min(19, d.max_zoom || 18);
      attrib.textContent = map.attribution;
      attrib.hidden = !map.attribution;
      (d.tiles || []).forEach(function (t) {
        var img = new Image();
        img.onload = draw;
        img.src = t.src;
        putTile(tk + '/' + d.z + '/' + t.x + '/' + t.y, img);
      });
      (d.failed || []).forEach(function (t) { tileFailed[tk + '/' + d.z + '/' + t[0] + '/' + t[1]] = Date.now(); });
      if ((d.missing || []).length) setTimeout(draw, 400);
    }, function (e) {
      keys.forEach(function (k) { tileFailed[k] = Date.now(); });
      if (e && e.status === 400) showNote(e.message);
    }).then(function () {
      keys.forEach(function (k) { delete tilePending[k]; });
      tileInflight--;
      draw();
    });
  }
  function showNote(text) {
    note.textContent = text;
    note.hidden = !text;
  }

  function draw() {
    if (map.raf) return;
    map.raf = requestAnimationFrame(function () { map.raf = 0; paint(); });
  }

  var PLANE = typeof Path2D === 'function' ? new Path2D(
    'M0,-10 L1.6,-6.5 L1.6,-2 L9,2.8 L9,4.8 L1.6,2.6 L1.2,7 L4,9 L4,10.5 L0,9.6 L-4,10.5 L-4,9 L-1.2,7 L-1.6,2.6 L-9,4.8 L-9,2.8 L-1.6,-2 L-1.6,-6.5 Z') : null;

  function paint() {
    var ctx = canvas.getContext('2d');
    if (!ctx || !map.w) return;
    var w = map.w, h = map.h, z = map.z, n = Math.pow(2, z);
    ctx.setTransform(map.dpr, 0, 0, map.dpr, 0, 0);
    ctx.fillStyle = css('--surface-2', '#ddd');
    ctx.fillRect(0, 0, w, h);
    var live = st.live;

    // Tiles
    var missing = [], now = Date.now();
    if (live && live.map.available) {
      var x0 = Math.floor((map.cx - w / 2) / TILE), x1 = Math.floor((map.cx + w / 2) / TILE);
      var y0 = Math.max(0, Math.floor((map.cy - h / 2) / TILE)), y1 = Math.min(n - 1, Math.floor((map.cy + h / 2) / TILE));
      for (var ty = y0; ty <= y1; ty++) {
        for (var tx = x0; tx <= x1; tx++) {
          var wx = ((tx % n) + n) % n, key = tileKey(z, wx, ty);
          var sx = Math.round(w / 2 + tx * TILE - map.cx), sy = Math.round(h / 2 + ty * TILE - map.cy);
          var img = getTile(key);
          if (img) { ctx.drawImage(img, sx, sy, TILE, TILE); continue; }
          drawFallback(ctx, z, wx, ty, sx, sy);
          if (!tilePending[key] && !(tileFailed[key] && now - tileFailed[key] < 60000) && !tileImgs.has(key))
            missing.push([tx, ty]);
        }
      }
      if (isDark() && !map.autoTheme && live.map.provider === 'OpenStreetMap') {
        ctx.fillStyle = 'rgba(0,0,0,0.28)';    // calm the bright OSM tiles in dark mode
        ctx.fillRect(0, 0, w, h);
      }
    }
    var hm = live && live.home;

    // Range rings
    if (hm && live.rings && live.rings.length) {
      var c = toScreen(hm.lat, hm.lon);
      var mpp = Math.cos(hm.lat * Math.PI / 180) * 2 * Math.PI * 6378137 / worldSize(z);
      ctx.save();
      ctx.strokeStyle = isDark() ? 'rgba(255,255,255,0.45)' : 'rgba(0,0,0,0.42)';
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 5]);
      ctx.font = '11px system-ui, sans-serif';
      ctx.textAlign = 'center';
      live.rings.forEach(function (km) {
        var r = km * 1000 / mpp;
        if (r < 12 || r > 4 * Math.max(w, h)) return;
        ctx.beginPath();
        ctx.arc(c[0], c[1], r, 0, Math.PI * 2);
        ctx.stroke();
        var label = (km % 1 ? km : Math.round(km)) + ' km';
        var ly = c[1] - r;
        if (ly > 8 && ly < h - 4) {
          var tw = ctx.measureText(label).width + 8;
          ctx.fillStyle = css('--surface', '#fff');
          ctx.globalAlpha = 0.85;
          ctx.fillRect(c[0] - tw / 2, ly - 8, tw, 16);
          ctx.globalAlpha = 1;
          ctx.fillStyle = css('--text', '#222');
          ctx.fillText(label, c[0], ly + 4);
        }
      });
      ctx.restore();
    }

    // Trails
    if (live && live.trails) {
      ctx.save();
      ctx.lineWidth = 2;
      ctx.lineJoin = 'round';
      ctx.globalAlpha = 0.75;
      Object.keys(live.trails).forEach(function (hx) {
        var pts = live.trails[hx], prev = null, color = null;
        for (var i = 0; i < pts.length; i++) {
          var p = toScreen(pts[i][0], pts[i][1]), col = altColor(pts[i][2], false);
          if (prev && Math.abs(p[0] - prev[0]) < w) {
            if (col !== color) {
              if (color) ctx.stroke();
              ctx.strokeStyle = color = col;
              ctx.beginPath();
              ctx.moveTo(prev[0], prev[1]);
            }
            ctx.lineTo(p[0], p[1]);
          } else if (color) { ctx.stroke(); color = null; }
          prev = p;
        }
        if (color) ctx.stroke();
      });
      ctx.restore();
    }

    // Receiver marker
    if (hm) {
      var r0 = toScreen(hm.lat, hm.lon);
      ctx.save();
      ctx.translate(r0[0], r0[1]);
      ctx.rotate(Math.PI / 4);
      ctx.fillStyle = css('--text', '#222');
      ctx.strokeStyle = css('--surface', '#fff');
      ctx.lineWidth = 2;
      ctx.fillRect(-5, -5, 10, 10);
      ctx.strokeRect(-5, -5, 10, 10);
      ctx.restore();
    }

    // Aircraft
    var drawn = [];
    if (live) {
      var outline = isDark() ? 'rgba(0,0,0,0.85)' : 'rgba(255,255,255,0.95)';
      live.aircraft.forEach(function (ac) {
        if (ac.lat == null) return;
        var p = toScreen(ac.lat, ac.lon);
        if (p[0] < -20 || p[1] < -20 || p[0] > w + 20 || p[1] > h + 20) return;
        drawn.push({ ac: ac, x: p[0], y: p[1] });
      });
      // selected last so it sits on top
      drawn.sort(function (a, b) { return (a.ac.hex === st.sel) - (b.ac.hex === st.sel); });
      drawn.forEach(function (d) {
        var ac = d.ac, selected = ac.hex === st.sel || ac.hex === st.hover;
        ctx.save();
        ctx.translate(d.x, d.y);
        if (selected) {
          ctx.beginPath();
          ctx.arc(0, 0, 15, 0, Math.PI * 2);
          ctx.strokeStyle = css('--accent', '#f59e0b');
          ctx.lineWidth = 2.5;
          ctx.stroke();
        }
        ctx.fillStyle = altColor(ac.alt, ac.ground);
        ctx.strokeStyle = outline;
        ctx.lineWidth = 1.4;
        if (PLANE && ac.track != null) {
          ctx.rotate(ac.track * Math.PI / 180);
          var sc = ac.ground ? 0.8 : 1.1;
          ctx.scale(sc, sc);
          ctx.stroke(PLANE);
          ctx.fill(PLANE);
        } else {
          ctx.beginPath();
          ctx.arc(0, 0, 4.5, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
        }
        ctx.restore();
        if (selected && ac.flight) {
          ctx.font = '600 11px system-ui, sans-serif';
          ctx.textAlign = 'left';
          ctx.lineWidth = 3;
          ctx.strokeStyle = outline;
          ctx.strokeText(ac.flight, d.x + 14, d.y - 10);
          ctx.fillStyle = css('--text', '#222');
          ctx.fillText(ac.flight, d.x + 14, d.y - 10);
        }
      });
    }
    map.drawn = drawn;

    // Notes
    if (!live) showNote('Loading…');
    else if (live.state === 'unconfigured') showNote(live.message);
    else if (!live.map.available) showNote('Set a custom tile URL in the plugin settings to show a map.');
    else if (!hm && !live.aircraft.some(function (a) { return a.lat != null; })) showNote('Receiver position unknown: set latitude and longitude in the plugin settings.');
    else if (note.textContent === 'Loading…' || note.textContent.indexOf('position unknown') >= 0 ||
             note.textContent.indexOf('custom tile') >= 0) showNote('');
    requestTiles(missing, z);
  }
  function drawFallback(ctx, z, x, y, sx, sy) {
    for (var k = 1; k <= 4 && z - k >= 0; k++) {
      var img = getTile(tileKey(z - k, x >> k, y >> k));
      if (!img) continue;
      var size = TILE >> k;
      ctx.drawImage(img, (x - ((x >> k) << k)) * size, (y - ((y >> k) << k)) * size, size, size, sx, sy, TILE, TILE);
      return;
    }
  }

  // Hit testing and tooltip
  function hit(x, y, radius) {
    var best = null, bd = radius * radius;
    map.drawn.forEach(function (d) {
      var dd = (d.x - x) * (d.x - x) + (d.y - y) * (d.y - y);
      if (dd <= bd) { bd = dd; best = d; }
    });
    return best;
  }
  function tipRow(label, value) {
    var r = el('div', 'row');
    r.appendChild(el('span', null, label));
    r.appendChild(el('span', null, value));
    return r;
  }
  function showTip(d) {
    var ac = d.ac;
    clear(tip);
    tip.appendChild(el('b', null, ac.flight || ac.hex.toUpperCase()));
    tip.appendChild(tipRow('Hex', ac.hex.toUpperCase()));
    if (ac.type || ac.reg) tip.appendChild(tipRow('Type / reg', [ac.type, ac.reg].filter(Boolean).join(' · ')));
    tip.appendChild(tipRow('Altitude', altText(ac)));
    tip.appendChild(tipRow('Speed', ac.gs == null ? '–' : fmtInt(ac.gs) + ' kt'));
    tip.appendChild(tipRow('Vertical rate', ac.vr == null ? '–' : (ac.vr > 0 ? '+' : '') + fmtInt(ac.vr) + ' ft/min'));
    tip.appendChild(tipRow('Squawk', dash(ac.squawk)));
    tip.appendChild(tipRow('Distance', ac.dist == null ? '–' : fmt1(ac.dist) + ' km ' + compass(ac.brg)));
    tip.appendChild(tipRow('Source', (ac.src || '').toUpperCase()));
    tip.hidden = false;
    var tw = tip.offsetWidth, th = tip.offsetHeight;
    var x = d.x + 16, y = d.y + 16;
    if (x + tw > map.w - 4) x = Math.max(4, d.x - tw - 16);
    if (y + th > map.h - 4) y = Math.max(4, d.y - th - 16);
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  }
  function hideTip() { tip.hidden = true; }

  function select(hex, pan) {
    st.sel = hex;
    var found = null;
    if (hex && st.live) st.live.aircraft.forEach(function (a) { if (a.hex === hex) found = a; });
    if (found && pan && found.lat != null) {
      var p = toScreen(found.lat, found.lon);
      if (p[0] < 30 || p[1] < 30 || p[0] > map.w - 30 || p[1] > map.h - 30) centerOn(found.lat, found.lon);
    }
    renderTable();
    draw();
    if (found && found.lat != null) {
      requestAnimationFrame(function () {
        var d = null;
        map.drawn.forEach(function (x) { if (x.ac.hex === hex) d = x; });
        if (d) showTip(d);
      });
    } else hideTip();
  }

  // Pointer input: drag to pan, pinch and wheel to zoom, tap to select
  var pointers = new Map(), drag = null, pinch = null, moved = false, wasPinch = false, wheelAt = 0;
  function local(e) {
    var r = wrap.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }
  function pinchInfo() {
    var p = Array.from(pointers.values());
    return { d: Math.hypot(p[0][0] - p[1][0], p[0][1] - p[1][1]) || 1,
             mx: (p[0][0] + p[1][0]) / 2, my: (p[0][1] + p[1][1]) / 2 };
  }
  wrap.addEventListener('pointerdown', function (e) {
    if (e.target.closest('.mapctl')) return;
    try { wrap.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    pointers.set(e.pointerId, local(e));
    if (pointers.size === 1) {
      var p = local(e);
      drag = { x: p[0], y: p[1], cx: map.cx, cy: map.cy };
      moved = false; wasPinch = false;
    } else if (pointers.size === 2) {
      var pi = pinchInfo();
      pinch = { d: pi.d, z: map.z };
      drag = null; wasPinch = true;
    }
  });
  wrap.addEventListener('pointermove', function (e) {
    var p = local(e);
    if (pointers.has(e.pointerId)) {
      pointers.set(e.pointerId, p);
      if (pinch && pointers.size >= 2) {
        var pi = pinchInfo();
        var target = pinch.z + Math.round(Math.log(pi.d / pinch.d) / Math.LN2);
        if (target !== map.z) zoomAt(target, pi.mx, pi.my);
        return;
      }
      if (drag) {
        var dx = p[0] - drag.x, dy = p[1] - drag.y;
        if (!moved && Math.abs(dx) + Math.abs(dy) > 4) { moved = true; wrap.classList.add('dragging'); hideTip(); }
        if (moved) {
          map.cx = drag.cx - dx; map.cy = drag.cy - dy;
          clampView();
          draw();
        }
      }
      return;
    }
    if (e.pointerType === 'mouse') {
      var d = hit(p[0], p[1], 14);
      var hx = d ? d.ac.hex : null;
      if (hx !== st.hover) { st.hover = hx; draw(); }
      if (d) showTip(d); else if (!st.sel) hideTip();
      wrap.style.cursor = d ? 'pointer' : '';
    }
  });
  function endPointer(e) {
    if (!pointers.has(e.pointerId)) return;
    var p = local(e);
    pointers.delete(e.pointerId);
    wrap.classList.remove('dragging');
    if (pointers.size < 2) pinch = null;
    if (pointers.size === 1) {
      var rest = Array.from(pointers.values())[0];
      drag = { x: rest[0], y: rest[1], cx: map.cx, cy: map.cy };
      moved = true;
      return;
    }
    if (pointers.size === 0) {
      if (!moved && !wasPinch && e.type === 'pointerup') {
        var d = hit(p[0], p[1], e.pointerType === 'mouse' ? 14 : 22);
        select(d ? d.ac.hex : null, false);
      }
      drag = null;
    }
  }
  wrap.addEventListener('pointerup', endPointer);
  wrap.addEventListener('pointercancel', endPointer);
  wrap.addEventListener('pointerleave', function (e) {
    if (e.pointerType === 'mouse' && st.hover) { st.hover = null; draw(); if (!st.sel) hideTip(); }
  });
  wrap.addEventListener('wheel', function (e) {
    e.preventDefault();
    var t = Date.now();
    if (t - wheelAt < 160 || Math.abs(e.deltaY) < 1) return;
    wheelAt = t;
    var p = local(e);
    zoomAt(map.z + (e.deltaY < 0 ? 1 : -1), p[0], p[1]);
  }, { passive: false });
  wrap.addEventListener('dblclick', function (e) { var p = local(e); zoomAt(map.z + 1, p[0], p[1]); });
  zin.addEventListener('click', function () { zoomAt(map.z + 1); });
  zout.addEventListener('click', function () { zoomAt(map.z - 1); });
  centerBtn.addEventListener('click', centerHome);

  // ── Aircraft table ─────────────────────────────────────────────────────
  var COLS = [
    { key: 'flight', label: 'Callsign', text: function (a) { return a.flight || '–'; } },
    { key: 'hex', label: 'Hex', mono: true, text: function (a) { return a.hex.toUpperCase(); } },
    { key: 'type', label: 'Type', text: function (a) { return dash(a.type); } },
    { key: 'reg', label: 'Reg', text: function (a) { return dash(a.reg); } },
    { key: 'alt', label: 'Altitude (ft)', num: true, text: function (a) { return a.ground ? 'ground' : fmtInt(a.alt); } },
    { key: 'gs', label: 'Speed (kt)', num: true, text: function (a) { return fmtInt(a.gs); } },
    { key: 'vr', label: 'V/S (ft/min)', num: true, text: function (a) { return a.vr == null ? '–' : (a.vr > 0 ? '+' : '') + fmtInt(a.vr); } },
    { key: 'squawk', label: 'Squawk', mono: true, text: function (a) { return dash(a.squawk); } },
    { key: 'dist', label: 'Distance (km)', num: true, text: function (a) { return a.dist == null ? '–' : fmt1(a.dist) + ' ' + compass(a.brg); } },
    { key: 'src', label: 'Source', text: function (a) { return (a.src || '').toUpperCase(); } },
    { key: 'seen', label: 'Seen (s)', num: true, text: function (a) { return fmt1(a.seen); } }
  ];
  function buildHead() {
    clear(thead);
    var tr = el('tr');
    COLS.forEach(function (c) {
      var th = el('th', c.num ? 'num' : null, c.label);
      th.scope = 'col';
      th.tabIndex = 0;
      if (st.sort.key === c.key) th.setAttribute('aria-sort', st.sort.dir > 0 ? 'ascending' : 'descending');
      function sortBy() {
        if (st.sort.key === c.key) st.sort.dir = -st.sort.dir;
        else { st.sort.key = c.key; st.sort.dir = c.num && c.key !== 'dist' ? -1 : 1; }
        buildHead();
        renderTable();
      }
      th.addEventListener('click', sortBy);
      th.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sortBy(); } });
      tr.appendChild(th);
    });
    thead.appendChild(tr);
  }
  function sortValue(a, key) {
    if (key === 'alt') return a.ground ? 0 : a.alt;
    var v = a[key];
    return v === '' ? null : v;
  }
  function renderTable() {
    var list = st.live ? st.live.aircraft.slice() : [];
    var key = st.sort.key, dir = st.sort.dir;
    list.sort(function (a, b) {
      var va = sortValue(a, key), vb = sortValue(b, key);
      if (va == null && vb == null) return a.hex < b.hex ? -1 : 1;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (va < vb) return -dir;
      if (va > vb) return dir;
      return a.hex < b.hex ? -1 : 1;
    });
    var frag = document.createDocumentFragment();
    list.forEach(function (a) {
      var tr = el('tr');
      if (a.hex === st.sel) tr.className = 'sel';
      if (a.lat == null) tr.className += ' nopos';
      COLS.forEach(function (c, i) {
        var td = el('td', (c.num ? 'num' : '') + (c.mono ? ' mono' : ''));
        if (i === 0) {
          var sw = el('span', 'swatch');
          sw.style.background = altColor(a.alt, a.ground);
          td.appendChild(sw);
        }
        td.appendChild(document.createTextNode(c.text(a)));
        tr.appendChild(td);
      });
      tr.addEventListener('click', function () { select(st.sel === a.hex ? null : a.hex, true); });
      frag.appendChild(tr);
    });
    clear(tbody);
    if (!list.length) {
      var tr = el('tr'), td = el('td', 'empty', st.live && st.live.state === 'ok' ? 'No aircraft right now' : 'No data');
      td.colSpan = COLS.length;
      tr.appendChild(td);
      frag.appendChild(tr);
    }
    tbody.appendChild(frag);
    tableTitle.textContent = 'Aircraft' + (list.length ? ' (' + list.length + ')' : '');
  }

  // ── Charts ─────────────────────────────────────────────────────────────
  function niceStep(max, ticks) {
    var raw = max / ticks, mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10)), n = raw / mag;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
  }
  function seriesSpan() {
    var now = Date.now() / 1000;
    if (st.range === 'week') return [now - 7 * 86400, now, 3600];
    if (st.range === 'today') { var d = new Date(); d.setHours(0, 0, 0, 0); return [d.getTime() / 1000, now, 300]; }
    return [now - 86400, now, 300];
  }
  function sizeCanvas(cv, box) {
    var dpr = Math.min(window.devicePixelRatio || 1, 2), w = box.clientWidth || 300, h = box.clientHeight || 200;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    }
    var ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: w, h: h };
  }
  // lines: [{col, color, axis: 'l'|'r', label, unit}]
  function drawChart(ch, rows, lines) {
    var s = sizeCanvas(ch.canvas, ch.box), ctx = s.ctx, w = s.w, h = s.h;
    var span = seriesSpan(), t0 = span[0], t1 = span[1], step = span[2];
    var hasRight = lines.some(function (l) { return l.axis === 'r'; });
    var L = 44, R = hasRight ? 48 : 12, T = 8, B = 22, pw = w - L - R, phh = h - T - B;
    var text = css('--muted', '#666'), grid = css('--border', '#ddd');
    ctx.clearRect(0, 0, w, h);
    ctx.font = '11px system-ui, sans-serif';
    Array.prototype.forEach.call(ch.keys.querySelectorAll('i'), function (i) { i.style.background = css(i.dataset.color, '#888'); });
    if (!rows.length) {
      ctx.fillStyle = text; ctx.textAlign = 'center';
      ctx.fillText('No history yet', w / 2, h / 2);
      ch.state = null;
      return;
    }
    function axisMax(axis) {
      var m = 0;
      lines.forEach(function (l) { if (l.axis === axis) rows.forEach(function (r) { m = Math.max(m, r[l.col] || 0); }); });
      m = Math.max(m, 1);
      var st2 = niceStep(m, 4);
      return { max: Math.ceil(m / st2) * st2, step: st2 };
    }
    var ax = { l: axisMax('l'), r: null };
    if (hasRight) {
      // Right axis shares the left axis' grid lines, with its own round step.
      var ticks = Math.round(ax.l.max / ax.l.step), rmax = 0;
      lines.forEach(function (l) { if (l.axis === 'r') rows.forEach(function (r) { rmax = Math.max(rmax, r[l.col] || 0); }); });
      var rstep = niceStep(Math.max(rmax, 1), ticks);
      ax.r = { max: rstep * ticks, step: rstep };
    }
    function X(t) { return L + (t - t0) / (t1 - t0) * pw; }
    function Y(v, a) { return T + phh - v / ax[a].max * phh; }
    // grid + left labels
    ctx.strokeStyle = grid; ctx.lineWidth = 1; ctx.fillStyle = text;
    for (var v = 0; v <= ax.l.max + 1e-9; v += ax.l.step) {
      var y = Math.round(Y(v, 'l')) + 0.5;
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + pw, y); ctx.stroke();
      ctx.textAlign = 'right'; ctx.fillText(fmtInt(v), L - 6, y + 4);
      if (ax.r) {
        ctx.textAlign = 'left';
        ctx.fillText(fmtInt(v / ax.l.max * ax.r.max), L + pw + 6, y + 4);
      }
    }
    // x labels
    ctx.textAlign = 'center';
    var d = new Date(t0 * 1000);
    if (st.range === 'week') {
      d.setHours(24, 0, 0, 0);
      for (; d.getTime() / 1000 < t1; d.setDate(d.getDate() + 1)) {
        var xx = X(d.getTime() / 1000);
        ctx.fillText(d.toLocaleDateString('en-GB', { weekday: 'short' }), xx, h - 6);
      }
    } else {
      var every = pw < 360 ? 6 : 3;
      d.setMinutes(0, 0, 0);
      d.setHours(d.getHours() + 1);
      for (; d.getTime() / 1000 < t1; d.setHours(d.getHours() + 1)) {
        if (d.getHours() % every) continue;
        ctx.fillText(pad(d.getHours()) + ':00', X(d.getTime() / 1000), h - 6);
      }
    }
    // lines
    lines.forEach(function (l) {
      ctx.strokeStyle = css(l.color, '#888');
      ctx.lineWidth = 1.8;
      ctx.lineJoin = 'round';
      ctx.beginPath();
      var prev = null;
      rows.forEach(function (r) {
        var x = X(r[0] + step / 2), y = Y(r[l.col] || 0, l.axis);
        if (prev == null || r[0] - prev > step * 2.5) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        prev = r[0];
      });
      ctx.stroke();
    });
    ch.state = { rows: rows, lines: lines, X: X, L: L, pw: pw, step: step, Y: Y, T: T, phh: phh };
    if (ch.hover != null) chartHover(ch);
  }
  function chartHover(ch) {
    var s = ch.state;
    if (!s || ch.hover == null) { ch.tip.hidden = true; return; }
    var best = null, bd = Infinity;
    s.rows.forEach(function (r) {
      var dx = Math.abs(s.X(r[0] + s.step / 2) - ch.hover);
      if (dx < bd) { bd = dx; best = r; }
    });
    if (!best || bd > 40) { ch.tip.hidden = true; return; }
    var ctx = ch.canvas.getContext('2d'), x = s.X(best[0] + s.step / 2);
    ctx.save();
    ctx.strokeStyle = css('--border-strong', '#999');
    ctx.beginPath(); ctx.moveTo(x, s.T); ctx.lineTo(x, s.T + s.phh); ctx.stroke();
    ctx.restore();
    var when = st.range === 'week'
      ? new Date(best[0] * 1000).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric' }) + ' ' + hhmm(best[0])
      : hhmm(best[0]);
    ch.tip.textContent = when + ' · ' + s.lines.map(function (l) { return l.label + ' ' + fmt1(best[l.col]); }).join(' · ');
    ch.tip.hidden = false;
    var tw = ch.tip.offsetWidth;
    ch.tip.style.left = Math.max(0, Math.min(ch.box.clientWidth - tw, x - tw / 2)) + 'px';
  }
  [chartTraffic, chartRange].forEach(function (ch) {
    ch.canvas.addEventListener('pointermove', function (e) {
      var r = ch.canvas.getBoundingClientRect();
      ch.hover = e.clientX - r.left;
      drawCharts();
    });
    ch.canvas.addEventListener('pointerleave', function () { ch.hover = null; drawCharts(); });
  });
  function drawCharts() {
    var rows = st.stats && st.stats.series ? st.stats.series[st.range] || [] : [];
    // row: [t, aircraft, with_pos, msg_per_s, max_range_km]
    drawChart(chartTraffic, rows, [
      { col: 1, color: '--accent', axis: 'l', label: 'aircraft' },
      { col: 3, color: '--ok', axis: 'r', label: 'msg/s' }
    ]);
    drawChart(chartRange, rows, [{ col: 4, color: '--accent', axis: 'l', label: 'km' }]);
  }

  // ── Records and receiver cards ─────────────────────────────────────────
  function kv(rows) {
    var dl = el('dl', 'kv');
    rows.forEach(function (r) {
      dl.appendChild(el('dt', null, r[0]));
      dl.appendChild(el('dd', null, r[1]));
    });
    return dl;
  }
  function recText(r, unit, fmt) {
    if (!r || r[unit] == null) return '–';
    var who = r.flight || (r.hex || '').toUpperCase();
    return fmt(r[unit]) + ' · ' + who + (r.type ? ' (' + r.type + ')' : '') + ' · ' + hhmm(r.t);
  }
  function renderRecords() {
    clear(recBody);
    var s = st.stats;
    if (!s) { recBody.appendChild(el('p', 'muted small', 'Loading…')); return; }
    var t = s.today || {}, b = s.best || {};
    function km(v) { return fmt1(v) + ' km'; }
    function ft(v) { return fmtInt(v) + ' ft'; }
    function kt(v) { return fmtInt(v) + ' kt'; }
    recBody.appendChild(el('div', 'small muted', 'Today'));
    recBody.appendChild(kv([
      ['Unique aircraft', fmtInt(t.unique || 0)],
      ['Most at once', fmtInt(t.max_aircraft || 0)],
      ['Furthest', recText(t.furthest, 'km', km)],
      ['Highest', recText(t.highest, 'ft', ft)],
      ['Fastest', recText(t.fastest, 'kt', kt)]
    ]));
    var best = el('div', 'small muted', 'Best of the last ' + s.history_days + ' days');
    best.style.marginTop = '10px';
    recBody.appendChild(best);
    function withDay(r, unit, f) { return r ? recText(r, unit, f) + ' · ' + r.day : '–'; }
    recBody.appendChild(kv([
      ['Furthest', withDay(b.furthest, 'km', km)],
      ['Highest', withDay(b.highest, 'ft', ft)],
      ['Fastest', withDay(b.fastest, 'kt', kt)]
    ]));
  }
  function renderReceiver() {
    clear(rxBody);
    var c = (st.stats && st.stats.current) || {}, live = st.live;
    function db(v) { return v == null ? '–' : fmt1(v) + ' dBFS'; }
    var rows = [
      ['Signal', db(c.signal)], ['Noise', db(c.noise)], ['Peak signal', db(c.peak_signal)],
      ['Strong signals (1 min)', fmtInt(c.strong_signals)],
      ['Gain', c.gain_db == null ? '–' : fmt1(c.gain_db) + ' dB'],
      ['Frequency error', c.estimated_ppm == null ? '–' : fmt1(c.estimated_ppm) + ' ppm'],
      ['Messages', c.msg_rate_1min == null ? '–' : fmt1(c.msg_rate_1min) + ' /s (1 min)'],
      ['Max range (15 min)', c.max_distance_km_15min == null ? '–' : fmt1(c.max_distance_km_15min) + ' km'],
      ['Tracks (15 min)', fmtInt(c.tracks_15min)]
    ];
    if (live && live.home) rows.push(['Position', live.home.lat.toFixed(3) + ', ' + live.home.lon.toFixed(3) +
      (live.home.source === 'settings' ? ' (settings)' : ' (receiver)')]);
    if (live && live.receiver_version) rows.push(['Decoder', live.receiver_version]);
    rxBody.appendChild(kv(rows));
  }

  // ── Links ──────────────────────────────────────────────────────────────
  var LINK_STATE = { online: 'online', offline: 'offline', unknown: 'checking', off: '' };
  // ph.open (Pi Hub 8.3.1+) asks the dashboard to open the link in a new tab;
  // the sandbox itself cannot open windows.
  var canOpen = typeof ph.open === 'function', openAt = 0, noteTimer = 0;
  function linkMessage(text, bad) {
    linkNote.textContent = text;
    linkNote.style.color = bad ? 'var(--bad, #c33)' : '';
    clearTimeout(noteTimer);
    if (canOpen) noteTimer = setTimeout(function () { linkNote.textContent = ''; }, 6000);
  }
  function openLink(l) {
    var t = Date.now();
    if (t - openAt < 1100) return;          // the bridge allows one open per second
    openAt = t;
    ph.open(l.url).then(function (r) {
      if (r && r.ok === false) linkMessage('Could not open ' + l.label + ': ' + (r.error || 'refused'), true);
      else linkMessage('Opening ' + l.label + ' in a new tab.', false);
    }, function (e) {
      linkMessage('Could not open ' + l.label + ': ' + (e && e.message || 'error'), true);
    });
  }
  function renderLinks() {
    var links = (st.links && st.links.links) || [];
    linkCard.hidden = !links.length;
    if (!canOpen && !linkNote.textContent)
      linkMessage('Open these from the ADS-B receiver tab in the sidebar: this Pi Hub cannot open links from the map.', false);
    clear(linkGrid);
    links.forEach(function (l) {
      var item = el(canOpen ? 'button' : 'div', 'link');
      if (canOpen) {
        item.type = 'button';
        item.addEventListener('click', function () { openLink(l); });
        item.setAttribute('aria-label', 'Open ' + l.label + ' in a new tab');
      }
      item.dataset.state = l.state;
      item.title = l.url;
      var dot = el('span', 'ldot');
      if (LINK_STATE[l.state]) dot.title = LINK_STATE[l.state];
      item.appendChild(dot);
      var tx = el('div', 'ltext');
      tx.appendChild(el('div', 'lname', l.label));
      tx.appendChild(el('div', 'lurl', l.url));
      item.appendChild(tx);
      if (LINK_STATE[l.state]) item.appendChild(el('span', 'small muted', LINK_STATE[l.state]));
      if (canOpen) item.appendChild(el('span', 'lgo', '\u2197'));
      linkGrid.appendChild(item);
    });
  }

  // ── Status and tiles ───────────────────────────────────────────────────
  function renderStatus() {
    var d = st.live;
    statusLine.dataset.state = d ? d.state : 'starting';
    if (!d) { statusMsg.textContent = 'Connecting…'; statusRight.textContent = ''; return; }
    if (d.state === 'ok') statusMsg.textContent = 'Live · updated ' + ago(d.updated);
    else if (d.state === 'unreachable') statusMsg.textContent = d.message + (d.updated ? ' · last data ' + ago(d.updated) : '');
    else statusMsg.textContent = d.message;
    statusRight.textContent = d.home ? '' : 'Receiver position unknown';
  }
  function renderTiles() {
    var d = st.live, c = d ? d.counts : {};
    setTile('total', fmtInt(c.total));
    setTile('with_pos', fmtInt(c.with_pos));
    setTile('mlat', fmtInt(c.mlat));
    setTile('rate', c.msg_rate == null ? '–' : fmtInt(c.msg_rate));
    setTile('range', d && d.today.furthest_km != null ? fmtInt(d.today.furthest_km) : '–');
    setTile('unique', d ? fmtInt(d.today.unique) : '–');
  }

  // ── Loading ────────────────────────────────────────────────────────────
  function schedule(name, fn, ms) {
    clearTimeout(st.timers[name]);
    st.timers[name] = st.visible ? setTimeout(fn, ms) : 0;
  }
  function loadLive() {
    call('live?trails=1').then(function (d) {
      var firstMap = !map.inited;
      var autoBefore = map.autoTheme;
      st.live = d;
      map.autoTheme = !!d.map.auto_theme;
      if (autoBefore !== map.autoTheme) tileImgs.clear();
      if (firstMap) {
        if (d.home) { centerOn(d.home.lat, d.home.lon, d.start_zoom || 8); map.inited = true; }
        else {
          var pts = d.aircraft.filter(function (a) { return a.lat != null; });
          if (pts.length) {
            var la = 0, lo = 0;
            pts.forEach(function (a) { la += a.lat; lo += a.lon; });
            centerOn(la / pts.length, lo / pts.length, d.start_zoom || 8);
            map.inited = true;
          } else centerOn(30, 0, MIN_Z);
        }
      }
      if (st.sel && !d.aircraft.some(function (a) { return a.hex === st.sel; })) { st.sel = null; hideTip(); }
      renderStatus(); renderTiles(); renderTable(); renderReceiver();
      centerBtn.disabled = !d.home;
      draw();
      if (st.sel) {
        requestAnimationFrame(function () {
          map.drawn.forEach(function (x) { if (x.ac.hex === st.sel) showTip(x); });
        });
      }
      schedule('live', loadLive, Math.max(2, d.poll_seconds || 5) * 1000);
    }, function (e) {
      statusLine.dataset.state = 'unreachable';
      statusMsg.textContent = e && e.status === 403 ? 'Not allowed' : 'Pi Hub did not answer (' + (e && e.message || 'error') + ')';
      schedule('live', loadLive, 10000);
    });
  }
  function loadStats() {
    call('stats').then(function (d) {
      st.stats = d;
      drawCharts(); renderRecords(); renderReceiver();
      schedule('stats', loadStats, STATS_MS);
    }, function () { schedule('stats', loadStats, STATS_MS); });
  }
  function loadLinks() {
    call('links').then(function (d) {
      st.links = d;
      renderLinks();
      schedule('links', loadLinks, LINKS_MS);
    }, function () { schedule('links', loadLinks, LINKS_MS); });
  }
  function startAll() { loadLive(); loadStats(); loadLinks(); }
  function stopAll() {
    Object.keys(st.timers).forEach(function (k) { clearTimeout(st.timers[k]); st.timers[k] = 0; });
  }

  ph.on('visible', function (v) {
    var was = st.visible;
    st.visible = !!v;
    if (st.visible && !was) startAll();
    if (!st.visible) stopAll();
  });
  ph.on('theme', function () {
    buildLegend();
    if (map.autoTheme) hideTip();
    draw(); drawCharts(); renderTable();
  });
  ph.on('refresh', function () { stopAll(); startAll(); });

  // Status line clock
  setInterval(function () { if (st.visible) renderStatus(); }, 5000);

  if (window.ResizeObserver) {
    new ResizeObserver(function () { sizeMap(); }).observe(wrap);
    new ResizeObserver(function () { drawCharts(); }).observe(charts);
  }
  window.addEventListener('resize', function () { sizeMap(); drawCharts(); });

  buildHead();
  buildLegend();
  renderStatus(); renderTiles(); renderTable(); renderRecords(); renderReceiver();
  sizeMap();
  drawCharts();
  startAll();
})();
