from fastapi import APIRouter

from ..startup_timing import summary as startup_summary
from ..version import PLATFORM_VERSION

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    return {
        "status": "ok",
        "service": "kane-agent-platform-api",
        "version": PLATFORM_VERSION,
        "startup": startup_summary(),
    }
