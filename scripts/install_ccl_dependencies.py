"""Install the CCL revisions used for post-hoc revalidation into this interpreter."""
import subprocess
import sys

SNAPPY = (
    "https://github.com/cclgroupltd/ccl_simplesnappy/archive/"
    "3d085230baa8c46cf2090ebba29bf6e8eab31087.zip"
    "#sha256=42387593317196a8d3f273d09dacdf2b9c29cb22729f0b82cac436dd9f79755b"
)
READER = (
    "https://github.com/cclgroupltd/ccl_chromium_reader/archive/"
    "ef840de30221c4d65bc96d2f4d9057e9ef2f526d.zip"
    "#sha256=0df73d43c1626bb4d0d7858963910a46c0f137cafbe5551ba7c3463bed0ae1a5"
)


def main():
    pip = [sys.executable, "-m", "pip"]
    subprocess.run([*pip, "install", "Brotli>=1.1.0,<=1.2.0", "zstandard>=0.22.0",
                    "zstd==1.5.7.2", SNAPPY], check=True)
    # The reader metadata points to a moving Git ref. Install its pinned dependencies above.
    subprocess.run([*pip, "install", "--no-deps", READER], check=True)
    subprocess.run([*pip, "check"], check=True)


if __name__ == "__main__":
    main()
