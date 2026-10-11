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

