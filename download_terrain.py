import os
import urllib.request
import time
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# NZ Land Bounding Boxes (Conservative approximations for ocean skipping)
# Format: (lat_min, lat_max, lon_min, lon_max)
NZ_LAND_BBOXES = [
    (-41.7, -34.0, 172.5, 178.6),  # North Island
    (-47.3, -40.5, 166.4, 174.5),  # South Island
    (-47.3, -46.6, 167.3, 168.3),  # Stewart Island
]

def is_land(lat, lon):
    """Check if a coordinate is within the simplified NZ land boxes."""
    for (la_min, la_max, lo_min, lo_max) in NZ_LAND_BBOXES:
        if la_min <= lat <= la_max and lo_min <= lon <= lo_max:
            return True
    return False

def deg2num(lat_deg, lon_deg, zoom):
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return (xtile, ytile)

def num2deg(xtile, ytile, zoom):
    """Convert tile coordinates back to lat/lon (top-left)."""
    n = 2.0 ** zoom
    lon_deg = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ytile / n)))
    lat_deg = math.degrees(lat_rad)
    return (lat_deg, lon_deg)

def download_tile(url, path_file, req_headers):
    """Worker function to download a single tile."""
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=10) as response, open(path_file, 'wb') as out_file:
            out_file.write(response.read())
        return True
    except Exception as e:
        # Silently fail for individual tiles, retry might be needed for a production tool
        return False

def download_terrain_bbox(zoom, lat_min, lat_max, lon_min, lon_max, max_workers=16):
    # Top-Left (max lat, min lon)
    x_min, y_min = deg2num(lat_max, lon_min, zoom)
    # Bottom-Right (min lat, max lon)
    x_max, y_max = deg2num(lat_min, lon_max, zoom)
    
    # Ensure min/max ordering
    x_min, x_max = min(x_min, x_max), max(x_min, x_max)
    y_min, y_max = min(y_min, y_max), max(y_min, y_max)

    base_url = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
    base_dir = os.path.join(os.path.dirname(__file__), "terrain_data")
    req_headers = {'User-Agent': 'MilRadio/1.0 Tactical Simulator (Accelerated Terrain Downloader)'}
    
    # 1. Generate task list and apply Land Mask
    tasks = []
    skipped_ocean = 0
    already_cached = 0
    
    print(f"\nZoom Level {zoom}: Analyzing tiles for NZ landmass...")
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            # Check if land
            lat, lon = num2deg(x, y, zoom)
            if not is_land(lat, lon):
                skipped_ocean += 1
                continue
            
            path_file = os.path.join(base_dir, str(zoom), str(x), f"{y}.png")
            if os.path.exists(path_file):
                already_cached += 1
                continue
                
            tasks.append((x, y, path_file))
            
    total_land_tiles = len(tasks) + already_cached
    print(f"-> Total Land Tiles: {total_land_tiles}")
    print(f"-> Already Cached:   {already_cached}")
    print(f"-> Skipped Ocean:    {skipped_ocean}")
    
    if not tasks:
        print("-> All land tiles for this zoom level are already cached.")
        return

    # 2. Parallel Download
    print(f"-> Downloading {len(tasks)} new tiles using {max_workers} threads...")
    
    start_time = time.time()
    count = 0
    errors = 0
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for x, y, path_file in tasks:
            os.makedirs(os.path.dirname(path_file), exist_ok=True)
            url = base_url.format(z=zoom, x=x, y=y)
            futures[executor.submit(download_tile, url, path_file, req_headers)] = (x, y)
            
        for future in as_completed(futures):
            count += 1
            if not future.result():
                errors += 1
            
            if count % 100 == 0 or count == len(tasks):
                elapsed = time.time() - start_time
                tiles_per_sec = count / elapsed
                remaining = len(tasks) - count
                eta_sec = remaining / tiles_per_sec if tiles_per_sec > 0 else 0
                
                eta_str = time.strftime("%H:%M:%S", time.gmtime(eta_sec))
                print(f"[{count}/{len(tasks)}] {tiles_per_sec:.1f} tiles/sec | ETA: {eta_str} | Errors: {errors}")

    print(f"Done. Processed {count} tiles in {time.time() - start_time:.1f}s.")

if __name__ == "__main__":
    # New Zealand Bounding Box (Initial request area)
    lat_min, lat_max = -48.0, -34.0
    lon_min, lon_max = 166.0, 179.0
    
    print("====================================================")
    print("   Tactical Radio Simulation - Accelerated Downloader")
    print("====================================================")
    print("Optimizations Active: Multi-threading (16x), Ocean Masking")
    
    target_zooms = [10, 12, 14]
    
    for z in target_zooms:
        download_terrain_bbox(z, lat_min, lat_max, lon_min, lon_max)
        
    print("\nSUCCESS: Phase 3 Terrain Data sync complete.")
