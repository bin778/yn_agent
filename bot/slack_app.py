"""Run the Phase 3 Slack Socket Mode adapter with ``python -m bot.slack_app``."""

from .slack_adapter.app import main


if __name__ == "__main__":
    raise SystemExit(main())
