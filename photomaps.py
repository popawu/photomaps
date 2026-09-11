#!/usr/bin/env python3
"""
photomaps.py — Generate an interactive photo heatmap from GPS EXIF metadata.

Modes:
  --extract-only   Scan files and save GPS data to cache JSON (slow, once)
  --from-cache     Load cache and regenerate the map (fast)
  --incremental    Only scan files without GPS + new files (update)
  (default)        Full scan + generate map + save cache

Usage:
  python3 photomaps.py --path /volume1/photo --extract-only
  python3 photomaps.py --from-cache --output maps.html
  python3 photomaps.py --path /volume1/photo --incremental
  python3 photomaps.py --path /volume1/photo/2009 --output maps_test.html

Configuration:
  Copy .env.example to .env and edit the values.
  CLI arguments always take precedence over .env.
"""

from __future__ import annotations
import argparse
import json
import math
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import folium

# ---------------------------------------------------------------------------
# Load .env (simple key=value, no dependencies)
# ---------------------------------------------------------------------------

def load_env(env_file: Path = Path(__file__).parent / ".env") -> None:
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

load_env()

# ---------------------------------------------------------------------------
# Configuration (from .env or defaults)
# ---------------------------------------------------------------------------

DEFAULT_PHOTO_ROOT      = Path(os.environ.get("PHOTO_ROOT",      "/volume1/photo"))
DEFAULT_EXCLUDE_SUBDIRS = os.environ.get("EXCLUDE_SUBDIRS",      "misc").split(",")
DEFAULT_CACHE_FILE      = Path(os.environ.get("CACHE_FILE",      str(Path(__file__).parent / "cache.json")))
DEFAULT_OUTPUT_FILE     = Path(os.environ.get("OUTPUT_FILE",     "/volume1/docker/nginx/conf/photomaps/maps.html"))

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif",
    ".raw", ".arw", ".cr2", ".cr3", ".nef", ".orf", ".rw2", ".dng",
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".mts", ".m2ts",
}

# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def is_excluded(path: Path, exclude_dirs: list) -> bool:
    for excl in exclude_dirs:
        try:
            path.relative_to(excl)
            return True
        except ValueError:
            pass
    return False


def collect_files(photo_root: Path, exclude_dirs: list, known_paths: set = None, no_gps_paths: set = None) -> list:
    known_paths  = known_paths  or set()
    no_gps_paths = no_gps_paths or set()
    incremental  = bool(known_paths or no_gps_paths)
    print(f"[*] Scanning {photo_root} ({'incremental' if incremental else 'all'}) ...")
    files = []
    for f in photo_root.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        if is_excluded(f, exclude_dirs):
            continue
        if str(f) in known_paths:
            continue
        files.append(f)
    print(f"[*] Found {len(files):,} files to scan ({len(no_gps_paths):,} no-GPS re-checked)")
    return files


# ---------------------------------------------------------------------------
# GPS extraction
# ---------------------------------------------------------------------------

