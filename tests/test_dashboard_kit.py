"""Unit tests for the ProofKit shared dashboard core (dashboard_kit.py).

These lock in the two pieces every builder shares and that used to be copy pasted:
the translation of a Query Builder queryData into a /api/v5/query_range spec, and
the dual-shape result counter that powers panel self-verification (it must count
BOTH the metric aggregations[].series[] shape AND the trace columns[] + data rows
shape). Pure and offline: no SigNoz, no API key, no network. Importing the module
must not require the .signoz_api_key file, which these tests also assert.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard_kit as dk


def test_qd_trace_shape():
    q = dk.qd_trace("count()", "legend", filt="service.name = 'x'", gb=dk.groupby("a11y.cohort"))
    assert q["dataSource"] == "traces"
    assert q["aggregations"] == [{"expression": "count()"}]
    assert q["filter"] == {"expression": "service.name = 'x'"}
    assert q["groupBy"][0]["key"] == "a11y.cohort"


def test_qd_metric_shape():
    q = dk.qd_metric("accesstrace.violation.count", "{{impact}}", gb=dk.groupby("impact"))
    assert q["dataSource"] == "metrics"
    agg = q["aggregations"][0]
    assert agg["metricName"] == "accesstrace.violation.count"
    assert agg["timeAggregation"] == "count" and agg["spaceAggregation"] == "sum"


def test_qd_to_spec_maps_groupby_to_names():
    q = dk.qd_trace("sum(a11y.violations.critical)", filt="service.name = 'accesstrace'",
                    gb=dk.groupby("a11y.cohort"))
    spec = dk.qd_to_spec(q)
    assert spec["name"] == "A"
    assert spec["signal"] == "traces"
    assert spec["aggregations"] == [{"expression": "sum(a11y.violations.critical)"}]
    assert spec["groupBy"] == [{"name": "a11y.cohort"}]


def test_count_results_metric_series_shape():
    resp = {"data": {"data": {"results": [
        {"aggregations": [{"series": [{"v": 1}, {"v": 2}, {"v": 3}]}]}]}}}
    assert dk.count_results(resp) == 3


def test_count_results_trace_columns_and_rows_shape():
    resp = {"data": {"data": {"results": [{
        "columns": [{"name": "__result_0", "columnType": "aggregation"}],
        "data": [[5], [None], [7]],   # the None row must not be counted
    }]}}}
    assert dk.count_results(resp) == 2


def test_count_results_rows_without_aggregation_columns():
    resp = {"data": {"data": {"results": [{
        "columns": [{"name": "label", "columnType": "group"}],
        "data": [["nav"], ["main"]],
    }]}}}
    assert dk.count_results(resp) == 2


def test_count_results_empty_is_zero():
    assert dk.count_results({}) == 0
    assert dk.count_results({"data": {"data": {"results": []}}}) == 0


def test_widget_envelope_is_builder_type():
    wid, w = dk.widget("Title", [dk.qd_trace("count()", filt="x")], "value", "short", "d", 0)
    assert w["panelTypes"] == "value"
    assert w["query"]["queryType"] == "builder"
    assert w["query"]["builder"]["queryData"][0]["aggregations"] == [{"expression": "count()"}]
    assert w["id"] == wid


def test_import_does_not_require_api_key_file():
    # The module is import-safe offline: the key is only read on a live call. If a
    # key file happens to exist we cannot prove the negative, so just assert the
    # accessor is lazy (a function), not evaluated at import time.
    assert callable(dk.api_key)
    assert callable(dk.headers)
