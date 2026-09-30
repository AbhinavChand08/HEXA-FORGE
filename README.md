# SIH 26155 — Network Config Normalizer & Compliance Auditor

**Team:** Hexa forge
**Problem Statement:** SIH 26155 — Compliance auditing of network device configurations across multiple vendors

---

## What it does

Uploads raw network device configuration files (Cisco, Juniper, Arista, FortiGate, Huawei, F5, and others), normalizes them into a vendor-neutral canonical schema, and audits them against security benchmarks (CIS, NIST, STIG).

**Core pipeline:**

```
Raw config file
  → Flatten (per-dialect parser)
  → Templatize (pattern matching)
  → Normalize (registry mappings → canonical key/value pairs)
  → Audit (rule evaluation → PASS / FAIL / UNKNOWN per control)
```

Unknown commands are routed to an ML suggestion engine and then to a human-in-the-loop review UI. Once a mapping is approved, it applies to all future devices of that vendor automatically — no code changes, no redeploy.

---

## Requirements

| Requirement | Version |
|---|---|
| Docker | 24+ |
| Docker Compose | v2 (bundled with Docker Desktop) |
| Free ports | 3000 (UI), 8000 (API), 8001 (ML), 5432 (DB) |
| RAM | 2 GB minimum |
| Disk | 1 GB |

No other dependencies. Everything runs inside containers.

---

## Run

```bash
# Clone and enter the project
git clone https://github.com/AbhinavChand08/HEXA-FORGE.git
cd HEXA-FORGE

# Build and start all 4 services (registry-db, api, ml, ui)
docker compose up -d --build

# Wait ~30 seconds for the DB to initialize and seed
# Then open the UI
open http://localhost:3000
```

**Services started:**

| Service | URL | Description |
|---|---|---|
| UI | http://localhost:3000 | React frontend |
| API | http://localhost:8000 | FastAPI backend |
| ML | http://localhost:8001 | Mapping suggestion service |
| DB | localhost:5432 | PostgreSQL + pgvector |

To stop: `docker compose down`
To wipe data and restart clean: `docker compose down -v && docker compose up -d --build`

---

## Evaluator walkthrough

### 1. Upload a device configuration

- Open http://localhost:3000
- Click **Add devices**
- Upload any `.conf`, `.cfg`, or `.txt` file from `samples/configs/` (e.g. `samples/configs/cisco/` or `samples/configs/junos/`)
- The system auto-detects the vendor, flattens, and normalizes the config

Sample configs are included for: **Cisco, Juniper, Arista, FortiGate, F5, Huawei, Sophos, VyOS, MikroTik, Palo Alto**

### 2. Run a compliance audit

- Click a device → **Run audit**
- Select one or more benchmarks: **CIS**, **NIST**, or **STIG**
- View PASS / FAIL / UNKNOWN findings per control with source line evidence
- Download the text report

### 3. Review unknown commands (HITL)

- If a device shows **"N commands need review"**, open the **Mapping review** tab
- The ML service suggests a canonical key and value expression
- Accept or override the suggestion, then click **Approve mapping**
- The device is re-normalized immediately; re-run the audit to see updated findings

### 4. API (optional)

```bash
# Health check
curl http://localhost:8000/api/v1/health

# List devices
curl http://localhost:8000/api/v1/devices

# Upload a config
curl -X POST http://localhost:8000/api/v1/devices \
     -F "files=@samples/configs/cisco/cis-hardened-running-config.txt"
```

---

## Project structure

```
v6/
├── backend/          FastAPI app (Python 3.11, psycopg3, PostgreSQL)
├── frontend/         React + TypeScript + Next.js UI
├── ml/               Mapping suggestion service (sentence-transformers / heuristic)
├── registry-seed/    Vendor profiles, ontology, rules, learned mappings
│   ├── vendors/      Dialect fingerprints for 10 vendors
│   ├── ontology/     Canonical key definitions (SSH, SNMP, NTP, crypto, …)
│   ├── rules/        Benchmark rule sets (baseline, nist, stig)
│   └── learned/      Pre-mined templates and mappings
└── samples/          Sample device configs for testing
```

---

## Troubleshooting

**UI shows "Failed to load devices"** — API is still starting. Wait 30 s and refresh.

**Port conflict** — If 3000 or 8000 is in use, edit `docker-compose.yml` and change the host-side port (left side of `:`).

**Check logs:**
```bash
docker compose logs api     # Backend logs
docker compose logs ui      # Frontend logs
docker compose logs ml      # ML service logs
```

