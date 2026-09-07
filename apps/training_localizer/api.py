from fastapi import FastAPI

app = FastAPI(title="training localizer")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "stage": "scaffold"}
