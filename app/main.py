import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database.session import init_db
from app.services import llm_client
from app.web.routes import router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="AuditAI", lifespan=lifespan)
app.include_router(router)

static_dir = Path(__file__).parent / "web" / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/health")
def health():
    return {"status": "ok", "llm_available": llm_client.is_available(), "model": llm_client.MODEL_NAME}
