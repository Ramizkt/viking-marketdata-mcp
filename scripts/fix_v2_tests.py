from pathlib import Path

path = Path('tests/test_mcp.py')
source = path.read_text(encoding='utf-8')
source = source.replace(
    '        "list_available_portfolios",\n',
    '        "list_available_portfolios",\n        "search_portfolios",\n',
    1,
)
source = source.replace(
    'async def get_current_portfolio_data(self, *, robot_id: str, portfolio: str):',
    'async def get_current_portfolio_data(\n'
    '            self, *, robot_id: str, portfolio: str, raw: bool = False\n'
    '        ):',
    1,
)
source = source.replace(
    '''            return {
                "robot_id": robot_id,
                "portfolio": portfolio,
                "value": {
                    "name": portfolio,
                    "dynamic_field": {"nested": True},
                    "securities": {
                        "BTCUSDT": {
                            "sec_key": "BTCUSDT",
                            "custom_security_field": 123,
                        }
                    },
                },
                "unsubscribed": True,
            }
''',
    '''            return {
                "data_status": "ok",
                "row_count": 1,
                "truncated": False,
                "coverage": None,
                "notes": [],
                "robot_id": robot_id,
                "portfolio": portfolio,
                "subscription_closed": True,
                "items": [
                    {
                        "name": portfolio,
                        "dynamic_field": {"nested": True},
                        "securities": {
                            "BTCUSDT": {
                                "sec_key": "BTCUSDT",
                                "custom_security_field": 123,
                            }
                        },
                    }
                ],
            }
''',
    1,
)
source = source.replace(
    'result.structuredContent["value"]["dynamic_field"]',
    'result.structuredContent["items"][0]["dynamic_field"]',
    1,
)
source = source.replace(
    'result.structuredContent["value"]["securities"]["BTCUSDT"]',
    'result.structuredContent["items"][0]["securities"]["BTCUSDT"]',
    1,
)
source = source.replace(
    '''            message_filter,
            limit,
        ):
            return {
                "robot_id": robot_id,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "message_filter": message_filter,
                "limit": limit,
                "log_count": 1,
                "logs": [
                    {
                        "dt": "1677586103000245321",
                        "r_id": robot_id,
                        "name": "demo",
                        "level": 1,
                        "msg": "test",
                    }
                ],
            }
''',
    '''            message_filter,
            limit,
            verbosity="compact",
            timezone="Europe/Moscow",
            raw=False,
        ):
            return {
                "data_status": "ok",
                "row_count": 1,
                "truncated": False,
                "coverage": {
                    "from": date_from.isoformat(),
                    "to": date_to.isoformat(),
                    "tz": timezone,
                },
                "notes": [],
                "robot_id": robot_id,
                "verbosity": verbosity,
                "items": [
                    {
                        "dt": "1677586103000245321",
                        "dt_iso": "2023-02-28T15:08:23.000+03:00",
                        "event_type": "log",
                    }
                ],
            }
''',
    1,
)
source = source.replace(
    'assert result.structuredContent["log_count"] == 1\n'
    '    assert result.structuredContent["message_filter"] == "*test*"',
    'assert result.structuredContent["row_count"] == 1\n'
    '    assert result.structuredContent["verbosity"] == "compact"',
    1,
)
path.write_text(source, encoding='utf-8')
