import requests
import websocket
import json
import time
import random

def test_fake_mouse(regimes_file_path):
    try:
        print("[1] Đang tìm Chrome ở cổng 9222...")
        res = requests.get("http://127.0.0.1:9222/json")
        tabs = res.json()
        
        # Lấy tab đầu tiên là trang web
        ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
        ws = websocket.create_connection(ws_url, suppress_origin=True)
        print(f"[2] Đã cắm ống vào Tab: {ws_url}")

        # Điều hướng tab tới một trang test (tùy chọn)
        # ws.send(json.dumps({
        #     "id": 1,
        #     "method": "Page.navigate",
        #     "params": {"url": "https://example.com"}
        # }))

        # Lấy kích thước Viewport
        ws.send(json.dumps({
            "id": 2,
            "method": "Runtime.evaluate",
            "params": {"expression": "({width: window.innerWidth, height: window.innerHeight})", "returnByValue": True}
        }))
        size_res = json.loads(ws.recv())
        viewport = size_res.get("result", {}).get("result", {}).get("value", {"width": 1024, "height": 768})
        
        current_x = random.uniform(100, viewport["width"] - 100)
        current_y = random.uniform(100, viewport["height"] - 100)

        # Đọc dữ liệu mô phỏng
        with open(regimes_file_path, 'r') as f:
            regimes = json.load(f).get("regimes", [])

        print(f"[3] Bắt đầu rải Input.dispatchMouseEvent. Tọa độ xuất phát: ({current_x:.1f}, {current_y:.1f})")
        dt = 0.05 # Chạy chậm lại chút (50ms) để mắt người dễ nhìn log
        
        for idx, regime in enumerate(regimes):
            print(f"\n---> Chuyển sang nhịp vẩy chuột thứ {idx + 1}")
            drift_x = regime["drift"]["x"]
            drift_y = regime["drift"]["y"]
            
            for step in range(20): # Mỗi nhịp bắn 20 tọa độ
                current_x += (drift_x * dt) + random.gauss(0, 2.0)
                current_y += (drift_y * dt) + random.gauss(0, 2.0)

                # Ép trong màn hình
                current_x = max(0, min(current_x, viewport["width"]))
                current_y = max(0, min(current_y, viewport["height"]))

                # Bắn tín hiệu chuột
                ws.send(json.dumps({
                    "id": random.randint(1000, 9999),
                    "method": "Input.dispatchMouseEvent",
                    "params": {
                        "type": "mouseMoved", 
                        "x": int(current_x), 
                        "y": int(current_y)
                    }
                }))
                
                print(f"Bắn tọa độ: x={int(current_x)}, y={int(current_y)}")
                time.sleep(dt)
                
        ws.close()
        print("\n[4] XONG! Đã ngắt kết nối.")
    except Exception as e:
        print(f"[LỖI] {e}")

if __name__ == "__main__":
    test_fake_mouse("/Users/phanan/pointer-regimes.json")