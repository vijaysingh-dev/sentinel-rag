"""Response shaping in rca/clients.

The clients trim Prometheus and Jaeger payloads to the shape the specialists reason over. These
tests pin that reduction: a range result becomes per-series first/last/direction, a trace becomes
its error span and slowest span, and the flag "resting variant" inference matches the demo
convention. No network — the shaping functions take raw API dicts.
"""

from __future__ import annotations

from sentinel.rca.clients import _resting_variant, _summarise_matrix, _summarise_trace


def test_summarise_matrix_reports_direction_and_endpoints():
    raw = [
        {
            "metric": {"service_name": "checkout", "rpc_grpc_status_code": "13"},
            "values": [[1000, "0.01"], [1030, "0.05"], [1060, "0.34"]],
        }
    ]
    out = _summarise_matrix(raw)
    s = out["series"][0]
    assert s["first"] == 0.01
    assert s["last"] == 0.34
    assert s["direction"] == "rising"
    assert s["labels"] == {"service_name": "checkout", "rpc_grpc_status_code": "13"}
    assert s["points"] == 3


def test_summarise_matrix_flat_series():
    raw = [{"metric": {"service_name": "currency"}, "values": [[0, "0.011"], [30, "0.012"]]}]
    assert _summarise_matrix(raw)["series"][0]["direction"] == "flat"


def test_summarise_trace_picks_error_and_slowest_span():
    trace = {
        "traceID": "t1",
        "processes": {"p1": {"serviceName": "checkout"}, "p2": {"serviceName": "payment"}},
        "spans": [
            {
                "spanID": "root",
                "processID": "p1",
                "operationName": "POST /checkout",
                "startTime": 0,
                "duration": 1_800_000,
                "references": [],
                "tags": [],
            },
            {
                "spanID": "child",
                "processID": "p2",
                "operationName": "grpc.Charge",
                "startTime": 100_000,
                "duration": 1_600_000,
                "references": [{"refType": "CHILD_OF", "spanID": "root"}],
                "tags": [
                    {"key": "error", "value": "true"},
                    {"key": "exception.message", "value": "charge failed: fault injected"},
                ],
            },
        ],
    }
    out = _summarise_trace(trace)
    assert out["found"] is True
    assert out["slowest_span"]["service"] == "payment"
    assert out["slowest_span"]["share_of_trace"] >= 0.8
    assert out["error_spans"][0]["service"] == "payment"
    assert "fault injected" in out["error_spans"][0]["message"]
    assert set(out["services"]) == {"checkout", "payment"}


def test_summarise_trace_handles_no_spans():
    assert _summarise_trace({"traceID": "t", "spans": []})["found"] is False


def test_resting_variant_prefers_off_then_false_then_smallest():
    assert _resting_variant("x", {"on": True, "off": False}) == "off"
    assert _resting_variant("x", {"true": True, "false": False}) == "false"
    assert _resting_variant("x", {"50%": 0.5, "0%": 0.0, "100%": 1.0}) == "0%"
    assert _resting_variant("x", {"a": "aa", "b": "bb"}) == "a"


def test_resting_variant_prefers_the_shipped_baseline():
    """loadGeneratorTraffic rests at 'on'; the off-first convention alone called it an active fault."""
    variants = {"on": True, "off": False}
    assert _resting_variant("loadGeneratorTraffic", variants, baseline="on") == "on"
    # a baseline naming a variant the live file no longer has falls back to the convention
    assert _resting_variant("x", variants, baseline="gone") == "off"


def test_shipped_baseline_marks_no_fault_flag_active():
    """Every fault flag ships at its resting variant; only then can 'active' mean 'injected'."""
    from sentinel.rca.clients import _baseline_defaults

    baseline = _baseline_defaults()
    assert baseline, "tracked demo.flagd.json not found"
    assert baseline["loadGeneratorTraffic"] == "on"
    assert baseline["paymentFailure"] == "off"


def test_trim_labels_keeps_the_labels_that_identify_non_service_series():
    """container_*, consumer lag and span metrics are not keyed by service_name; dropping their
    identifying labels turned a per-container memory query into a list of anonymous series."""
    from sentinel.rca.clients import _trim_labels

    kept = _trim_labels(
        {"container_name": "email", "group": "accounting", "span_kind": "SPAN_KIND_SERVER",
         "status_code": "STATUS_CODE_ERROR", "otel_scope_name": "noise", "host_name": "noise"}
    )
    assert kept == {"container_name": "email", "group": "accounting", "span_kind": "SPAN_KIND_SERVER",
                    "status_code": "STATUS_CODE_ERROR"}
