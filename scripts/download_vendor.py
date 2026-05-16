"""
Download vendored frontend assets for pipeline_editor.

Run once to vendor Litegraph.js locally so the pipeline editor has no
external network dependency at runtime:

    venv/Scripts/python.exe scripts/download_vendor.py
"""
import urllib.request
import os
import sys

VENDOR_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "web", "vendor")

ASSETS = [
    (
        "https://cdn.jsdelivr.net/npm/litegraph.js@0.7.18/build/litegraph.js",
        "litegraph.js",
    ),
]


def main():
    os.makedirs(VENDOR_DIR, exist_ok=True)
    for url, filename in ASSETS:
        dest = os.path.join(VENDOR_DIR, filename)
        print(f"Downloading {filename} ...", end=" ", flush=True)
        try:
            urllib.request.urlretrieve(url, dest)
            size_kb = os.path.getsize(dest) // 1024
            print(f"OK ({size_kb} KiB -> {dest})")
        except Exception as e:
            print(f"FAILED: {e}", file=sys.stderr)
            sys.exit(1)
    print("Done. Restart the server to serve vendored assets.")


if __name__ == "__main__":
    main()
