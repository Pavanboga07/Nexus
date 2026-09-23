from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.ask import router as ask_router
from app.api.routes.chat import router as chat_router
from app.api.routes.memory import router as memory_router
from app.api.routes.pairing import router as pairing_router

app = FastAPI()
# Dev-only CORS: the local frontend runs on :3001 while the API runs on
# :8001, so browsers block cross-origin fetches without these headers.
# Production same-origin deployments need none of this.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3001", "http://127.0.0.1:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(pairing_router)
app.include_router(ask_router)
app.include_router(chat_router)
app.include_router(memory_router)


@app.get("/health")
def health():
    return {"status": "ok"}
