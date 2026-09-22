from fastapi import FastAPI

from app.api.routes.pairing import router as pairing_router

app = FastAPI()
app.include_router(pairing_router)


@app.get("/health")
def health():
    return {"status": "ok"}
