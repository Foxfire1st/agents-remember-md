# Python test policy and commands

Preserve important behavior with the smallest maintainable suite. A new case must protect a
distinct user operation, consequential failure, or actual regression. Extend, consolidate or
replace existing tests before adding overlapping protection. Private helper branches, coverage
percentages and higher test counts are not reasons to add tests. Remove unused support when
removing tests; do not hide a large suite behind integration markers.

The default unit budget is 1,000 parametrized collected cases. `pyproject.toml` also declares a
150-case integration ceiling. `pytest_collection_finish` checks selected items without a second
collection, dependency scan or test invocation. Combined verification observes both complete
populations. Growth beyond a budget requires an explicit tradeoff in the change description:
distinct protection, why consolidation is insufficient, case count, test/support size and
elapsed runtime. A changed configuration limit alone is not that justification.

## Commands

- Units: `mcp/.venv/bin/python -m pytest`.
- Integration: `mcp/.venv/bin/python -m pytest -m integration`.
- Combined: `mcp/.venv/bin/python -m pytest -m ""`.
- Focused debugging: supply a test file/node, optionally with `-n=0`.

Four workers run by default. The existing file membership list skips integration-only modules
before import during ordinary unit runs. Keep small parser, selection, reporting and retry
input/output tests in the unit population; use real application/process/publication boundaries
sparingly. Do not run nested pytest, repeated collection or repository scans to test the suite.

The Dagger delivery plan supplies `--certify -m ""` and collects branch coverage from production
and real Python children. Reuse the existing shared engine/configuration. Only genuine Dagger
admission and the existing lifecycle owners can produce certifying evidence. An ordinary host
run has no certification authority.

## Diagnostic metrics

Coverage, including changed-production coverage, is information only. There is no mandatory
percentage and uncovered lines do not automatically demand tests. CRAP uses production
functions only, excluding tests and verification support. Its threshold of 20 prompts review
but does not block delivery. Choose simpler production code, a meaningful behavioral test, or
a concise justified acceptance in the change description. Do not add a score-exception system,
coverage baseline or ratchet. Lint, formatting, typing, structural rules and test failures remain
enforcing; diagnostic-tool execution errors remain distinct visible failures.

## Isolation

Root conftest pins candidate imports, reuses the reversible Git environment setup and genuine
test-process declaration, and creates disposable home/config/data/cache directories before
collection. It removes inherited credentials and live-provider opt-ins. Owned-global restoration
remains active. Tests that need application composition request `worktree_services`; ordinary
units do not bootstrap it automatically. Preserve live repository, process and coordination
ownership boundaries. Test reduction changes neither memory workflows nor certification owners.
