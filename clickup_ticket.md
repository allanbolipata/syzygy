**Title:** Add tumor-normal pairing trigger with historical pairings, latest-normal selection, and override/reinvoke logic

**Description**

The pairing service currently supports creating and listing samples, but it does not yet create tumor-normal pairings automatically. Add a trigger event so the service can evaluate samples and create a `PairingTable` row when a valid tumor and normal sample exist.

Old pairings should be retained in the database. The `latest` flag represents the current/active pairing for a tumor sample.

---

## Pairing cardinality

- A tumor sample can have **multiple historical pairings**.
- A tumor sample can have **only one active/latest pairing** at a time.
- A normal sample can be paired to **multiple tumor samples**.

In other words:

- Each `tumor_id` should have at most one row with `latest = true`.
- The same `normal_id` may appear in multiple pairings, including multiple latest pairings if it is paired to different tumors.
- Old pairings remain in the database with `latest = false`.

---

## Trigger modes

The trigger should support at least three modes:

### 1. Automatic trigger

Trigger automatically when a sample is created.

- If the newly created sample is a **TUMOR**:
  - Use that tumor as the target sample.
  - Find matching normal samples.
  - If the tumor has no existing latest pairing, create a pairing.
  - If the tumor already has a latest pairing, do **not** replace it automatically.

- If the newly created sample is a **NORMAL**:
  - Use that normal as a candidate normal.
  - Find matching tumor samples.
  - For matching tumors that do **not** already have a latest pairing, create a pairing.
  - For matching tumors that already have a latest pairing, do **not** replace them automatically.

Automatic triggers should never replace an existing latest pairing.

---

### 2. Manual trigger / manual reinvocation

Provide a manual endpoint such as:


POST /v1/pairing/trigger


Example payload:


{

"tumor_id": "uuid-value",

"trigger_type": "manual",

"pairing_logic": "TEMPO"

}

A manual trigger explicitly reinvokes pairing for a specific tumor.

- If the tumor has no latest pairing, create one.

- If the tumor already has a latest pairing, a manual trigger may replace it.

- If the existing latest pairing is already with the same normal, do not create a duplicate.



---



### 3. Override trigger



Also support an override signal, such as:


{

"tumor_id": "uuid-value",

"trigger_type": "override",

"pairing_logic": "IMPACT"

}




or an `override: true` flag.



An override should allow replacing an existing latest pairing even if the pairing would otherwise be locked.



---



## Minimum pairing rule



A valid pairing requires:



- One sample where `tumor_or_normal = "TUMOR"`

- One sample where `tumor_or_normal = "NORMAL"`

- The two samples match on all of the following fields:

  - `patient_id`

  - `sample_type`

  - `gene_panel`

  - `bait_set`



For a target tumor sample, the service should search for normal samples where all of those fields match the tumor sample.



---



## Latest normal selection logic



When multiple normal samples match a tumor, the service should choose the “latest” normal based on the most recent sequencing timestamp.



Recommended behavior:



- Add a `sequencing_date` field to `Sample`, or use an existing timestamp if sequencing date is already tracked.

- If `sequencing_date` is available, choose the normal with the most recent `sequencing_date`.

- If `sequencing_date` is not available, fall back to `created_date`.

- If timestamps tie, use a deterministic tie-breaker, such as:

  - most recent `created_date`

  - then highest `id`

  - or another stable tie-breaker



This selected normal becomes the normal for the new latest pairing.



---



## `pairing_logic` value



`PairingTable.pairing_logic` should store the name of the pipeline the pair is mainly associated with.



Allowed values:



- `TEMPO`

- `IMPACT`

- `ACCESS`



The value should be stored as an enum or validated string.



The pairing trigger should assign the appropriate pipeline value when creating or replacing a pairing.



---



## Re-pairing and override logic



Once a tumor has an active/latest pairing, it should not be paired again automatically.



A tumor may only be re-paired in the following cases:



1. **Manual reinvocation**

   - A user explicitly triggers pairing for that tumor.

   - The service may replace the existing latest pairing.



2. **Override**

   - An override signal is sent.

   - The service may replace the existing latest pairing.



Automatic triggers must not replace an existing latest pairing.



Replacement behavior:



- If the target tumor has no latest pairing:

  - Create a new pairing with `latest = true`.



- If the target tumor already has a latest pairing:

  - If the trigger is automatic:

    - Do not create a new pairing.

    - Do not modify the existing pairing.

  - If the trigger is manual or override:

    - If the existing latest pairing is already with the same normal:

      - Do not create a duplicate.

    - If the existing latest pairing is with a different normal:

      - Keep the old pairing row.

      - Set the old pairing’s `latest = false`.

      - Create the new pairing with `latest = true`.



---



## Historical pairing behavior



Old pairings should be retained.



When a new latest pairing replaces an older latest pairing:



- The old row remains in `PairingTable`.

- The old row is updated to `latest = false`.

- The new row is created with `latest = true`.



This preserves history while ensuring only one active pairing per tumor.



---



## Database enforcement



Because historical pairings are retained, do **not** enforce a simple unique constraint on `tumor_id`.



Instead, enforce uniqueness only for active/latest pairings.



For PostgreSQL, this could be a partial unique index such as:


CREATE UNIQUE INDEX uq_pairing_table_tumor_latest

ON pairing_table (tumor_id)

WHERE latest;




Optionally, to prevent duplicate latest pairings for the same tumor and normal:


CREATE UNIQUE INDEX uq_pairing_table_tumor_normal_latest

ON pairing_table (tumor_id, normal_id)

WHERE latest;




Do **not** enforce a unique constraint on `normal_id`, because a normal sample may be paired to multiple tumors.



---



## Acceptance criteria



- A trigger mechanism exists for tumor-normal pairing.

- Automatic triggering occurs when appropriate samples are created.

- Manual triggering can be invoked for a specific tumor.

- Override triggering can replace an existing latest pairing.

- A valid pairing requires matching:

  - `patient_id`

  - `sample_type`

  - `gene_panel`

  - `bait_set`

- When multiple matching normals exist, the trigger selects the latest normal based on the most recent sequencing timestamp or fallback timestamp.

- `pairing_logic` is stored as one of:

  - `TEMPO`

  - `IMPACT`

  - `ACCESS`

- A tumor sample can have multiple historical pairing rows.

- A tumor sample cannot have more than one active/latest pairing.

- A normal sample can be associated with multiple tumor pairings.

- If a tumor already has an active/latest pairing:

  - Automatic triggers do not replace it.

  - Manual or override triggers may replace it.

- If replacing an existing latest pairing:

  - The old pairing remains in the database.

  - The old pairing is marked `latest = false`.

  - The new pairing is marked `latest = true`.

- Repeated triggers do not create duplicate latest pairings for the same tumor and normal.

- If no valid matching pair exists, no pairing is created.

- Existing `GET /v1/pairing?latest=true` behavior remains correct.

