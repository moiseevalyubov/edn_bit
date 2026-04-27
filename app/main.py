import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.database import Base, engine
from app.routers import api, handler, incoming, install, settings_page
from app.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

Base.metadata.create_all(bind=engine)

if settings.app_base_url:
    logger.info("APP_BASE_URL configured: %s", settings.app_base_url)
else:
    logger.error("APP_BASE_URL is NOT set — event binding and webhook URLs will be broken!")

app = FastAPI(title="MAX Bot — Bitrix24 Connector", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = round((time.time() - start) * 1000)
    logger.info("%s %s → %d (%dms)", request.method, request.url.path, response.status_code, duration)
    return response


app.include_router(install.router)
app.include_router(handler.router)
app.include_router(incoming.router)
app.include_router(api.router)
app.include_router(settings_page.router)


@app.get("/health")
def health():
    return {"status": "ok"}
