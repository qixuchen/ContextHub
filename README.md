# ContextHub AMC

AMC (Agent Memory Core) is the memory subsystem in ContextHub.
It stores agent trajectories and makes them reusable through retrieval and skill evolution.

## What This Repo Does

Core capabilities:

- **Commit**: ingest a trajectory, build raw/clean dependency graphs, generate summaries, and persist artifacts.
- **Retrieve**: recall relevant trajectories via vector similarity and optional graph matching.
- **Skill system**: retrieve skills, route create/update decisions, and evolve `SKILL.md`.
- **Inter-trajectory trigger**: link related trajectories and auto-trigger skill route when thresholds are met.
- **Promote**: promote agent-scope trajectories to team scope for sharing.

Main docs live in `AMC_plan/`:

- `AMC_plan/00-index.md` (reading order)
- `AMC_plan/02-architecture.md` (runtime architecture)
- `AMC_plan/04-commit-pipeline.md`
- `AMC_plan/05-retrieve-pipeline.md`
- `AMC_plan/06-skill-evolve-trace2skill-plan.md`

## Setup Guide

### 1) Prerequisites

- Python **3.11+**
- PostgreSQL with `pgvector` extension
- Neo4j

> Full chain testing requires both PostgreSQL and Neo4j running.

#### 1.1 Install PostgreSQL + pgvector (Ubuntu)

```bash
sudo apt install -y postgresql-common
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
sudo apt install -y postgresql postgresql-17-pgvector
sudo systemctl enable postgresql
sudo systemctl start postgresql
```

Enable extension in your DB:

```bash
sudo -u postgres psql
CREATE EXTENSION IF NOT EXISTS vector;
\q
```

#### 1.2 Install Neo4j (Ubuntu)

```bash
sudo apt update
sudo apt install -y neo4j
sudo systemctl enable neo4j
sudo systemctl start neo4j
```

Check status:

```bash
sudo systemctl status neo4j
```

### 2) Configure Environment

Create `.env` from `.env.example`, then fill at least:

- `AMC_OPENAI_API_KEY`
- `AMC_PGVECTOR_DSN`
- `AMC_NEO4J_URI`
- `AMC_NEO4J_USER`
- `AMC_NEO4J_PASSWORD`

Non-secret defaults are in `config/config.yaml`.
Config reference: `AMC_plan/11-configuration-spec.md`.

### 3) Install Dependencies

Using `uv` (recommended):

```bash
uv sync --extra dev
```

Or using `pip`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
```

### 4) Start AMC

Recommended one-command startup:

```bash
bash scripts/start_amc.sh
```

This script:

- ensures `.venv` exists
- installs dependencies when needed
- starts PostgreSQL and Neo4j if needed
- runs API server (`main:app` from `src/`)

API default:

- Base: `http://127.0.0.1:8000/api/v1/amc`
- Health: `http://127.0.0.1:8000/healthz`

## Quick Verification

In another terminal:

```bash
python scripts/test_commit_api.py --pretty
python scripts/test_retrieve_api.py --pretty
python scripts/test_promote_api.py --trajectory-id <trajectory_id> --pretty
```

## Useful CLI Commands

Direct CLI commit (without HTTP):

```bash
amc-commit-trajectory sample_traj/traj1.json --account-id acc-demo --agent-id agent-a --scope agent --owner-space agent-a --pretty
```

Batch commit:

```bash
amc-commit-trajectory-batch --account-id acc-demo --agent-id agent-a --scope agent --owner-space agent-a --pretty
```

Inter-trajectory graph visualization:

```bash
amc-visualize-intertrajectory --account-id acc-demo --agent-id agent-a --scope agent --owner-space agent-a --pretty
```

Skill utilities:

```bash
amc-compute-skill-embedding
amc-route-skill --help
amc-evolve-skill --help
```

## Reset AMC Storage

Use reset script when you want a clean local state.

Dry run (preview only):

```bash
python scripts/reset_amc_storage.py --dry-run --pretty
```

Reset trajectory content + trajectory vector index + Neo4j:

```bash
python scripts/reset_amc_storage.py --yes --pretty
```

Also clear skill embedding index:

```bash
python scripts/reset_amc_storage.py --yes --include-skill-embedding --pretty
```
