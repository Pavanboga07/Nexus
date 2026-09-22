from fastapi import FastAPI

from app.api.routes.ask import router as ask_router
from app.api.routes.chat import router as chat_router
from app.api.routes.memory import router as memory_router
from app.api.routes.pairing import router as pairing_router

app = FastAPI()
app.include_router(pairing_router)
app.include_router(ask_router)
app.include_router(chat_router)
app.include_router(memory_router)


@app.get("/health")
def health():
    return {"status": "ok"}