def extract_gps_data(files: list, cache_path: Path = None,
                     existing_records: list = None, existing_no_gps: list = None,
                     batch_size: int = 2000) -> tuple:
    print(f"[*] Extracting GPS metadata ({len(files):,} files, batch={batch_size}) ...")
    all_records = list(existing_records or [])
    all_no_gps  = list(existing_no_gps  or [])
    list_file   = Path("/tmp/photomaps_files.txt")
    total_batches = math.ceil(len(files) / batch_size) if files else 0

    for batch_num, i in enumerate(range(0, len(files), batch_size), 1):
        batch = files[i:i + batch_size]
        print(f"[*] Batch {batch_num}/{total_batches} ({len(batch)} files)...")
        list_file.write_text("\n".join(str(f) for f in batch))

        cmd = ["exiftool", "-csv", "-GPSLatitude#", "-GPSLongitude#",
               "-DateTimeOriginal", "-CreateDate", "-@", str(list_file)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode not in (0, 1):
            print(f"[!] exiftool error batch {batch_num}: {result.stderr[:300]}", file=sys.stderr)
            continue

        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            continue

        header = [h.strip() for h in lines[0].split(",")]
        idx = {h: i for i, h in enumerate(header)}

        batch_ok = batch_ng = 0
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) < len(header):
                continue
            file_path = parts[idx.get("SourceFile", -1)].strip() if "SourceFile" in idx else ""
            try:
                lat_str = parts[idx.get("GPSLatitude#", idx.get("GPSLatitude", -1))].strip()
                lon_str = parts[idx.get("GPSLongitude#", idx.get("GPSLongitude", -1))].strip()
                if not lat_str or not lon_str:
                    raise ValueError
                lat, lon = float(lat_str), float(lon_str)
                if not (-90 <= lat <= 90) or not (-180 <= lon <= 180) or (lat == 0.0 and lon == 0.0):
                    raise ValueError
            except (ValueError, IndexError):
                all_no_gps.append(file_path)
                batch_ng += 1
                continue

            raw_date = ""
            for field in ("DateTimeOriginal", "CreateDate"):
                if field in idx:
                    val = parts[idx[field]].strip()
                    if val and val != "0000:00:00 00:00:00":
                        raw_date = val
                        break
            date_str = ""
            if raw_date:
                try:
                    dt = datetime.strptime(raw_date[:19], "%Y:%m:%d %H:%M:%S")
                    # Sanity check: reject years far in the future
                    if dt.year > datetime.now().year + 1:
                        raise ValueError
                    date_str = dt.strftime("%Y-%m-%d")
                except ValueError:
                    pass

            all_records.append({"lat": lat, "lon": lon, "date": date_str, "path": file_path})
            batch_ok += 1

        print(f"    -> {batch_ok} with GPS, {batch_ng} without (total: {len(all_records):,} / {len(all_no_gps):,})")
        if cache_path:
            save_cache(all_records, cache_path, all_no_gps)

    print(f"[*] Done: {len(all_records):,} with GPS, {len(all_no_gps):,} without")
    return all_records, all_no_gps


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def save_cache(records: list, cache_path: Path, no_gps: list = None) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "generated_at": datetime.now().isoformat(),
        "scan_mtime":   datetime.now().timestamp(),
        "count":        len(records),
        "records":      records,
        "no_gps":       no_gps or [],
    }
    cache_path.write_text(json.dumps(data, ensure_ascii=False))
    print(f"[*] Cache saved -> {cache_path} ({len(records):,} GPS / {len(no_gps or []):,} no-GPS)")


def load_cache(cache_path: Path) -> tuple:
    print(f"[*] Loading cache from {cache_path} ...")
    data      = json.loads(cache_path.read_text())
    records   = data["records"]
    no_gps    = data.get("no_gps", [])
    scan_mtime = data.get("scan_mtime", 0.0)
    print(f"[*] {len(records):,} with GPS, {len(no_gps):,} without (generated {data.get('generated_at','?')})")
    return records, no_gps, scan_mtime


def merge_records(existing: list, existing_no_gps: list,
                  new_records: list, new_no_gps: list) -> tuple:
    known_paths  = {r["path"] for r in existing}
    added        = [r for r in new_records if r["path"] not in known_paths]
    fixed_paths  = {r["path"] for r in new_records}
    merged_no_gps = [p for p in existing_no_gps if p not in fixed_paths] + new_no_gps
    print(f"[*] Merge: +{len(added):,} new GPS records")
    return existing + added, list(set(merged_no_gps))


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def cluster_points(records: list, radius_km: float = 0.1) -> list:
    print(f"[*] Clustering {len(records):,} points (radius={radius_km} km)...")

    def haversine(lat1, lon1, lat2, lon2):
        R = 6371
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat/2)**2
             + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
             * math.sin(dlon/2)**2)
        return R * 2 * math.asin(math.sqrt(a))

    clusters = []
    assigned = [False] * len(records)
    for i, rec in enumerate(records):
        if assigned[i]:
            continue
        cluster = {"lat": rec["lat"], "lon": rec["lon"], "count": 1,
                   "dates": {rec["date"]} if rec["date"] else set()}
        assigned[i] = True
        for j in range(i + 1, len(records)):
            if assigned[j]:
                continue
            if haversine(rec["lat"], rec["lon"], records[j]["lat"], records[j]["lon"]) <= radius_km:
                cluster["count"] += 1
                if records[j]["date"]:
                    cluster["dates"].add(records[j]["date"])
                assigned[j] = True
        clusters.append(cluster)

    print(f"[*] {len(clusters):,} clusters generated")
    return clusters


