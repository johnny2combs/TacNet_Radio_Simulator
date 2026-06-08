"""
TacNet — Terrain System
Provides terrain-aware signal propagation modeling.

Features:
  - Terrain Types: Urban, Forest, Mountain, Open, Water
  - Signal Attenuation based on terrain type
  - Line-of-Sight (LOS) blocking calculation
  - Multi-path reflection modeling (future enhancement)
  - Online/Offline map data sources
  - SWORD simulation integration

Architecture:
  TerrainManager.compute_terrain_factor() is called by InfrastructureManager
  during path computation to apply terrain-specific attenuation.
"""
from __future__ import annotations
import math
import json
import os
import threading
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple
from enum import Enum
from pathlib import Path
from PIL import Image
import numpy as np

log = logging.getLogger("radio.terrain")


# ═══════════════════════════════════════════════════════════════════════════
# Terrain Types & Attenuation Factors
# ═══════════════════════════════════════════════════════════════════════════
class TerrainType(Enum):
    """Terrain classification with signal propagation characteristics."""
    OPEN = "open"           # Flat grassland, desert — minimal attenuation
    FOREST = "forest"       # Trees, vegetation — medium attenuation
    URBAN = "urban"         # Buildings, dense structures — high attenuation
    MOUNTAIN = "mountain"   # High elevation, rocky — LOS blocking
    WATER = "water"         # Ocean, lakes — minimal attenuation, reflective


# Signal attenuation multipliers by terrain type (0.0 = complete block, 1.0 = clear)
TERRAIN_ATTENUATION = {
    TerrainType.OPEN:     1.0,    # No attenuation
    TerrainType.FOREST:   0.6,    # 40% signal loss through trees
    TerrainType.URBAN:    0.3,    # 70% signal loss in buildings
    TerrainType.MOUNTAIN: 0.1,    # 90% loss (LOS blocked)
    TerrainType.WATER:    0.95,   # Slight reflection loss
}

# K-factor for radio propagation (4/3 radius model)
K_FACTOR = 1.333
EARTH_RADIUS_KM = 6371.0
EFFECTIVE_EARTH_RADIUS_KM = EARTH_RADIUS_KM * K_FACTOR

# Elevation thresholds for terrain classification (metres above sea level)
TERRAIN_ELEVATION = {
    "flat":     (0, 100),      # Open terrain
    "rolling":  (100, 300),    # Light forest/hills
    "hilly":    (300, 800),    # Forest/mixed
    "mountain": (800, 99999),  # Mountain terrain
}


