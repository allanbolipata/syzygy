"""Subject dispatch for the internal NATS ingest endpoint.

The standalone smile-listener service POSTs every consumed JetStream
message to /v1/ingest/nats; dispatch() routes it to the per-event
handler by subject.

Payloads are the JSON-decoded message bodies: a JSON object for
new-request and sample-update, a JSON array for request-update (matching
beagle's smile_callback, which branches on isinstance(data, list)).

Known limitation (re-pairing on update): the cmo-sample-update path
re-runs the same cascade as ingestion — TUMOR samples go through
``trigger_pairing`` with the ``automatic`` trigger, which returns the
existing latest pairing WITHOUT re-running the strategy. Key changes on
an already-paired sample are therefore not re-evaluated;
``POST /pairing/trigger-all`` remains the gap-fill.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import settings
from models import PairingTable, Sample
from bait_sets import resolve_bait_set_logic
from pairing_service import trigger_pairing, trigger_pairing_for_normal
from request_metadata import request_exists, update_request_metadata

logger = logging.getLogger(__name__)

# Provenance bucket for samples registered via NATS ingestion.
SMILE_FILE_GROUP = "SMILE"

# Sample-level payload keys that map onto Sample columns. Every other
# sample key (importDate, qcReports, cmoSampleIdFields, status,
# sampleAliases, patientAliases, additionalProperties, datasource,
# libraries, ...) is preserved verbatim in additional_metadata.
_SAMPLE_COLUMN_KEYS = frozenset({
    "primaryId",
    "cmoSampleName",
    "cmoPatientId",
    "sampleType",
    "genePanel",
    "baitSet",
    "tumorOrNormal",
    "preservation",
    "sampleOrigin",
    "libraries",
})

# Top-level payload keys that are not part of the request-level metadata
# blob: per-sample data, and fields that have their own home (status ->
# the requestStatus metadata key, requestJson -> the source of
# requestMetadata itself).
_REQUEST_BLOB_EXCLUDED = frozenset({"samples", "requestJson", "status"})

# latestSampleMetadata keys consumed into Sample columns (or into
# run_mode / sequencing_date via libraries); everything else present in
# latestSampleMetadata is preserved in additional_metadata.
_UPDATE_COLUMN_KEYS = frozenset({
    "primaryId",
    "cmoSampleName",
    "cmoPatientId",
    "sampleType",
    "genePanel",
    "baitSet",
    "tumorOrNormal",
    "preservation",
    "sampleOrigin",
    "igoRequestId",
    "libraries",
})

# Column changes that require re-running the pairing cascade (the same
# set the ingest path treats as pairing-relevant).
_PAIRING_RELEVANT_COLUMNS = (
    "tumor_or_normal",
    "gene_panel",
    "bait_set",
    "preservation",
    "sample_origin",
    "run_mode",
    "patient__cmo",
)


def _first_run(sample_entry: Dict[str, Any]) -> Dict[str, Any]:
    """The run carrying runMode/runDate: libraries[0].runs[0]."""
    libraries = sample_entry.get("libraries")
    if not isinstance(libraries, list) or not libraries:
        return {}
    first_library = libraries[0]
    if not isinstance(first_library, dict):
        return {}
    runs = first_library.get("runs")
    if not isinstance(runs, list) or not runs:
        return {}
    first_run = runs[0]
    return first_run if isinstance(first_run, dict) else {}


def _parse_run_date(value: Any) -> Optional[datetime]:
    """SMILE runDate -> UTC datetime; date-only values become midnight."""
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("nats new-request: unparseable runDate %r", value)
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _request_metadata_blob(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The request-level blob stored as requestMetadata on each sample:
    the parsed requestJson when present, otherwise the top-level request
    fields themselves (minus samples/status/requestJson)."""
    request_json = payload.get("requestJson")
    parsed: Optional[Dict[str, Any]] = None
    if isinstance(request_json, str) and request_json.strip():
        try:
            candidate = json.loads(request_json)
        except ValueError as exc:
            logger.warning("nats new-request: requestJson is not valid JSON: %s", exc)
            candidate = None
        if isinstance(candidate, dict):
            parsed = candidate
    elif isinstance(request_json, dict):
        parsed = request_json
    if parsed is not None:
        return parsed
    return {
        key: value
        for key, value in payload.items()
        if key not in _REQUEST_BLOB_EXCLUDED
    }


