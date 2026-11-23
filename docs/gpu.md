# GPU monitoring

## Probes

`GpuProbe` has three implementations in the node agent, selected by `NODE_AGENT_GPU_PROBE`:

| value | source | use |
|---|---|---|
| `nvidia_smi` | `nvidia-smi --query-gpu=... --format=csv` | any host with the NVIDIA driver |
| `dcgm` | `dcgmi dmon` / `dcgmi topo` | richer: NVLink topology, PCIe replay, split ECC |
| `fake` | synthesised | **no hardware at all** |
| `auto` | dcgm -> nvidia_smi -> fake | default; picks by what actually works |

An explicit choice is never silently downgraded. If an operator asks for DCGM and DCGM
is broken, that surfaces as a visible failure — reporting fake GPUs on a real GPU host
would be far worse than reporting none.

### Why the CSV interface

`nvidia-smi`'s human-readable table changes layout between driver releases, so a scraper
built on it breaks silently on upgrade. The `--query-gpu` field list is a documented,
stable interface.

### `FakeGpuProbe`

Operationally the most important probe. The reference development machine has
no NVIDIA GPU, so without it nothing GPU-adjacent — registration, inventory sync, metric
collection, retention, placement, the end-to-end agent scenario — could be developed or
regression-tested anywhere but the target hardware.

Telemetry is *plausible*, not random: utilisation follows slow sine waves phase-shifted
per device, temperature and power track it with lag, memory moves in GiB steps. Uniform
noise would make dashboards, alert thresholds and rollup logic untestable, because every
window would look identical. Devices deliberately do not move together — a scheduler that
picks the least-loaded GPU is untestable if every device reports the same load.

Device UUIDs are deterministic per `(node_name, index)` and stable across restarts. The
control plane keys GPUs on UUID, so a random one each boot would create duplicate rows on
every restart and make historical metrics unjoinable.

A node reporting synthetic telemetry sets `gpu_synthetic`, surfaced in the API and
labelled `SYNTHETIC` in the admin UI. Presenting fabricated numbers as real capacity would
be the single most misleading thing this platform could do.

## Health classification

Shared by every probe, so a node's health means the same thing regardless of how it was
measured:

| condition | level |
|---|---|
| any uncorrectable ECC error | `CRITICAL` |
| temperature >= 90 C | `CRITICAL` |
| temperature >= 83 C | `WARNING` |
| memory >= 98% used | `WARNING` |
| otherwise | `HEALTHY` |

An uncorrectable ECC error is CRITICAL on its own: it indicates failing memory, and
inference on failing memory produces *wrong answers* rather than obvious crashes — the
worst failure mode this platform has.

ECC counters are nullable, never defaulted to zero. A consumer GPU that does not support
ECC must not be recorded as a device reporting no ECC errors.

## Health events

Recorded on **change** only (`gpu_health_events`). An alert stream repeating "GPU 3 is
hot" every 15 seconds is one an operator learns to ignore.

## Threshold alerts

Health events are history; alerts are what someone gets told about. The worker job
`gpu_alerts` runs every `WORKERS__GPU_ALERT_INTERVAL_SECONDS` (default 60, range
15–3600). It compares each GPU's newest sample with the thresholds below and keeps one
alert per GPU (source `gpu_threshold`) at the worst level any of them gives:

| condition | alert |
|---|---|
| any uncorrectable ECC error | `CRITICAL` |
| temperature at or above `gpu.alert_temperature_critical_celsius` (default 90 °C) | `CRITICAL` |
| temperature at or above `gpu.alert_temperature_warning_celsius` (default 83 °C) | `WARNING` |
| memory use at or above `gpu.alert_memory_warning_percent` (default 98%) | `WARNING` |

The title names the node, the GPU index and what crossed the line, for example
*GPU 2 on gpu-node-01: 92 °C, memory 99% used*. A GPU that gets worse moves its alert from
WARNING to CRITICAL, which channels announce again. The alert clears itself when the GPU is
back within every threshold, when it has sent no sample for five minutes (an unreachable
node shows in its own status), or when `gpu.alerts_enabled` is turned off.

