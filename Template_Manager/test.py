from pathlib import Path
from Template_Manager.template_manifest import ensure_manifest_vector_store
from Template_Manager.openai_client import client

def main():
    keep_id = Path("Template_Manager/template_manifest_file_id.txt").read_text().strip()
    vs_id = ensure_manifest_vector_store(None)
    listing = client.vector_stores.files.list(vector_store_id=vs_id)
    for f in getattr(listing, "data", []) or []:
        fid = getattr(f, "id", None)
        if fid and fid != keep_id:
            try:
                client.vector_stores.files.delete(vector_store_id=vs_id, file_id=fid)
                client.files.delete(fid)
                print("Removed", fid)
            except Exception as exc:
                print("Failed to remove", fid, exc)

if __name__ == "__main__":
    main()
