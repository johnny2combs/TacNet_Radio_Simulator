import os
import urllib.request
import time
import math

def deg2num(lat_deg, lon_deg, zoom):
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return (xtile, ytile)

def download_tiles_bbox(zoom, lat_min, lat_max, lon_min, lon_max):
    # Top-Left (max lat, min lon)
    x_min, y_min = deg2num(lat_max, lon_min, zoom)
    # Bottom-Right (min lat, max lon)
    x_max, y_max = deg2num(lat_min, lon_max, zoom)
    
    # Ensure min/max ordering
    x_min, x_max = min(x_min, x_max), max(x_min, x_max)
    y_min, y_max = min(y_min, y_max), max(y_min, y_max)

    base_url = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    base_dir = os.path.join(os.path.dirname(__file__), "offline_map_data")
    req_headers = {'User-Agent': 'MilRadio/1.0 Tactical Simulator (Offline Map Downloader)'}
    
    total = (x_max - x_min + 1) * (y_max - y_min + 1)
    count = 0
    skips = 0
    
    print(f"Zoom Level {zoom}: {total} tiles covering New Zealand...")
    
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            url = base_url.format(z=zoom, x=x, y=y)
            path_dir = os.path.join(base_dir, str(zoom), str(x))
            path_file = os.path.join(path_dir, f"{y}.png")
            
            if os.path.exists(path_file):
                count += 1
                skips += 1
                continue
                
            os.makedirs(path_dir, exist_ok=True)
            
            try:
                req = urllib.request.Request(url, headers=req_headers)
                with urllib.request.urlopen(req) as response, open(path_file, 'wb') as out_file:
                    out_file.write(response.read())
                
                count += 1
                if count % 10 == 0:
                    print(f"[{count}/{total}] Downloaded {zoom}/{x}/{y}")
                time.sleep(0.05)  # Slightly faster but still respectful
            except Exception as e:
                print(f"Failed to download {zoom}/{x}/{y}: {e}")
                
    if skips > 0:
         print(f"-> Skipped {skips} tiles (already downloaded).")

if __name__ == "__main__":
    # New Zealand Bounding Box
    lat_min, lat_max = -48.0, -34.0
    lon_min, lon_max = 166.0, 179.0
    
    print("Starting map tile download...")
    
    # Zoom 0 to 4: The whole world (very few tiles)
    print("Fetching World View (Zoom 0-4)...")
    for z in range(0, 5):
        download_tiles_bbox(z, -85.0, 85.0, -180.0, 180.0)

    # Zoom 5 through 10: New Zealand specifically
    print("Fetching NZ Detail (Zoom 5-10)...")
    for z in range(5, 11):
        download_tiles_bbox(z, lat_min, lat_max, lon_min, lon_max)
        
    print("Map Tiles Ready! You have World View (0-4) and NZ Detail (5-10).")
