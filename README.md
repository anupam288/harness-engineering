# SDLC Harness

A generic, domain-agnostic Python scaffold for running AI agents across all
six SDLC phases using **harness engineering** principles from OpenAI's Codex
experiment.

Drop this into any new repo, configure `policies/`, and agents will drive
development with deterministic constraints enforcing quality at every gate.

---

## Core idea

> The engineer's job is no longer to write code. It is to design the
> environment in which agents write code reliably.

Three pillars:

| Pillar | What it means |
|--------|--------------|
| **Context engineering** | All knowledge lives as versioned files in the repo. Agents read docs/ and policies/ — never Google Docs, never Slack, never prompts. |
| **Architectural constraints** | Hard rules are enforced by deterministic linters before any LLM call. The LLM only handles judgment, not rule enforcement. |
| **Entropy management** | The GC agent runs nightly, reads all logs, and proposes PRs to fix drift. The harness improves itself. |

---

## Quickstart

```bash
# 1. Install
pip install -r requirements.txt

# 2. Check gate status (all phases start blocked — expected)
python cli.py gate --all

# 3. Configure your project
#    Edit harness_config.yaml, then fill in docs/requirements.md

# 4. Run requirements phase
python cli.py run requirements --input inputs/my_project.json

# 5. Check gate again — requirements gate should now be open
python cli.py gate --all

# 6. Run subsequent phases in order
python cli.py run design
python cli.py run testing
python cli.py run monitoring   # or schedule gc_agent nightly

# 7. Check overall status
python cli.py status
```

---

## Repository structure

```
sdlc-harness/
├── AGENTS.md                    ← Master map injected into every agent context
├── harness_config.yaml          ← Project configuration (edit this)
├── cli.py                       ← Single entrypoint for all phases
│
├── docs/                        ← All harness artifacts (versioned)
│   ├── requirements.md          ← Phase 1 output
│   ├── architecture.md          ← Phase 2 output
│   ├── uncertain_terms.md       ← Ambiguities flagged for human resolution
│   ├── quality.md               ← Per-agent confidence scores
│   └── edge_cases.md            ← QA findings (open items block deployment)
│
├── policies/                    ← Hard rules (read-only at runtime)
│   ├── policy.yaml              ← Domain rules (deterministic + LLM-based)
│   ├── conflict_policy.yaml     ← How orchestrator resolves disagreements
│   ├── rollback_triggers.yaml   ← Deployment rollback thresholds
│   └── agent_schema.json        ← Input/output schema for agents
│
├── harness/
│   ├── config.py                ← HarnessConfig (loads harness_config.yaml)
│   ├── gate.py                  ← PhaseGate (blocks phases until artifacts exist)
│   ├── agents/
│   │   ├── base_agent.py        ← BaseAgent (all agents inherit from this)
│   │   ├── requirements_agent.py
│   │   ├── architecture_agent.py
│   │   ├── qa_agent.py          ← QAAgent, ScenarioAgent, AdversarialAgent
│   │   └── gc_agent.py          ← Nightly garbage collection
│   ├── constraints/
│   │   └── validators.py        ← SchemaValidator, PolicyLinter, StructuralLinter
│   └── logs/
│       ├── decision_log.py      ← Append-only log of all agent outputs
│       └── conflict_log.py      ← ConflictLog + OverrideLog
│
├── tests/
│   ├── test_constraints.py      ← Tests for gates and validators
│   └── scenarios/
│       └── test_scenarios.yaml  ← Test cases (grown by ScenarioAgent)
│
└── .harness/
    ├── logs/                    ← decision_log.jsonl, conflict_log.jsonl, override_log.jsonl
    └── proposed_prs/            ← PRs proposed by GCAgent, awaiting human review
```

---

## The six phases and their gates

### Phase 1 — Requirements
**Agent:** `RequirementsAgent`
**Gate opens when:** `docs/requirements.md` is committed
**Produces:** `requirements.md`, `uncertain_terms.md`, `policies/agent_schema.json`

```bash
python cli.py run requirements --input inputs/project_spec.json
```

### Phase 2 — Design
**Agent:** `ArchitectureAgent`
**Gate opens when:** `uncertain_terms.md` has zero open `[ ]` items AND all policy files exist
**Produces:** `architecture.md`, `policy.yaml`, `conflict_policy.yaml`

```bash
# First: resolve all items in docs/uncertain_terms.md
# Change every "- [ ]" to "- [x]", commit, then:
python cli.py run design
```

### Phase 3 — Development
**Agents:** Your specialist agents (extend `BaseAgent`)
**Gate opens when:** All policy files exist + `architecture.md` committed
**Structural linter:** Enforced in CI — Layer 1 agents cannot import each other

### Phase 4 — Testing
**Agents:** `QAAgent`, `ScenarioAgent`, `AdversarialAgent`
**Gate opens when:** Requirements + architecture docs exist
**Deployment blocked until:** `edge_cases.md` has zero open `[ ]` items

```bash
python cli.py run testing
```

### Phase 5 — Deployment
**Gate opens when:** `edge_cases.md` = zero open items + `rollback_triggers.yaml` exists
**Rollback agent:** Monitors `rollback_triggers.yaml` thresholds in production

### Phase 6 — Monitoring
**Agent:** `GCAgent` (runs nightly)
**No gate** — continuous
**Produces:** PRs in `.harness/proposed_prs/` for human review

```bash
python cli.py gc
```

---

## Adapting to your domain

### 1. Add your domain rules to `policies/policy.yaml`

```yaml
rules:
  - rule_id: DOMAIN_001
    description: "Credit score must be at least 700"
    condition: "credit_score < 700"
    action: block
    deterministic: true
```

### 2. Add your input schema to `policies/agent_schema.json`

```json
{
  "required": ["applicant_id", "credit_score", "income"],
  "properties": {
    "credit_score": {"type": "number", "minimum": 300, "maximum": 900},
    "income": {"type": "number", "minimum": 0}
  }
}
```

### 3. Build your specialist agents by extending `BaseAgent`

```python
from harness.agents.base_agent import BaseAgent, AgentResult

class MyDomainAgent(BaseAgent):
    phase = "development"

    def run(self, input_data: dict) -> AgentResult:
        context = self.build_context(extra_docs=["requirements.md"])
        # ... call LLM with context ...
        return AgentResult(
            agent_name=self.name,
            phase=self.phase,
            status="pass",
            output={"result": "..."},
            confidence=0.9,
        )
```

### 4. Record conflicts and overrides

```python
from harness.logs.conflict_log import ConflictLog, OverrideLog

conflict_log = ConflictLog(config.logs_dir)
conflict_log.record(
    input_id="app_123",
    agent_a="BureauAgent", output_a={"risk": "low"},
    agent_b="FraudAgent",  output_b={"risk": "high"},
    resolution="conservative — took high risk",
)

override_log = OverrideLog(config.logs_dir)
override_log.record(
    input_id="app_123",
    agent_name="PolicyAgent",
    agent_decision={"approved": True},
    human_decision={"approved": False},
    reason="Income source unverifiable despite clean bureau score",
)
```

The GC agent will read these nightly and propose policy rules that encode
the human judgment automatically for next time.

---

## The harness rule

> **When an agent struggles, fix the harness — not the prompt.**

Every agent failure is a signal. Identify what is missing — a schema
constraint, a policy rule, a documentation gap — and commit a fix.
The agent will not make that mistake again.

---

## Running tests

```bash
pytest tests/ -v
```

---

## Environment variables

```bash
export ANTHROPIC_API_KEY=your_key_here
```