The thresholds are on the Settings page under *GPU monitoring* and apply from the next
check. The warning temperature must be below the critical one. Model servers usually
reserve 90–95% of GPU memory when they start, so keep the memory threshold above that.

### Alerts that don't flap

A GPU running at 82–84 °C against an 83 °C line would otherwise raise and clear its alert
every minute, and every raise reaches the channels. Two settings stop that:

| setting | default | effect |
|---|---|---|
| `gpu.alert_sustain_minutes` | 0 | A temperature or memory breach raises an alert only once every sample for this many minutes has been over a line. A sample back under the line, or a gap of more than five minutes between samples, starts the count again. 0 raises on the first sample. |
| `gpu.alert_recovery_margin_celsius` | 3 | While an alert is open, the temperature lines are this much lower: an alert raised at 83 °C clears below 80 °C, and a CRITICAL one raised at 90 °C drops to WARNING below 87 °C. |
| `gpu.alert_recovery_margin_percent` | 2 | The same for memory: an alert raised at 98% clears below 96%. |

Uncorrectable ECC errors always raise at once; they don't come and go. A GPU that is
waiting out the sustain time has no alert yet, so getting worse during it (for example
crossing the critical line) raises one only when the time is up. A GPU that stops
reporting clears its alert whatever the margins say. All three are on the Settings page
under *GPU monitoring*.

The thresholds are the same for every GPU. Per-node thresholds (a node in a hot room, a
card rated for higher temperatures) are not supported yet.

The ECC counter only goes back to zero when the driver resets it (usually a reboot or a
GPU reset), so an ECC alert stays open until then. GPUs belong to no team, so their alerts
go to channels set to *Every team*.

## Retention

Four GPUs at the default 15-second interval is roughly 700k rows per month per node.
`GPU__METRIC_RETENTION_DAYS` (default 30) bounds it; the retention job runs hourly rather
than per collection, because a bulk DELETE takes locks on the platform's busiest table.

## Metric queries

`GET /gpus/{id}/metrics` is windowed (`since_minutes`) and capped (`limit <= 5000`).
Unbounded, a month of samples would be ~175k rows for a single chart.

`GET /gpus` returns every GPU with its latest sample, fetched in one `DISTINCT ON` query —
the dashboard renders the whole fleet on one page, and N+1 there would be N round trips
per refresh.

## Installing the agent on a GPU host

`sudo ./install-node.sh --server <url> --name <node> --token <token>`, run from the copied
bundle. See [installation.md](installation.md) for the surrounding flow; this section is
what the script leaves behind and how to change it.

| | |
|---|---|
| `/etc/ai-platform/node-agent.env` | mode `0600`, contains this node's agent token |
| `/etc/ai-platform/docker-compose.node.yml` | one service, `restart: unless-stopped` |
| container | `ai-platform-node-agent`, Compose project `ai-platform-node` |

```bash
docker compose -p ai-platform-node -f /etc/ai-platform/docker-compose.node.yml logs -f
```

**`NODE_AGENT_GPU_PROBE` is pinned, not left on `auto`.** When the installer sees the NVIDIA
container runtime it writes `nvidia_smi` explicitly, so a broken driver fails loudly instead
of falling through to the synthetic probe and reporting four invented A100s. On a host with
no NVIDIA runtime it stays `auto` and the node is registered CPU-only, which is a legitimate
configuration — the scheduler refuses GPU work there.

**Upgrading**: copy the newer bundle and re-run the same command. The image is reloaded, the
container recreated, and the agent token preserved.

**Removing**: `docker compose -p ai-platform-node -f /etc/ai-platform/docker-compose.node.yml
down`, then delete the node in the console. Deleting the node stops the platform managing it;
workloads already running on that host are left alone.

**Model runtime images are not installed by this script.** Until they are loaded on the host
with `docker load`, deployments scheduled there fail with "image is not present".
