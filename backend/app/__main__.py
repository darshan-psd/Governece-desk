"""``python -m app``: start the API on the configured loopback address (default 127.0.0.1:5174)."""
import uvicorn

from app.core.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    # Logging is configured by the app (structured JSON); uvicorn's access log is replaced by the
    # request log, which carries the request ID.
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, proxy_headers=False, access_log=False, log_config=None)
