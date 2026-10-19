"""
Sends Slack messages through Incoming Webhooks:
- periodic metric summaries to the channel in SLACK_WEBHOOK_URL
- critical alerts to the channel in SLACK_ALERT_WEBHOOK_URL
Webhook URLs are secrets, so keep them out of alert_config.json and git.
"""

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


class SlackNotifier:
    envwebhook = os.environ.get("SLACK_WEBHOOK_URL")
    envalertwebhook = os.environ.get("SLACK_ALERT_WEBHOOK_URL")

    def __init__(
        self, webhook_url=envwebhook, alert_webhook_url=envalertwebhook, timeout=5
    ):
        self.webhook_url = webhook_url
        self.alert_webhook_url = alert_webhook_url
        self.timeout = timeout

    def send_summary(self, summary, start, end):
        self._post(
            self.webhook_url, self.format_summary(summary, start, end), "summary"
        )

    def send_alert(self, hostname, metric, value, threshold, consecutive):
        message = (
            f"*CRITICAL* · *{hostname}* · `{metric}` = *{value}* "
            f"(above {threshold} for {consecutive} consecutive checks)"
        )
        self._post(self.alert_webhook_url, {"text": message}, "alert")

    def send_resolved(self, hostname, metric, value, level):
        # level is None when back to normal, or "warning" if still above warning
        status = "back to normal" if level is None else f"now {level}"
        message = (
            f"*RESOLVED* · *{hostname}* · `{metric}` = *{value}* "
            f"(critical cleared, {status})"
        )
        self._post(self.alert_webhook_url, {"text": message}, "resolved alert")

    @staticmethod
    def format_summary(summary, start, end):
        # summary = {hostname: {metric: {"min": .., "max": .., "avg": ..}}}
        period = f"{start:%Y-%m-%d %H:%M}–{end:%H:%M} UTC"
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Metrics summary"},
            },
            {"type": "context", "elements": [{"type": "mrkdwn", "text": period}]},
        ]

        for hostname, metrics in summary.items():
            # monospace table so the columns line up in Slack
            width = max(len("metric"), *(len(metric) for metric in metrics))
            rows = [f"{'metric':<{width}}  {'min':>8}  {'max':>8}  {'avg':>8}"]
            for metric, stats in metrics.items():
                rows.append(
                    f"{metric:<{width}}  {stats['min']:>8.2f}  "
                    f"{stats['max']:>8.2f}  {stats['avg']:>8.2f}"
                )
            table = "\n".join(rows)
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{hostname}*\n```{table}```"},
                }
            )

        return {
            # plain text fallback, shown in notifications
            "text": f"Metrics summary for {', '.join(summary)} ({period})",
            "blocks": blocks,
        }

    def _post(self, webhook_url, payload, kind):
        # no webhook configured for this kind of message: log only
        if not webhook_url:
            return

        request = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        # a Slack failure must not stop alerting: log it and carry on
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                logger.info(f"Sent {kind} to Slack (HTTP {response.status})")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.warning(f"Slack rejected {kind}: HTTP {e.code} {body}")
        except (urllib.error.URLError, OSError) as e:
            logger.warning(f"Could not send {kind} to Slack: {e}")
