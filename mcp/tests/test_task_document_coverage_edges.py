"""Defensive branch coverage for task topology schema helpers."""

from __future__ import annotations

import unittest
from unittest import mock

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import document as document_module
from agents_remember.tasks import document_field_effects as effects_module
from agents_remember.tasks import execution_graph_validation as validation_module
from agents_remember.tasks import leaf_binding as binding_module
from agents_remember.tasks.document import (
    SprintExecutionEdge,
    SprintExecutionNode,
    SubTaskRef,
    TaskDocument,
)
from agents_remember.tasks.document_field_effects import (
    TaskDocumentFieldEffect,
    TaskDocumentFieldEffectProjector,
    TaskDocumentFieldEffectTaxonomyError,
)
from agents_remember.tasks.leaf_binding import CanonicalLeafBindingError
from pydantic import BaseModel

NOW = "2026-08-31T00:00:00+00:00"
REPOSITORY = "agents-remember"
MASTER_REF = TaskDocumentRef(repository=REPOSITORY, path="master/task.json")
LEAF_REF = TaskDocumentRef(repository=REPOSITORY, path="master/leaf.json")


def _document(
    *,
    kind: str,
    identifier: str,
    slug: str,
    rows: list[SubTaskRef] | None = None,
) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": identifier,
            "slug": slug,
            "title": slug,
            "kind": kind,
            "status": "inProgress",
            "repo": REPOSITORY,
            "createdAt": NOW,
            "subTasks": [row.model_dump(mode="json") for row in rows or []],
        }
    )


