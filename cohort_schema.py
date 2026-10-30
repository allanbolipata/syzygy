"""Cohort request / validation-report schema helpers (T3).

Vendors the two JSON schemas staged verbatim from ``mskcc/ccs_cohort_utils``
(branch main, 6e92d89) in ``schemas/`` next to this file and exposes thin
validation helpers on top:

- ``strip_pi_name`` — drops the top-level keys the request schema rejects
  (``piName``) plus ``status``/``date`` (legacy stripped both before
  validation too),
- ``validate_cohort_request`` — schema-validates a raw SMILE cohort request
  and returns the cleaned+normalized cohort (legacy ``Cohort.__init__``
  semantics, G3) so ``cohort_crf.to_crf`` gets exactly what the legacy
  pipeline serialized,
- ``validate_report`` — self-checks a validation report against the
  (fixed) validator schema before it is published.

Documented assumption (G7e, user-confirmed): SMILE always sends both
``primaryId`` and ``cmoId`` per sample. The request schema only requires
anyOf ``primaryId``|``cmoId``, so a sample missing ``primaryId`` passes
here and surfaces later as a schema-failure report — never a 500.

Per-sample ``embargoDate`` is accepted by the request schema but must be
IGNORED downstream — never validated or used (dropping the legacy
embargo-date file is intentional, G9).

The validator schema differs from upstream in exactly one place: the
property is named ``passesCheck`` (upstream shipped ``passesAllChecks``
against a ``required`` entry of ``passesCheck`` — a contract bug).

Note: ``schemas/`` is a plain directory on purpose. The repo's ``schemas.py``
module keeps precedence for ``import schemas`` (G12) — never add
``schemas/__init__.py``.
"""

import json
from pathlib import Path

import jsonschema

from cohort_crf import clean_nones, prepare_cohort

_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"

_REQUEST_SCHEMA = json.loads(
    (_SCHEMAS_DIR / "cohort-request.schema.json").read_text(encoding="utf-8")
)
_VALIDATOR_SCHEMA = json.loads(
    (_SCHEMAS_DIR / "cohort-validator.schema.json").read_text(encoding="utf-8")
)

_STRIPPED_KEYS = ("piName", "status", "date")


def strip_pi_name(payload: dict) -> dict:
    """Return a copy of ``payload`` with top-level ``piName``, ``status``
    and ``date`` removed.

    The request schema's top-level ``additionalProperties: false`` rejects
    ``piName``; the legacy pipeline stripped ``status``/``date`` before
    validation as well. Only the top level is touched — the input dict is
    not mutated (downstream ``clean_nones`` rebuilds the nested
    containers).
    """
    return {key: value for key, value in payload.items() if key not in _STRIPPED_KEYS}


def validate_cohort_request(payload: dict) -> dict:
    """Validate a raw SMILE cohort request and return the preprocessed cohort.

    Steps: ``strip_pi_name`` → ``clean_nones`` (legacy ``Cohort.__init__``
    step 1, so null fields cannot trip the schema) →
    ``jsonschema.validate`` against the vendored request schema (raises
    ``jsonschema.ValidationError`` on failure — the caller turns that into
    a schema-failure report, never a 500) → the rest of the legacy
    preprocessing (``normalize_id`` on ``cmoId``/``normalCmoId``, ``type``
    defaulting to ``"investigator"``, ``status``/``date`` stripped).

    The returned cohort is exactly what the legacy pipeline serialized, so
    ``cohort_crf.to_crf`` / ``write_crf`` can consume it directly (G3).
    A per-sample ``embargoDate`` passes validation but must be ignored by
    downstream code.
    """
    cleaned = clean_nones(strip_pi_name(payload))
    jsonschema.validate(instance=cleaned, schema=_REQUEST_SCHEMA)
    return prepare_cohort(cleaned)


def validate_report(report: dict) -> None:
    """Self-check a validation report against the (fixed) validator schema.

    Call before publishing the report: a report that fails its own schema
    is a bug in the report builder, not a SMILE problem. Raises
    ``jsonschema.ValidationError`` on failure.
    """
    jsonschema.validate(instance=report, schema=_VALIDATOR_SCHEMA)
