"""Shared test setup."""

import pytest

# Tests that check the real shipped default must see it unpatched.
_REAL_DEFAULT_TESTS = {"test_stock_count_is_read_as_packs_by_default"}


@pytest.fixture(autouse=True)
def _fixtures_count_stock_in_units(request, monkeypatch):
    """The synthetic fixtures and their hand-calculated results count stock in units. The
    real default is now packs (confirmed 2026-11-05), so pin the old behaviour for the tests
    that predate it; tests that care about the switch set it themselves."""
    if request.node.name in _REAL_DEFAULT_TESTS:
        return
    import clean
    import config

    monkeypatch.setattr(clean, "STOCK_QTY_IS_PACK_COUNT", False)
    monkeypatch.setattr(config, "STOCK_QTY_IS_PACK_COUNT", False)
