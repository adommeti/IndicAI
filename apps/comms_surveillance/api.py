from fastapi import FastAPI

app = FastAPI(title="comms surveillance")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "stage": "scaffold"}
