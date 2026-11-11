"""Fixture (b): minimal cohort (G3 golden case).

``type`` is absent (schema-optional) and ``projectSubtitle`` is null — the
legacy pipeline emits ``#type:investigator`` and NO ``projectSubtitle`` line.
Some samples lack ``normalCmoId``/``normalPrimaryId`` (or both id styles on
one sample), which must serialize as empty cells, never "None".
"""

MINIMAL_COHORT = {
    "cohortId": "99902",
    "endUsers": ["analyst3@example.org"],
    "pmUsers": ["pm2@example.org"],
    "projectTitle": "Minimal Cohort",
    "projectSubtitle": None,
    "status": "PROVISIONAL",
    "samples": [
        {
            "cmoId": "C-PPPP19-X001-d01",
            "normalCmoId": "C-PPPP19-X001-n01",
            "primaryId": "99902_A_1_1_1",
        },
        {
            "cmoId": "s_C_PPPP20_X002_d02",
        },
        {
            "cmoId": "C-PPPP21-X003-d03",
            "primaryId": "99902_C_1_1_1",
        },
    ],
}
