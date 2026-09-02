# Repository Certification Profiles

A repository certification profile is the repository-owned authority for the concrete work in
closeout Gates 1–4. Agents Remember owns the meanings and fixed order of those gates; each
repository owns its commands, runtimes, selectors, artifacts, result decoder, and sandbox adapter.
Gate 5 remains the Agents Remember memory/coherence gate and is not configurable here.

The MCP authority settings select one exact file:

```json
{
  "repositories": {
    "my-repository": {
      "certificationProfile": "config/repository-certification.json"
    }
  }
}
```

The value is an explicit repository-relative path. There is no default filename, directory scan,
wrapper-presence check, repository-name special case, or previously successful fallback. The
resolved file must be a real regular file under the selected repository. Absolute paths, `..`,
backslashes, symlink components, missing files, and repository-identity mismatches refuse before
any repository command starts.

## Gate meanings

The profile can select repository work but cannot redefine these classifications:

| Gate | Fixed meaning | Profile obligation |
| --- | --- | --- |
| 1 | Deterministic pre-test quality | Rails do not consume Gate-2 results and do not need the Gate-4 clean room. |
| 2 | Ordinary test suite | Rails declare exact test-selection authority and publish their exact-candidate suite artifacts. |
| 3 | Suite-dependent quality | Every rail consumes a declared Gate-2 artifact or green suite result. |
| 4 | Integration and E2E | Every rail declares clean-room execution and always-run teardown. |

Every selection contains Gates 1, 2, 3, and 4 in that order. A repository with no work for one
gate records `status: "not-applicable"`, a repository-owned `reason`, no rail IDs, and no hidden
work. Not-applicable is a typed result; it is not pass or skip.

## Required selections

Version 1 requires one unambiguous selection for each purpose and altitude:

| Purpose | Mode | Use |
| --- | --- | --- |
| `local-precommit` | `targeted` | Repository-local fast selection over the shared rail catalog. |
| `closeout` | `targeted` | Exact leaf candidate. |
| `closeout` | `full` | Exact master integration candidate. |

Selections reference rail identities. They do not copy rail commands. A repeated purpose/mode
pair is ambiguous and fails admission.

## Version 1 document shape

The top-level `schemaVersion` is `repository-certification-profile/v1`. Unknown fields are
rejected. A profile contains:

| Field | Meaning |
| --- | --- |
| `semanticRevision` | Semantic version of the repository contract. |
| `repositoryId` | Exact MCP repository id selected by settings. |
| `profileId` | Stable repository-owned profile identity. |
| `profileDigest` | SHA-256 semantic digest of the canonical profile with this field omitted. |
| `selections` | Explicit local/closeout, targeted/full selections over the rail catalog. |
| `rails` | Versioned Gate 1–4 rail definitions. |
| `selectors` | Exact Gate-2 population authorities and their configuration identities. |
| `executorAdapters` | Sandboxed repository-function adapters. Version 1 supports a declared Dagger module function. |
| `resultDecoders` | Typed terminal-result decoders. Version 1 supports a declared JSON status/exit-code artifact plus declarative artifact-reference checks. |
| `publishedArtifacts` | Exact bounded publication inventory; undeclared exports are rejected and required entries are mandatory on pass. |

Each rail declares:

- a stable `railId` plus semantic `version`, fixed gate and rail class, enforcing or report-only
  posture, owner and corrective owner;
- deterministic `orderKey`, rail prerequisites, required earlier-gate artifacts, and declared
  output artifacts;
- runtime/toolchain/image/lock/environment/secret-policy identities through `runtimeInputs`;
- evidence media types and byte budgets;
- a repository-owned execution definition: adapter identity, working directory, argument vector,
  environment contract, scope provider and selectors, decoder, timeout, resource policy, and
  immutable execution evidence reference; and
- Gate-4 clean-room and teardown declarations where applicable.

Each JSON terminal decoder declares its exact artifact and status/exit-code field/value mapping.
When the repository result also refers to other published evidence, `artifactReferences` declares
each structured object-key path and whether it contains one string path or a list of string paths.
Every observed reference must be a safe relative path present in the exact exported artifact
inventory. A reference rule may explicitly treat JSON null as no reference while retaining field
presence for activation checks; otherwise null is invalid. A separate null-parent policy may treat
an optional null parent object as an absent nested reference without accepting null for the final
field. A `referenceActivations` entry may name
one string-list field, one activating value, and a set of declared reference fields. When that
selector field exists, all named references are required if it contains the value and forbidden if
it does not; an absent selector leaves the reference fields governed only by their ordinary
path/inventory checks. A repository may explicitly give the activation selector an
`ignore-activation` null policy when its predecessor contract treats JSON null exactly like an
absent selector; otherwise null is invalid. This preserves repository-specific result
relationships as profile data.
The generic decoder does not recognize a repository's field names or step names.

Rail commands are data owned by the repository, but they never become host commands. Version 1's
typed `dagger-module` adapter resolves only the Dagger CLI; its `executable` field must therefore
be exactly `dagger` and cannot select a candidate-relative or arbitrary host program. The MCP
invokes that admitted sandbox adapter against the exact staged candidate and Git ancestry bundle.
The repository adapter is responsible for executing its admitted rail contract inside that
boundary. A missing adapter runtime is an execution-prerequisite failure, not permission to run
the command on the host.

If the admitted adapter runtime is unavailable when execution begins, the MCP raises
`certification-executor-prerequisite-failed`. Its finding names the adapter, frozen runtime
digest, earliest affected gate, complete affected-gate set, and repository corrective owners.
No host command or alternate adapter is attempted.