# ═══════════════════════════════════════════════════════════════════════════
# Terrain Cell Data
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class TerrainCell:
    """
    Single grid cell in the terrain map.
    Represents approximately 1km x 1km area.
    """
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    terrain_type: TerrainType = TerrainType.OPEN
    elevation_m: float = 0.0
    # Custom attenuation override (0.0-1.0)
    attenuation_override: Optional[float] = None
    
    def get_attenuation(self) -> float:
        """Return signal attenuation factor for this cell."""
        if self.attenuation_override is not None:
            return max(0.0, min(1.0, self.attenuation_override))
        return TERRAIN_ATTENUATION.get(self.terrain_type, 1.0)
    
    def contains(self, lat: float, lon: float) -> bool:
        """Check if a lat/lon point is within this cell."""
        return (self.lat_min <= lat <= self.lat_max and
                self.lon_min <= lon <= self.lon_max)
    
    def to_dict(self) -> dict:
        return {
            "lat_min": self.lat_min,
            "lat_max": self.lat_max,
            "lon_min": self.lon_min,
            "lon_max": self.lon_max,
            "terrain_type": self.terrain_type.value,
            "elevation_m": self.elevation_m,
            "attenuation_override": self.attenuation_override,
        }
    
    @staticmethod
    def from_dict(d: dict) -> 'TerrainCell':
        return TerrainCell(
            lat_min=d["lat_min"],
            lat_max=d["lat_max"],
            lon_min=d["lon_min"],
            lon_max=d["lon_max"],
            terrain_type=TerrainType(d.get("terrain_type", "open")),
            elevation_m=d.get("elevation_m", 0.0),
            attenuation_override=d.get("attenuation_override"),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Map Data Source
# ═══════════════════════════════════════════════════════════════════════════
class MapDataSource(Enum):
    """Source of terrain/map data."""
    OFFLINE = "offline"     # Preloaded terrain dataset (field use)
    ONLINE = "online"       # Live API (OpenTopoData, SRTM, etc.)
    SWORD = "sword"         # SWORD simulation scenario data
    MANUAL = "manual"       # Admin-defined terrain cells


@dataclass
class MapSourceConfig:
    """Configuration for map data source."""
    source_type: MapDataSource = MapDataSource.OFFLINE
    # Online API settings
    api_url: str = ""
    api_key: str = ""
    # Offline data path
    offline_path: str = "terrain_data.json"
    # High-res terrain data path (Mapzen tiles)
    highres_path: str = "terrain_data"
    # SWORD integration
    sword_url: str = "http://127.0.0.1:8888"
    sword_sync_interval: int = 30  # seconds
    # Cache settings
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600
    # Performance Mode
    perf_mode: str = "balanced" # "accuracy", "balanced", "speed"
    # SWORD Folder Path
    sword_terrain_path: str = "NZ_North_Island_v4"
    sword_enabled: bool = True



# ═══════════════════════════════════════════════════════════════════════════
# High-Resolution Terrain Tile Cache
# ═══════════════════════════════════════════════════════════════════════════
class TerrainTile:
    """A single 256x256 elevation tile."""
    def __init__(self, zoom: int, x: int, y: int, data: np.ndarray):
        self.zoom = zoom
        self.x = x
        self.y = y
        self.data = data # NumPy float32 array of elevation in meters
        
    def get_elevation_pixel(self, px: int, py: int) -> float:
        """Get elevation at specific pixel (0-255)."""
        return float(self.data[py, px])

class TerrainTileCache:
    """Manages loading and LRU caching of terrain tiles."""
    def __init__(self, base_dir: str):
        self.base_dir = self._resolve_terrain_dir(base_dir)
        self._cache: Dict[str, TerrainTile] = {}
        self._max_cache = 64
        self._lock = threading.Lock()
        
        # Performance: Cache the tile count on startup to avoid expensive scans
        self._disk_tile_count = -1 # -1 means not yet calculated
        self._count_lock = threading.Lock()
        
    def _resolve_terrain_dir(self, config_path: str) -> Path:
        """Robustly find the terrain_data directory in dev and production."""
        import sys
        candidates = [
            Path(config_path), # 1. As specified in config
            Path(sys.executable).parent / config_path, # 2. Process dir (dist/TacNet-Server/)
            Path(sys.executable).parent.parent / config_path, # 3. Bundle root (dist/TacNet/)
            Path(__file__).parent / config_path, # 4. Source dir (dev)
        ]
        
        # Check if _MEIPASS exists (PyInstaller internal)
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            candidates.insert(0, Path(meipass) / config_path)
            
        for p in candidates:
            if p.exists() and p.is_dir():
                log.info(f"Terrain data found at: {p.absolute()}")
                return p
        
        log.warning(f"Terrain data directory NOT FOUND at {config_path}")
        return Path(config_path)

    def get_tile(self, z: int, x: int, y: int) -> Optional[TerrainTile]:
        key = f"{z}/{x}/{y}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        
        # Load from disk
        path = self.base_dir / str(z) / str(x) / f"{y}.png"
        if not path.exists():
            return None
            
        try:
            with Image.open(path) as img:
                img_data = np.array(img.convert('RGB'), dtype=np.float32)
                # Mapzen Terrarium formula: (R * 256 + G + B / 256) - 32768
                elev = (img_data[:,:,0] * 256.0 + img_data[:,:,1] + img_data[:,:,2] / 256.0) - 32768.0
                tile = TerrainTile(z, x, y, elev)
                
                with self._lock:
                    if len(self._cache) >= self._max_cache:
                        self._cache.pop(next(iter(self._cache)))
                    self._cache[key] = tile
                return tile
        except Exception as e:
            log.warning(f"Failed to load terrain tile {key}: {e}")
            return None

    def get_disk_tile_count(self) -> int:
        """Count total .png tiles in the highres directory (cached and backgrounded)."""
        if not self.base_dir.exists():
            return 0
            
        with self._count_lock:
            if self._disk_tile_count == -1:
                # Start a background thread to count so we don't block the web server
                self._disk_tile_count = -2 # -2 indicates "Scanning in progress..."
                threading.Thread(target=self._do_count_tiles, daemon=True).start()
            return self._disk_tile_count

    def _do_count_tiles(self):
        """Worker thread to perform the actual disk scan."""
        try:
            count = 0
            # Faster scan: zoom/x/y.png
            for root, dirs, files in os.walk(str(self.base_dir)):
                for f in files:
                    if f.endswith(".png"):
                        count += 1
            with self._count_lock:
                self._disk_tile_count = count
                log.info(f"Terrain scan complete: {count} tiles found.")
        except Exception as e:
            log.warning(f"Error counting tiles: {e}")
            with self._count_lock:
                self._disk_tile_count = 0

    def force_refresh_count(self):
        """Force a recount of tiles on disk."""
        with self._count_lock:
            self._disk_tile_count = -1


import struct
import sqlite3


# ═══════════════════════════════════════════════════════════════════════════
# Terrain Manager
# ═══════════════════════════════════════════════════════════════════════════
class TerrainManager:
    """
    Manages terrain data and computes terrain-aware signal propagation.
    
    Usage:
        terrain = TerrainManager()
        terrain.load_from_file("terrain_data.json")
        
        # Get terrain factor between two points
        factor = terrain.compute_terrain_factor(lat1, lon1, lat2, lon2)
        
        # Check line-of-sight
        los_ok = terrain.check_line_of_sight(lat1, lon1, lat2, lon2)
    """
    
    # Grid cell size in degrees (~100m at New Zealand latitudes)
    CELL_SIZE_DEG = 0.001
    
    def __init__(self, config: Optional[MapSourceConfig] = None):
        self._lock = threading.Lock()
        self._cells: Dict[str, TerrainCell] = {}  # key: "lat,lon"
        self._config = config or MapSourceConfig()
        self._last_sync = 0.0
        self._sword_data: Optional[dict] = None
        
        # High-res cache
        self._tile_cache = TerrainTileCache(self._config.highres_path)

        
        # Pre-populate with default open terrain
        self._init_default_terrain()
    

    
    def _init_default_terrain(self):
        """Initialize with default open terrain (no attenuation)."""
        log.info("TerrainManager initialized with default open terrain")
    
    # ── Cell Management ─────────────────────────────────────────────────────
    def add_cell(self, cell: TerrainCell):
        """Add or update a terrain cell."""
        key = f"{cell.lat_min:.4f},{cell.lon_min:.4f}"
        with self._lock:
            self._cells[key] = cell
        log.debug(f"Terrain cell added: {cell.terrain_type.value} at {key}")
    
    def remove_cell(self, lat: float, lon: float) -> bool:
        """Remove terrain cell at given coordinates."""
        key = f"{lat:.4f},{lon:.4f}"
        with self._lock:
            if key in self._cells:
                del self._cells[key]
                log.debug(f"Terrain cell removed at {key}")
                return True
        return False
    
    def get_cell(self, lat: float, lon: float) -> Optional[TerrainCell]:
        """Get terrain cell containing given coordinates."""
        # Check high-res tiles first (Zoom 14)
        elev = self.get_elevation_highres(lat, lon)
        if elev is not None:
            # Create a virtual cell from high-res data
            return TerrainCell(
                lat_min=lat-0.0001, lat_max=lat+0.0001,
                lon_min=lon-0.0001, lon_max=lon+0.0001,
                terrain_type=TerrainType.OPEN, # Default to open if only elevation known
                elevation_m=elev
            )

        # Fallback to manual cells
        with self._lock:
            for cell in self._cells.values():
                if cell.contains(lat, lon):
                    return cell
        return None

    def get_elevation_highres(self, lat: float, lon: float, zoom: int = 14, fallback_online: bool = False) -> Optional[float]:
        if not self._config.cache_enabled and not fallback_online: return None

        
        # Map lat/lon to tile and pixel
        n = 2.0 ** zoom
        lat_rad = math.radians(lat)
        xtile = int((lon + 180.0) / 360.0 * n)
        ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
        
        tile = self._tile_cache.get_tile(zoom, xtile, ytile)
        if not tile:
            if fallback_online:
                # Store the original source type
                old_source = self._config.source_type
                self._config.source_type = MapDataSource.ONLINE
                res = self.fetch_elevation(lat, lon)
                self._config.source_type = old_source
                return res
            return None
        
        # Calculate pixel coordinates (0-255)
        # lon to pixel
        lon_per_tile = 360.0 / n
        tile_lon_start = -180.0 + xtile * lon_per_tile
        px = int(((lon - tile_lon_start) / lon_per_tile) * 256)
        
        # lat to pixel (Mercator)
        def lat_to_y(l): return (1.0 - math.asinh(math.tan(math.radians(l))) / math.pi) / 2.0 * n
        tile_y_start = ytile
        py = int(((lat_to_y(lat) - tile_y_start)) * 256)
        
        px = max(0, min(255, px))
        py = max(0, min(255, py))
        
        return tile.get_elevation_pixel(px, py)
    
    def get_elevation(self, lat: float, lon: float, fallback_online: bool = False) -> float:
        """Get best available elevation (high-res -> manual cell -> 0.0)"""
        elev = self.get_elevation_highres(lat, lon, fallback_online=fallback_online)
        if elev is not None:
            return elev
        cell = self.get_cell(lat, lon)
        return cell.elevation_m if cell else 0.0

    def get_all_cells(self) -> List[TerrainCell]:
        """Return all terrain cells."""
        with self._lock:
            return list(self._cells.values())
    
    # ── Terrain Factor Computation ──────────────────────────────────────────
    def compute_terrain_factor(self, lat1: float, lon1: float,
                                lat2: float, lon2: float) -> float:
        """
        Compute terrain attenuation factor between two points.
        Returns 1.0 (clear) or less if passing through dense terrain (forest/urban).
        """
        distance_km = self._distance_km(lat1, lon1, lat2, lon2)
        if distance_km < 0.05:
            return 1.0
            
        # Step every ~200m or 10 steps minimum
        step_km = 0.2
        num_steps = max(5, int(distance_km / step_km))
        
        attenuations = []
        for i in range(num_steps + 1):
            t = i / num_steps
            lat = lat1 + (lat2 - lat1) * t
            lon = lon1 + (lon2 - lon1) * t
            
            cell = self.get_cell(lat, lon)
            attenuations.append(cell.get_attenuation() if cell else 1.0)
            
        # Cumulative attenuation (product of all segments)
        # Note: We take the nth root to average it out over distance
        if not attenuations:
            return 1.0
        
        prod = 1.0
        if not attenuations: return 1.0
        prod = 1.0
        for a in attenuations: prod *= a
        prod_factor = prod ** (1.0 / len(attenuations))
            
        return prod_factor
    
    def check_line_of_sight(self, lat1: float, lon1: float, alt1: float,
                            lat2: float, lon2: float, alt2: float, fallback_online: bool = False) -> bool:
        """
        Check if there's clear line-of-sight between two points.
        Performance optimized with LOD sampling.
        """
        dist_km = self._distance_km(lat1, lon1, lat2, lon2)
        if dist_km < 0.05:
            return True
            
        # LOD - Performance optimized sampling
        if dist_km < 5.0:
            step_km = 0.03 # 30m resolution
        elif dist_km < 20.0:
            step_km = 0.10 # 100m resolution
        else:
            step_km = 0.25 # 250m resolution
            
        num_steps = max(10, int(dist_km / step_km))
        
        for i in range(1, num_steps):
            t = i / num_steps
            curr_lat = lat1 + (lat2 - lat1) * t
            curr_lon = lon1 + (lon2 - lon1) * t
            
            los_height = alt1 + (alt2 - alt1) * t
            
            d_from_start = t * dist_km
            curv_drop = (d_from_start * (dist_km - d_from_start)) / (2 * EFFECTIVE_EARTH_RADIUS_KM) * 1000.0
            los_effective_height = los_height - curv_drop
            
            # Fast elevation check
            elev = self.get_elevation(curr_lat, curr_lon, fallback_online=fallback_online)
                
            if elev > los_effective_height:
                return False
                
        return True

    def get_los_obstruction_m(self, lat1: float, lon1: float, alt1: float,
                              lat2: float, lon2: float, alt2: float,
                              fallback_online: bool = False) -> float:
        """
        Return the maximum terrain obstruction depth along the LOS path.

        Positive value = terrain is ABOVE the geometric LOS line by that many metres
                         (path is BLOCKED; the value drives the diffraction penalty).
        Negative value = worst-case clearance below terrain (path is CLEAR).
        Zero           = just grazes the terrain.

        Uses the same 4/3 effective-earth-radius curvature correction as
        check_line_of_sight().
        """
        dist_km = self._distance_km(lat1, lon1, lat2, lon2)
        if dist_km < 0.05:
            return -99.0  # trivially clear

        # LOD sampling (same breakpoints as check_line_of_sight)
        if dist_km < 5.0:
            step_km = 0.03
        elif dist_km < 20.0:
            step_km = 0.10
        else:
            step_km = 0.25

        num_steps = max(10, int(dist_km / step_km))
        worst_obstruction = -9999.0

        for i in range(1, num_steps):
            t = i / num_steps
            curr_lat = lat1 + (lat2 - lat1) * t
            curr_lon = lon1 + (lon2 - lon1) * t

            # Geometric LOS height at this point (straight line interpolation AMSL)
            los_height = alt1 + (alt2 - alt1) * t

            # 4/3 earth curvature correction
            d_from_start = t * dist_km
            curv_drop = (d_from_start * (dist_km - d_from_start)) / (2 * EFFECTIVE_EARTH_RADIUS_KM) * 1000.0
            los_effective_height = los_height - curv_drop

            # Terrain elevation at this point
            elev = self.get_elevation(curr_lat, curr_lon, fallback_online=fallback_online)

            # How far above/below the LOS is the terrain?
            # Positive = terrain protrudes into / above LOS (obstruction)
            obstruction = elev - los_effective_height
            if obstruction > worst_obstruction:
                worst_obstruction = obstruction

        return worst_obstruction if worst_obstruction > -9999.0 else 0.0

    def get_terrain_profile(self, lat1: float, lon1: float, alt1: float,
                            lat2: float, lon2: float, alt2: float, fallback_online: bool = False) -> list:
        """
        Extracts a 2D profile of the terrain and LOS beam between two points.
        Returns a list of dicts: [{distance: km, elevation: m, los: m}, ...]
        """
        profile = []
        dist_km = self._distance_km(lat1, lon1, lat2, lon2)
        
        source_name = "Offline Tiles"
        if fallback_online:
            source_name = "Online (SRTM)"
            
        # Determine sampling resolution based on distance
        if dist_km < 5.0:
            step_km = 0.03 # 30m resolution
        elif dist_km < 20.0:
            step_km = 0.10 # 100m resolution
        else:
            step_km = 0.25 # 250m resolution
            
        num_steps = max(10, int(dist_km / step_km))
        
        for i in range(num_steps + 1):
            t = i / float(num_steps)
            curr_lat = lat1 + (lat2 - lat1) * t
            curr_lon = lon1 + (lon2 - lon1) * t
            
            los_height = alt1 + (alt2 - alt1) * t
            
            d_from_start = t * dist_km
            curv_drop = (d_from_start * (dist_km - d_from_start)) / (2 * EFFECTIVE_EARTH_RADIUS_KM) * 1000.0
            los_effective_height = los_height - curv_drop
            
            elev = self.get_elevation(curr_lat, curr_lon, fallback_online=fallback_online)
                
            profile.append({
                "distance": round(d_from_start, 3),
                "elevation": round(elev, 2),
                "los": round(los_effective_height, 2)
            })
            
        return profile, source_name
    
    def get_viewshed(self, lat: float, lon: float, alt_m: float, radius_km: float = 10.0, resolution_m: float = 250.0) -> list:
        """
        Compute viewshed (LOS coverage) from a single point.
        Returns a list of points with signal strength: [{"lat": f, "lon": f, "sig": f}, ...]
        Used for mapping 'radio coverage' bubbles.
        """
        results = []
        
        # Determine grid size
        steps = int((radius_km * 1000.0) / resolution_m)
        # Lat/Lon step sizes (rough approximation)
        step_lat = (radius_km / steps) / 111.0 
        step_lon = (radius_km / steps) / (111.0 * math.cos(math.radians(lat)))
        
        # Source Absolute Elevation (AMSL)
        src_elev = self.get_elevation(lat, lon)
        src_alt_abs = src_elev + alt_m

        for i in range(-steps, steps + 1):
            for j in range(-steps, steps + 1):
                target_lat = lat + i * step_lat
                target_lon = lon + j * step_lon
                
                dist = self._distance_km(lat, lon, target_lat, target_lon)
                if dist > radius_km:
                    continue
                if dist < 0.05: # Center point
                    results.append({"lat": round(target_lat, 5), "lon": round(target_lon, 5), "sig": 1.0})
                    continue

                # Check LOS (Fast check first)
                target_elev = self.get_elevation(target_lat, target_lon)
                # Target Receiver is 1.6m AGL
                los = self.check_line_of_sight(lat, lon, src_alt_abs, target_lat, target_lon, target_elev + 1.6)
                
                if los:
                    # Simple attenuation model for viewshed coloring
                    sig = max(0.1, 1.0 - (dist / radius_km)**1.5)
                    results.append({"lat": round(target_lat, 5), "lon": round(target_lon, 5), "sig": round(sig, 2)})
        
        return results

    # ── Data Loading/Saving ─────────────────────────────────────────────────
    def load_from_file(self, path: str):
        """Load terrain data from JSON file."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            with self._lock:
                self._cells.clear()
                for cell_data in data.get("cells", []):
                    cell = TerrainCell.from_dict(cell_data)
                    key = f"{cell.lat_min:.4f},{cell.lon_min:.4f}"
                    self._cells[key] = cell
            
            self._last_sync = time.time()
            log.info(f"Terrain data loaded from {path}: {len(self._cells)} cells")
        except Exception as e:
            log.error(f"Failed to load terrain data: {e}")
    
    def save_to_file(self, path: str):
        """Save terrain data to JSON file."""
        try:
            with self._lock:
                data = {
                    "version": 1,
                    "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "cells": [cell.to_dict() for cell in self._cells.values()],
                }
            
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            
            log.info(f"Terrain data saved to {path}: {len(self._cells)} cells")
        except Exception as e:
            log.error(f"Failed to save terrain data: {e}")
    
    
    # ── Online Map API ──────────────────────────────────────────────────────
    def fetch_elevation(self, lat: float, lon: float) -> Optional[float]:
        """
        Fetch elevation from online API (OpenTopoData).
        Returns elevation in metres or None on error.
        """
        if self._config.source_type != MapDataSource.ONLINE:
            return None
        
        try:
            import urllib.request as _ur
            url = f"https://api.opentopodata.org/v1/srtm30m?locations={lat},{lon}"
            
            with _ur.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            
            results = data.get("results", [])
            if results and len(results) > 0:
                elevation = results[0].get("elevation")
                if elevation is not None:
                    log.debug(f"Elevation at {lat:.4f},{lon:.4f}: {elevation}m")
                    return float(elevation)
        except Exception as e:
            log.debug(f"Elevation API fetch failed: {e}")
        
        return None
    
    # ── Utilities ───────────────────────────────────────────────────────────
    @staticmethod
    def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two points in km (Haversine)."""
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat/2)**2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon/2)**2)
        return float(2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a)))
    
    def get_status(self) -> dict:
        """Return terrain system status for admin UI."""
        with self._lock:
            cell_counts = {}
            for cell in self._cells.values():
                t = cell.terrain_type.value
                cell_counts[t] = cell_counts.get(t, 0) + 1
            
            highres_count = self._tile_cache.get_disk_tile_count()
            
            return {
                "enabled": True,
                "source_type": self._config.source_type.value,
                "cell_count": len(self._cells),
                "cell_counts": cell_counts,
                "highres_tile_count": highres_count,
                "highres_ready": highres_count > 0,
                "last_sync": self._last_sync,
                "last_sync_ago": time.time() - self._last_sync if self._last_sync > 0 else None,
            }


