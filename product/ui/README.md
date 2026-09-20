# Faultline investigation workspace

A dark, open spatial investigation workspace with white unnamed nodes, fine wire connections, and orange-red agent activity. Production and clones occupy separate horizontal levels. This is an interactive design preview with synthetic data, not a connection to production.

## Run locally

Use Node 24 and installed Google Chrome for browser checks.

```sh
npm ci
npm run dev -- --port 4173 --strictPort
```

Open http://127.0.0.1:4173. No credentials, Docker stack, or backend processes are required.

## Explore

- **Follow investigation** plays the scripted incident and follows significant events in the map. Dragging the camera switches to manual control.
- Select **Commerce platform** or **Event pipeline**. Both run through the same graph and replay model.
- Select a layer to focus it and fade other layers. All layers restores the full system. Select a clone to inspect its isolated state. Click a floating node to zoom/pan into it and reveal its identity, telemetry and environment-scoped agent activity. Close the inspector to pull back. There are no persistent node labels, platforms or ground planes.
- Expand Investigation steps to navigate phases; the phase controls and replay transport share one cursor. Seeking backward removes future actions, clones, evidence and verdicts.
- Use **Find an entity** for keyboard-friendly selection. The workspace is 3D only.
- Small test columns surround the affected structure in each clone. Hover for names; click for recorded assertions. Each turns green when its own simulated check passes.
- Observability, experiment lab and replay library retain the same time and environment context.
- Experiment drafts validate locally. Approvals are explicitly unavailable. Neither surface executes infrastructure actions or persists a draft.

## Verify

```sh
npm run build
npm test
npm run test:browser
```

The browser suite starts/reuses port 4173, checks actual WebGL, both architectures, backward replay, draft controls, mobile/reduced motion, phase navigation and accessibility. Screenshots are written to ignored `test-results/`.

## Boundaries

`model.ts` is a frontend view model, not a C1–C6 contract change. Scenario metrics are illustrative presentation values; rate displays such as error percentages are not raw C1 ratios. Live wiring must use the canonical Owner 2 telemetry builder/store and audited Product adapters through an authenticated backend. Never place backend credentials in frontend environment variables.

Read `PRODUCT.md`, `DESIGN.md`, and the root PRD for the agreed direction and parallel file ownership.
