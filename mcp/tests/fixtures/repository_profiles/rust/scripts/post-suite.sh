set -eu

proof_path=$1
grep -F '"suiteResult":"rust-suite.json"' "$proof_path" >/dev/null
grep -F '"verified":true' "$proof_path" >/dev/null
