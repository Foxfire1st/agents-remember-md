set -eu

suite_path=$1
proof_path=$2
test_name=$3
cargo test --locked --test "$test_name"
mkdir -p "$(dirname "$suite_path")"
printf '%s\n' "{\"status\":\"passed\",\"selectedTest\":\"$test_name\"}" > "$suite_path"
printf '%s\n' '{"suiteResult":"rust-suite.json","verified":true}' > "$proof_path"
