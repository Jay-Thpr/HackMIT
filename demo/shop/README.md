# shop — demo checkout path

A small, realistic service tree that Faultline's durable-fix step patches during the demo.
It mirrors the retry topology of the sandbox (Orders -> Envoy -> Payments -> DB) at the
shape of a production shop, so the pull request judges see touches every hop a retry
storm travels through.

```
client -> gateway (envoy) -> api -> cache (redis)
                                 -> inventory -> primary-db (postgres, via pgbouncer)
                                 -> payments  -> processor
```

Nothing here is deployed by the sandbox. Configuration values are the pre-incident
defaults: unbounded-ish retries at every hop, no retry budget, no deadline propagation,
no statement timeout on the primary. That is the amplification path Faultline's
experiment isolates (`H_meta`, retry storm) and the pull request bounds.

| Directory | What lives there |
|---|---|
| `gateway/` | Envoy route config: per-route retry policy and timeouts |
| `api/` | Checkout API: outbound retry policy, downstream clients, deadline handling |
| `cache/` | Redis client config: miss handling, stampede protection |
| `inventory/` | Inventory service: DB pool + lock-timeout retry config |
| `payments/` | Payments service: processor client retries and hedging |
| `primary-db/` | Postgres + pgbouncer: connection limits, statement timeouts, queue wait |
