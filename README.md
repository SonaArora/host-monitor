# host-monitor

A tool that collects hardware metrics (CPU, memory, disk and NVIDIA GPU) from
Linux hosts similar to node exporter, compute resource usage metrics from it, raises alerts 
when something crosses a configured threshold, and posts a periodic summary to Slack.

Everything talks through Redis Streams, so each piece is a separate Python script
that can run on its own.

This was developed on Fedora 44 and also tested on an Ubuntu VM. The GPU part was tested
in a Kaggle notebook with the GPU accelerator turned on (2x Tesla T4), since my
dev machine has no NVIDIA card.

## How it works

```
+--------------+     +---------------------+     +-------------+
| collector.py | --> | metrics.<host>      | --> | consumer.py |
+--------------+     | (Redis stream)      |     +-------------+
  every                                                 |
  scrape_interval                                       v

+-------------+     +-------------------------+     +-------------+
| Slack       | <-- | alert.py                | <-- | processed_  |
| - summary   |     | - alerts (log + Slack)  |     | metrics_    |
| - critical  |     | - summary every 5 min   |     | <host>      |
+-------------+     +-------------------------+     +-------------+
```

| File | What it does |
|---|---|
| `collector.py` | Reads CPU times, load average, memory, swap, disk partitions/IO, temperature sensors and GPU stats with `psutil` and `pynvml`, and publishes them to `metrics.<hostname>` every `SCRAPE_INTERVAL` seconds. |
| `consumer.py` | Reads the raw streams, computes usage percentages, and publishes to `processed_metrics_<hostname>`. |
| `alert.py` | Reads the processed streams, checks values against `alert_config.json`, logs warning/critical/resolved alerts, and logs a min/max/avg summary every `interval_seconds`. |
| `notifier.py` | Sends the summary and critical alerts to Slack through Incoming Webhooks. |
| `publisher.py` | Small helper used by the collector and consumer to publish (`XADD`) to a stream. |
| `alert_config.json` | Alert thresholds and summary settings. |
| `read_stream.py` | Debug helper that prints the redis stream `processed_metrics_*`. |

## Installation

Requires Python 3, Docker (or Podman) for Redis, and optionally an NVIDIA GPU with
drivers installed.

```bash
# python packages
pip install -r requirements.txt

# redis, only reachable from this machine
docker run -d -p 127.0.0.1:6379:6379 --name redis redis
```

`nvidia-ml-py` is the official NVIDIA package and gives you `import pynvml`. The old
`pynvml` package also works, but it prints a deprecation warning on start. If there
is no NVIDIA GPU or driver, the GPU collector is skipped and everything else still
works.

### Environment variables

| Variable | Default | Used by |
|---|---|---|
| `REDIS_HOST` | `localhost` | collector, consumer, alert |
| `REDIS_PORT` | `6379` | collector, consumer, alert |
| `SCRAPE_INTERVAL` | `5` (seconds) | collector |
| `ALERT_CONFIG` | `alert_config.json` next to `alert.py` | alert |
| `SLACK_WEBHOOK_URL` | not set | alert (summary channel) |
| `SLACK_ALERT_WEBHOOK_URL` | not set | alert (`#node-metric-alert`) |

If the Slack variables are not set, `alert.py` still runs and only logs.

### Slack setup

##### Doc for reference https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/

1. Create a free workspace at slack.com and two channels, one for summaries and
   one for alerts (I used `#node-metrics-alert` and `#node-metrics-summary`).
2. Go to https://api.slack.com/apps, **Create New App** > **From scratch**, and
   pick your workspace.
3. Open **Incoming Webhooks**, switch it on, then **Add New Webhook to Workspace**
   once for each channel.
4. Export the two URLs:

```bash
export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'        # summaries
export SLACK_ALERT_WEBHOOK_URL='https://hooks.slack.com/services/...'  # alerts
```

Testing the webhook with curl to check if this is working...

