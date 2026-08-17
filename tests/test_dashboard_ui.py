from ui.app import moving_average


def test_dashboard_price_history_uses_a_time_window() -> None:
    history = moving_average(
        [
            {"ts": 1, "brti": 100},
            {"ts": 31, "brti": 110},
            {"ts": 62, "brti": 130},
        ],
        60,
    )

    assert [point["average"] for point in history] == [100.0, 105.0, 120.0]


if __name__ == "__main__":
    test_dashboard_price_history_uses_a_time_window()
