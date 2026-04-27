"""Entry point: python run.py"""
from dotenv import load_dotenv
load_dotenv(override=True)

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_includes=["*.env", ".env"],
    )