# ---------------------------------------------------------------------------
# Map generation
# ---------------------------------------------------------------------------

def build_map(clusters: list) -> folium.Map:
    print(f"[*] Building map ({len(clusters):,} clusters)...")

    m = folium.Map(location=[20, 0], zoom_start=3, tiles=None, control_scale=True)

    # Tile layers (last = default active)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Topographie").add_to(m)
    folium.TileLayer("OpenStreetMap",        name="OpenStreetMap").add_to(m)
    folium.TileLayer("CartoDB dark_matter",  name="Sombre").add_to(m)
    folium.TileLayer("CartoDB positron",     name="Clair").add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Satellite").add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    # Build cluster data for JS
    clusters_data = []
    all_years = set()
    for c in clusters:
        dates = sorted(c["dates"])
        years = sorted({d[:4] for d in dates if d})
        for y in years:
            all_years.add(int(y))
        date_tree = {}
        for d in dates:
            if not d:
                continue
            y, mo, day = d[:4], d[5:7], d[8:10]
            date_tree.setdefault(y, {}).setdefault(mo, []).append(day)
        clusters_data.append({
            "lat":       c["lat"],
            "lon":       c["lon"],
            "count":     c["count"],
            "years":     years,
            "dates":     dates,
            "date_tree": date_tree,
        })

    min_year     = min(all_years) if all_years else 1990
    max_year     = max(all_years) if all_years else datetime.now().year
    clusters_json = json.dumps(clusters_data)

    html_css = """
    <style>
        body { background-color: #1a1a1a !important; }
        .leaflet-container { background-color: #1a1a1a !important; }
        #slider-panel {
            position: fixed; bottom: 30px; left: 50%;
            transform: translateX(-50%); z-index: 1000;
            background: #2a2a2a; color: #eee;
            padding: 12px 20px; border-radius: 10px;
            box-shadow: 0 4px 12px rgba(0,0,0,.6);
            font-family: -apple-system, BlinkMacSystemFont, sans-serif;
            font-size: 13px; display: flex; align-items: center;
            gap: 12px; min-width: 360px; user-select: none;
        }
        #year-range-display { font-weight: 700; color: #fff; min-width: 90px; text-align: center; }
        #range-track-wrap {
            position: relative; flex: 1; height: 20px; cursor: pointer;
        }
        #range-track {
            position: absolute; top: 8px; height: 4px;
            width: 100%; background: #555; border-radius: 2px;
        }
        #range-fill {
            position: absolute; top: 8px; height: 4px;
            background: #e74c3c; border-radius: 2px;
        }
        .range-thumb {
            position: absolute; top: 2px;
            width: 16px; height: 16px; margin-left: -8px;
            border-radius: 50%; background: #e74c3c;
            border: 2px solid #fff; cursor: grab; z-index: 5;
            box-shadow: 0 1px 4px rgba(0,0,0,.5);
        }
        .range-thumb:active { cursor: grabbing; }
        #legend-panel {
            position: fixed; bottom: 105px; left: 30px; z-index: 1000;
            background: #2a2a2a; color: #eee;
            padding: 10px 15px; border-radius: 8px;
            box-shadow: 2px 2px 6px rgba(0,0,0,.5);
            font-family: -apple-system, BlinkMacSystemFont, sans-serif; font-size: 13px;
        }
    </style>
    <div id="legend-panel">
        <b>&#128247; photomaps</b><br>
        <span style="color:#e74c3c">&#9679;</span> &ge; 100 photos<br>
        <span style="color:#e67e22">&#9679;</span> &ge; 20 photos<br>
        <span style="color:#2980b9">&#9679;</span> &ge; 5 photos<br>
        <span style="color:#7f8c8d">&#9679;</span> &lt; 5 photos
    </div>
    <div id="slider-panel">
        <b>&#128197;</b>
        <div id="year-range-display">""" + str(min_year) + """ &ndash; """ + str(max_year) + """</div>
        <div id="range-track-wrap">
            <div id="range-track"></div>
            <div id="range-fill"></div>
            <div class="range-thumb" id="thumb-min"></div>
            <div class="range-thumb" id="thumb-max"></div>
        </div>
        <button onclick="resetSlider()" style="background:#444;color:#eee;border:none;border-radius:5px;padding:4px 8px;cursor:pointer;font-size:12px;">Reset</button>
    </div>
    """

    js_code = """
    <script>
    var CLUSTERS  = """ + clusters_json + """;
    var MIN_YEAR  = """ + str(min_year) + """;
    var MAX_YEAR  = """ + str(max_year) + """;
    var curMinY   = MIN_YEAR;
    var curMaxY   = MAX_YEAR;
    var heatLayer = null;
    var map       = null;
    var markerGroup = null;
    var MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

    function getColor(n) {
        if (n >= 100) return '#e74c3c';
        if (n >= 20)  return '#e67e22';
        if (n >= 5)   return '#2980b9';
        return '#7f8c8d';
    }
    function getSize(n) {
        if (n >= 1000) return 52;
        if (n >= 100)  return 44;
        if (n >= 20)   return 38;
        if (n >= 5)    return 32;
        return 28;
    }

    function buildPopup(c, minY, maxY) {
        var tree  = c.date_tree;
        var years = Object.keys(tree).filter(function(y) {
            return parseInt(y) >= minY && parseInt(y) <= maxY;
        }).sort();
        if (years.length === 0) {
            return '<b>' + c.count + ' photo(s)</b><br><i>No date</i>';
        }
        var h = '<div style="font-family:-apple-system,sans-serif;font-size:13px;min-width:200px;">';
        h += '<b>' + c.count + ' photo(s)</b><br><br>';

        // Single date: direct link, no tree
        var allDates = [];
        years.forEach(function(y) {
            Object.keys(tree[y]).sort().forEach(function(mo) {
                tree[y][mo].sort().forEach(function(day) {
                    allDates.push(y + '-' + mo + '-' + day);
                });
            });
        });

        if (allDates.length === 1) {
            var url = 'https://photos.google.com/search/' + allDates[0];
            h += '<a href="' + url + '" target="_blank" style="color:#3498db;">&#128197; ' + allDates[0] + '</a>';
        } else {
            years.forEach(function(y) {
                var months = Object.keys(tree[y]).sort();
                var totalDays = 0;
                months.forEach(function(mo) { totalDays += tree[y][mo].length; });

                h += '<details style="margin-bottom:4px;">';
                h += '<summary style="cursor:pointer;font-weight:600;color:#2980b9;">' + y + ' (' + totalDays + ')</summary>';
                h += '<div style="padding-left:10px;">';
                months.forEach(function(mo) {
                    h += '<details style="margin:2px 0;">';
                    h += '<summary style="cursor:pointer;color:#e67e22;">' + MONTHS[parseInt(mo)-1] + ' (' + tree[y][mo].length + ')</summary>';
                    h += '<div style="padding-left:10px;">';
                    tree[y][mo].sort().forEach(function(day) {
                        var date = y + '-' + mo + '-' + day;
                        var url  = 'https://photos.google.com/search/' + date;
                        h += '<a href="' + url + '" target="_blank" style="display:block;padding:2px 0;color:#3498db;">&#128197; ' + date + '</a>';
                    });
                    h += '</div></details>';
                });
                h += '</div></details>';
            });
        }
        h += '</div>';
        return h;
    }

    function makeMarker(c, count, minY, maxY) {
        var color = getColor(count);
        var size  = getSize(count);
        var icon  = L.divIcon({
            html: '<div style="background:' + color + ';color:white;border-radius:50%;' +
                  'width:' + size + 'px;height:' + size + 'px;' +
                  'display:flex;align-items:center;justify-content:center;' +
                  'font-weight:700;font-size:11px;' +
                  'font-family:-apple-system,BlinkMacSystemFont,sans-serif;">' + count + '</div>',
            className: '', iconSize: [size, size], iconAnchor: [size/2, size/2],
        });
        var mk = L.marker([c.lat, c.lon], {icon: icon, photoCount: count});
        mk.bindPopup(buildPopup(c, minY, maxY), {maxWidth: 280});
        mk.bindTooltip(count + ' photo(s)');
        return mk;
    }

    function filterByYears(minY, maxY) {
        markerGroup.clearLayers();
        var heatData = [];
        CLUSTERS.forEach(function(c) {
            var filteredYears = c.years.filter(function(y) {
                return parseInt(y) >= minY && parseInt(y) <= maxY;
            });
            if (filteredYears.length === 0) return;
            var ratio         = filteredYears.length / (c.years.length || 1);
            var filteredCount = Math.max(1, Math.round(c.count * ratio));
            markerGroup.addLayer(makeMarker(c, filteredCount, minY, maxY));
            heatData.push([c.lat, c.lon, Math.min(filteredCount / 10, 1.0)]);
        });
        if (heatLayer) { map.removeLayer(heatLayer); }
        heatLayer = L.heatLayer(heatData, {
            radius: 18, blur: 25, minOpacity: 0.3,
            gradient: {0.2: 'blue', 0.5: 'lime', 0.8: 'orange', 1.0: 'red'}
        }).addTo(map);
    }

    // --- Custom dual-thumb slider ---
    function pct(year) {
        return (year - MIN_YEAR) / ((MAX_YEAR - MIN_YEAR) || 1);
    }
    function yearFromPct(p) {
        return Math.round(MIN_YEAR + p * (MAX_YEAR - MIN_YEAR));
    }
    function updateSliderUI() {
        var left  = pct(curMinY) * 100;
        var right = pct(curMaxY) * 100;
        document.getElementById('range-fill').style.left  = left + '%';
        document.getElementById('range-fill').style.width = (right - left) + '%';
        document.getElementById('thumb-min').style.left   = left + '%';
        document.getElementById('thumb-max').style.left   = right + '%';
        document.getElementById('year-range-display').textContent = curMinY + ' - ' + curMaxY;
        filterByYears(curMinY, curMaxY);
    }
    function resetSlider() {
        curMinY = MIN_YEAR; curMaxY = MAX_YEAR;
        updateSliderUI();
    }
    function initSlider() {
        var wrap = document.getElementById('range-track-wrap');
        function getPct(e) {
            var rect = wrap.getBoundingClientRect();
            var x    = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
            return Math.max(0, Math.min(1, x / rect.width));
        }
        function dragThumb(thumb, isMin) {
            function onMove(e) {
                e.preventDefault();
                var p    = getPct(e);
                var year = yearFromPct(p);
                if (isMin) {
                    curMinY = Math.min(year, curMaxY);
                } else {
                    curMaxY = Math.max(year, curMinY);
                }
                updateSliderUI();
            }
            function onUp() {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup',  onUp);
                document.removeEventListener('touchmove', onMove);
                document.removeEventListener('touchend',  onUp);
            }
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup',   onUp);
            document.addEventListener('touchmove', onMove, {passive: false});
            document.addEventListener('touchend',  onUp);
        }
        document.getElementById('thumb-min').addEventListener('mousedown',  function(e) { e.preventDefault(); dragThumb(this, true); });
        document.getElementById('thumb-max').addEventListener('mousedown',  function(e) { e.preventDefault(); dragThumb(this, false); });
        document.getElementById('thumb-min').addEventListener('touchstart', function(e) { e.preventDefault(); dragThumb(this, true); }, {passive: false});
        document.getElementById('thumb-max').addEventListener('touchstart', function(e) { e.preventDefault(); dragThumb(this, false); }, {passive: false});
        updateSliderUI();
    }

    function initMap() {
        setTimeout(function() {
            map = Object.values(window).find(function(v) {
                return v && typeof v === 'object' && v._container && v.eachLayer;
            });
            if (!map) { setTimeout(initMap, 200); return; }
            markerGroup = L.markerClusterGroup({
                iconCreateFunction: function(cluster) {
                    var total = 0;
                    cluster.getAllChildMarkers().forEach(function(mk) {
                        total += (mk.options.photoCount || 1);
                    });
                    var size  = total >= 1000 ? 52 : total >= 100 ? 44 : total >= 20 ? 38 : total >= 5 ? 32 : 28;
                    var color = total >= 1000 ? '#c0392b' : total >= 100 ? '#e74c3c' : total >= 20 ? '#e67e22' : total >= 5 ? '#2980b9' : '#7f8c8d';
                    return L.divIcon({
                        html: '<div style="background:' + color + ';color:white;border-radius:50%;' +
                              'width:' + size + 'px;height:' + size + 'px;' +
                              'display:flex;align-items:center;justify-content:center;' +
                              'font-weight:700;font-size:12px;' +
                              'font-family:-apple-system,BlinkMacSystemFont,sans-serif;">' + total + '</div>',
                        className: '', iconSize: [size, size],
                    });
                },
                maxClusterRadius: 40,
            });
            markerGroup.addTo(map);
            initSlider();
            map.on('baselayerchange', function(e) {
                if (e.layer && e.layer._url) localStorage.setItem('photomaps_layer', e.layer._url);
            });
        }, 300);
    }

    function loadScript(url, cb) {
        var s = document.createElement('script');
        s.src = url; s.onload = cb;
        document.head.appendChild(s);
    }

    document.addEventListener('DOMContentLoaded', function() {
        loadScript(
            'https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.min.js',
            function() {
                if (typeof L.heatLayer === 'undefined') {
                    loadScript('https://cdnjs.cloudflare.com/ajax/libs/leaflet.heat/0.2.0/leaflet-heat.js', initMap);
                } else {
                    initMap();
                }
            }
        );
    });
    </script>
    """

    m.get_root().header.add_child(folium.Element("""
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.css"/>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.Default.css"/>
    """))

    m.get_root().html.add_child(folium.Element(html_css))
    m.get_root().html.add_child(folium.Element(js_code))
    return m


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="photomaps - GPS photo heatmap generator")
    parser.add_argument("--path",         type=Path, default=DEFAULT_PHOTO_ROOT)
    parser.add_argument("--output",       type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--cache",        type=Path, default=DEFAULT_CACHE_FILE)
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--from-cache",   action="store_true")
    parser.add_argument("--incremental",  action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print("  photomaps - GPS photo heatmap generator")

    exclude_dirs = (
        [DEFAULT_PHOTO_ROOT / s for s in DEFAULT_EXCLUDE_SUBDIRS]
        if args.path == DEFAULT_PHOTO_ROOT else []
    )

    if args.from_cache:
        if not args.cache.exists():
            print(f"[!] Cache not found: {args.cache}", file=sys.stderr); sys.exit(1)
        print(f"  Mode   : from cache\n  Cache  : {args.cache}\n  Output : {args.output}")
        print("=" * 60)
        records, no_gps, _ = load_cache(args.cache)

    elif args.incremental:
        if not args.cache.exists():
            print(f"[!] Cache not found: {args.cache}", file=sys.stderr); sys.exit(1)
        print(f"  Mode   : incremental\n  Source : {args.path}\n  Cache  : {args.cache}\n  Output : {args.output}")
        print("=" * 60)
        existing, existing_no_gps, _ = load_cache(args.cache)
        known_paths = {r["path"] for r in existing}
        files = collect_files(args.path, exclude_dirs,
                              known_paths=known_paths, no_gps_paths=set(existing_no_gps))
        if files:
            new_records, new_no_gps = extract_gps_data(
                files, cache_path=args.cache,
                existing_records=existing, existing_no_gps=existing_no_gps)
            records, no_gps = merge_records(existing, existing_no_gps, new_records, new_no_gps)
            save_cache(records, args.cache, no_gps)
        else:
            records, no_gps = existing, existing_no_gps

    elif args.extract_only:
        print(f"  Mode   : extract only\n  Source : {args.path}\n  Cache  : {args.cache}")
        print("=" * 60)
        files = collect_files(args.path, exclude_dirs)
        if not files:
            print("[!] No media files found.", file=sys.stderr); sys.exit(1)
        records, no_gps = extract_gps_data(files, cache_path=args.cache)
        if not records:
            print("[!] No GPS data found.", file=sys.stderr); sys.exit(1)
        save_cache(records, args.cache, no_gps)
        print("[*] Done. Run --from-cache to generate the map.")
        print("=" * 60)
        return

    else:
        print(f"  Mode   : full scan\n  Source : {args.path}\n  Cache  : {args.cache}\n  Output : {args.output}")
        print("=" * 60)
        files = collect_files(args.path, exclude_dirs)
        if not files:
            print("[!] No media files found.", file=sys.stderr); sys.exit(1)
        records, no_gps = extract_gps_data(files, cache_path=args.cache)
        if not records:
            print("[!] No GPS data found.", file=sys.stderr); sys.exit(1)
        save_cache(records, args.cache, no_gps)

    clusters = cluster_points(records)
    m = build_map(clusters)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(args.output))
    print(f"[*] Map saved -> {args.output}")
    print(f"[*] Done. {len(records):,} geotagged files -> {len(clusters):,} clusters.")
    print("=" * 60)


if __name__ == "__main__":
    main()
