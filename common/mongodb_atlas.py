import logging
import os

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.server_api import ServerApi

load_dotenv()

logger = logging.getLogger(__name__)

_client: MongoClient | None = None


def get_atlas_client() -> MongoClient:
    """Return a shared MongoDB Atlas client built from MONGODB_ALART_CLIENT_URL.

    The client is created once and reused, mirroring pymongo's own
    connection-pooling guidance (one MongoClient per process).
    """
    global _client
    if _client is None:
        uri = os.getenv("MONGODB_ALART_CLIENT_URL")
        if not uri:
            raise RuntimeError("MONGODB_ALART_CLIENT_URL is not set")
        try:
            _client = MongoClient(uri, server_api=ServerApi("1"))
            logger.info("MongoDB Atlas client created")
        except Exception:
            logger.exception("MongoDB Atlas connection failed")
            raise
    return _client