```bash
curl -X POST -H 'Content-Type: application/json' \
     -d '{"text": "hello"}' "$SLACK_ALERT_WEBHOOK_URL"
```

## Running

Start each script in its own terminal, in this order. The consumer and `alert.py`
look for streams when they start, so the stream before them needs to exist first
(give each one ~5 seconds).

```bash
# terminal 1: collect and publish raw metrics
python collector.py

# terminal 2: compute usage and publish processed metrics
python consumer.py

# terminal 3: alerts and summaries (export the Slack URLs here)
python alert.py

# optional, terminal 4: watch processed messages
python read_stream.py
```

Stop any of them with Ctrl+C.

### Alert config

```json
"cpu_percent": {"warning": 80, "critical": 90, "consecutive": 3}
```

- An alert fires when the value is **above** the threshold for `consecutive`
  messages in a row (3 x 5s = ~15s here). `warning` or `critical` can be left out.
- It is logged once when the level changes, not on every message, and a
  `RESOLVED` line is logged when it goes back to normal.
- Only critical alerts (and when they clear) are sent to Slack. Warnings stay in
  the log.
- disk usage (`disk_percent`) is checked per mount point, e.g. `disk_percent[/home]`, and
  `temperature_c` per sensor, e.g. `temperature_c[k10temp.Tctl]`. (example from my test system)
- `summary.metrics` lists what goes into the summary, and `interval_seconds` is
  how often it is sent (use 30 while testing, 300 normally).

### Processed metrics

This is what `consumer.py` publishes for each host:

```python
{
    "hostname": "fedora",
    "timestamp": "2026-10-18T01:36:10.066927+00:00",
    "metrics": {
        "loadavg_1m": 0.52, "loadavg_5m": 0.73, "loadavg_15m": 0.68,
        "cpu_percent": 6.45,               # None on the first message
        "memory_percent": 31.9,
        "swap_percent": 0.0,
        "disk_percent": {"/": 23.8, "/home": 80.5},
        "temperature_c": {"k10temp.Tctl": 64.38, "nvme.Composite": 41.85},
        "gpu_utilization_percent": 0,      # only on hosts with a GPU
        "gpu_memory_percent": 2.92,
        "gpu_temperature": 40,
    },
}
```

## Linting

```bash
tox -e lint      # black --check and flake8
tox -e format    # let black fix formatting
```

## Assumptions

- **Linux only.** Some fields (`iowait`, `softirq`, `steal`, `buffers`, `slab`)
  only exist in `psutil` on Linux.
- **NVIDIA only** for GPU metrics, through NVML. No GPU means no `gpu` section.
- **Temperature sensors** come from `psutil.sensors_temperatures()` (Linux
  hwmon). VMs and containers usually expose none, so `temperature_c` is just
  empty there, also highly depends on the exact hardware.
- **GPU memory** means VRAM used / total. NVML's `memory_utilization_percent` is
  memory bus busy time, not how full the memory is, so it is not used for alerts.
- **Hostnames are unique.** The stream name comes from `platform.node()`, so two
  hosts with the same name would mix their data.
- **One collector per host.** Two collectors on the same host write to the same
  stream and make the CPU numbers wrong.
- **Memory usage** is `(total - available) / total`
- **CPU usage** is the change in busy time between two samples, so the first
  message after the consumer starts has `cpu_percent: None`.
- **Streams are found once at startup.** A host that starts publishing later is
  not picked up until the consumer and `alert.py` are restarted.
- **No history is replayed.** The consumer and `alert.py` read from `$` (new
  messages only), and alert state lives in memory, so a restart starts fresh.
- **Each stream keeps the last 1000 messages** (`maxlen`), about 80 minutes at
  a 5 second interval.
- **Redis is local and has no password.** Fine for a single machine, not for a
  real network (see `DESIGN.md`).
- All timestamps are UTC.

See [DESIGN.md](DESIGN.md) for the design choices, trade-offs, scaling and
security notes.
