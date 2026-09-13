# Dashboard attribution

`Garmin-Grafana-Dashboard.json` and `Garmin-Strength-Training-Dashboard.json`
are vendored unmodified from
[arpanghosh8453/garmin-grafana](https://github.com/arpanghosh8453/garmin-grafana)
and are licensed BSD-3-Clause, Copyright (c) 2025 Arpan Ghosh.

They are included here so the stack provisions a working dashboard on first
start rather than requiring a manual import. The InfluxDB measurement and field
names that this repository's `overlay` service reads were also taken from that
project's writer (`src/garmin_grafana/garmin_fetch.py`).

To refresh them against upstream:

```bash
curl -sL https://raw.githubusercontent.com/arpanghosh8453/garmin-grafana/main/Grafana_Dashboard/Garmin-Grafana-Dashboard.json \
  -o grafana/dashboards/Garmin-Grafana-Dashboard.json
```

`provider.yaml` in this directory is our own Grafana provisioning config, not
from upstream.
