from fastapi import FastAPI

from app.api.routes.ask import router as ask_router
from app.api.routes.pairing import router as pairing_router

app = FastAPI()
app.include_router(pairing_router)
app.include_router(ask_router)


@app.get("/health")
def health():
    return {"status": "ok"}
