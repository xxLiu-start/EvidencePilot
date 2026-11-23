"""Read-only discovery endpoints for supported research domains."""
from fastapi import APIRouter, HTTPException

from app.domains.profiles import get_domain_profile, list_domain_profiles

router = APIRouter()


@router.get("/domains")
def list_domains():
    return {"items": [profile.model_dump(mode="json") for profile in list_domain_profiles()]}


@router.get("/domains/{domain_id}")
def get_domain(domain_id: str):
    try:
        profile = get_domain_profile(domain_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Research domain not found") from None
    return profile.model_dump(mode="json")
