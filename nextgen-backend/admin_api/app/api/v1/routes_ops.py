from fastapi import APIRouter

router = APIRouter(tags=["ops"])

@router.get("/health")
async def health():
    return {"status": "ok"}

@router.get("/ready")
async def ready():
    return {"status": "ok"}  # (optionally check DB/Redis)