"""Shared dispatch-test hygiene.

Several test modules import ``config_query`` (directly or via identity /
trigger_grants / notify / automation) inside their own ``mock_aws`` context.
``config_query`` caches its DynamoDB Table resource in a module global
(``_table``), so whichever test touches it FIRST pins a client bound to that
test's moto instance — and every later test module inheriting the cached module
object gets a handle to a torn-down mock (ResourceNotFoundException). Reset the
cache after every test so each moto context re-resolves its own table.
"""

import sys

import pytest


@pytest.fixture(autouse=True)
def _reset_config_query_table():
    yield
    mod = sys.modules.get("config_query")
    if mod is not None:
        mod._table = None