class TaskDocumentCoverageEdgeTests(unittest.TestCase):
    def test_cycle_compatibility_wrapper_translates_indexed_validation_error(self) -> None:
        node = SprintExecutionNode(ref=MASTER_REF)
        missing = TaskDocumentRef(repository=REPOSITORY, path="missing/task.json")
        graph = document_module.SprintExecutionGraph.model_construct(
            nodes=[node],
            edges=[
                SprintExecutionEdge(
                    predecessor=node.ref,
                    successor=missing,
                    reason="Invalid endpoint for translation coverage.",
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "endpoint"):
            document_module._find_cycle_members(graph, [node])

    def test_indexed_cycle_search_covers_empty_and_multi_residual_paths(self) -> None:
        self.assertEqual(
            validation_module._find_cycle_members(
                [],
                [],
                validation_module._WorkCounter(),
            ),
            (),
        )
        left = SprintExecutionNode(ref=MASTER_REF)
        right = SprintExecutionNode(
            ref=TaskDocumentRef(repository=REPOSITORY, path="right/task.json")
        )
        independent = SprintExecutionNode(
            ref=TaskDocumentRef(repository=REPOSITORY, path="independent/task.json")
        )
        edges = [
            SprintExecutionEdge(predecessor=left.ref, successor=right.ref, reason="right"),
            SprintExecutionEdge(predecessor=right.ref, successor=left.ref, reason="left"),
        ]
        with self.assertRaises(validation_module.ExecutionGraphValidationError) as raised:
            validation_module.validate_execution_graph([left, right, independent], edges)
        self.assertEqual(
            raised.exception.detail,
            f"execution-graph must be acyclic; cycle members: {left.ref.key} -> {right.ref.key}",
        )
        self.assertNotIn(independent.ref.key, raised.exception.detail)
        self.assertEqual(raised.exception.work.waveNodeVisits, 1)
        self.assertEqual(raised.exception.work.residualNodeChecks, 3)
        self.assertEqual(raised.exception.work.cycleNodeVisits, 2)

    def test_taxonomy_refuses_unknown_models_and_defensive_lookup_loss(self) -> None:
        class UnknownDocumentPart(BaseModel):
            value: str = "unknown"

        with self.assertRaisesRegex(
            TaskDocumentFieldEffectTaxonomyError,
            "unclassified task-document schema model",
        ):
            effects_module.validate_task_document_field_effects(root_model=UnknownDocumentPart)

        structural = effects_module.fields_with_effect(
            TaskDocument,
            TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY,
        )
        self.assertIn("id", structural)

        with (
            mock.patch.object(
                effects_module,
                "validate_task_document_field_effects",
                return_value=None,
            ),
            self.assertRaisesRegex(
                TaskDocumentFieldEffectTaxonomyError,
                "unclassified task-document schema model",
            ),
        ):
            effects_module.fields_with_effect(
                UnknownDocumentPart,
                TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY,
            )

        projector = TaskDocumentFieldEffectProjector()
        document = _document(kind="subTask", identifier="L01", slug="leaf")
        with (
            mock.patch.dict(effects_module.TASK_DOCUMENT_FIELD_EFFECTS, {}, clear=True),
            self.assertRaisesRegex(
                TaskDocumentFieldEffectTaxonomyError,
                "unclassified task-document schema model",
            ),
        ):
            projector.project(document, TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY)

    def test_taxonomy_projects_mapping_enum_and_classified_base_model(self) -> None:
        class ProjectionEnvelope(BaseModel):
            refs: dict[str, TaskDocumentRef]
            effect: TaskDocumentFieldEffect

        classification = {
            "refs": frozenset({TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY}),
            "effect": frozenset({TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY}),
        }
        envelope = ProjectionEnvelope(
            refs={"z": LEAF_REF, "a": MASTER_REF},
            effect=TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY,
        )
        with mock.patch.dict(
            effects_module.TASK_DOCUMENT_FIELD_EFFECTS,
            {ProjectionEnvelope: classification},
        ):
            projected = effects_module.project_model_field_effect(
                envelope,
                TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY,
            )
        projected_refs = projected["refs"]
        assert isinstance(projected_refs, dict)
        self.assertEqual(list(projected_refs), ["a", "z"])
        self.assertEqual(projected["effect"], "structural-topology")

        class DerivedTaskDocumentRef(TaskDocumentRef):
            pass

        inherited = effects_module.project_model_field_effect(
            DerivedTaskDocumentRef(repository=REPOSITORY, path="master/task.json"),
            TaskDocumentFieldEffect.STRUCTURAL_TOPOLOGY,
        )
        self.assertEqual(
            inherited,
            {"repository": REPOSITORY, "path": "master/task.json"},
        )

    def test_leaf_binding_rejects_invalid_document_and_parent_shapes(self) -> None:
        row = SubTaskRef(
            number="L01",
            name="Leaf",
            file="leaf.md",
            status="inProgress",
        )
        leaf = _document(kind="subTask", identifier="L01", slug="leaf")
        master = _document(kind="master", identifier="MASTER", slug="master", rows=[row])

        with self.assertRaises(CanonicalLeafBindingError) as raised:
            binding_module.require_leaf_parent_row(leaf, "L01")
        self.assertEqual(raised.exception.status, "task-leaf-binding-parent-invalid")
        self.assertEqual(
            raised.exception.detail,
            "canonical leaf binding requires a master parent document",
        )

        with self.assertRaises(CanonicalLeafBindingError) as raised:
            binding_module.canonical_leaf_source(
                TaskDocumentRef(repository=REPOSITORY, path="master/not-task.json"),
                row,
            )
        self.assertEqual(raised.exception.status, "task-leaf-binding-parent-invalid")
        self.assertEqual(
            raised.exception.detail,
            "leaf parent must use a canonical task.json address: "
            f"{REPOSITORY}/master/not-task.json",
        )

        with self.assertRaises(CanonicalLeafBindingError) as raised:
            binding_module.require_canonical_leaf_binding(
                MASTER_REF,
                leaf,
                LEAF_REF,
                leaf,
            )
        self.assertEqual(raised.exception.status, "task-leaf-binding-parent-invalid")
        self.assertEqual(
            raised.exception.detail,
            "canonical leaf binding requires a master parent document",
        )

        with self.assertRaises(CanonicalLeafBindingError) as raised:
            binding_module.require_canonical_leaf_binding(
                MASTER_REF,
                master,
                LEAF_REF,
                master,
            )
        self.assertEqual(raised.exception.status, "task-leaf-binding-child-invalid")
        self.assertEqual(
            raised.exception.detail,
            "canonical leaf binding requires a subTask child document",
        )

    def test_leaf_binding_detects_split_stem_and_keeps_defensive_success_path(self) -> None:
        mismatched = SubTaskRef(
            number="L01",
            name="Leaf",
            file="other.md",
            status="inProgress",
        )
        invalid_stem = SubTaskRef(
            number="OTHER",
            name="Other",
            file="leaf.txt",
            status="inProgress",
        )
        parent = _document(
            kind="master",
            identifier="MASTER",
            slug="master",
            rows=[mismatched, invalid_stem],
        )
        candidate = _document(kind="subTask", identifier="L01", slug="leaf")
        with self.assertRaises(CanonicalLeafBindingError) as raised:
            binding_module.require_canonical_leaf_binding(
                MASTER_REF,
                parent,
                LEAF_REF,
                candidate,
            )
        self.assertEqual(raised.exception.status, "task-leaf-binding-identity-split")
        self.assertEqual(
            raised.exception.detail,
            "row number and file-stem source identify different candidate leaves",
        )

        exact = SubTaskRef(
            number="L01",
            name="Leaf",
            file="leaf.md",
            status="inProgress",
        )
        exact_parent = _document(
            kind="master",
            identifier="MASTER",
            slug="master",
            rows=[exact],
        )
        with mock.patch.object(
            binding_module,
            "_rows_for_candidate_source",
            return_value=[],
        ):
            binding = binding_module.require_canonical_leaf_binding(
                MASTER_REF,
                exact_parent,
                LEAF_REF,
                candidate,
            )
        self.assertEqual(binding.row, exact)
