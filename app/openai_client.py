"""Locate .env credentials and expose a shared OpenAI client for all modules."""

from openai import OpenAI
import os
from dotenv import load_dotenv
from pathlib import Path

# --- STEP 1: Locate your .env file ---
# Walk upward from this file until we find a .env; default to None if missing.
def _locate_env(start: Path):
    for path in [start, *start.parents]:
        candidate = path / ".env"
        if candidate.exists():
            return candidate
    return None

env_path = _locate_env(Path(__file__).resolve().parent)

# --- STEP 2: Load the .env file ---
if env_path:
    load_dotenv(dotenv_path=env_path)

# --- STEP 3: Read the key from environment ---
api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    print("⚠️ WARNING: OPENAI_API_KEY not found. Check your .env path or contents.")

# --- STEP 4: Initialize the OpenAI client ---
# The `client` object will be imported by other modules
client = OpenAI(api_key=api_key)

# --- Optional: diagnostic check ---
if api_key:
    print("✅ OpenAI client initialized successfully.")
else:
    print("❌ OpenAI client initialization failed due to missing API key.")
