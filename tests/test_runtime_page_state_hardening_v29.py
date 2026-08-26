from services.application_actions.privileged_action_v1 import detect_page_state


def _snap(text):
    return {"snapshot": text}


def test_application_form_header_sign_in_does_not_become_auth_gate():
    nodes = [
        {"ref": "fn", "role": "textbox", "label": "First name"},
        {"ref": "ln", "role": "textbox", "label": "Last name"},
        {"ref": "em", "role": "textbox", "label": "Email"},
        {"ref": "submit", "role": "button", "label": "Submit application"},
        {"ref": "signin", "role": "link", "label": "Sign in"},
    ]
    state, _ = detect_page_state(
        "https://ats.example/apply",
        _snap("Jobs | Sign in\nApply for Software Engineer\nFirst name Last name Email"),
        nodes,
    )
    assert state == "application_form_ready"


def test_recaptcha_legal_footer_is_not_an_active_checkpoint():
    nodes = [
        {"ref": "fn", "role": "textbox", "label": "First name"},
        {"ref": "ln", "role": "textbox", "label": "Last name"},
        {"ref": "submit", "role": "button", "label": "Submit application"},
    ]
    state, _ = detect_page_state(
        "https://ats.example/apply",
        _snap("Apply now. This site is protected by reCAPTCHA and the Google Privacy Policy applies."),
        nodes,
    )
    assert state == "application_form_ready"


def test_text_message_preference_question_is_not_mfa():
    nodes = [
        {"ref": "phone", "role": "textbox", "label": "Phone number"},
        {"ref": "yes", "role": "radio", "label": "Yes - receive text message updates"},
        {"ref": "resume", "role": "file", "label": "Resume"},
    ]
    state, _ = detect_page_state(
        "https://ats.example/apply",
        _snap("Would you like to receive text message updates about your application?"),
        nodes,
    )
    assert state == "application_form_ready"


def test_real_login_still_routes_to_account_auth():
    nodes = [
        {"ref": "email", "role": "textbox", "label": "Email"},
        {"ref": "continue", "role": "button", "label": "Continue"},
    ]
    state, _ = detect_page_state(
        "https://ats.example/login", _snap("Sign in to your candidate account"), nodes
    )
    assert state == "needs_account_auth"


def test_real_sms_code_still_routes_to_mfa():
    nodes = [
        {"ref": "code", "role": "textbox", "label": "Security code"},
        {"ref": "verify", "role": "button", "label": "Verify"},
    ]
    state, _ = detect_page_state(
        "https://ats.example/account/challenge",
        _snap("We sent you a text message. Enter the security code."),
        nodes,
    )
    assert state == "needs_mfa"


def test_real_captcha_control_still_routes_to_human_checkpoint():
    nodes = [
        {"ref": "robot", "role": "checkbox", "label": "I'm not a robot"},
    ]
    state, _ = detect_page_state(
        "https://ats.example/apply", _snap("reCAPTCHA"), nodes
    )
    assert state == "needs_human_checkpoint"


def test_real_email_verification_still_routes_separately():
    nodes = [
        {"ref": "code", "role": "textbox", "label": "Verification code"},
    ]
    state, detail = detect_page_state(
        "https://ats.example/account/verify",
        _snap("Verify your email. Check your email for the code."),
        nodes,
    )
    assert state == "needs_email_verification"
    assert detail["field_ref"] == "code"


def test_upload_only_application_page_is_still_form_ready():
    nodes = [
        {"ref": "resume", "role": "button", "label": "Upload Resume"},
        {"ref": "submit", "role": "button", "label": "Submit application"},
    ]
    state, _ = detect_page_state(
        "https://ats.example/apply", _snap("Upload Resume"), nodes
    )
    assert state == "application_form_ready"


def test_authenticator_experience_question_is_not_an_mfa_gate():
    nodes = [
        {"ref": "fn", "role": "textbox", "label": "First name"},
        {"ref": "q", "role": "radio", "label": "Have you used an authenticator app at work?"},
        {"ref": "submit", "role": "button", "label": "Submit application"},
    ]
    state, _ = detect_page_state(
        "https://ats.example/apply",
        _snap("Application question: Have you used an authenticator app at work?"),
        nodes,
    )
    assert state == "application_form_ready"


def test_email_verification_experience_question_is_not_an_email_gate():
    nodes = [
        {"ref": "fn", "role": "textbox", "label": "First name"},
        {"ref": "q", "role": "textbox", "label": "Describe your email verification experience"},
        {"ref": "submit", "role": "button", "label": "Submit application"},
    ]
    state, _ = detect_page_state(
        "https://ats.example/apply",
        _snap("Describe your email verification experience"),
        nodes,
    )
    assert state == "application_form_ready"
