"""Fixture (a): full cohort — all four id fields on every sample.

Raw (pre-preprocessing) request data, mirroring the shape of real cohort
requests: ids in raw ``s_`` / ``IGO-`` / dashed forms, an explicit
non-default ``type``, a present ``projectSubtitle``, plus ``status``/``date``
which the legacy constructor strips before serialization.
"""

FULL_COHORT = {
    "cohortId": "99901",
    "type": "MSKWESRP",
    "endUsers": ["analyst1@example.org", "analyst2@example.org"],
    "pmUsers": ["pm1@example.org"],
    "projectTitle": "Full Cohort Study",
    "projectSubtitle": "All four id fields on every sample",
    "status": "PASS",
    "date": "2026-10-29",
    "samples": [
        {
            "cmoId": "s_C_PPPP16_X001_d01",
            "normalCmoId": "C-PPPP16-X001-n01",
            "primaryId": "99901_A_1_1_1",
            "normalPrimaryId": "99901_A_2_1_1",
        },
        {
            "cmoId": "IGO_C_PPPP17_X002_d02",
            "normalCmoId": "s_C_PPPP17_X002_n02",
            "primaryId": "99901_B_1_1_1",
            "normalPrimaryId": "99901_B_2_1_1",
        },
        {
            "cmoId": "C-PPPP18-X003-d03",
            "normalCmoId": "C-PPPP18-X003-n03",
            "primaryId": "99901_C_1_1_1",
            "normalPrimaryId": "99901_C_2_1_1",
        },
    ],
}
