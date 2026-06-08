import json
import traceback
from radio_effects import RangeModel

class DummyRadioServer:
    def __init__(self):
        self.range_model = RangeModel()

class DummyServerRef:
    def __init__(self):
        self.radio_server = DummyRadioServer()

class DummyHandler:
    def __init__(self):
        self.server_ref = DummyServerRef()

    def _json(self, data):
        print("JSON_SUCCESS:", json.dumps(data))

    def _err(self, obj):
        print("JSON_ERROR:", json.dumps(obj))

def test_sim():
    handler = DummyHandler()
    body = {
        "nodes": [
            {'id':'n1', 'lat': 34.0, 'lng': -118.0, 'type': 'vehicular', 'netId': 'net1'},
            {'id':'n2', 'lat': 34.1, 'lng': -118.0, 'type': 'vehicular', 'netId': 'net1'}
        ],
        "nets": [
            {"id": "net1", "name": "Net 1"}
        ]
    }
    
    try:
        nodes = body.get("nodes", [])
        nets = body.get("nets", [])
        links = []
        
        profiles = {
            'handheld': {'power': 5.0,  'max_range': 20.0, 'full_quiet': 5.0},
            'manpack':  {'power': 20.0, 'max_range': 50.0, 'full_quiet': 15.0},
            'vehicular':{'power': 50.0, 'max_range': 80.0, 'full_quiet': 30.0},
            'retrans':  {'power': 50.0, 'max_range': 120.0,'full_quiet': 50.0}
        }
        
        rm = handler.server_ref.radio_server.range_model
        
        for net in nets:
            net_nodes = [n for n in nodes if n.get("netId") == net["id"]]
            for i in range(len(net_nodes)):
                for j in range(i + 1, len(net_nodes)):
                    na = net_nodes[i]
                    nb = net_nodes[j]
                    dist = rm.haversine_km(float(na["lat"]), float(na["lng"]), float(nb["lat"]), float(nb["lng"]))
                    
                    pa = profiles.get(na["type"], profiles['handheld'])
                    pb = profiles.get(nb["type"], profiles['handheld'])
                    
                    sigA = rm.evaluate_profile(dist, pa['power'], pa['max_range'], pa['full_quiet'])
                    sigB = rm.evaluate_profile(dist, pb['power'], pb['max_range'], pb['full_quiet'])
                    
                    link_sig = min(sigA, sigB)
                    
                    links.append({
                        "source": na["id"],
                        "target": nb["id"],
                        "distance": dist,
                        "signal": link_sig,
                        "netId": net["id"]
                    })
                    
        handler._json({"ok": True, "links": links})
    except Exception as e:
        handler._err({"error": str(e), "traceback": traceback.format_exc()})

if __name__ == '__main__':
    test_sim()
