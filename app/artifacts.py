import os
import pathlib

from app.openai_client import client


SAVE_ROOT = os.environ.get("SAVE_ROOT", "./artifacts")


def download_file_bytes(fid: str) -> bytes:
    return client.files.retrieve_content(fid)


def save_hardcopies(project: str, consultant: str, step: int, file_ids):
    base = pathlib.Path(SAVE_ROOT) / project / f"{consultant}" / f"step{step}"
    base.mkdir(parents=True, exist_ok=True)
    paths = []
    for fid in file_ids:
        content = download_file_bytes(fid)
        fname = f"{fid}.bin"
        fpath = base / fname
        with open(fpath, "wb") as f: f.write(content)
        paths.append(str(fpath))
    return paths
