#!/usr/bin/env python3
import os, sys, json, hashlib, mimetypes, pathlib
from openai import OpenAI
from dotenv import load_dotenv

# Config
CONSULTANTS_ROOT = pathlib.Path(__file__).resolve().parents[1] / "app" / "consultants"
OUT_JSON = pathlib.Path(__file__).resolve().parents[1] / "consultant_assets.json"
INSTRUCTIONS_NAME = "instructions.txt"

# Purpose for files used with file_search/code_interpreter (still "assistants")
OPENAI_FILE_PURPOSE = "assistants"

def _load_project_env():
    start = pathlib.Path(__file__).resolve().parent
    for p in [start, *start.parents]:
        candidate = p / ".env"
        if candidate.exists():
            load_dotenv(dotenv_path=candidate)
            return
    load_dotenv()


def sha256_path(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def upload_file(client: OpenAI, p: pathlib.Path) -> str:
    with open(p, "rb") as fh:
        f = client.files.create(file=(p.name, fh), purpose=OPENAI_FILE_PURPOSE)
    return f.id

def main():
    _load_project_env()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: set OPENAI_API_KEY in your environment or .env", file=sys.stderr)
        sys.exit(1)
    client = OpenAI(api_key=api_key)

    result = {}
    # Optional cache to avoid re-upload: map sha256 -> file_id
    cache_path = OUT_JSON.with_suffix(".cache.json")
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except Exception:
            cache = {}

    for consultant_dir in sorted(CONSULTANTS_ROOT.iterdir()):
        if not consultant_dir.is_dir():
            continue
        key = consultant_dir.name  # e.g., finance, legal, ops
        rubric_files = []
        default_files = []
        instructions_text = ""

        instr_path = consultant_dir / INSTRUCTIONS_NAME
        if instr_path.exists():
            instructions_text = instr_path.read_text(encoding="utf-8").strip()

        # Collect files (rubric first by name match, everything else as default)
        for p in sorted(consultant_dir.iterdir()):
            if p.name == INSTRUCTIONS_NAME or not p.is_file():
                continue
            if p.suffix.lower() not in [".pdf", ".docx", ".txt", ".md", ".csv"]:
                # Skip unknown types; add more as needed
                continue

            role = "rubric" if "rubric" in p.stem.lower() else "default"
            digest = sha256_path(p)
            file_id = cache.get(digest)

            if not file_id:
                print(f"Uploading {p} …")
                file_id = upload_file(client, p)
                cache[digest] = file_id
            else:
                print(f"Using cached upload for {p} -> {file_id}")

            if role == "rubric":
                rubric_files.append(file_id)
            else:
                default_files.append(file_id)

        result[key] = {
            "instructions": instructions_text,
            "rubric_files": rubric_files,
            "default_files": default_files,
        }

    OUT_JSON.write_text(json.dumps(result, indent=2))
    cache_path.write_text(json.dumps(cache, indent=2))
    print(f"\nWrote mapping to {OUT_JSON}")
    print(f"Cache at {cache_path}")

if __name__ == "__main__":
    main()
