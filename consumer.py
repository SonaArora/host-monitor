from publisher import RedisPublisher
import redis
import json
import logging
import os
from abc import ABC, abstractmethod

"""
This module consumes raw metrics from Redis streams, compute usage from cumulative
metrics(specially for CPU), and publish the computed metrics to new Redis streams.
The new streams are named "processed_metrics_<hostname>" where <hostname> is the
hostname of the machine (the edge device) that produced the raw metrics.
"""

logger = logging.getLogger(__name__)


# Abstract base class for computing derived metrics from raw metrics.
class UsageCalculator(ABC):
    key = None

    @abstractmethod
    def compute(self, raw):
        pass

    @staticmethod
    def _percent(part, total):
        if not total:
            return None
        return round((part / total) * 100, 2)


# Computes CPU usage percentage based on cumulative CPU times.


class CPUUsage(UsageCalculator):
    key = "cpu"
    cpu_parameters = (
        "cpu_usage_user",
        "cpu_usage_nice",
        "cpu_usage_system",
        "cpu_usage_idle",
        "cpu_usage_iowait",
        "cpu_usage_irq",
        "cpu_usage_softirq",
        "cpu_usage_steal",
        "cpu_usage_guest",
    )

    def __init__(self):
        # cpu_times() are cumulative, so usage needs the previous sample
        self.prev = None

    def _total(self, cpu):
        return sum(cpu[parameter] for parameter in self.cpu_parameters)

    def compute(self, raw):
        prev, self.prev = self.prev, raw
        if not prev:
            return {"cpu_percent": None}

        curr_total_cpu = self._total(raw)
        prev_total_cpu = self._total(prev)

        delta_total_cpu = curr_total_cpu - prev_total_cpu
        delta_idle = raw["cpu_usage_idle"] - prev["cpu_usage_idle"]

        if delta_total_cpu == 0:
            return {"cpu_percent": 0.0}
        return {"cpu_percent": round(((1 - (delta_idle / delta_total_cpu)) * 100), 2)}


# Computes load average metrics.


class LoadAverage(UsageCalculator):
    key = "cpu"

    def compute(self, raw):
        return {
            "loadavg_1m": raw.get("loadavg_1m"),
            "loadavg_5m": raw.get("loadavg_5m"),
            "loadavg_15m": raw.get("loadavg_15m"),
        }


# Computes memory usage and swap usage percentage
class MemoryUsage(UsageCalculator):
    key = "memory"

    def compute(self, raw):
        total = raw.get("total_memory")
        available = raw.get("available_memory")
        return {
            "memory_percent": (
                self._percent(total - available, total) if total else None
            ),
            "swap_percent": self._percent(raw.get("used_swap"), raw.get("total_swap")),
        }


# Computes disk usage percentage for each mounted partition.
class DiskUsage(UsageCalculator):
    key = "disk"

    def compute(self, raw):
        # collector publishes disk metrics as [partitions, io_counters]
        partitions = raw[0] if raw else []
        disk_percent = {}
        for p in partitions:
            disk_percent[p["mountpoint"]] = p["usage_percent"]
        return {"disk_percent": disk_percent}


# Computes GPU usage percentage for each GPU
class GPUUsage(UsageCalculator):
    key = "gpu"

    def compute(self, raw):
        return {
            "gpu": [
                {
                    "index": gpu["index"],
                    "name": gpu["name"],
                    "gpu_percent": gpu["gpu_utilization_percent"],
                    "memory_percent": self._percent(
                        gpu["memory_used_mb"], gpu["memory_total_mb"]
                    ),
                }
                for gpu in raw
            ]
        }


# Consumes raw metrics from Redis streams, computes derived metrics,
# and publishes them to new streams.
class MetricsConsumer:
    envhost = os.environ.get("REDIS_HOST", "localhost")
    envport = os.environ.get("REDIS_PORT", 6379)

    def __init__(self, host=envhost, port=envport):
        self.client = redis.Redis(host=host, port=port, decode_responses=True)
        self.publisher = RedisPublisher(host=host, port=port)
        # one set of calculators per stream so CPU deltas don't mix across hosts
        self.calculators = {}

    def _get_streams(self):
        return self.client.keys("metrics.*")

    def process(self, stream, raw_metrics):
        # first message from this host: create its own set of calculators
        if stream not in self.calculators:
            self.calculators[stream] = [
                LoadAverage(),
                CPUUsage(),
                MemoryUsage(),
                DiskUsage(),
                GPUUsage(),
            ]
        computed = {}
        for calculator in self.calculators[stream]:
            section = raw_metrics.get(calculator.key)
            if section is not None:
                computed.update(calculator.compute(section))
        return computed

    def consume_stream_and_publish_newstream(self):
        streams = self._get_streams()
        if not streams:
            logger.warning("No metric streams found in Redis")
            return

        stream_offsets = {stream: "$" for stream in streams}
        logger.info(f"Consuming streams: {streams}")

        while True:
            results = self.client.xread(stream_offsets, block=0)
            for stream, messages in results:
                for msg_id, fields in messages:
                    raw_metrics = json.loads(fields["metrics"])
                    computed = self.process(stream, raw_metrics)
                    logger.info(f"[{stream}] {json.dumps(computed)}")

                    # "metrics.<hostname>" -> "<hostname>"
                    # (split once: hostnames can contain dots)
                    hostname = stream.split(".", 1)[1]
                    new_stream_processed = f"processed_metrics_{hostname}"
                    self.publisher.publish(
                        new_stream_processed,
                        {
                            "hostname": hostname,
                            "timestamp": raw_metrics.get("timestamp"),
                            "metrics": computed,
                        },
                    )
                    print(f"Published to stream '{new_stream_processed}'")
                    stream_offsets[stream] = msg_id


if __name__ == "__main__":
    consumer = MetricsConsumer()
    consumer.consume_stream_and_publish_newstream()
