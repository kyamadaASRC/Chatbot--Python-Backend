"""Locate .env credentials for the Template Manager and expose a shared OpenAI client."""

from openai import OpenAI
import os
from dotenv import load_dotenv
from pathlib import Path


def _locate_env(start: Path):
    """Look for a .env within the Template_Manager folder (no repo walk-up)."""
    candidate = start / ".env"
    return candidate if candidate.exists() else None


BASE_DIR = Path(__file__).resolve().parent
env_path = _locate_env(BASE_DIR)
if env_path:
    load_dotenv(dotenv_path=env_path)

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    print("⚠️ Template Manager: OPENAI_API_KEY not found in Template_Manager/.env")

client = OpenAI(api_key=api_key)

