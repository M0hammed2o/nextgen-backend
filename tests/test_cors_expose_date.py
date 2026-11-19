"""The business API exposes the Date response header cross-origin, so the
staff PWA can detect a wrong device clock before stamping offline sales."""
from fastapi.middleware.cors import CORSMiddleware


def test_business_api_exposes_date_header():
    from backend.app.main import app

    cors = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert len(cors) == 1
    assert "Date" in cors[0].kwargs.get("expose_headers", [])
