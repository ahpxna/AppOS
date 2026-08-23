import os
import time
import requests
import logging

log = logging.getLogger(__name__)

# Tích hợp CAPSOLVER_API_KEY từ ApplyPilot config
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "")

def solve_captcha(website_url: str, website_key: str, captcha_type: str = "ReCaptchaV2TaskProxyLess") -> str:
    if not CAPSOLVER_API_KEY:
        raise ValueError("Missing CAPSOLVER_API_KEY in .env")

    payload = {
        "clientKey": CAPSOLVER_API_KEY,
        "task": {
            "type": captcha_type,
            "websiteURL": website_url,
            "websiteKey": website_key
        }
    }
    
    log.info(f"[CapSolver] Đang gửi request cho URL: {website_url}")
    res = requests.post("https://api.capsolver.com/createTask", json=payload).json()
    
    if res.get("errorId", 0) > 0:
        raise RuntimeError(f"CapSolver Error: {res.get('errorDescription')}")
        
    task_id = res.get("taskId")
    log.info(f"[CapSolver] Task ID: {task_id}. Đang chờ giải mã...")

    # Polling chờ kết quả
    while True:
        time.sleep(3.0)
        res = requests.post("https://api.capsolver.com/getTaskResult", json={
            "clientKey": CAPSOLVER_API_KEY,
            "taskId": task_id
        }).json()
        
        status = res.get("status")
        if status == "ready":
            log.info("[CapSolver] Giải CAPTCHA thành công!")
            solution = res.get("solution", {})
            return solution.get("gRecaptchaResponse") or solution.get("token") or solution.get("text")
        elif status == "failed":
            raise RuntimeError("[CapSolver] Giải thất bại.")