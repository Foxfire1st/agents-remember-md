set -eu

result_path=$1
cargo test --locked --test service
mkdir -p "$(dirname "$result_path")"
printf '%s\n' '{"status":"passed","tool":"cargo-test"}' > "$result_path"
