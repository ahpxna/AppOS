import requests
import websocket
import json
import time
import random
import threading

def inject_mouse_movements(regimes_file_path):
    try:
        # 1. Kết nối vào tab hiện tại
        res = requests.get("http://127.0.0.1:9222/json")
        ws_url = next(t["webSocketDebuggerUrl"] for t in res.json() if t["type"] == "page")
        ws = websocket.create_connection(ws_url)

        # 2. Lấy kích thước Viewport để Random tọa độ
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": "({width: window.innerWidth, height: window.innerHeight})", "returnByValue": True}
        }))
        size_res = json.loads(ws.recv())
        viewport = size_res.get("result", {}).get("result", {}).get("value", {"width": 1024, "height": 768})
        
        current_x = random.uniform(100, viewport["width"] - 100)
        current_y = random.uniform(100, viewport["height"] - 100)

        # 3. Đọc dữ liệu mô phỏng từ file JSON
        with open(regimes_file_path, 'r') as f:
            regimes = json.load(f).get("regimes", [])

        # 4. Múa chuột
        dt = 0.02
        for regime in regimes:
            drift_x = regime["drift"]["x"]
            drift_y = regime["drift"]["y"]
            
            for _ in range(50): # Mỗi regime kéo dài ~1 giây
                current_x += (drift_x * dt) + random.gauss(0, 1.5)
                current_y += (drift_y * dt) + random.gauss(0, 1.5)

                # Giới hạn không cho chuột bay ra khỏi màn hình
                current_x = max(0, min(current_x, viewport["width"]))
                current_y = max(0, min(current_y, viewport["height"]))

                ws.send(json.dumps({
                    "id": random.randint(1000, 9999),
                    "method": "Input.dispatchMouseEvent",
                    "params": {"type": "mouseMoved", "x": int(current_x), "y": int(current_y)}
                }))
                time.sleep(dt)
                
        ws.close()
    except Exception as e:
        print(f"[FAKE MOUSE] Lỗi: {e}")

def start_fake_mouse_thread(regimes_file_path):
    t = threading.Thread(target=inject_mouse_movements, args=(regimes_file_path,))
    t.daemon = True
    t.start()