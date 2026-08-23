import logging
from typing import Tuple
log = logging.getLogger(__name__)
# Trích xuất từ ApplyPilot (smartextract.py)
CAPTCHA_SIGNALS = [
    "captcha",
    "are you a human",
    "verify you",
    "unusual requests",
    "access denied",
    "please verify",
    "bot detection"
    ]
# Trích xuất từ ApplyPilot (sites.yaml)
MANUAL_ATS_BLACKLIST = [
    "ibegin.tcsapps.com",
    "myworkdayjobs.com/captcha"
    ]
def analyze_captcha_risk(html_content: str, url: str) -> Tuple[bool, str]:

    url_lower = url.lower()
    # 1. Kiểm tra Blacklist (Permanent Failures)
    for bad_domain in MANUAL_ATS_BLACKLIST:
        if bad_domain in url_lower:
            log.warning(f"URL bị liệt vào danh sách đen ATS thủ công: {bad_domain}")
            return True, "manual_ats_blacklist"
    # 2. Kiểm tra HTML Content
    if html_content:
        html_lower = html_content.lower()
        for signal in CAPTCHA_SIGNALS:
            if signal in html_lower:
                log.warning(f"Phát hiện tín hiệu chặn: '{signal}' trong HTML")
                return True, f"captcha_signal_detected:{signal.replace(' ', '_')}"
    return False, "ok"