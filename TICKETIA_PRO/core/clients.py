import os
import logging
from openai import OpenAI
from runwayml import RunwayML
from core.config import Config

logger = logging.getLogger(__name__)

# --- Singleton Clients ---

# OpenAI
_openai_client = None

def get_openai_client():
    global _openai_client
    if _openai_client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            logger.warning("OPENAI_API_KEY not found.")
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client

# RunwayML
_runway_client = None

def get_runway_client():
    global _runway_client
    if _runway_client is None:
        api_key = Config.RUNWAYML_API_SECRET
        if not api_key:
            logger.warning("RUNWAYML_API_SECRET not found.")
        try:
            _runway_client = RunwayML(api_key=api_key)
        except Exception as e:
            logger.error("Error initializing Runway client: %s", e)
    return _runway_client
