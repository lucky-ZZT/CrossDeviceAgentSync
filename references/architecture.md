# Architecture

## Objective

Reconcile two independently changing Codex homes while allowing the user to select conversations and related workspace content. The initial transport is an offline package. A future network transport may carry the same manifests and checkpoints, but it must not change conflict semantics.

## Components

1. Inventory layer: Read session JSONL files and produce content and event fingerprints without copying conversation text into the comparison plan.
2. Planning layer: Match by task ID, determine prefix relationships, classify divergence, and record explicit selections and direction.
3. Migration layer: Use Codex ReHome to package selected content, rewrite paths, restore indexes, and verify visibility.
4. Local repair layer: Use codex-provider-sync after restore only when provider/model metadata prevents visibility or continuation.
5. Checkpoint layer: Retain device IDs, inventory hashes, plan, package hash, restore transaction ID, and verification result.

## Conversation Relationships

Comparison uses ordered semantic line fingerprints. Known machine-local metadata fields such as `cwd`, `workspace_root`, `rollout_path`, `model_provider`, and `model` are normalized. Conversation content, identifiers, timestamps, tool calls, and encrypted payloads are not normalized.

| Relationship | Meaning | Safe default |
| --- | --- | --- |
| identical | Raw files match | Skip |
| metadata_equivalent | Semantic events match after local metadata normalization | Preserve content; repair metadata if needed |
| left/right only | Task exists on one device | Import to missing side if selected |
| left/right ahead | One ordered event sequence is a prefix of the other | Treat as fast-forward candidate; branch until replacement is proven safe |
| diverged | Both share a prefix and then differ | Preserve both branches |
| id_collision | Same task ID with no shared semantic prefix | Rewrite imported branch ID |
| duplicate_local_id | More than one local file claims the same task ID | Manual inspection |

## Selection Model

Selection is task-based and direction-specific. A plan must record:

- source and target device IDs
- source and target inventory hashes
- selected task IDs
- per-task relationship and requested action
- deterministic proposed branch ID for each conflicting source version
- whether projects, skills, plugins, or generated artifacts are included
- creation time and plan schema version

Changing a selection produces a new plan. Applying a plan against an inventory with a different hash requires a rescan.

## Bidirectional Sequence

There is no cross-device atomic transaction. Use this sequence:

1. Inventory both devices.
2. Select and apply device A to device B.
3. Verify device B and record a checkpoint.
4. Inventory both devices again.
5. Select and apply device B to device A.
6. Verify device A and record the final checkpoint.

Never generate both write packages from the original comparison and apply them independently. The first restore changes the state used to plan the second restore.

## Merge Meanings

Storage merge means both histories are visible and correctly indexed. Semantic merge means a new continuation summarizes and reconciles both branches. Storage merge must happen first; semantic merge never mutates the source branches.

## Future Transport Contract

A live synchronization service should exchange signed inventory deltas and encrypted content-addressed blobs. It must preserve the same task classifications, selection plan, deterministic branch identities, checkpoint validation, and one-target-at-a-time apply discipline.