def _upsert_sample(
    db: Session,
    sample_entry: Dict[str, Any],
    request_id: Optional[str],
    request_metadata: Dict[str, Any],
    request_status: Any,
) -> tuple[Optional[Sample], bool, List[str]]:
    """Register or refresh one SMILE sample, keyed on primary_id.

    Returns (sample, created, errors); sample is None when the entry is
    invalid and must be skipped (errors explains why). Nothing is
    committed here — the message handler commits once per message.
    """
    errors: List[str] = []

    primary_id = sample_entry.get("primaryId")
    if not primary_id:
        errors.append("primaryId missing")

    # Required non-null columns (plus primaryId for the upsert key and a
    # valid tumor/normal role).
    for payload_key, column in (
        ("sampleType", "sample_type"),
        ("genePanel", "gene_panel"),
        ("cmoSampleName", "cmo_sample_name"),
    ):
        if not sample_entry.get(payload_key):
            errors.append(f"{payload_key} missing")
    tumor_or_normal = str(sample_entry.get("tumorOrNormal") or "").strip().upper()
    if tumor_or_normal not in ("TUMOR", "NORMAL"):
        errors.append(
            f"tumorOrNormal invalid ({sample_entry.get('tumorOrNormal')!r}); "
            "expected Tumor or Normal"
        )
    if errors:
        return None, False, errors

    run = _first_run(sample_entry)
    additional_metadata = {
        key: value
        for key, value in sample_entry.items()
        if key not in _SAMPLE_COLUMN_KEYS and value is not None
    }
    # Request-level data (Option A): carried on every sample of the
    # request, same keys update_request_metadata() maintains.
    additional_metadata["requestMetadata"] = request_metadata
    additional_metadata["requestStatus"] = request_status

    fields = {
        "primary_id": primary_id,
        "cmo_sample_name": sample_entry["cmoSampleName"],
        "patient__cmo": sample_entry.get("cmoPatientId"),
        "request_id": request_id,
        "sample_type": sample_entry["sampleType"],
        "gene_panel": sample_entry["genePanel"],
        "bait_set": sample_entry.get("baitSet"),
        "tumor_or_normal": tumor_or_normal,
        "preservation": sample_entry.get("preservation"),
        "sample_origin": sample_entry.get("sampleOrigin"),
        "run_mode": run.get("runMode"),
        "sequencing_date": _parse_run_date(run.get("runDate")),
        "additional_metadata": additional_metadata,
        "file_group": SMILE_FILE_GROUP,
    }

    # Application-level upsert (no schema change): the samples table has
    # no unique constraint on primary_id, so pick the first matching row.
    existing = db.scalar(select(Sample).where(Sample.primary_id == primary_id).limit(1))
    if existing is not None:
        for key, value in fields.items():
            setattr(existing, key, value)
        db.flush()
        return existing, False, []
    sample = Sample(**fields)
    db.add(sample)
    db.flush()
    return sample, True, []


