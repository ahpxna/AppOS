import threading
import time
import json
import random
import requests
import websocket
import logging
from services.autofill.capsolver_api import solve_captcha

log = logging.getLogger(__name__)

def _fake_mouse_routine(ws_url: str, regimes_file_path: str, stop_event: threading.Event):
    """
    Luồng giả lập chuột CDP nâng cao: Di chuyển + Click ngẫu nhiên.
    Sẽ chạy liên tục cho đến khi stop_event được set (CapSolver giải xong).
    """
    try:
        log.info("[FakeMouse] Đang kết nối CDP websocket...")
        ws = websocket.create_connection(ws_url, suppress_origin=True)
        
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

        dt = 0.05 # 50ms interval
        log.info(f"[FakeMouse] Bắt đầu rải event. Tọa độ xuất phát: ({current_x:.1f}, {current_y:.1f})")

        # QUAN TRỌNG: Lặp lại các regimes liên tục cho đến khi CapSolver giải xong
        while not stop_event.is_set():
            for idx, regime in enumerate(regimes):
                if stop_event.is_set(): 
                    break
                
                drift_x = regime["drift"]["x"]
                drift_y = regime["drift"]["y"]
                
                for step in range(20): # Mỗi nhịp bắn 20 tọa độ
                    if stop_event.is_set(): 
                        break
                        
                    current_x += (drift_x * dt) + random.gauss(0, 2.0)
                    current_y += (drift_y * dt) + random.gauss(0, 2.0)

                    # Ép trong màn hình
                    current_x = max(0, min(current_x, viewport["width"]))
                    current_y = max(0, min(current_y, viewport["height"]))

                    # 1. Bắn tín hiệu di chuyển chuột (mouseMoved)
                    ws.send(json.dumps({
                        "id": random.randint(1000, 9999),
                        "method": "Input.dispatchMouseEvent",
                        "params": {
                            "type": "mouseMoved", 
                            "x": int(current_x), 
                            "y": int(current_y)
                        }
                    }))
                    
                    # 2. Random Click (Tỷ lệ 5% sẽ click chuột trái)
                    if random.random() < 0.00:
                        ws.send(json.dumps({
                            "id": random.randint(1000, 9999),
                            "method": "Input.dispatchMouseEvent",
                            "params": {"type": "mousePressed", "button": "left", "clickCount": 1, "x": int(current_x), "y": int(current_y)}
                        }))
                        time.sleep(0.05) # Giữ chuột 50ms
                        ws.send(json.dumps({
                            "id": random.randint(1000, 9999),
                            "method": "Input.dispatchMouseEvent",
                            "params": {"type": "mouseReleased", "button": "left", "clickCount": 1, "x": int(current_x), "y": int(current_y)}
                        }))
                        log.debug(f"[FakeMouse] Click ngẫu nhiên tại: x={int(current_x)}, y={int(current_y)}")

                    time.sleep(dt)
                    
        ws.close()
        log.info("[FakeMouse] Đã nhận tín hiệu dừng. Ngắt kết nối CDP.")
    except Exception as e:
        log.error(f"[FakeMouse] Lỗi luồng: {e}")

def execute_parallel_bypass(cdp_port: int, website_url: str, website_key: str, regimes_path: str, captcha_type: str = "ReCaptchaV2TaskProxyLess"):
    """
    Hàm Orchestrator: Quản lý 2 luồng song song.
    """
    try:
        log.info(f"Đang tìm Chrome ở cổng {cdp_port}...")
        res = requests.get(f"http://127.0.0.1:{cdp_port}/json")
        tabs = res.json()
        ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
    except Exception as e:
        raise RuntimeError(f"Không thể kết nối Chrome CDP ở port {cdp_port}: {e}")

    # Khởi chạy luồng Fake Mouse
    stop_event = threading.Event()
    mouse_thread = threading.Thread(target=_fake_mouse_routine, args=(ws_url, regimes_path, stop_event))
    mouse_thread.start()

    # Gọi CapSolver API 
    solution_token = None
    try:
        solution_token = solve_captcha(website_url, website_key, captcha_type)
    except Exception as e:
        log.error(f"Lỗi giải CAPTCHA: {e}")
    finally:
        # Tắt chuột giả
        stop_event.set()
        mouse_thread.join()

    # Inject Token vào DOM
    if solution_token:
        log.info("Injecting token vào DOM...")
        ws = websocket.create_connection(ws_url, suppress_origin=True)
        inject_js = f"document.getElementById('g-recaptcha-response').innerHTML = '{solution_token}';"
        ws.send(json.dumps({
            "id": 9999,
            "method": "Runtime.evaluate",
            "params": {"expression": inject_js}
        }))
        # Kích hoạt callback nếu có
        ws.send(json.dumps({
            "id": 10000,
            "method": "Runtime.evaluate",
            "params": {"expression": "if(typeof captchaCallback === 'function'){ captchaCallback(); }"}
        }))
        ws.close()
        
    return solution_token