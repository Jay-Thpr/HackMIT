CREATE TABLE IF NOT EXISTS tenants (
    tenant_id text PRIMARY KEY,
    next_sequence bigint NOT NULL DEFAULT 1 CHECK (next_sequence > 0)
);

CREATE TABLE IF NOT EXISTS orders (
    tenant_id text NOT NULL REFERENCES tenants(tenant_id),
    order_id uuid NOT NULL,
    sequence bigint NOT NULL CHECK (sequence > 0),
    amount_cents integer NOT NULL CHECK (amount_cents > 0),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    fulfilled_at timestamptz,
    PRIMARY KEY (tenant_id, order_id),
    UNIQUE (tenant_id, sequence)
);

CREATE TABLE IF NOT EXISTS outbox (
    tenant_id text NOT NULL,
    order_id uuid NOT NULL,
    sequence bigint NOT NULL,
    published_at timestamptz,
    PRIMARY KEY (tenant_id, order_id),
    FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id)
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (tenant_id, sequence) WHERE published_at IS NULL;

CREATE TABLE IF NOT EXISTS payments (
    tenant_id text NOT NULL,
    order_id uuid NOT NULL,
    sequence bigint NOT NULL,
    amount_cents integer NOT NULL CHECK (amount_cents > 0),
    processed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, order_id),
    UNIQUE (tenant_id, sequence),
    FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, order_id)
);

CREATE TABLE IF NOT EXISTS tenant_progress (
    tenant_id text PRIMARY KEY REFERENCES tenants(tenant_id),
    last_sequence bigint NOT NULL DEFAULT 0 CHECK (last_sequence >= 0)
);

CREATE TABLE IF NOT EXISTS catalog (
    item_id integer PRIMARY KEY,
    price_cents integer NOT NULL CHECK (price_cents > 0),
    revision bigint NOT NULL DEFAULT 1
);
INSERT INTO catalog (item_id, price_cents)
SELECT item, 1000 + item FROM generate_series(1, 32) AS item
ON CONFLICT (item_id) DO NOTHING;
