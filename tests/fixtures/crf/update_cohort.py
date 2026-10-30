"""Fixture (c): update scenario — new cohort metadata + a pre-existing CRF.

``UPDATE_COHORT`` is the (raw) cohort whose header replaces the old one;
``EXISTING_CRF`` is the content of the previously written
``99903.cohort.txt`` (also stored as ``update_existing.cohort.txt``).

Note the old header has a ``projectSubtitle`` line while the new cohort has
none, so the merge must replace the header block wholesale (6 ``#`` lines
down to 5), preserving every data row verbatim — including the row whose
normal cell is empty.
"""

UPDATE_COHORT = {
    "cohortId": "99903",
    "type": "operational",
    "endUsers": ["analyst4@example.org"],
    "pmUsers": ["pm3@example.org", "pm4@example.org"],
    "projectTitle": "Updated Title",
    "samples": [
        {
            "cmoId": "C-PPPP22-X001-d01",
            "normalCmoId": "C-PPPP22-X001-n01",
        },
    ],
}

EXISTING_CRF = (
    "#endUsers:old_user@example.org\n"
    "#pmUsers:old_pm@example.org\n"
    "#projectTitle:Original Title\n"
    "#projectSubtitle:Original Subtitle\n"
    "#type:investigator\n"
    "#TUMOR_ID\tNORMAL_ID\n"
    "C-PPPP22-X001-d01\tC-PPPP22-X001-n01\n"
    "C-PPPP22-X002-d02\tC-PPPP22-X002-n02\n"
    "C-PPPP22-X003-d03\t\n"
)
