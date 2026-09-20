LOCK_TENANT = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"
ENSURE_TENANT = "INSERT INTO tenants (tenant_id) VALUES ($1) ON CONFLICT DO NOTHING"
EXISTING_ORDER = "SELECT order_id, sequence, amount_cents, accepted_at, fulfilled_at FROM orders WHERE tenant_id = $1 AND order_id = $2"
NEXT_SEQUENCE = "UPDATE tenants SET next_sequence = next_sequence + 1 WHERE tenant_id = $1 RETURNING next_sequence - 1"
INSERT_ORDER = "INSERT INTO orders (tenant_id, order_id, sequence, amount_cents) VALUES ($1, $2, $3, $4) RETURNING accepted_at"
INSERT_OUTBOX = "INSERT INTO outbox (tenant_id, order_id, sequence) VALUES ($1, $2, $3)"
PENDING_OUTBOX = """
SELECT o.tenant_id, o.order_id, o.sequence, o.amount_cents, o.accepted_at
FROM outbox b JOIN orders o USING (tenant_id, order_id)
WHERE b.published_at IS NULL
ORDER BY b.tenant_id, b.sequence
LIMIT 100
FOR UPDATE OF b
"""
MARK_PUBLISHED = "UPDATE outbox SET published_at = clock_timestamp() WHERE tenant_id = $1 AND order_id = $2"
EXISTING_PAYMENT = "SELECT sequence, amount_cents FROM payments WHERE tenant_id = $1 AND order_id = $2"
ENSURE_PROGRESS = "INSERT INTO tenant_progress (tenant_id) VALUES ($1) ON CONFLICT DO NOTHING"
LOCK_PROGRESS = "SELECT last_sequence FROM tenant_progress WHERE tenant_id = $1 FOR UPDATE"
INSERT_PAYMENT = "INSERT INTO payments (tenant_id, order_id, sequence, amount_cents) VALUES ($1, $2, $3, $4)"
ADVANCE_PROGRESS = "UPDATE tenant_progress SET last_sequence = $2 WHERE tenant_id = $1"
FULFILL_ORDER = "UPDATE orders SET fulfilled_at = clock_timestamp() WHERE tenant_id = $1 AND order_id = $2"
CATALOG_ITEM = "SELECT item_id, price_cents, revision FROM catalog WHERE item_id = $1"
PRIMARY_POSITION = "SELECT pg_current_wal_lsn()::text AS lsn, pg_is_in_recovery() AS in_recovery"
REPLICA_POSITION = "SELECT pg_last_wal_replay_lsn()::text AS lsn, pg_is_in_recovery() AS in_recovery"
REPLICATION_PEERS = "SELECT application_name, state, sync_state, write_lsn::text, flush_lsn::text, replay_lsn::text FROM pg_stat_replication"
PAUSE_REPLAY = "SELECT pg_wal_replay_pause()"
RESUME_REPLAY = "SELECT pg_wal_replay_resume()"
REPLAY_PAUSED = "SELECT pg_is_wal_replay_paused()"
TENANT_SNAPSHOT = """
SELECT o.tenant_id,
       count(*) AS accepted,
       count(p.order_id) AS paid,
       count(*) FILTER (WHERE o.fulfilled_at IS NOT NULL) AS fulfilled,
       count(*) FILTER (WHERE b.published_at IS NULL) AS outbox_pending,
       count(*) FILTER (WHERE p.order_id IS NULL) AS outstanding,
       extract(epoch FROM (clock_timestamp() - min(o.accepted_at)
         FILTER (WHERE p.order_id IS NULL))) * 1000 AS oldest_outstanding_ms,
       count(*) FILTER (WHERE p.order_id IS NOT NULL AND
         (p.amount_cents <> o.amount_cents OR p.sequence <> o.sequence)) AS mismatched_effects,
       count(*) FILTER (WHERE (p.order_id IS NULL) <> (o.fulfilled_at IS NULL)) AS inconsistent_fulfillment
FROM orders o
LEFT JOIN payments p USING (tenant_id, order_id)
LEFT JOIN outbox b USING (tenant_id, order_id)
GROUP BY o.tenant_id ORDER BY o.tenant_id
"""
ORDERING_VIOLATIONS = """
WITH ordered AS (
    SELECT tenant_id, sequence, processed_at,
           lag(processed_at) OVER (PARTITION BY tenant_id ORDER BY sequence) AS previous_at
    FROM payments
)
SELECT tenant_id, count(*) AS violations
FROM ordered WHERE processed_at < previous_at GROUP BY tenant_id
"""
MISSING_OUTBOX = """
SELECT o.tenant_id, count(*) AS missing
FROM orders o LEFT JOIN outbox b USING (tenant_id, order_id)
WHERE b.order_id IS NULL GROUP BY o.tenant_id
"""
ORPHAN_PAYMENTS = """
SELECT p.tenant_id, count(*) AS orphaned
FROM payments p LEFT JOIN orders o USING (tenant_id, order_id)
WHERE o.order_id IS NULL GROUP BY p.tenant_id
"""
DUPLICATE_EFFECTS = """
SELECT tenant_id, order_id, count(*) AS effects
FROM payments GROUP BY tenant_id, order_id HAVING count(*) > 1
"""
COMPLETION_WINDOW = """
SELECT tenant_id, count(*) AS completed,
       percentile_cont(0.5) WITHIN GROUP
         (ORDER BY extract(epoch FROM (fulfilled_at - accepted_at)) * 1000) AS p50_ms,
       percentile_cont(0.99) WITHIN GROUP
         (ORDER BY extract(epoch FROM (fulfilled_at - accepted_at)) * 1000) AS p99_ms
FROM orders
WHERE fulfilled_at >= $1 AND fulfilled_at < $2
GROUP BY tenant_id ORDER BY tenant_id
"""
