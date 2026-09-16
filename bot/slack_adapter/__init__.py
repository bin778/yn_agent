"""Phase 3 Slack test adapter.

This package deliberately does not import the Phase 1 workflow or Phase 2
execution services. It owns only Slack identity mapping, test UI state, and
Block Kit transport concerns.
"""

from .config import (
    SlackActor,
    SlackConfigurationError,
    SlackIdentityProvider,
    SlackMapping,
    SlackSettings,
    load_slack_mapping,
)
from .fixtures import SlackTestFixture, load_test_fixture
from .service import (
    SlackTestAuthorizationError,
    SlackTestService,
    SlackTestStaleActionError,
    SlackTestValidationError,
)

__all__ = [
    "SlackActor",
    "SlackConfigurationError",
    "SlackIdentityProvider",
    "SlackMapping",
    "SlackSettings",
    "SlackTestAuthorizationError",
    "SlackTestFixture",
    "SlackTestService",
    "SlackTestStaleActionError",
    "SlackTestValidationError",
    "load_slack_mapping",
    "load_test_fixture",
]
