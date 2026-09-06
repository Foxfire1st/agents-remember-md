"""Tests for the post-``model_dump`` mutation fitness function.

The repository test scans the production package. Synthetic bites cover inline,
memoized, pass-through, copied, merged, and mutated dumps; known-good fixtures pin
re-validation, model-before-dump edits, unrelated mappings, read-only access, and the
declared served-tail owner. Offender tests require complete actionable output.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

import pytest
from agents_remember_test_support.code_quality import wire_contract

pytestmark = pytest.mark.fitness

PACKAGE_ROOT = MCP_SRC / "agents_remember"


def _wire(source: str) -> list[str]:
    """What the rule reports for a single-module ``source``, as ``line [form]`` strings."""
    tree = ast.parse(source)
    trees = {"fixture.py": tree}
    return [
        f"{offender.line} [{offender.form}]"
        for offender in wire_contract.module_mutation_offenders(
            tree,
            "fixture.py",
            producers=wire_contract.dump_returning_names(trees),
            validators=wire_contract.validating_names(trees),
        )
    ]


class PostDumpMutationTests(unittest.TestCase):
    """The armed check. It runs in the ordinary suite, so it runs wherever the suite does."""

    @pytest.mark.integration
    def test_no_dumped_payload_is_mutated_after_it_leaves_its_model(self) -> None:
        offenders = wire_contract.post_dump_mutation_offenders(PACKAGE_ROOT)
        self.assertEqual(
            [str(offender) for offender in offenders],
            [],
            msg=wire_contract.report(
                offenders,
                headline="a payload is changed after model_dump stopped describing it",
                remediation=wire_contract.WIRE_DUMP_REMEDIATION,
            ),
        )


class FunctionBoundaryTests(unittest.TestCase):
    """Detect dump-derived values returned across a function boundary."""

    def test_a_function_returning_a_dump_makes_its_callers_taint_sources(self) -> None:
        source = (
            "def build(model):\n"
            "    return model.model_dump()\n"
            "\n"
            "def serve(model):\n"
            "    body = build(model)\n"
            "    body['extra'] = 1\n"
            "    return body\n"
        )
        self.assertEqual(_wire(source), ["6 [post-dump mutation]"])

    def test_a_dump_memoized_through_a_tuple_is_still_a_dump(self) -> None:
        # The exact shape of `_ProjectionBodyCache.body`: the dump is stashed in a tuple
        # and returned by index, so plain name-binding taint cannot follow it.
        source = (
            "class Cache:\n"
            "    def body(self, projection):\n"
            "        entry = (projection, projection.model_dump(by_alias=True))\n"
            "        self._entry = entry\n"
            "        return entry[1]\n"
            "\n"
            "cache = Cache()\n"
            "\n"
            "def serve(projection):\n"
            "    payload = dict(cache.body(projection))\n"
            "    payload['servingBuild'] = stamp\n"
            "    return payload\n"
        )
        self.assertEqual(_wire(source), ["11 [post-dump mutation]"])

    def test_a_pass_through_carries_the_dump_through_itself(self) -> None:
        # `finalize_payload_tokens` returns the dict it was handed. Without this the wire
        # choke point's payload is untainted and every mutation below it is invisible --
        # which is exactly what a planted violation there proved before this was added.
        source = (
            "def stamp(payload):\n"
            "    payload['tokens'] = 0\n"
            "    return payload\n"
            "\n"
            "def emit(model):\n"
            "    finalized = stamp(model.model_dump(mode='json'))\n"
            "    finalized['debugTrace'] = name\n"
            "    return finalized\n"
        )
        self.assertEqual(_wire(source), ["7 [post-dump mutation]"])

    def test_a_pass_through_handed_no_dump_produces_no_taint(self) -> None:
        # The pass-through arm resolves against the ARGUMENT, not the callee alone, so an
        # ordinary helper that returns its parameter does not taint the whole package.
        source = (
            "def stamp(payload):\n"
            "    return payload\n"
            "\n"
            "def emit():\n"
            "    plain = stamp({'a': 1})\n"
            "    plain['b'] = 2\n"
            "    return plain\n"
        )
        self.assertEqual(_wire(source), [])

    def test_a_validating_helper_is_followed_across_its_own_boundary(self) -> None:
        # application/task_doc_tools.py re-validates through a local `_validate(data)`.
        source = (
            "def _validate(data):\n"
            "    return TaskDocument.model_validate(data)\n"
            "\n"
            "def edit(doc):\n"
            "    data = doc.model_dump(by_alias=True)\n"
            "    data['status'] = 'Completed'\n"
            "    return _validate(data)\n"
        )
        self.assertEqual(_wire(source), [])


class WireSweepReachTests(unittest.TestCase):
    """Every mutation and laundering form the rule claims to catch."""

    def test_a_plain_key_assignment_is_caught(self) -> None:
        source = "def f(m):\n    p = m.model_dump()\n    p['x'] = 1\n    return p\n"
        self.assertEqual(_wire(source), ["3 [post-dump mutation]"])

    def test_every_dict_mutator_is_swept_not_only_setitem(self) -> None:
        source = (
            "def f(m):\n"
            "    p = m.model_dump()\n"
            "    p.update(tail)\n"
            "    p.setdefault('k', [])\n"
            "    p.pop('j', None)\n"
            "    p.clear()\n"
            "    return p\n"
        )
        self.assertEqual(_wire(source), [f"{line} [post-dump mutation]" for line in (3, 4, 5, 6)])

    def test_deleting_and_merging_in_place_are_mutations(self) -> None:
        deleted = "def f(m):\n    p = m.model_dump()\n    del p['x']\n    return p\n"
        merged = "def f(m):\n    p = m.model_dump()\n    p |= tail\n    return p\n"
        self.assertEqual(_wire(deleted), ["3 [post-dump mutation]"])
        self.assertEqual(_wire(merged), ["3 [post-dump mutation]"])

    def test_a_copy_does_not_launder_the_dump(self) -> None:
        # Mutating a copy of a dumped body ships exactly the same undeclared key.
        for form in ("dict(m.model_dump())", "m.model_dump().copy()", "{**m.model_dump()}"):
            source = f"def f(m):\n    p = {form}\n    p['x'] = 1\n    return p\n"
            self.assertEqual(_wire(source), ["3 [post-dump mutation]"], form)

    def test_a_deep_copy_taken_as_an_argument_carries_the_taint(self) -> None:
        # `copy.deepcopy(dump)` -- the argument form, as opposed to `dump.copy()`.
        source = (
            "import copy\n"
            "def f(m):\n"
            "    p = copy.deepcopy(m.model_dump())\n"
            "    p['x'] = 1\n"
            "    return p\n"
        )
        self.assertEqual(_wire(source), ["4 [post-dump mutation]"])

    def test_an_augmented_assignment_to_a_key_is_a_mutation(self) -> None:
        source = "def f(m):\n    p = m.model_dump()\n    p['count'] += 1\n    return p\n"
        self.assertEqual(_wire(source), ["3 [post-dump mutation]"])

    def test_an_augmented_assignment_off_the_dump_is_not_a_mutation(self) -> None:
        # A counter and an untainted dict's key: both are AugAssign, neither is the payload.
        source = (
            "def f(m, other):\n"
            "    p = m.model_dump()\n"
            "    counter = 0\n"
            "    counter += 1\n"
            "    other['x'] += 1\n"
            "    return p, counter\n"
        )
        self.assertEqual(_wire(source), [])

    def test_deleting_from_an_untainted_dict_beside_a_dump_is_not_reported(self) -> None:
        source = (
            "def f(m, other):\n"
            "    p = m.model_dump()\n"
            "    del other['x']\n"
            "    del p['y'], other['z']\n"
            "    return p\n"
        )
        self.assertEqual(_wire(source), ["4 [post-dump mutation]"])

    def test_a_dict_mutator_on_an_untainted_name_is_not_reported(self) -> None:
        source = (
            "def f(m, other):\n"
            "    p = m.model_dump()\n"
            "    other.update(extra)\n"
            "    return p, other\n"
        )
        self.assertEqual(_wire(source), [])

    def test_a_dump_bound_through_another_name_is_still_tracked(self) -> None:
        source = (
            "def f(m):\n    first = m.model_dump()\n    second = first\n"
            "    second['x'] = 1\n    return second\n"
        )
        self.assertEqual(_wire(source), ["4 [post-dump mutation]"])

    def test_a_mutation_before_a_validation_of_a_DIFFERENT_dict_is_still_reported(self) -> None:
        source = (
            "def f(m, other):\n"
            "    p = m.model_dump()\n"
            "    p['x'] = 1\n"
            "    Model.model_validate(other)\n"
            "    return p\n"
        )
        self.assertEqual(_wire(source), ["3 [post-dump mutation]"])


class WireSweepFalsePositiveTests(unittest.TestCase):
    """Known-good constructs the package really contains. None of these may be reported."""

    def test_the_task_document_round_trip_is_not_an_escape(self) -> None:
        # The nine-site shape in finalize.py, reopen.py, leaf_doc.py, master_sync.py and
        # task_doc_tools.py. The dumped dict never reaches a consumer; the model checks it.
        source = (
            "def f(doc):\n"
            "    data = doc.model_dump(by_alias=True)\n"
            "    data['status'] = 'Completed'\n"
            "    data.setdefault('decisions', []).append(entry)\n"
            "    updated = TaskDocument.model_validate(data)\n"
            "    return updated\n"
        )
        self.assertEqual(_wire(source), [])

    def test_setting_fields_on_the_model_before_the_dump_is_the_sanctioned_pattern(self) -> None:
        # mcp/tools/base.py: the choke point writes nextStep/agentNotifierBanner onto the
        # MODEL, then dumps once. This is what the remediation asks for.
        source = (
            "def f(model):\n"
            "    model.nextStep = step\n"
            "    model.agentNotifierBanner = banner\n"
            "    return model.model_dump(mode='json', exclude_none=True)\n"
        )
        self.assertEqual(_wire(source), [])

    def test_a_dict_that_never_came_from_a_model_is_not_a_payload(self) -> None:
        source = "def f():\n    p = {'a': 1}\n    p['b'] = 2\n    return p\n"
        self.assertEqual(_wire(source), [])

    def test_reading_a_dumped_dict_is_not_mutating_it(self) -> None:
        source = (
            "def f(m):\n"
            "    p = m.model_dump()\n"
            "    value = p['x']\n"
            "    keys = sorted(p)\n"
            "    other = {**p, 'extra': 1}\n"
            "    return value, keys, other\n"
        )
        self.assertEqual(_wire(source), [])

    def test_mutating_a_dict_built_beside_a_dump_is_not_reported(self) -> None:
        # A report dict assembled next to a dump -- extremely common, never an escape.
        source = (
            "def f(m):\n"
            "    payload = m.model_dump()\n"
            "    report = {'path': p}\n"
            "    report['state'] = 'ok'\n"
            "    return payload, report\n"
        )
        self.assertEqual(_wire(source), [])

    def test_attribute_and_element_mutation_are_out_of_scope_and_stay_that_way(self) -> None:
        # Known blind spot: only local names are tracked. Wider tracking requires an
        # explicit contract change and new bites.
        source = (
            "def f(m):\n"
            "    p = m.model_dump()\n"
            "    self._cache['k'] = p\n"
            "    rows[0]['k'] = 1\n"
            "    return p\n"
        )
        self.assertEqual(_wire(source), [])

    def test_a_str_replace_style_call_on_a_dump_is_not_a_dict_mutator(self) -> None:
        source = "def f(m):\n    p = m.model_dump_json()\n    p.replace('a', 'b')\n    return p\n"
        self.assertEqual(_wire(source), [])


class SanctionedOwnerTests(unittest.TestCase):
    """The one permitted serve-time tail builder -- an owner, not an exception entry."""

    def test_a_hand_written_tail_key_is_reported_even_beside_the_owner_call(self) -> None:
        # The permission is the CALL, not the file and not the line. Writing the same key
        # by hand next to a sanctioned merge is still an escape.
        source = (
            "def stream(model):\n"
            "    payload = dict(model.model_dump(by_alias=True))\n"
            "    payload.update(served_state_tail(build=b, heartbeat=h))\n"
            "    payload['servingBuild'] = stamp\n"
            "    return payload\n"
        )
        self.assertEqual(_wire(source), ["4 [post-dump mutation]"])

    def test_the_owner_call_alone_is_not_reported(self) -> None:
        source = (
            "def stream(model):\n"
            "    payload = dict(model.model_dump(by_alias=True))\n"
            "    payload.update(served_state_tail(build=b, heartbeat=h))\n"
            "    return payload\n"
        )
        self.assertEqual(_wire(source), [])

    def test_a_tail_merged_from_anything_but_the_owner_is_reported(self) -> None:
        # The complement: `update()` is not blanket-permitted on a dumped body. Only the
        # declared builder is, because only it declares which keys it may add.
        source = (
            "def stream(model):\n"
            "    payload = dict(model.model_dump(by_alias=True))\n"
            "    payload.update(some_other_tail(build=b))\n"
            "    return payload\n"
        )
        self.assertEqual(_wire(source), ["3 [post-dump mutation]"])


class OffenderReportTests(unittest.TestCase):
    """L6-R15: the message names every offender and the fix, or the check is unusable."""

    def test_the_message_carries_the_whole_list_and_the_remediation(self) -> None:
        offenders = [
            wire_contract.Offender("a/one.py", 12, "post-dump mutation", "assigns a new key"),
            wire_contract.Offender("b/two.py", 40, "post-dump mutation", "update() on the dict"),
        ]
        message = wire_contract.report(
            offenders, headline="payloads escape their models", remediation="declare the field"
        )
        self.assertIn("(2 found)", message)
        self.assertIn("a/one.py:12", message)
        self.assertIn("b/two.py:40", message)
        self.assertIn("remediation: declare the field", message)


if __name__ == "__main__":
    unittest.main()
