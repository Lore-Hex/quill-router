# ClickHouse Sharding Gate

TrustedRouter currently uses one logical ClickHouse shard with three replicas.
The replicas provide read availability and durable copies. They do not multiply
write throughput because one copy of each metadata row belongs to the shard.

Run the bounded capacity probe before a major traffic increase:

```bash
TR_CLICKHOUSE_CAPACITY_ROWS=5000000 \
  scripts/deploy/clickhouse_capacity_smoke.sh --apply
```

The probe touches only a temporary table and removes it on success or failure.
Add another shard when either condition holds:

1. Projected peak metadata rows per second exceed 25 percent of measured
   replicated ingest throughput.
2. Production p95 query latency exceeds 250 ms or replication lag exceeds 30
   seconds under sustained load.

New shards should use three replicas each. Distribute tenant activity by the
opaque tenant hash and provider or synthetic data by stable event ID. Keep a
Distributed table for cross-shard analytics. Never shard on a raw workspace ID,
API key, prompt, or output.
