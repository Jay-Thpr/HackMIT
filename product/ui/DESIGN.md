# Faultline design system

An on-call engineer in a bright shared workspace needs to calmly reconstruct an incident without losing where an observation came from.

## Palette

User-directed light theme: eggshell background, beige/tan environment surfaces, dark warm ink and oxide red for the active investigation path. Restrained semantic green is reserved for confirmed healthy states; amber for degradation, neutral gray for unknown. Clone identity is warm ochre and graphite, reinforced by labels.

## Typography and layout

The original template is not a visual reference. Recompose around the spatial system: compact incident title and context, dominant 3D map, contextual evidence, and a replay transport. The primary action follows the investigation.

A readable sans-serif family with tabular numeric data. Compact headings, a persistent narrow sidebar, context and metrics above the topology, adjacent evidence inspector, synchronized replay and supporting observability. Fine separators and consistent modest radii. Mobile stacks panels and retains accessible navigation and controls.

## Motion

Red trail shows emitted investigation attention, not literal packets or private reasoning. Camera follows meaningful lifecycle events only when follow is enabled. Manual interaction suspends follow. Playback pause freezes motion. Reduced motion preserves information without travel. Standard controls transition in roughly 150–250 ms.

## Component rules

Every actionable control has a focus state and a descriptive name. Environment selection scopes observations. Draft validation is local and nonpersistent; approvals cannot execute. Charts disclose illustrative values and omit unknown metrics. Graph nodes represent logical entities, never invented container counts.


### Spatial exploration revision (latest direction)

The user's Sorbet palette reference supersedes the tan dashboard treatment: near-white #FEFEFE, pale gray #EDECEC, gray #CCCCCC, muted sage #B7C396/#E0E7D7 and dusty rose #BA9A91, with restrained red for investigation attention. No floors, platforms, pedestals, ground grids, or persistent node name/metric cards. Systems are unnamed three-dimensional forms suspended in open space, joined by thin wires. Node selection reveals its identity, symptoms, environment-scoped agent activity, tool calls and evidence. The inspector and supporting charts open on demand; the default scene is unobstructed. Accessible labels and an entity finder preserve keyboard/screen-reader access without adding visible name clutter. Environment identities remain available through explicit selectors.

Node selection animates a deliberate zoom and pan; inspector content enters as focus settles. Closing selection restores the broader environment. Reduced motion disables travel and entrance movement. Motion must not reveal future events or imply unrecorded agent reasoning.


### Dark layered workspace (supersedes prior light scene)

Use a continuous dark charcoal-green spatial backdrop and white infrastructure. The logo follows the white/sage/orange-red palette. Agent activity highlights the actual target structure in orange-red, while health remains a separate status in the inspector. Production entities share one horizontal level; each isolated clone occupies its own distinct horizontal level. There are no floor slabs or bounding boxes. Selecting a layer or its node makes the other levels translucent; All layers restores the complete system. Selection retains the zoom/pan transition, reduced-motion alternative, environment-scoped evidence, and replay isolation. Keep the workspace unboxed with restrained controls and an on-demand inspector.


### Recovery and clone test columns (current direction)

The workspace is 3D only; remove the flat-view toggle. Keep the desktop inspector as a stable overlay with fixed identity/tabs and one scrolling evidence body. Camera selections ease over roughly 0.65 seconds, with smoothly changing isolation opacity and material colors; reduced motion remains supported. Floating exclamation buttons identify affected systems and open their symptoms and scoped activity. System material transitions from red (degraded), through orange (recovery awaiting confirmation), to white (measured confirmation in that same environment). Agent attention remains a separate orange trail/ring.

Tests appear as a short row of miniature three-dimensional columns around the base of the affected structure in each clone, without a floor or detached marker panel. Each column becomes green only when its individual recorded assertion passes; all unpassed tests stay red, including pending, running, and failed checks. Names stay hidden until hover/click; selection opens expected/observed details in the inspector. A passing prediction is not a claim of system recovery: Clone B can correctly predict overload returning. All current results are explicitly simulated and replayable, with no future results shown when seeking backward.

Parallel work: spatial agent owns scene geometry, camera, issue markers and test columns; visual agent owns panel and motion CSS; suite dialogue agent advises test semantics and gathers preferences through the lead; integration lead owns replay data, inspection, tests and documentation. Verify locally with `cd product/ui && npm run build && npm test && npm run test:browser`; start preview with `npm run dev -- --port 4173 --strictPort`. Check both architectures, column progress and rewind, issue details, layer translucency, responsive inspector, and reduced motion. Browser checks require installed Chrome.


### Explanation page and large test suites

“Why this incident?” is a separate page showing current production symptoms, competing causes, why each reversible clone perturbation was chosen, expected responses, test results, production/clone charts, and the current conclusion. It shares the replay cursor and hides future evidence. “Agent workspace” remains the 3D activity view, with links between the two pages.

The scene renders four small columns per affected clone structure, representing baseline, reproduction, probe, and release groups. A column stays red until every test in its group passes, then becomes green. Hover exposes counts; selection opens a searchable inspector with status filters and 20-case pages, so 1,000 cases do not become 1,000 meshes or DOM rows. Optional frontend test manifests and individual result IDs support this aggregation. Existing demos still contain four simulated tests, not 1,000 invented live results. A synthetic 1,000-case test verifies bounded groups, partial completion and rewind.


### Shared visual system

All pages and dialogs use the charcoal-green background, off-white text, sage controls, and restrained rust accents from the spatial workspace. Charts use light axes and distinct light rust/sand lines. Avoid decorative gradient banners, oversized tilted icons, nested cards and marketing-style copy. Panels use clear headings, readable 13px text with generous line spacing, and short 180–220ms reveals; reduced motion removes them. Run the cross-page accessibility check after palette changes, including explanation, observability, experiments, replay, and the draft dialog.


### Agent identity and quieter recovery motion

Infrastructure stays near off-white with a muted rose tint while degraded and a warm ivory tint while recovery awaits confirmation. Material interpolation eases these nearby colors; avoid saturated red/orange/white body swaps. Issue buttons retain explicit state. Each environment has a clickable agent marker that glides to the current target with a restrained scanning ring during playback. The agent inspector leads with current work, reason for the step, and the next observation; these are recorded simulation summaries, not hidden model reasoning. Result-only events without a target must not pull the camera back from the current system. Honor pause and reduced motion.
