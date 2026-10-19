# Design

## Architecture


```
  Edge devices (one collector each)
  +-------------+   +-------------+          +-------------+
  |  device 1   |   |  device 2   |   ...    |  device N   |
  | collector.py|   | collector.py|          | collector.py|
  +------+------+   +------+------+          +------+------+
         |                 |   push (XADD)          |
         v                 v                        v
 +----------------------------------------------------------+
 |  Central server / container/ k8s cluster                           |
 |                                                          |
 |  Redis   metrics.<host1>  metrics.<host2> ...    (raw)   |
 |              |                                           |
 |              v                                           |
 |          consumer.py      cpu %, memory %, disk %, temp  |
 |              |                                           |
 |              v                                           |
 |  Redis   processed_metrics_<host> ...                    |
 |              |                     |                     |
 |              v                     v                     |
 |          alert.py              ui.py (Streamlit)         |
 |          thresholds, summary   status page, :8501        |
 +----------------------------------------------------------+
               |  HTTPS (webhooks)    |
               v                      v
  Slack: summary, #node-metrics-alert  browser
```

- The `collector` is inspired by
  [node_exporter](https://github.com/prometheus/node_exporter): it reads the same
  kind of host data and sends raw counters, no computation is done.
  Unlike node_exporter, which is scraped (pull), the collector pushes to Redis.
- The `collector` reads hardware metrics, pusblishes to redis and the `consumer` do usage calculation and publish to a new stream. collector run on edge devices push metrics to central server. Where as consumer run on container,or central server or a kubernetes cluster.
- In current deployment, consumer, alert and ui scripts run in containers via docker compose.
-  `alert.py` checks metrics against configured thresholds and sends alerts 
  via a notifier, also run on a central server/k8s cluster and not on edge. 
- **Raw and processed data are kept apart.** The collector sends raw counters
  (e.g. cumulative CPU times) so the edge stays dumb and cheap. The consumer
  turns them into percentage. If a formula changes, only the consumer needs
  redeploying, not every device.
- **One class per metric type** in collector and consumer (`CPUMetrics`, `DiskMetrics`,
  and `CPUUsage`, `DiskUsage`, ...). The intention is to make classes modular in collector
  and consumer so that it can be easily extended to add new metrics without changing extsing code.
  Just add new class in collector and consumer.
- **Alert rules live in a JSON file**, not in code, with warning/critical
  levels and a `consecutive` counts so a single data point doesn't cause alert.
  This makes it easier for users to configure alerts and summary without changing
  code or need of a re/deploy. 
- **Status UI with Streamlit.** `ui.py` reads the latest message from each
  `processed_metrics_<host>` stream every 5 seconds and shows one row per
  host with a status column (OK/WARNING/CRITICAL, or STALE if the host stopped
  sending). Metric cells that cross a threshold are colored too, so it's clear
  what made a host red. Streamlit was picked because it's Python like the rest of the
  project and gives the table, styling and auto refresh with very little
  code, no HTML/JS needed. It reuses `alert_config.json`, so the page and the
  alerts use the same thresholds.
- **Slack through Incoming Webhooks.** I only need to post messages to Slack,
  not read anything back, so a webhook was the simplest choice. It's just an
  HTTP POST using `urllib`, so I didn't need the Slack SDK or a bot token. I
  set up one webhook per channel, which is how summaries and critical alerts
  end up in different channels. A webhook can only post to its own channel,
  so even if the URL leaks, nobody can read the workspace with it. The
  downside is that I can't reply in threads or edit a message once it's sent.

## Trade-offs

- **Push instead of pull.** Each edge device connects to the central server.
  In pull based model, central server pull the metrics from edge device, which 
  require open ports to be configured in edge devices for firewall, which increase attack surface.
  Also, in pull mode, central server should maintain inventory for each edge device
  which need an extra system to keep it updated.
- **Why redis used instead of other MQTT:** Redis is light weight and in-memory(fast),
  which is perfect particularly for small projects and would scale fine even for thousands
  of edge devices, given the edge devices has enough memory. So this can be
  later tuned to support more edge devices.
- **Plain `XREAD` instead of consumer groups.** Simpler to write, but Redis
  doesn't remember what the consumer has already read. If the consumer stops
  and restarts, it only reads new messages, so anything sent while it was down
  is skipped.
- **psutil instead of reading `/proc` manually.** psutil reads the same files
  (`/proc/stat`, `/proc/meminfo`, `/proc/diskstats`, ...) and parses them for
  us, so there is no parsing code to write or maintain, and it is widely used
  and well tested. psutil is cross-platform compatible, and can be used to
  add more metrics later (network, fans, sensors). The cost is an extra dependency.
  - **CPU usage:** At first I used `psutil.cpu_percent(interval)`, which gives CPU used % 
  directly but block for the whole interval. With a 10 second interval, CPU was measured over
  those 10 seconds while memory, disk and the rest metrics were read after 10 seconds, so the
  metrics in one message were not from the same timestamp. To solve this, I used 
  `psutil.cpu_times()`  which give total CPU seconds spent in each mode since boot, which
  is instant, and the consumer works out CPU used % from the difference between
  two samples. The cost is a bit more computation, and the consumer has to
  keep the previous sample for each host (so the first message has no CPU % and is none).
- **Resource usage percent is computed in the consumer**, which makes it stateful 
  (it needs the  previous sample per host). That's why each host gets its own calculators.
- **Alert state is in memory.** A restart forgets active alerts, so a critical
  that is still going on gets posted again.
- **Only critical alerts go to Slack.** Fewer messages people learn to ignore,
  but warnings are only visible in the logs.
- **Summaries use fixed windows** (e.g. every 5 minutes), not a sliding window.
  Simpler and no overlap between messages.


## Deployment

- **Central side in containers.** Redis, the consumer, `alert.py` and the UI
  run with Docker Compose from one image, only the command changes. Slack URLs
  come from the environment, not from the image. `alert_config.json` is mounted,
  so a threshold change is a restart, not a rebuild. Redis listens on
  `127.0.0.1` unless `REDIS_BIND` is set to the server's private IP for edge
  devices. On k8s the same image would be three Deployments, with Redis
  managed or as a StatefulSet.
- **Collector as a systemd service, not a container.** It needs the device's
  own `/proc`, `/sys` and GPU. In a container that means mounting host paths,
  sharing the host PID namespace and setting the hostname by hand, which is
  more work and easy to get wrong for a small agent. systemd also restarts it
  if it crashes. On k8s nodes it would be a DaemonSet.

## Scaling to thousands of devices

Message volume is not the problem: 1000 devices every 5 seconds is about 200
messages/s, which one Redis handles easily. There are some other scaling issue
which were a shortcut that was taken to get the POC up and running and in most
cases are trivial to fix with some additional work.

- **New devices are never picked up.** The consumer and `alert.py` look for
  streams once at startup (`KEYS`), so a device added later is ignored until
  they are restarted.
  Fix: one shared stream (or a few shards) with the hostname inside the
  message, instead of one stream per device.
- **Only one consumer can run.** It handles one message at a time, and with
  plain `XREAD` a second consumer would process every message again (CPU %
  also needs the previous sample from the same host).
  Fix: consumer groups, with each host always going to the same consumer.
- **Slack doesn't scale with hosts.** The summary puts one block per host in
  one message and Slack allows 50, so it breaks at about 48 hosts. If many
  hosts go critical together, one post per host hits the webhook limit
  (about one per second) and most alerts are dropped.
  Fix: fleet-level summaries (worst N hosts) and grouped alerts.
- **Redis memory.** A raw message is ~3.8 KB, so 1000 messages x 1000 devices
  is ~4 GB of RAM.
  Fix: keep fewer raw messages, send less disk data, use a Redis cluster.
- **The UI reads every stream on every refresh.** One `XREVRANGE` per host every
  5 seconds, for each open browser, is thousands of Redis calls at this size.
  Fix: cache the result for a few seconds (`st.cache_data(ttl=5)`) so all
  viewers share one read, and page or filter the table.
- **Hostnames clash.** Cloned images often share names, and two devices with
  the same name write to the same stream and corrupt each other's CPU %.
  Fix: a unique device ID (e.g. machine-id).

## Security

- **Redis has no password or TLS right now.** For real use: we need to turn on ACLs 
  with one user per role (a collector can only `XADD` to its own stream, `alert.py`
  can only read), enable TLS, ensure port 6379 is exposed to specific interface.
- **Secrets stay out of git.** Webhook URLs come from environment variables.
  In production they could come from a secrets manager or systemd credentials
  and be rotated if leaked (Slack can revoke a webhook). For a k8s it could be 
  ExternalSecret from an external secret provider like Vault.
- **Data exposure.** Hostnames, mount points and GPU models go to Slack, which
  tells an attacker something about the fleet. So its importanmt to keep the channel
  private and add only relevent employees say SREs who would have access to these
  systems.
- **The status UI has no login.** It shows the same host details as Slack, so
  Docker Compose binds it to `127.0.0.1`. To share it, put it behind a reverse
  proxy with SSO or a VPN rather than opening the port.
- **Trust in the payload.** Any client that can write to Redis can pretend to
  be any host. Per-device credentials and checking that the stream name matches
  the device would fix this. Was skipped for a POC for simplicity.
- **Least privilege.** The collector only reads `/proc` and NVML, so it can run
  as a normal user, does not need root.
