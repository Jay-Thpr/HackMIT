-- Same schema on db-primary and db-standby.
CREATE TABLE payments (
    id           bigserial PRIMARY KEY,
    order_id     text        NOT NULL,
    amount_cents integer     NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Per-query cost of this database instance. The fault controller is the only writer
-- (base_ms on startup, extra_ms for injected slowdowns); the app never reads it directly.
CREATE TABLE io_profile (
    id       integer PRIMARY KEY,
    base_ms  integer NOT NULL,
    extra_ms integer NOT NULL
);
INSERT INTO io_profile VALUES (1, 38, 0);

-- The one query Payments runs. It holds its connection for base_ms + extra_ms, which makes the
-- capacity through a pool of N connections exactly N / (base_ms + extra_ms) queries/s.
CREATE FUNCTION process_payment(p_order_id text, p_amount_cents integer) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    cost_ms integer;
    new_id  bigint;
BEGIN
    SELECT base_ms + extra_ms INTO cost_ms FROM io_profile WHERE id = 1;
    PERFORM pg_sleep(cost_ms / 1000.0);
    INSERT INTO payments (order_id, amount_cents) VALUES (p_order_id, p_amount_cents) RETURNING id INTO new_id;
    RETURN new_id;
END $$;
