# Python test commands and isolation

Ordinary development uses `mcp/.venv/bin/python -m pytest`, or an explicit test file.
The default marker expression is `not integration`; four workers run independent tests.
Use `-n=0` for serial debugging. There is no Dagger admission, coverage scoring, evidence
catalog scan, or automatic application composition in this command.

Delivery boundaries run with `mcp/.venv/bin/python -m pytest -m integration`.
They retain real temporary code/memory Git publication, interruption and retry, competing
writers, checkout isolation, essential application wiring, and one observation of each
consequential repository contract. Small parser, selector, retry-state and validation tests
remain in the ordinary population even when the product feature concerns certification.

The existing Dagger delivery plan explicitly supplies `--certify -m ""` to run both
populations together. The combined branch-coverage artifact feeds changed-production
coverage (90%) and production CRAP (unchanged threshold 30). No host run can create a
Dagger admission capability or become certifying by omitting that option.

Root conftest pins candidate imports, reuses the existing reversible Git environment setup
and test-process declaration, and creates disposable home/config/data/cache directories
before collection. Inherited credentials and live-provider opt-ins are removed. Existing
owned-global restoration remains active. Application composition is an explicit
`worktree_services` fixture for the tests that need it, not an autouse unit fixture.

Recursive retry-matrix, repeated whole-suite collection, historical count/retirement proofs
and their unused drivers are removed. Supported retry selection and certificate behavior
retain direct input/output assertions and limited real boundary tests. Existing lifecycle
certification, memory workflows and review ownership are otherwise unchanged.
