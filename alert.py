"""
This module checks metrics against configured thresholds and sends alerts via a
notifier.
"""

import redis
import json
import logging
import os
import time
from datetime import datetime, timezone

from notifier import SlackNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get(
    "ALERT_CONFIG", os.path.join(os.path.dirname(__file__), "alert_config.json")
)


def load_config(path):
    with open(path) as f:
        return json.load(f)


class AlertManager:
    def __init__(self, config, notifier=None):
        self.notifier = notifier
        self.alerts = config.get("alerts", {})
        summary = config.get("summary", {})
        self.summary_metrics = summary.get("metrics", [])
        self.summary_interval = summary.get("interval_seconds", 300)

        # keyed by (hostname, metric) so each host is tracked separately
        self.breach_counts = {}
        self.active_alerts = {}
        self.samples = {}
        self.last_summary = time.monotonic()
        # wall-clock start of the summary period, shown in the Slack message
        self.summary_start = datetime.now(timezone.utc)

    def check(self, hostname, metrics):
        for metric, value in metrics.items():
            # skip None (first cpu sample) and nested values (disk, temperature)
            if not isinstance(value, (int, float)):
                continue
            if metric in self.alerts:
                self._check_threshold(hostname, metric, value, self.alerts[metric])
            if metric in self.summary_metrics:
                self.samples.setdefault((hostname, metric), []).append(value)

        # nested metrics are {name: value}: check and summarise each entry, e.g.
        # disk_percent {mountpoint: percent}, temperature_c {sensor: celsius}
        for metric in ("disk_percent", "temperature_c"):
            rule = self.alerts.get(metric)
            for item, value in metrics.get(metric, {}).items():
                if not isinstance(value, (int, float)):
                    continue
                name = f"{metric}[{item}]"
                if rule:
                    self._check_threshold(hostname, name, value, rule)
                if metric in self.summary_metrics:
                    self.samples.setdefault((hostname, name), []).append(value)

        if time.monotonic() - self.last_summary >= self.summary_interval:
            self.log_summary()

    def _check_threshold(self, hostname, metric, value, rule):
        key = (hostname, metric)
        consecutive = rule.get("consecutive", 1)

        # count checks in a row above each threshold
        counts = self.breach_counts.setdefault(key, {"warning": 0, "critical": 0})
        for level in ("warning", "critical"):
            if level in rule and value > rule[level]:
                counts[level] += 1
            else:
                counts[level] = 0

        if counts["critical"] >= consecutive:
            level = "critical"
        elif counts["warning"] >= consecutive:
            level = "warning"
        else:
            level = None

        # log only when the level changes, not on every check
        previous = self.active_alerts.get(key)
        if level == previous:
            return
        if level is None:
            self.active_alerts.pop(key)
            logger.info(f"[{hostname}] RESOLVED {metric}={value} ({previous} cleared)")
        else:
            self.active_alerts[key] = level
            log = logger.critical if level == "critical" else logger.warning
            log(
                f"[{hostname}] {level.upper()} {metric}={value} above {rule[level]} "
                f"for {consecutive} consecutive checks"
            )

        # Slack gets critical alerts, and a message when a critical alert clears
        if not self.notifier:
            return
        if level == "critical":
            self.notifier.send_alert(hostname, metric, value, rule[level], consecutive)
        elif previous == "critical":
            self.notifier.send_resolved(hostname, metric, value, level)

    def log_summary(self):
        # summary = {hostname: {metric: {"min": .., "max": .., "avg": ..}}}
        summary = {}
        for (hostname, metric), values in self.samples.items():
            stats = {
                "min": min(values),
                "max": max(values),
                "avg": round(sum(values) / len(values), 2),
            }
            summary.setdefault(hostname, {})[metric] = stats
            logger.info(
                f"[{hostname}] SUMMARY {metric}: "
                f"min={stats['min']} max={stats['max']} avg={stats['avg']}"
            )

        end = datetime.now(timezone.utc)
        if summary and self.notifier:
            self.notifier.send_summary(summary, self.summary_start, end)

        self.samples = {}
        self.last_summary = time.monotonic()
        self.summary_start = end
        return summary


def main():
    notifier = SlackNotifier()
    if not notifier.webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not set: summaries will only be logged")
    if not notifier.alert_webhook_url:
        logger.warning("SLACK_ALERT_WEBHOOK_URL not set: alerts will only be logged")
    alert_manager = AlertManager(load_config(CONFIG_PATH), notifier=notifier)

    client = redis.Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=os.environ.get("REDIS_PORT", 6379),
        decode_responses=True,
    )

    streams = client.keys("processed_metrics_*")
    if not streams:
        logger.warning("No processed metric streams found in Redis")
        return

    stream_offsets = {stream: "$" for stream in streams}
    logger.info(f"Watching streams for alerts: {streams}")

    try:
        while True:
            results = client.xread(stream_offsets, block=0)
            for stream, messages in results:
                for msg_id, fields in messages:
                    data = json.loads(fields["metrics"])
                    alert_manager.check(data["hostname"], data["metrics"])
                    stream_offsets[stream] = msg_id
    except KeyboardInterrupt:
        print("\nStopping alerts...")


if __name__ == "__main__":
    main()
