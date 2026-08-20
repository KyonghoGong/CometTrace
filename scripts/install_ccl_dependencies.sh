# These commits define the 2026-08-09 post-hoc revalidation environment.
# They are not claimed to be the unrecorded commits used for the original
# paper-associated outputs.

python -m pip install Brotli
python -m pip install zstandard zstd

python -m pip install \
  "https://github.com/cclgroupltd/ccl_simplesnappy/archive/3d085230baa8c46cf2090ebba29bf6e8eab31087.zip"

python -m pip install --no-deps \
  "https://github.com/cclgroupltd/ccl_chromium_reader/archive/ef840de30221c4d65bc96d2f4d9057e9ef2f526d.zip"