def handle_new_request(
    db_session: Session, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Ingest a fresh, complete cmo-new-request message.

    Registers (upserts) every sample of the request — which also
    establishes the request's existence for later update messages — and
    triggers automatic pairing for each sample covered by a pairing
    logic: TUMOR samples go through the pairing cascade, NORMAL samples
    back-fill unpaired tumors. One commit per message; a summary dict is
    returned for the response/log.
    """
    if not isinstance(payload, dict):
        logger.warning("nats new-request: payload is not a JSON object")
        return {"status": "error", "detail": "new-request payload must be a JSON object"}

    samples_payload = payload.get("samples")
    if not isinstance(samples_payload, list):
        samples_payload = []
    request_id = payload.get("igoRequestId")
    request_metadata = _request_metadata_blob(payload)
    request_status = payload.get("status")

    pairings_before = (
        db_session.scalar(select(func.count()).select_from(PairingTable)) or 0
    )

    created = 0
    updated = 0
    skipped: List[Dict[str, Any]] = []
    uncovered: List[Dict[str, Any]] = []

    for entry in samples_payload:
        if not isinstance(entry, dict):
            skipped.append({"primary_id": None, "errors": ["sample entry is not an object"]})
            logger.warning("nats new-request: skipping non-object sample entry")
            continue

        sample, was_created, errors = _upsert_sample(
            db_session, entry, request_id, request_metadata, request_status
        )
        if sample is None:
            skipped.append({"primary_id": entry.get("primaryId"), "errors": errors})
            logger.warning(
                "nats new-request %s: skipping sample %s: %s",
                request_id, entry.get("primaryId"), "; ".join(errors),
            )
            continue
        if was_created:
            created += 1
        else:
            updated += 1

        # Coverage gate: only pair under the logic the bait set resolves
        # to; samples outside it are registered but never paired.
        logic = resolve_bait_set_logic(db_session, sample.bait_set)
        if logic is None:
            reason = f"no pairing logic for bait set {sample.bait_set!r}"
            uncovered.append(
                {"primary_id": sample.primary_id, "bait_set": sample.bait_set, "reason": reason}
            )
            logger.info(
                "nats new-request %s: sample %s not paired (%s)",
                request_id, sample.primary_id, reason,
            )
            continue
        if not logic.active:
            reason = f"pairing logic {logic.name} is not active"
            uncovered.append(
                {"primary_id": sample.primary_id, "bait_set": sample.bait_set, "reason": reason}
            )
            logger.info(
                "nats new-request %s: sample %s not paired (%s)",
                request_id, sample.primary_id, reason,
            )
            continue

        if sample.tumor_or_normal == "TUMOR":
            trigger_pairing(db_session, sample, logic, "automatic")
        else:
            trigger_pairing_for_normal(db_session, sample, logic)

    db_session.commit()
    pairings_after = (
        db_session.scalar(select(func.count()).select_from(PairingTable)) or 0
    )
    pairings_created = pairings_after - pairings_before

    detail = (
        f"request {request_id or '<no igoRequestId>'}: {created} sample(s) created, "
        f"{updated} sample(s) updated, {pairings_created} pairing(s) created, "
        f"{len(skipped)} skipped, {len(uncovered)} uncovered"
    )
    logger.info("nats new-request: %s", detail)
    return {
        "status": "ok",
        "detail": detail,
        "summary": {
            "request_id": request_id,
            "samples_created": created,
            "samples_updated": updated,
            "pairings_created": pairings_created,
            "skipped": skipped,
            "uncovered": uncovered,
        },
    }


def handle_request_update(
    db_session: Session, payload: Dict[str, Any] | List[Any]
) -> Dict[str, Any]:
    """Ingest a cmo-request-update message: merge REQUEST-LEVEL metadata
    onto every sample of each request it names.

    The payload is a JSON ARRAY of entries, each
    ``{igoRequestId, requestMetadataJson (a JSON-STRING of the request
    metadata), importDate, status {validationStatus, validationReport}}``.

    Updates NEVER create anything: a request exists iff at least one
    ``Sample`` row carries its ``igoRequestId`` as ``request_id``. An
    unknown request is skipped with a WARN naming the request id and is
    counted in the per-message result; processing continues with the
    remaining entries.

    Entries are applied in order. When several entries target the same
    request, the later entry (later importDate) wins: each call to
    ``request_metadata.update_request_metadata`` stamps
    ``requestMetadata`` / ``requestStatus`` / ``requestImportDate`` onto
    every sample of the request, replacing prior values.
    """
    if not isinstance(payload, list):
        logger.warning("nats request-update: payload is not a JSON array")
        return {"status": "error", "detail": "request-update payload must be a JSON array"}

    per_entry: List[Dict[str, Any]] = []
    requests_updated = 0
    missing_requests = 0
    errors = 0

    for entry in payload:
        if not isinstance(entry, dict):
            errors += 1
            per_entry.append(
                {"igo_request_id": None, "outcome": "error", "reason": "entry is not an object"}
            )
            logger.warning("nats request-update: skipping non-object entry")
            continue

        request_id = entry.get("igoRequestId")
        if not request_id:
            missing_requests += 1
            per_entry.append(
                {
                    "igo_request_id": None,
                    "outcome": "missing",
                    "reason": "entry has no igoRequestId",
                }
            )
            logger.warning("nats request-update: entry has no igoRequestId — skipped")
            continue
        if not request_exists(db_session, request_id):
            missing_requests += 1
            per_entry.append(
                {
                    "igo_request_id": request_id,
                    "outcome": "missing",
                    "reason": f"unknown request {request_id}",
                }
            )
            logger.warning(
                "nats request-update: unknown request %s — skipped, nothing created",
                request_id,
            )
            continue

        request_metadata_json = entry.get("requestMetadataJson")
        if not isinstance(request_metadata_json, str) or not request_metadata_json.strip():
            errors += 1
            per_entry.append(
                {
                    "igo_request_id": request_id,
                    "outcome": "error",
                    "reason": "requestMetadataJson missing or not a string",
                }
            )
            logger.warning(
                "nats request-update %s: requestMetadataJson missing or not a string",
                request_id,
            )
            continue
        try:
            request_metadata = json.loads(request_metadata_json)
        except ValueError as exc:
            errors += 1
            per_entry.append(
                {
                    "igo_request_id": request_id,
                    "outcome": "error",
                    "reason": f"requestMetadataJson is not valid JSON: {exc}",
                }
            )
            logger.warning(
                "nats request-update %s: requestMetadataJson is not valid JSON: %s",
                request_id,
                exc,
            )
            continue

        samples_updated = update_request_metadata(
            db_session,
            request_id,
            request_metadata=request_metadata,
            status=entry.get("status"),
            import_date=entry.get("importDate"),
        )
        requests_updated += 1
        per_entry.append(
            {
                "igo_request_id": request_id,
                "outcome": "updated",
                "samples_updated": samples_updated,
                "import_date": entry.get("importDate"),
            }
        )

    detail = (
        f"{len(payload)} entrie(s): {requests_updated} request(s) updated, "
        f"{missing_requests} missing, {errors} error(s)"
    )
    logger.info("nats request-update: %s; per-entry: %s", detail, per_entry)
    return {
        "status": "ok",
        "detail": detail,
        "summary": {
            "entries": len(payload),
            "requests_updated": requests_updated,
            "missing_requests": missing_requests,
            "errors": errors,
            "per_entry": per_entry,
        },
    }


def handle_sample_update(
    db_session: Session, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Ingest a cmo-sample-update message: refresh one sample's
    SAMPLE-LEVEL metadata.

    Updates NEVER create anything. The sample row is resolved by
    ``latestSampleMetadata.primaryId`` (the key syzygy stores), falling
    back to the ``sampleAliases`` entry of namespace ``igoId``; the
    request key comes from ``latestSampleMetadata.igoRequestId``
    (fallback ``additionalProperties.igoRequestId``). A missing row is
    rejected and reported: no sample carries the igoRequestId ->
    "unknown request <id> for sample <key>"; the request is known but the
    sample is not registered under it -> "sample <key> not registered
    under existing request <id>". Either way nothing is created.

    Only sample-level fields present in ``latestSampleMetadata`` are
    written (same mapping as new-request ingest; absent fields are left
    as-is). The remaining keys (importDate, qcReports,
    cmoSampleIdFields, status, investigatorSampleId, sampleName,
    additionalProperties, ...) are merged into ``additional_metadata``,
    replacing same-named keys, and the full ``sampleMetadataList``
    history is stored verbatim under ``sampleMetadataList``.

    When a pairing-relevant field changed, the ingestion path is re-run:
    logic via ``resolve_bait_set_logic``, TUMOR -> ``trigger_pairing``
    ("automatic"), NORMAL -> ``trigger_pairing_for_normal``. See the
    module docstring for the known "automatic" short-circuit limitation.
    """
    if not isinstance(payload, dict):
        logger.warning("nats sample-update: payload is not a JSON object")
        return {"status": "error", "detail": "sample-update payload must be a JSON object"}

    latest = payload.get("latestSampleMetadata")
    if not isinstance(latest, dict):
        logger.warning("nats sample-update: latestSampleMetadata missing or not an object")
        return {"status": "error", "detail": "sample-update payload requires a latestSampleMetadata object"}

    # Sample key: primaryId (the key syzygy stores); fallback is the
    # sampleAliases entry with namespace "igoId".
    sample_key = latest.get("primaryId")
    if not sample_key:
        aliases = payload.get("sampleAliases")
        if isinstance(aliases, list):
            for alias in aliases:
                if (
                    isinstance(alias, dict)
                    and alias.get("namespace") == "igoId"
                    and alias.get("value")
                ):
                    sample_key = alias["value"]
                    break
    if not sample_key:
        logger.warning("nats sample-update: no primaryId or igoId alias in payload")
        return {"status": "error", "detail": "sample-update payload has no primaryId or igoId alias"}

    # Request key: latestSampleMetadata.igoRequestId, with the
    # additionalProperties fallback.
    request_id = latest.get("igoRequestId")
    additional_properties = latest.get("additionalProperties")
    if not request_id and isinstance(additional_properties, dict):
        request_id = additional_properties.get("igoRequestId")

    sample = db_session.scalar(
        select(Sample).where(Sample.primary_id == sample_key).limit(1)
    )
    if sample is None:
        # Guard (agreed semantics): updates NEVER create anything.
        if request_id is None or not request_exists(db_session, request_id):
            request_label = request_id if request_id is not None else "<none>"
            message = f"unknown request {request_label} for sample {sample_key}"
        else:
            message = f"sample {sample_key} not registered under existing request {request_id}"
        logger.warning("nats sample-update: %s — skipped, nothing created", message)
        return {
            "status": "skipped",
            "detail": message,
            "summary": {"sample_id": sample_key, "request_id": request_id, "reason": message},
        }

    fields: Dict[str, Any] = {}
    if latest.get("tumorOrNormal"):
        role = str(latest["tumorOrNormal"]).strip().upper()
        if role in ("TUMOR", "NORMAL"):
            fields["tumor_or_normal"] = role
        else:
            logger.warning(
                "nats sample-update %s: invalid tumorOrNormal %r, leaving unchanged",
                sample_key, latest["tumorOrNormal"],
            )
    for payload_key, column in (
        ("genePanel", "gene_panel"),
        ("baitSet", "bait_set"),
        ("sampleType", "sample_type"),
        ("preservation", "preservation"),
        ("sampleOrigin", "sample_origin"),
        ("cmoSampleName", "cmo_sample_name"),
    ):
        if latest.get(payload_key):
            fields[column] = latest[payload_key]
    if request_id:
        fields["request_id"] = request_id
    patient_cmo = latest.get("cmoPatientId")
    if not patient_cmo:
        patient = payload.get("patient")
        if isinstance(patient, dict):
            cmo = patient.get("cmoPatientId")
            # The update envelope wraps patient.cmoPatientId as
            # {"value": ..., "namespace": ...} while
            # latestSampleMetadata carries it as a plain string — accept
            # both shapes.
            if isinstance(cmo, dict):
                cmo = cmo.get("value")
            if cmo:
                patient_cmo = cmo
    if patient_cmo:
        fields["patient__cmo"] = patient_cmo
    run = _first_run(latest)
    if run.get("runMode"):
        fields["run_mode"] = run["runMode"]
    if run.get("runDate"):
        parsed_date = _parse_run_date(run["runDate"])
        if parsed_date is not None:
            fields["sequencing_date"] = parsed_date

    # Everything else in latestSampleMetadata goes to additional_metadata
    # (merge, replacing same-named keys), plus the full metadata history.
    meta = dict(sample.additional_metadata or {})
    meta_keys_touched: List[str] = []
    for key, value in latest.items():
        if key in _UPDATE_COLUMN_KEYS or value is None:
            continue
        meta[key] = value
        meta_keys_touched.append(key)
    if isinstance(payload.get("sampleMetadataList"), list):
        meta["sampleMetadataList"] = payload["sampleMetadataList"]
        meta_keys_touched.append("sampleMetadataList")

    changed = []
    for column, value in fields.items():
        if getattr(sample, column) != value:
            setattr(sample, column, value)
            changed.append(column)
    if changed or meta_keys_touched:
        sample.additional_metadata = meta

    pairing: Dict[str, Any] = {
        "triggered": False,
        "reason": "no pairing-relevant field changed",
    }
    if any(column in changed for column in _PAIRING_RELEVANT_COLUMNS):
        pairings_before = (
            db_session.scalar(select(func.count()).select_from(PairingTable)) or 0
        )
        logic = resolve_bait_set_logic(db_session, sample.bait_set)
        if logic is None:
            pairing = {
                "triggered": True,
                "pairing_created": False,
                "reason": f"no pairing logic for bait set {sample.bait_set!r}",
            }
        elif not logic.active:
            pairing = {
                "triggered": True,
                "pairing_created": False,
                "reason": f"pairing logic {logic.name} is not active",
            }
        elif sample.tumor_or_normal == "TUMOR":
            latest_pairing = trigger_pairing(db_session, sample, logic, "automatic")
            pairings_after = (
                db_session.scalar(select(func.count()).select_from(PairingTable)) or 0
            )
            pairing = {
                "triggered": True,
                "trigger_type": "automatic",
                # "automatic" may return the existing latest pairing
                # without creating a row (module-docstring limitation);
                # report the truth.
                "pairing_created": pairings_after > pairings_before,
                "pairing_id": str(latest_pairing.id) if latest_pairing else None,
                "normal_id": str(latest_pairing.normal_id) if latest_pairing else None,
            }
        else:
            trigger_pairing_for_normal(db_session, sample, logic)
            pairings_after = (
                db_session.scalar(select(func.count()).select_from(PairingTable)) or 0
            )
            pairing = {
                "triggered": True,
                "backfill": True,
                "pairings_created": pairings_after - pairings_before,
            }
        logger.info("nats sample-update %s: re-paired (%s)", sample_key, pairing)

    db_session.commit()

    detail = (
        f"sample {sample_key}: {len(changed)} column field(s) changed {changed}, "
        f"{len(meta_keys_touched)} metadata key(s) refreshed; pairing: {pairing}"
    )
    logger.info("nats sample-update: %s", detail)
    return {
        "status": "ok",
        "detail": detail,
        "summary": {
            "sample_id": sample_key,
            "request_id": request_id,
            "fields_changed": changed,
            "metadata_keys": meta_keys_touched,
            "pairing": pairing,
        },
    }


def dispatch(
    db_session: Session, subject: str, payload: Dict[str, Any] | List[Any]
) -> Dict[str, Any]:
    """Route an ingested NATS message to its handler by subject.

    Unknown subjects are logged and reported as an error result (the
    router turns that into a 422).
    """
    handlers: Dict[str, Any] = {
        settings.nats_new_request_subject: handle_new_request,
        settings.nats_request_update_subject: handle_request_update,
        settings.nats_sample_update_subject: handle_sample_update,
    }
    handler = handlers.get(subject)
    if handler is None:
        logger.warning("nats ingest: unknown subject %s", subject)
        return {"status": "error", "detail": f"unknown subject: {subject}"}
    return handler(db_session, payload)
