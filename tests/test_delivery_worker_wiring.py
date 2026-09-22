"""
Worker registration + config sanity — `process_delivery_order`,
`process_delivery_cancellation`, and the accept-deadline watchdog cron are
actually wired into `WorkerSettings`, and the accept deadline is
deliberately under Uber's real 690s auto-cancel window.
"""

from backend.app.core.config import get_settings
from backend.app.worker import WorkerSettings


class TestWorkerRegistration:
    def test_delivery_jobs_are_registered(self):
        names = {f.__name__ for f in WorkerSettings.functions}
        assert "process_delivery_order" in names
        assert "process_delivery_cancellation" in names

    def test_accept_deadline_watchdog_cron_is_registered_every_30_seconds(self):
        watchdog_crons = [
            c for c in WorkerSettings.cron_jobs
            if c.coroutine.__name__ == "delivery_accept_deadline_watchdog"
        ]
        assert len(watchdog_crons) == 1
        assert watchdog_crons[0].second == {0, 30}


class TestDeliveryConfigDefaults:
    def test_accept_deadline_is_under_ubers_real_690_second_deadline(self):
        settings = get_settings()
        assert settings.DELIVERY_ACCEPT_DEADLINE_SECONDS < 690
        assert settings.DELIVERY_ACCEPT_DEADLINE_SECONDS == 600

    def test_grace_period_is_positive_and_small(self):
        settings = get_settings()
        assert 0 < settings.DELIVERY_DEADLINE_GRACE_SECONDS < settings.DELIVERY_ACCEPT_DEADLINE_SECONDS

    def test_mr_d_is_not_wired_to_any_webhook_enabled_provider_via_config(self):
        """
        Defence in depth alongside registry.py's own allow-list: even if
        UBER_EATS_WEBHOOK_ENABLED were flipped, there is no equivalent
        MRD_WEBHOOK_ENABLED setting at all — no config path can enable it.
        """
        settings = get_settings()
        assert not hasattr(settings, "MRD_WEBHOOK_ENABLED")
