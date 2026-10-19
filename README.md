# host-monitor

A tool that collects hardware metrics (CPU, memory, disk, temperature and NVIDIA GPU) from
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
                                                           |
                    +-------------------------+            |
                    | ui.py (status page)     | <----------+
                    | http://localhost:8501   |
                    +-------------------------+
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
| `ui.py` | Status page (Streamlit) with the latest metrics and an OK/WARNING/CRITICAL/STALE status for each host, refreshed every 5 seconds. |
| `Dockerfile`, `docker-compose.yml` | Run the central side (Redis, consumer, alert, ui) in containers. |

## Installation

It's installed in two parts: the **control plane** runs once on a central
server (Docker Compose), and every **edge device** runs only the collector.

```
edge device(s)                       control plane (docker compose)
collector.py  --- redis :6379 --->   redis -> consumer -> alert.py -> Slack
                                                       -> ui.py    -> :8501
```

### 1. Control plane (central server)

Needs Docker with the compose plugin, or Podman with `podman-compose`
(`pip install podman-compose`). Python is not needed here, it's in the image.

```bash
git clone https://github.com/SonaArora/host-monitor.git
cd host-monitor

# Slack webhooks (see Slack setup below); without them alerts are only logged
export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'
export SLACK_ALERT_WEBHOOK_URL='https://hooks.slack.com/services/...'

# let edge devices reach redis: use this server's private IP
# (default is 127.0.0.1, which only works for a collector on this same machine)
export REDIS_BIND=10.0.0.5

docker compose up -d --build     # podman: podman-compose up -d --build
docker compose ps                # redis, consumer, alert and ui should be up
```

- Open port 6379 in the firewall only for the edge devices' network. Redis
  has no password yet (see Security in `DESIGN.md`).
- The consumer and `alert.py` restart until the first collector sends data,
  that's expected.
- The UI only listens on the server itself. From your laptop use an SSH
  tunnel: `ssh -L 8501:localhost:8501 user@server`, then open
  http://localhost:8501.
- After editing `alert_config.json`: `docker compose restart alert ui`.

### 2. Edge devices (collector)

Needs Linux, Python 3.10+ and access to the control plane on port 6379. GPU
metrics need the NVIDIA driver, without it the GPU part is skipped.

```bash
sudo git clone https://github.com/SonaArora/host-monitor.git /opt/host-monitor
cd /opt/host-monitor

# the collector only needs these three, not the whole requirements.txt
sudo python3 -m venv .venv
sudo .venv/bin/pip install psutil redis nvidia-ml-py

# quick test, should print "Published to stream 'metrics.<hostname>'" every 5s
REDIS_HOST=10.0.0.5 .venv/bin/python collector.py
```

Then run it as a systemd service, so it starts on boot and restarts if it
crashes (for example while Redis is down):

```ini
# /etc/systemd/system/host-monitor-collector.service
[Unit]
Description=host-monitor collector
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/host-monitor
Environment=REDIS_HOST=10.0.0.5
ExecStart=/opt/host-monitor/.venv/bin/python collector.py
# runs as a throwaway non-root user, it only reads /proc, /sys and NVML
DynamicUser=yes
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now host-monitor-collector
journalctl -u host-monitor-collector -f      # check it's publishing
```

The device shows up in the status page within a few seconds.

`nvidia-ml-py` is the official NVIDIA package and gives you `import pynvml`. The old
`pynvml` package also works, but it prints a deprecation warning on start.

### Trying it on one machine

Start the control plane as above but skip `REDIS_BIND` (it stays on
`127.0.0.1`), then run the collector on the same machine:

```bash
python3 -m venv .venv
.venv/bin/pip install psutil redis nvidia-ml-py
.venv/bin/python collector.py          # REDIS_HOST defaults to localhost
```

### Environment variables

| Variable | Default | Where |
|---|---|---|
| `REDIS_HOST` | `localhost` | edge (collector); set to `redis` in Docker Compose |
| `REDIS_PORT` | `6379` | edge and control plane |
| `SCRAPE_INTERVAL` | `5` (seconds) | edge (collector) |
| `REDIS_BIND` | `127.0.0.1` | control plane, address Redis listens on |
| `ALERT_CONFIG` | `alert_config.json` next to `alert.py` | control plane (alert, ui) |
| `SLACK_WEBHOOK_URL` | not set | control plane (summary channel) |
| `SLACK_ALERT_WEBHOOK_URL` | not set | control plane (`#node-metrics-alert`) |

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

## Status UI

The status page (http://localhost:8501) shows one row
per host with CPU, memory, swap, load, the fullest disk, the hottest sensor and
GPU, plus a status column:

- **OK / WARNING / CRITICAL** use the same thresholds as `alert_config.json`,
  so the page and the alerts agree. The page uses the latest value only, it
  doesn't wait for `consecutive` checks like `alert.py` does.
- **STALE** means no message for 30 seconds, so the collector or consumer for
  that host has stopped.
- The metric cells that cross a threshold are colored too (yellow for warning,
  red for critical), so it's clear which metric made the host red.

The counts at the top and the table refresh every 5 seconds. New hosts show up
on their own, the page looks for streams on every refresh.

## Running without Docker (development)

For working on the code, everything can run as plain scripts on one machine:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# redis, only reachable from this machine
docker run -d -p 127.0.0.1:6379:6379 --name redis redis
```

Start each script in its own terminal, in this order. The consumer and
`alert.py` look for streams when they start, so the stream before them needs to
exist first (give each one ~5 seconds). Stop the Docker Compose stack first, it
uses the same Redis port.

```bash
# terminal 1: collect and publish raw metrics
python collector.py

# terminal 2: compute usage and publish processed metrics
python consumer.py

# terminal 3: alerts and summaries (export the Slack URLs here)
python alert.py

# optional, terminal 4: status page at http://localhost:8501
streamlit run ui.py

# optional, terminal 5: watch processed messages
python read_stream.py
```

Stop any of them with Ctrl+C.

If `streamlit` is not found, use `python -m streamlit run ui.py`.

## Alert config

```json
"cpu_percent": {"warning": 80, "critical": 90, "consecutive": 10}
```

- An alert fires when the value is **above** the threshold for `consecutive`
  messages in a row (10 x 5s = ~50s here). `warning` or `critical` can be left out.
- It is logged once when the level changes, not on every message, and a
  `RESOLVED` line is logged when it goes back to normal.
- Only critical alerts (and when they clear) are sent to Slack. Warnings stay in
  the log.
- disk usage (`disk_percent`) is checked per mount point, e.g. `disk_percent[/home]`, and
  `temperature_c` per sensor, e.g. `temperature_c[k10temp.Tctl]`. (example from my test system)
- `summary.metrics` lists what goes into the summary, and `interval_seconds` is
  how often it is sent (use 30 while testing, 300 normally).

## Processed metrics

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
        "index": 0,                        # gpu fields only on hosts with a GPU
        "gpu_utilization_percent": 0,
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
  not picked up until the consumer and `alert.py` are restarted. (The UI is
  fine, it looks for streams on every refresh.)
- **No history is replayed.** The consumer and `alert.py` read from `$` (new
  messages only), and alert state lives in memory, so a restart starts fresh.
- **Each stream keeps the last 1000 messages** (`maxlen`), about 80 minutes at
  a 5 second interval.
- **Redis has no password.** It listens on `127.0.0.1` unless `REDIS_BIND` is
  set, so for edge devices on a network it must be firewalled to their subnet
  (see Security in `DESIGN.md`).
- All timestamps are UTC.

See [DESIGN.md](DESIGN.md) for the design choices, trade-offs, scaling and
security notes.
