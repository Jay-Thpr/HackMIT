import { deriveTopology, type NodeReading, type Scenario } from '../model'

type Shape = Pick<Scenario, 'topology' | 'baseline'>

const ok = (latency: number, qps: number, extra: Partial<NodeReading> = {}): NodeReading =>
  ({ health: 'healthy', latency, qps, errorRate: 0.1, retryRatio: 1, ...extra })
const unknown: NodeReading = { health: 'unknown' }

/** Each arc gets its own silhouette. The arcs describe different services, so reusing one
 *  topology across several would imply a single system had all six incidents. Node ids that
 *  an arc's readings reference are retained exactly; only the surrounding structure differs. */

// ambiguous-pair: the tie is database capacity versus retries, so the database tier is
// explicit - a read replica and the connection pool the callers queue on.
export const checkoutWithReplica: Shape = {
  topology: deriveTopology({
    services: { gateway: {}, 'catalog-api': {}, 'orders-api': {}, payments: {}, 'conn-pool': {}, 'primary-db': {}, 'primary-db-replica': {} },
    edges: [
      { src: 'gateway', dst: 'catalog-api' }, { src: 'gateway', dst: 'orders-api' },
      { src: 'catalog-api', dst: 'cache' }, { src: 'orders-api', dst: 'payments' },
      { src: 'orders-api', dst: 'conn-pool' }, { src: 'payments', dst: 'conn-pool' },
      { src: 'conn-pool', dst: 'primary-db' }, { src: 'primary-db', dst: 'primary-db-replica' },
      { src: 'catalog-api', dst: 'primary-db-replica' },
    ],
  }, {
    'primary-db': { kind: 'datastore' }, 'primary-db-replica': { kind: 'datastore', label: 'primary-db-replica - reads' },
    cache: { kind: 'datastore' }, 'conn-pool': { kind: 'queue', label: 'conn-pool - 40 slots' }, 'orders-api': { instances: 2 },
  }),
  baseline: {
    gateway: ok(48, 80), 'catalog-api': ok(24, 34), 'orders-api': ok(42, 80), payments: ok(32, 80),
    'conn-pool': ok(4, 80, { utilization: 46, resourceMetrics: { queued_requests: 2 } }),
    'primary-db': ok(16, 80, { utilization: 48 }),
    'primary-db-replica': ok(13, 34, { utilization: 31, resourceMetrics: { replica_lag_bytes: 3072 } }),
    cache: ok(3, 34, { utilization: 24 }),
  },
}

// benign-spike: the smallest graph in the set. A non-incident should look calm.
export const edgeCheckout: Shape = {
  topology: deriveTopology({
    services: { cdn: {}, gateway: {}, 'orders-api': {}, 'primary-db': {} },
    edges: [
      { src: 'cdn', dst: 'gateway' }, { src: 'gateway', dst: 'orders-api' },
      { src: 'orders-api', dst: 'cache' }, { src: 'orders-api', dst: 'primary-db' },
    ],
  }, { 'primary-db': { kind: 'datastore' }, cache: { kind: 'datastore' }, cdn: { kind: 'external', label: 'cdn - edge' } }),
  baseline: {
    cdn: unknown, gateway: ok(46, 96), 'orders-api': ok(41, 96),
    'primary-db': ok(15, 96, { utilization: 44 }), cache: ok(3, 62, { utilization: 22 }),
  },
}

