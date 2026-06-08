
import struct
import os

class SWORDBridge:
    def __init__(self):
        self.base_path = r'c:\Users\johnn\Desktop\mil_radiov3\NZ_North_Island_v4'
        self.det_path = os.path.join(self.base_path, 'Detection', 'detection.dat')
        self.rows, self.cols = 5182, 4483
        
        # DUAL-ANCHOR MATRIX (Locked to Ruapehu, Taranaki, and Ngauruhoe)
        self.m_lon = -1138.0
        self.b_lon = 201211.28
        self.m_lat = -11469.23
        self.b_lat = -448436.15
        
        self.ready = os.path.exists(self.det_path)

    def get_elevation(self, lat, lon):
        if not self.ready: return 0.0
        c = int(round(self.m_lon * lon + self.b_lon))
        r = int(round(self.m_lat * lat + self.b_lat))
        if not (0 <= c < self.cols and 0 <= r < self.rows): return 0.0
        
        offset = 16 + (r * self.cols + c) * 4
        try:
            with open(self.det_path, 'rb') as f:
                f.seek(offset)
                raw = f.read(4)
                s1, s2 = struct.unpack('<HH', raw)
                return float(s1)
        except: return 0.0

if __name__ == "__main__":
    bridge = SWORDBridge()
    print("="*60)
    print(" SWORD CALIBRATION V4 - DUAL-ANCHOR PRECISION")
    print("="*60)
    
    peaks = [
        ("Mt Ruapehu", -39.28, 175.56, 2797),
        ("Mt Taranaki", -39.29, 174.06, 2518),
        ("Mt Ngauruhoe", -39.15, 175.63, 2291),
        ("Mt Tongariro", -39.13, 175.64, 1978),
        ("Taupo Town", -38.68, 176.07, 360)
    ]
    
    print(f"{'Peak':<15} | {'Actual':<7} | {'SWORD':<10} | {'Diff'}")
    print("-" * 60)
    for name, lat, lon, actual in peaks:
        elev = bridge.get_elevation(lat, lon)
        print(f"{name:<15} | {actual:>7} | {elev:>10.0f} | {elev-actual:>6.0f}m")
