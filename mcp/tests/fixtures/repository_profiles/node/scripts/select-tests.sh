set -eu

output=$1
mode=$2
base=$3
candidate_kind=$4
candidate_value=$5
selector_id=$6
selector_version=$7
configuration_digest=$8
population=$mode
global_invalidators='[]'
if [ "$mode" = full ]; then
    global_invalidators='["declared-full-mode"]'
fi
mkdir -p "$(dirname "$output")"
payload=$(printf '%s' "{\"baseRevision\":\"$base\",\"candidateIdentity\":{\"kind\":\"$candidate_kind\",\"value\":\"$candidate_value\"},\"complete\":true,\"configurationDigest\":\"$configuration_digest\",\"dependencyReasons\":[{\"detail\":\"fixture-owned-test\",\"effect\":\"select\",\"input\":\"test/unit.test.mjs\",\"kind\":\"declared-consumer\",\"outputArtifact\":\"selected-tests\",\"outputValue\":\"test/unit.test.mjs\"}],\"failureCode\":null,\"globalInvalidators\":$global_invalidators,\"mode\":\"$mode\",\"outputs\":[{\"artifactId\":\"selected-tests\",\"values\":[\"test/unit.test.mjs\"]}],\"population\":\"$population\",\"schemaVersion\":\"repository-selector-result/v2\",\"selectorId\":\"$selector_id\",\"selectorVersion\":\"$selector_version\",\"unresolvedInputs\":[]}")
digest=$(printf '%s' "$payload" | sha256sum | cut -d ' ' -f 1)
printf '%s\n' "${payload%?},\"selectionDigest\":\"$digest\"}" > "$output"
