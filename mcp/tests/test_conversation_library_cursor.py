"""Library cursor/key authority and canonical scope contract tests (260718-CHATS-L2)."""

from __future__ import annotations

import pytest
from agents_remember.models.conversations.identity import (
    AuthorizationBinding,
)
from agents_remember.serving.conversation.library.cursor import (
    LibraryCursorAuthority,
    mint_signing_key,
)
from agents_remember.serving.conversation.library.errors import (
    InvalidLibraryCursorError,
    LibraryScopeError,
)
from agents_remember.serving.conversation.library.scope import (
    canonical_library_scope,
)

CALLER = AuthorizationBinding(principal_id="local-operator:1000", tenant_id="/ws")


def _scope(tmp_path, harness: str = "codex"):
    return canonical_library_scope(CALLER, harness, None, workspace_root=tmp_path)  # type: ignore[arg-type]


def _authority() -> LibraryCursorAuthority:
    return LibraryCursorAuthority(mint_signing_key())


def test_list_cursor_round_trip_and_tamper_rejection(tmp_path) -> None:
    authority = _authority()
    scope = _scope(tmp_path)
    cursor = authority.mint_list_cursor(scope, catalog_generation=7, native_cursor="native-abc")
    binding, position = authority.verify_list_cursor(cursor)
    assert binding.scope == scope
    assert binding.purpose == "library-list"
    assert binding.catalog_generation == 7
    assert position == "native-abc"

    token = str(cursor)
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    with pytest.raises(InvalidLibraryCursorError):
        authority.verify_list_cursor(type(cursor)(tampered))


def test_read_cursor_round_trip_and_wrong_purpose_rejection(tmp_path) -> None:
    authority = _authority()
    scope = _scope(tmp_path)
    read_cursor = authority.mint_read_cursor(scope, catalog_generation=3, native_cursor=41)
    binding, position = authority.verify_read_cursor(read_cursor)
    assert binding.purpose == "library-read"
    assert position == 41

    with pytest.raises(InvalidLibraryCursorError):
        authority.verify_list_cursor(read_cursor)  # type: ignore[arg-type]

    list_cursor = authority.mint_list_cursor(scope, catalog_generation=3, native_cursor="x")
    with pytest.raises(InvalidLibraryCursorError):
        authority.verify_read_cursor(list_cursor)  # type: ignore[arg-type]


def test_canonical_scope_rejects_traversal_symlink_and_cross_scope(tmp_path) -> None:
    outside = tmp_path.parent
    with pytest.raises(LibraryScopeError):
        canonical_library_scope(CALLER, "codex", str(outside), workspace_root=tmp_path)
    with pytest.raises(LibraryScopeError):
        canonical_library_scope(CALLER, "codex", "..", workspace_root=tmp_path)
    with pytest.raises(LibraryScopeError):
        canonical_library_scope(CALLER, "codex", "/etc", workspace_root=tmp_path)
    with pytest.raises(LibraryScopeError):
        canonical_library_scope(CALLER, "codex", "missing-dir", workspace_root=tmp_path)
    file_inside = tmp_path / "file.txt"
    file_inside.write_text("x")
    with pytest.raises(LibraryScopeError):
        canonical_library_scope(CALLER, "codex", str(file_inside), workspace_root=tmp_path)

    link = tmp_path / "escape-link"
    link.symlink_to(outside)
    with pytest.raises(LibraryScopeError):
        canonical_library_scope(CALLER, "codex", str(link), workspace_root=tmp_path)
