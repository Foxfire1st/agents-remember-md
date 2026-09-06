"""Explicit modeled admission input for quality-plan tests with injected runners.

This never changes the process environment or the real Dagger attestation path.
Delivery still obtains its own capability through the actual executor handshake.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from agents_remember_test_support.testing.dagger_admission import (
    DAGGER_TEST_ATTESTATION_ENV,
    require_dagger_admission,
)

with TemporaryDirectory(prefix="ar-quality-input-") as directory:
    attestation = Path(directory) / "nonce"
    nonce = "0123456789abcdef0123456789abcdef"
    attestation.write_text(nonce, encoding="utf-8")
    QUALITY_TEST_ADMISSION = require_dagger_admission(
        environ={DAGGER_TEST_ATTESTATION_ENV: nonce}, attestation_path=attestation
    )
