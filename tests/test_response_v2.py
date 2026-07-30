from app.response_v2 import add_iso_times, compact_log, envelope, sanitize_value


def test_sanitize_viking_missing_value_recursively():
    assert sanitize_value(
        {"orig_price": -(1 << 53), "nested": [1, -(1 << 53)]}
    ) == {"orig_price": None, "nested": [1, None]}


def test_add_iso_times_keeps_ns_and_adds_iso():
    row = add_iso_times(
        {"dt": "1774551550351384574"}, "Europe/Moscow"
    )
    assert row["dt"] == "1774551550351384574"
    assert row["dt_iso"] == "2026-03-26T21:59:10.351+03:00"


def test_compact_log_drops_large_snapshot_message():
    row = compact_log(
        {
            "dt": "1774551550351384574",
            "level": 1,
            "name": "alpha",
            "owner": "user@example.com",
            "msg": (
                'Edit portfolio {"lim_b": 1.5, '
                '"securities": {"A": {"pos": 1}}}'
            ),
        },
        "Europe/Moscow",
    )
    assert row["event_type"] == "param_change"
    assert "msg" not in row
    assert row["details"]["diff_available"] is False


def test_envelope_has_one_canonical_array():
    result = envelope([{"id": 1}], total_count=1)
    assert result["items"] == [{"id": 1}]
    assert result["row_count"] == 1
    assert "data" not in result
