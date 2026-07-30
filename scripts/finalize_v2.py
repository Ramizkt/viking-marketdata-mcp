from pathlib import Path

service_path = Path('app/service.py')
source = service_path.read_text(encoding='utf-8')
source = source.replace(
'''        selected = next(
            item
            for item in portfolios
            if item["robot_id"] == robot_id
            and item["portfolio"] == portfolio
        )
        if not selected["history_available"]:
''',
'''        selected = next(
            (
                item
                for item in portfolios
                if item["robot_id"] == robot_id
                and item["portfolio"] == portfolio
            ),
            None,
        )
        if selected is not None and not selected["history_available"]:
''',
1,
)
source = source.replace(
'''        base = {
            "robot_id": robot_id,
            "portfolio": portfolio,
            "date_from": date_from.astimezone(UTC).isoformat(),
            "date_to": date_to.astimezone(UTC).isoformat(),
            "fields": normalized_fields,
            "aggregation": aggregation,
            "row_count": len(rows),
''',
'''        data_status = "ok" if rows else "no_data_in_range"
        notes = [] if rows else ["Данных в запрошенном диапазоне нет."]
        base = {
            "data_status": data_status,
            "truncated": False,
            "coverage": {
                "from": date_from.isoformat(),
                "to": date_to.isoformat(),
                "tz": str(date_from.tzinfo),
            },
            "notes": notes,
            "robot_id": robot_id,
            "portfolio": portfolio,
            "date_from": date_from.astimezone(UTC).isoformat(),
            "date_to": date_to.astimezone(UTC).isoformat(),
            "fields": normalized_fields,
            "aggregation": aggregation,
            "row_count": len(rows),
''',
1,
)
source = source.replace(
'''        if actual_delivery in {"inline", "summary"}:
            if actual_delivery == "inline":
                base["rows"] = rows
''',
'''        if actual_delivery in {"inline", "summary"}:
            if actual_delivery == "inline":
                base["items"] = rows
            else:
                base["items"] = rows[:preview_rows]
                base["returned_count"] = len(base["items"])
''',
1,
)
service_path.write_text(source, encoding='utf-8')

test_path = Path('tests/test_service.py')
tests = test_path.read_text(encoding='utf-8')
tests = tests.replace(
'''    assert result.exported_file.path.exists()


def test_naive_datetime_is_rejected():
''',
'''    assert result.exported_file.path.exists()


async def test_empty_portfolio_history_has_explicit_reason(service):
    service.client.get_portfolio_history = AsyncMock(return_value=[])
    result = await service.get_portfolio_data(
        robot_id="1",
        portfolio="A",
        date_from=datetime.fromtimestamp(0, tz=UTC),
        date_to=datetime.fromtimestamp(10, tz=UTC),
        fields=["buy"],
        aggregation="raw",
        delivery="inline",
        preview_rows=10,
    )
    assert result.structured["data_status"] == "no_data_in_range"
    assert result.structured["items"] == []
    assert result.structured["notes"]


async def test_unknown_robot_does_not_raise_stop_iteration(service):
    service.client.get_portfolio_history = AsyncMock(return_value=[])
    result = await service.get_portfolio_data(
        robot_id="foreign",
        portfolio="hidden",
        date_from=datetime.fromtimestamp(0, tz=UTC),
        date_to=datetime.fromtimestamp(10, tz=UTC),
        fields=["buy"],
        aggregation="raw",
        delivery="inline",
        preview_rows=10,
    )
    assert result.structured["data_status"] == "no_data_in_range"


def test_naive_datetime_is_rejected():
''',
1,
)
test_path.write_text(tests, encoding='utf-8')
