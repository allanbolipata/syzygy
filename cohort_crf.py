"""CRF (Cohort Request File) serialization for cohort requests.

Vendored from ``mskcc/ccs_cohort_utils`` (branch main, 6e92d89) WITHOUT
pandas. ``normalize_id`` and ``clean_nones`` are verbatim from
``cohort_utils/utils.py``; ``to_crf`` reproduces
``cohort_utils/model/cohort.py::Cohort.to_crf`` byte-for-byte (legacy wrote
rows with ``df.to_csv(sep="\\t", header=False)``: missing/None cells become
empty fields, never the string "None").

Input contract (G3): ``to_crf`` expects a cohort that has already gone
through the legacy ``Cohort.__init__`` preprocessing — ``clean_nones`` on
the whole cohort, ``normalize_id`` on ``cmoId``/``normalCmoId``, ``type``
defaulting to ``"investigator"`` when absent, and ``status``/``date``
stripped. ``prepare_cohort`` implements exactly that preprocessing (schema
validation is separate — T3 owns it). Do not pass raw request data to
``to_crf`` directly.
"""

import os
import tempfile
import typing


def normalize_id(id):
    if id is None:
        return None
    id_normalize = id.replace("_", "-")
    if id_normalize.startswith("s-"):
        id_normalize = id_normalize[2:]
    if id_normalize.startswith("IGO-"):
        id_normalize = id_normalize[4:]
    return id_normalize


def clean_nones(value):
    """
    Recursively remove all None values from dictionaries and lists, and returns
    the result as a new dictionary or list.
    """
    if isinstance(value, list):
        return [clean_nones(x) for x in value if x is not None]
    elif isinstance(value, dict):
        return {
            key: clean_nones(val)
            for key, val in value.items()
            if val is not None
        }
    else:
        return value


def prepare_cohort(cohort):
    """Apply the legacy ``Cohort.__init__`` preprocessing to a raw cohort dict.

    Mirrors ``cohort_utils/model/cohort.py::Cohort.__init__`` (pre-schema-
    validation portion) step for step:

    1. ``clean_nones`` on the whole cohort (new containers; the input dict
       is not mutated),
    2. ``normalize_id`` on ``cmoId``/``normalCmoId`` where present,
    3. ``type`` defaults to ``"investigator"`` when absent,
    4. ``status`` and ``date`` are stripped.

    Returns the new, preprocessed cohort dict. Schema validation is NOT
    performed here (T3's ``validate_cohort_request`` owns it).
    """
    cohort = typing.cast("dict", clean_nones(cohort))
    for i in cohort["samples"]:
        for j in [k for k in ["cmoId", "normalCmoId"] if k in i]:
            i[j] = normalize_id(i[j])
    if "type" not in cohort:
        cohort["type"] = "investigator"
    if "status" in cohort:
        del cohort["status"]
    if "date" in cohort:
        del cohort["date"]
    return cohort


def crf_header(cohort, keep_primary_ids=False):
    """Return the ``#`` metadata header lines plus the column header line.

    Byte-identical to the legacy header portion: ``#endUsers``, ``#pmUsers``,
    ``#projectTitle``, ``#projectSubtitle`` (only when the key is present),
    ``#type``, then the tab-joined column header.
    """
    crf_string = ""
    crf_string += f"#endUsers:{','.join(cohort['endUsers'])}\n"
    crf_string += f"#pmUsers:{','.join(cohort['pmUsers'])}\n"
    crf_string += f"#projectTitle:{cohort['projectTitle']}\n"
    if "projectSubtitle" in cohort:
        crf_string += f"#projectSubtitle:{cohort['projectSubtitle']}\n"
    crf_string += f"#type:{cohort['type']}\n"
    if keep_primary_ids:
        crf_string += "#TUMOR_ID\tNORMAL_ID\tPRIMARY_ID\tNORMAL_PRIMARY_ID\n"
    else:
        crf_string += "#TUMOR_ID\tNORMAL_ID\n"
    return crf_string


def to_crf(cohort, keep_primary_ids=True):
    """
    Serialize a preprocessed cohort (see module docstring) to CRF format.

    Output is byte-identical to legacy ``Cohort.to_crf``: the ``#`` header
    lines, the column header line, then one tab-joined row per sample.
    Missing/None sample fields become empty cells (never "None").

    Args:
        cohort (dict): Preprocessed cohort (legacy-constructor semantics).
        keep_primary_ids (bool): Include primaryId/normalPrimaryId columns.
            Default True (4 columns); the 2-column TUMOR_ID/NORMAL_ID
            variant is used by ``write_crf``.

    Returns:
        str: CRF-formatted string.
    """
    crf_string = crf_header(cohort, keep_primary_ids=keep_primary_ids)
    if keep_primary_ids:
        keep_col = "cmoId|normalCmoId|primaryId|normalPrimaryId".split("|")
    else:
        keep_col = "cmoId|normalCmoId".split("|")
    for sample in cohort["samples"]:
        row = ["" if sample.get(col) is None else str(sample.get(col)) for col in keep_col]
        crf_string += "\t".join(row) + "\n"
    return crf_string


def write_crf(cohort_dir, cohort, rows, existing=None):
    """Write the CRF file ``<cohort_dir>/<cohortId>.cohort.txt`` atomically.

    The cohort must be preprocessed (legacy-constructor semantics). The CRF
    is always the 2-column variant (``keep_primary_ids=False``).

    Args:
        cohort_dir (str): Directory the file is written into (created if
            missing).
        cohort (dict): Preprocessed cohort dict.
        rows (list[tuple[str, str]]): Resolved (tumor, normal) pairs of
            already-normalized cmoIds. Every row has both ids (validated
            upstream). Used for the submit path.
        existing (str | None): Content of the previously written CRF file.
            ``None`` = submit: write the full CRF (header + ``rows``).
            A string = update: header-only merge — replace every ``#``
            header line with the new header, preserve the existing data
            rows verbatim (``rows`` is ignored).

    Returns:
        str: Absolute path of the file written.

    Atomicity: the content is written to a temp file in the same directory,
    then moved into place with ``os.replace``; on any failure the temp file
    is removed and the existing file is left untouched.
    """
    os.makedirs(cohort_dir, exist_ok=True)
    path = os.path.join(cohort_dir, f"{cohort['cohortId']}.cohort.txt")
    if existing is None:
        content = crf_header(cohort, keep_primary_ids=False)
        for tumor_id, normal_id in rows:
            content += f"{tumor_id}\t{normal_id}\n"
    else:
        content = crf_header(cohort, keep_primary_ids=False)
        for line in existing.splitlines():
            if line.startswith("#"):
                continue
            content += line + "\n"
    fd, tmp_path = tempfile.mkstemp(dir=cohort_dir, prefix=".crf-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path
