# AGENTS.md — SDLC Harness Master Map

This file is injected into every agent's context at runtime.
It is a MAP ONLY — ~100 lines. All detail lives in docs/ and policies/.

## What this harness is

A generic, domain-agnostic scaffold for running AI agents across all
six SDLC phases. Drop into any new repo, configure the policies/, and
agents will drive development with deterministic constraints enforcing
quality at every gate.

## The golden rule

> Anything the agent cannot access in-context does not exist.
> All knowledge must live as versioned files in this repository.

## Phase map

| Phase | Agent(s) | Gate artifact |
|-------|----------|---------------|
| 1 · Requirements | RequirementsAgent | docs/requirements.md committed |
| 2 · Design | ArchitectureAgent | policies/ all committed |
| 3 · Development | DevAgent (per module) | structural linter passes CI |
| 4 · Testing | QAAgent, ScenarioAgent, AdversarialAgent | docs/edge_cases.md zero open items |
| 5 · Deployment | ReleaseAgent, RollbackAgent | canary quality above threshold |
| 6 · Monitoring | GCAgent (nightly) | continuous — no gate |

## Knowledge sources (read these, not this file)

- docs/requirements.md        → structured intent for this project
- docs/architecture.md        → agent map, module boundaries
- docs/uncertain_terms.md     → ambiguous terms awaiting resolution
- docs/quality.md             → per-agent confidence scores
- docs/edge_cases.md          → QA findings, open items
- policies/policy.yaml        → domain-specific hard rules
- policies/conflict_policy.yaml → how to resolve agent disagreements
- policies/rollback_triggers.yaml → deployment rollback thresholds
- policies/agent_schema.json  → input/output schema for agents

## Constraint layers (never bypass these)

Layer 0 — Policy files: read-only at runtime. Changes via PR only.
Layer 1 — Specialist agents: may not call each other directly.
Layer 2 — Orchestrator: only cross-agent communication point.
Layer 3 — Output agents: run only after orchestrator produces decision.

## When the agent struggles

Treat it as a harness signal. Identify what is missing —
tools, guardrails, documentation — and file a PR to fix it.
Never patch in a prompt. Fix the harness.
