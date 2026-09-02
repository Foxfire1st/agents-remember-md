set -eu

output=$1
mkdir -p "$(dirname "$output")"
printf '%s\n' '{"schemaVersion":"repository-selector-result/v1","complete":true,"selected-tests":["unit"]}' > "$output"