## Graph rules

Admission aggregates independent findings before execution. Among other checks, it rejects:

- duplicate rail identities, multiple versions of one rail ID, or duplicate selection, selector,
  adapter, decoder, artifact, or field identities;
- missing or ambiguous required selections;
- a Dagger adapter that names an arbitrary host executable;
- missing, duplicated, reordered, or wrongly classified gates;
- unknown selected rails, adapters, decoders, or scope providers;
- a prerequisite in a later gate or a same-gate cycle;
- undeclared or ambiguously produced artifacts;
- a Gate-2 rail without test-selection authority or suite artifacts;
- a Gate-3 rail without a declared Gate-2 input;
- clean-room work outside Gate 4, or Gate-4 work without always-run teardown;
- a decoder whose authoritative artifact is absent or optional;
- duplicate artifact-reference paths, or an activation that names a reference field not declared
  by the same decoder; and
- a declared rail that belongs to no selection.

Schema, digest, authority, and graph failures use the typed status
`certification-profile-invalid`. No Gate-1 command starts.

## Canonicalization and digests

Canonicalization sorts identity-addressed collections and set-like fields before hashing. List
reordering therefore does not create a new semantic identity, while a command, selector,
runtime, artifact, or other semantic edit does.

When authoring a profile, set `profileDigest` to 64 zeroes while the content is changing, then use
the installed library to calculate the final digest:

```python
from pathlib import Path

from agents_remember.certification.repository_profiles import repository_profile_digest
from agents_remember.certification.repository_profiles.models import (
    RepositoryCertificationProfile,
)

path = Path("config/repository-certification.json")
profile = RepositoryCertificationProfile.model_validate_json(path.read_text(encoding="utf-8"))
print(repository_profile_digest(profile))
```

Write that exact value into `profileDigest`, then admit the file through the selected repository
context:

```python
from pathlib import Path

from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles import (
    compile_repository_profile_plan,
    load_repository_profile,
)

root = Path("/absolute/path/to/my-repository")
admitted = load_repository_profile(
    "my-repository",
    root,
    Path("config/repository-certification.json"),
)
for selection in admitted.canonical.profile.selections:
    compile_repository_profile_plan(
        admitted.canonical,
        selection_id=selection.selectionId,
        candidate_identity=CandidateIdentity(kind="content-digest", value="0" * 64),
    )
```

Do not reuse the zero digest, copy another repository's commands, or edit the digest by guessing.
Agents Remember's checked-in `mcp/certification-profile-v1.json` is a concrete instance for that
repository, not a generic default.

That reference instance records the complete approved Agents Remember inventory: runtime,
dependency, configuration/scope, generated projections/skills/runtime/harness, Python and
dashboard Gate-1 checks including exact selection ownership; both ordinary suites in Gate 2;
suite-artifact consumers in Gate 3; and the current clean-room, real-client, provider, browser,
and teardown/process-cleanliness work in Gate 4. The inventory remains repository data. Generic
profile code neither imports nor recognizes those rail names.

## Execution and evidence

Closeout reconstructs the exact staged candidate in a disposable sandbox, loads the profile from
that candidate, and freezes the profile source hash, semantic digest, selected plan, adapter, and
decoder in the admission manifest. The sandbox adapter exports only the profile-declared bounded
artifact inventory. A passing terminal result requires every selected artifact marked `required`;
a failing terminal result is still published without not-yet-produced pass artifacts so its
concrete rail/step failure is not replaced by a generic publication exception. Publication is
atomic and immutable by generation; the current pointer names the exact candidate tree, profile,
selection, plan, adapter, decoder, and file hashes.

Selector output is also bounded. A selector command that exits zero but publishes a missing,
unsafe, oversized, malformed, wrong-schema, incomplete, or shape-invalid result becomes a typed
failed selector step with its ID, exit code, and failure detail in the terminal result. It cannot
replace the terminal result with an adapter exception.

Each gate plan retains the admitted aggregate `profileDigest`, but its own `planDigest` covers only
that gate's semantic plan and candidate. This keeps an unchanged earlier gate's identity stable
when a later gate changes; the fixed gate dependency chain then invalidates the changed gate and
its downstream certificate closure without rebinding unrelated earlier work.

The human `reports/test-results.md` summary names the selected adapter and profile identities. The
machine result comes from the declared decoder artifact, whose path is returned as
`publishedResultPath`. Its declared artifact references are checked against the same immutable
generation before pass/fail is accepted. A generic exception cannot substitute another result
file or silently discard a repository-owned result relationship.

An interrupted run can be recovered only when candidate tree, attestation, profile digest, plan
digest, selection, adapter, decoder, and published artifact hashes still match. A profile edit
creates new admission authority. There is no host, legacy-reader, alternate-registry, or report
filename fallback.

## Onboarding checklist

Before declaring a repository ready for code closeout:

1. Inspect its documented build, static checks, suite, suite-dependent checks, and clean-room
   scenarios. Resolve a real semantic ambiguity with the repository owner; do not infer commands
   from filenames or executable presence.
2. Author one repository-owned profile and compute its canonical digest.
3. Compile every required selection and repair all admission findings.
4. Add `certificationProfile` to that repository's MCP authority settings entry and restart the
   MCP/harness because repository authority settings are read at boot.
5. Run focused repository verification for its adapter and decoder before the first lifecycle
   closeout.

Older tasks and repositories without a profile remain readable and onboardable. They simply have
no code-certification authority and fail closed if a later operation would need one.
