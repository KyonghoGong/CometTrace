# Forensic parsing dependencies for CometTrace.
# Run inside an activated virtual environment (Python 3.10-3.12).
#
# ccl_chromium_reader is not on PyPI and is installed from source.
# It imports Brotli and the "zstd" module at load time, so both codecs
# must be present or `import ccl_chromium_reader` fails with
# ModuleNotFoundError: No module named 'zstd'.

python -m pip install Brotli
python -m pip install zstandard zstd
python -m pip install "https://github.com/cclgroupltd/ccl_simplesnappy/archive/refs/heads/master.zip"
python -m pip install --no-deps "https://github.com/cclgroupltd/ccl_chromium_reader/archive/refs/heads/master.zip"