// bad-deploy: the outbox and consumer-group path only. Shards and replicas are irrelevant to
// a worker concurrency regression, so they are not drawn.
export const fulfilmentQueue: Shape = {
  topology: deriveTopology({
    services: {
      gateway: {}, 'checkout-api': {}, 'outbox-relay': {},
      'kafka-0': {}, 'kafka-1': {}, 'kafka-2': {},
      'worker-1': {}, 'worker-2': {}, 'worker-3': {}, 'shard-1': {},
    },
    edges: [
      { src: 'gateway', dst: 'checkout-api' }, { src: 'checkout-api', dst: 'shard-1' },
      { src: 'outbox-relay', dst: 'shard-1' },
      { src: 'outbox-relay', dst: 'kafka-0' }, { src: 'outbox-relay', dst: 'kafka-1' }, { src: 'outbox-relay', dst: 'kafka-2' },
      { src: 'kafka-0', dst: 'worker-1' }, { src: 'kafka-1', dst: 'worker-2' }, { src: 'kafka-2', dst: 'worker-3' },
      { src: 'worker-1', dst: 'payments-provider' }, { src: 'worker-2', dst: 'payments-provider' }, { src: 'worker-3', dst: 'payments-provider' },
    ],
  }, {
    'kafka-0': { kind: 'queue', label: 'kafka-0 - orders p0,p3' }, 'kafka-1': { kind: 'queue', label: 'kafka-1 - orders p1,p4' },
    'kafka-2': { kind: 'queue', label: 'kafka-2 - orders p2,p5' },
    'shard-1': { kind: 'datastore', label: 'shard-1 - tenants c,d', tenants: ['c', 'd'] },
    'worker-1': { tenants: ['a', 'b'] }, 'worker-2': { tenants: ['c', 'd'] }, 'worker-3': { tenants: ['e', 'f'] },
  }),
  baseline: {
    gateway: ok(46, 120), 'checkout-api': ok(38, 78),
    'outbox-relay': ok(12, 64, { resourceMetrics: { outbox_age_ms: 180 } }),
    'kafka-0': ok(9, 21, { utilization: 34, resourceMetrics: { consumer_lag_messages: 12 } }),
    'kafka-1': ok(9, 22, { utilization: 36, resourceMetrics: { consumer_lag_messages: 14 } }),
    'kafka-2': ok(9, 21, { utilization: 33, resourceMetrics: { consumer_lag_messages: 11 } }),
    'worker-1': ok(34, 21, { resourceMetrics: { oldest_pending_ms: 240 } }),
    'worker-2': ok(35, 22, { resourceMetrics: { oldest_pending_ms: 260 } }),
    'worker-3': ok(33, 21, { resourceMetrics: { oldest_pending_ms: 230 } }),
    'shard-1': ok(16, 52, { utilization: 51 }), 'payments-provider': unknown,
  },
}

// hot-key: recognisably the same family as tenant-confined but trimmed to the read-and-shard
// path, because a hot key is a data-distribution story rather than a fulfilment one.
export const shardedReads: Shape = {
  topology: deriveTopology({
    services: {
      gateway: {}, 'checkout-api': {}, 'inventory-api': {},
      'shard-0': {}, 'shard-1': {}, 'shard-2': {}, 'shard-1-replica': {}, redis: {},
      'kafka-1': {}, 'worker-2': {},
    },
    edges: [
      { src: 'gateway', dst: 'checkout-api' }, { src: 'gateway', dst: 'inventory-api' },
      { src: 'checkout-api', dst: 'redis' }, { src: 'inventory-api', dst: 'redis' },
      { src: 'checkout-api', dst: 'shard-0' }, { src: 'checkout-api', dst: 'shard-1' }, { src: 'checkout-api', dst: 'shard-2' },
      { src: 'inventory-api', dst: 'shard-1' }, { src: 'shard-1', dst: 'shard-1-replica' },
      { src: 'kafka-1', dst: 'worker-2' }, { src: 'worker-2', dst: 'shard-1' },
    ],
  }, {
    redis: { kind: 'datastore' }, 'kafka-1': { kind: 'queue', label: 'kafka-1 - orders p1,p4' },
    'shard-0': { kind: 'datastore', label: 'shard-0 - tenants a,b', tenants: ['a', 'b'] },
    'shard-1': { kind: 'datastore', label: 'shard-1 - tenants c,d', tenants: ['c', 'd'] },
    'shard-2': { kind: 'datastore', label: 'shard-2 - tenants e,f', tenants: ['e', 'f'] },
    'shard-1-replica': { kind: 'datastore' }, 'checkout-api': { instances: 2 }, 'worker-2': { tenants: ['c', 'd'] },
  }),
  baseline: {
    gateway: ok(46, 120), 'checkout-api': ok(38, 78), 'inventory-api': ok(21, 42),
    redis: ok(2, 120, { utilization: 22 }),
    'kafka-1': ok(9, 22, { utilization: 36, resourceMetrics: { consumer_lag_messages: 14 } }),
    'worker-2': ok(35, 22, { resourceMetrics: { oldest_pending_ms: 260 } }),
    'shard-0': ok(14, 40, { utilization: 44 }), 'shard-1': ok(16, 52, { utilization: 51 }), 'shard-2': ok(14, 39, { utilization: 43 }),
    'shard-1-replica': ok(12, 52, { utilization: 34, resourceMetrics: { replica_lag_bytes: 5120 } }),
  },
}
