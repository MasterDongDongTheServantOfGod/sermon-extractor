"""Entry point: python run.py"""
from dotenv import load_dotenv
load_dotenv(override=False)

import os
import uvicorn

if __name__ == "__main__":
    is_dev = os.getenv("ENVIRONMENT", "production") == "development"
    port = int(os.getenv("PORT", 8000))

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        reload=is_dev,
        reload_includes=["*.env", ".env"] if is_dev else [],
    )