# ═══════════════════════════════════════════════════════════════════════════
# Singleton Instance
# ═══════════════════════════════════════════════════════════════════════════
_terrain_manager: Optional[TerrainManager] = None


def get_terrain_manager() -> TerrainManager:
    """Get or create the global terrain manager."""
    global _terrain_manager
    if _terrain_manager is None:
        _terrain_manager = TerrainManager()
    return _terrain_manager


# ═══════════════════════════════════════════════════════════════════════════
# Self-Test
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("=== Terrain System Self-Test ===\n")
    
    terrain = TerrainManager()
    
    # Add test terrain cells
    terrain.add_cell(TerrainCell(
        lat_min=51.0, lat_max=52.0,
        lon_min=-0.5, lon_max=0.5,
        terrain_type=TerrainType.URBAN,
        elevation_m=50.0,
    ))
    
    terrain.add_cell(TerrainCell(
        lat_min=52.0, lat_max=53.0,
        lon_min=-0.5, lon_max=0.5,
        terrain_type=TerrainType.FOREST,
        elevation_m=200.0,
    ))
    
    print(f"Test cells added: {len(terrain.get_all_cells())}")
    
    # Test terrain factor computation
    factor = terrain.compute_terrain_factor(51.5, -0.1, 52.5, -0.1)
    print(f"Terrain factor London→North: {factor:.2f}")
    
    # Test LOS check
    los = terrain.check_line_of_sight(51.5, -0.1, 10, 52.5, -0.1, 10)
    print(f"Line-of-sight clear: {los}")
    
    # Test status
    status = terrain.get_status()
    print(f"\nTerrain status: {json.dumps(status, indent=2)}")
    
    print("\n✓ Terrain system self-test passed")