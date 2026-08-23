"""Single source of truth for approved identity keys exposed to autofill."""
from __future__ import annotations


# database applicant_identity field_name -> nested deterministic profile path
AUTOFILL_FIELD_REGISTRY = {
    "full_name": ("personal", "full_name"),
    "legal_first_name": ("personal", "first_name"),
    "legal_last_name": ("personal", "last_name"),
    "middle_name": ("personal", "middle_name"),
    "preferred_name": ("personal", "preferred_name"),
    "pronouns": ("personal", "pronouns"),
    "email": ("personal", "email"),
    "phone": ("personal", "phone"),
    "phone_country_code": ("personal", "phone_country_code"),
    "linkedin_url": ("personal", "linkedin"),
    "github_url": ("personal", "github"),
    "portfolio_url": ("personal", "portfolio"),
    "twitter_url": ("personal", "twitter"),
    "other_url": ("personal", "other_url"),
    "address_line1": ("address", "line1"),
    "address_line2": ("address", "line2"),
    "address_city": ("address", "city"),
    "address_state": ("address", "state"),
    "address_postal": ("address", "postal"),
    "address_postal_ext": ("address", "postal_extension"),
    "address_county": ("address", "county"),
    "address_country": ("address", "country"),
    "university_name": ("education", "university"),
    "degree": ("education", "degree"),
    "major": ("education", "major"),
    "graduation_date": ("education", "graduation_date"),
    "current_employer": ("employment", "current_employer"),
    "current_title": ("employment", "current_title"),
    "desired_title": ("employment", "desired_title"),
    "referral_source": ("preferences", "referral_source"),
}


PROFILE_PATH_TO_FIELD = {".".join(path): field for field, path in AUTOFILL_FIELD_REGISTRY.items()}
