"""Result publisher + outbox flusher (listener) tests.

Covers the two units the bridge/flusher rely on, both from the light
``listener.nats_publisher`` module (no Django / smile_client, so it imports
cleanly in the suite):

* ``publish_result`` — JetStream publish carrying the cohort id in the
  ``Nats-Msg-Id`` header (the ccs_cohort_utils EventHandler convention) plus a
  ``Nats-Msg-Subject`` header for legacy parity, with the report as the JSON
  payload.
* ``flush_pending`` — one cycle of the background flusher: poll the app's
  outbox (GET /pending), publish each stored report, then POST /mark-published
  for each — mirroring the httpx client ``smile_bridge.py`` already uses.

The JetStream context and the app HTTP are both faked, per the suite's
direct-call pattern.
"""
import asyncio
import json
import uuid

import httpx
import pytest

from listener.nats_publisher import (
    DEFAULT_RESULT_SUBJECT,
    flush_pending,
    publish_result,
    result_subject,
)

SUBJECT = "MDB_STREAM.consumers.tempo.cohort-result"
APP_URL = "http://app:8123"


def _run(coro):
    return asyncio.run(coro)


class FakeJSContext:
    """Stands in for the tempo client's JetStream context."""

    def __init__(self, fail_subjects=()):
        self.calls = []
        self.fail_subjects = set(fail_subjects)

    async def publish(self, subject, payload=b"", timeout=None, stream=None, headers=None):
        if subject in self.fail_subjects:
            raise RuntimeError("simulated NATS publish failure")
        self.calls.append(
            {
                "subject": subject,
                "payload": payload,
                "headers": dict(headers or {}),
            }
        )


class FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.is_success:
            return
        request = httpx.Request("GET", APP_URL + "/")
        raise httpx.HTTPStatusError(
            "status %d" % self.status_code, request=request, response=self
        )


def _rows(n):
    return [
        {
            "id": str(uuid.uuid4()),
            "cohort_id": "cohort-%d" % i,
            "subject": SUBJECT,
            "report_json": json.dumps(
                {"cohortId": "cohort-%d" % i, "passesCheck": i % 2 == 0}
            ),
            "created_at": "2026-10-30T00:00:00+00:00",
        }
        for i in range(n)
    ]


# --- publish_result -------------------------------------------------------


def test_publish_result_sets_cohort_id_header_and_payload():
    js = FakeJSContext()
    report = {
        "cohortId": "C-1",
        "passesCheck": False,
        "jsonSchemaValidated": True,
        "samples": [],
    }
    _run(publish_result(js, SUBJECT, "C-1", report))
    assert len(js.calls) == 1
    call = js.calls[0]
    assert call["subject"] == SUBJECT
    assert call["headers"]["Nats-Msg-Id"] == "C-1"
    # Legacy parity: the subject is echoed in its own header.
    assert call["headers"]["Nats-Msg-Subject"] == SUBJECT
    assert json.loads(call["payload"].decode("utf-8")) == report


def test_publish_result_coerces_cohort_id_to_str():
    js = FakeJSContext()
    _run(publish_result(js, SUBJECT, 12345, {"cohortId": 12345}))
    assert js.calls[0]["headers"]["Nats-Msg-Id"] == "12345"


def test_publish_result_without_context_raises():
    with pytest.raises(RuntimeError):
        _run(publish_result(None, SUBJECT, "C", {"cohortId": "C"}))


def test_result_subject_default_and_env(monkeypatch):
    assert result_subject() == DEFAULT_RESULT_SUBJECT
    monkeypatch.setenv("NATS_RESULT_SUBJECT", "custom.result.subject")
    assert result_subject() == "custom.result.subject"


# --- flush_pending (one flusher cycle) ------------------------------------


def test_flush_pending_publishes_then_marks_each_row(monkeypatch):
    js = FakeJSContext()
    rows = _rows(2)
    marked = []
    monkeypatch.setenv("SYZYGY_NATS_TOKEN", "tok")

    def fake_get(url, params=None, headers=None, timeout=None):
        assert url == APP_URL + "/v1/publish-outbox/pending"
        assert params["limit"] == 20
        assert headers["X-NATS-Token"] == "tok"
        return FakeResponse(rows)

    def fake_post(url, json=None, headers=None, timeout=None):
        assert url == APP_URL + "/v1/publish-outbox/mark-published"
        marked.append(json["id"])
        return FakeResponse({"id": json["id"], "published_at": "now"})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    published = _run(flush_pending(js, APP_URL))
    assert published == 2
    # One publish per row, carrying the cohort id + subject headers.
    assert [c["headers"]["Nats-Msg-Id"] for c in js.calls] == ["cohort-0", "cohort-1"]
    assert all(c["headers"]["Nats-Msg-Subject"] == SUBJECT for c in js.calls)
    # Each row's stored report is what got published.
    assert json.loads(js.calls[0]["payload"].decode()) == {
        "cohortId": "cohort-0",
        "passesCheck": True,
    }
    # Each row marked published, in poll order.
    assert marked == [rows[0]["id"], rows[1]["id"]]


def test_flush_pending_skips_failed_row_but_still_marks_others(monkeypatch):
    # Row 0's subject is configured to fail its publish; row 1 succeeds.
    js = FakeJSContext(fail_subjects={SUBJECT})
    rows = _rows(2)
    rows[1]["subject"] = "other.result.subject"
    marked = []

    def fake_get(url, params=None, headers=None, timeout=None):
        return FakeResponse(rows)

    def fake_post(url, json=None, headers=None, timeout=None):
        marked.append(json["id"])
        return FakeResponse({"id": json["id"], "published_at": "now"})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    published = _run(flush_pending(js, APP_URL))
    # Row 0's publish raised -> left pending and NOT marked. Row 1 published.
    assert published == 1
    assert [c["subject"] for c in js.calls] == [rows[1]["subject"]]
    assert marked == [rows[1]["id"]]


def test_flush_pending_poll_failure_publishes_nothing(monkeypatch):
    js = FakeJSContext()
    marked = []

    def fake_get(url, params=None, headers=None, timeout=None):
        raise httpx.ConnectError("app unreachable")

    def fake_post(url, json=None, headers=None, timeout=None):
        marked.append(json["id"])
        return FakeResponse({})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    published = _run(flush_pending(js, APP_URL))
    assert published == 0
    assert js.calls == []
    assert marked == []
