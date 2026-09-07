from fastapi import FastAPI

app = FastAPI(title="helpdesk agent")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "stage": "scaffold"}
