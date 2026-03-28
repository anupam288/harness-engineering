# SDLC Harness

A generic, domain-agnostic Python scaffold for running AI agents across all
six SDLC phases using **harness engineering** principles from OpenAI's Codex
experiment.

Drop this into any new repo, configure `policies/` and `model_config.yaml`,
and agents will drive development with deterministic constraints enforcing
quality at every gate.

---

## Core idea

> The engineer's job is no longer to write code. It is to design the
> environment in which agents write code reliably.

Three pillars:

| Pillar | What it means |
|--------|--------------|
| **Context engineering** | All knowledge lives as versioned files in the repo. Agents read `docs/` and `policies/` — never Google Docs, never Slack, never inline prompts. |
| **Architectural constraints** | Hard rules are enforced by deterministic linters before any LLM call. The LLM only handles judgment, not rule enforcement. |
| **Entropy management** | The GC agent runs nightly, reads all logs, and proposes PRs to fix drift. The harness improves itself. |

---

## Quickstart

```bash
# 1. Install
pip install -r requirements.txt

# 2. Set your API key
export ANTHROPIC_API_KEY=your_key_here

# 3. Check gate status (all phases start blocked — expected)
python cli.py gate --all

# 4. Configure your project
#    Edit harness_config.yaml and model_config.yaml
#    Fill in docs/requirements.md or run the requirements agent:
python cli.py run requirements --input inputs/my_project.json

# 5. Resolve uncertain terms, then open the design gate
#    Edit docs/uncertain_terms.md — change "- [ ]" to "- [x]" for each item
python cli.py gate --all   # design gate should now be open

# 6. Run subsequent phases in order
python cli.py run design
python cli.py run testing
python cli.py run monitoring   # or schedule gc_agent nightly via cron

# 7. Check overall status at any time
python cli.py status
```

---

## Repository structure

```
sdlc-harness/
├── AGENTS.md                        ← Master map injected into every agent context
├── harness_config.yaml              ← Project configuration (edit this)
├── model_config.yaml                ← Per-agent model routing and fallback chains
├── cli.py                           ← Single entrypoint for all phases
│
├── docs/                            ← All harness artifacts (versioned)
│   ├── requirements.md              ← Phase 1 output
│   ├── architecture.md              ← Phase 2 output
│   ├── uncertain_terms.md           ← Ambiguities flagged for human resolution
│   ├── quality.md                   ← Per-agent confidence scores
│   └── edge_cases.md                ← QA findings (open items block deployment)
│
├── policies/                        ← Hard rules (read-only at runtime)
│   ├── policy.yaml                  ← Domain rules (deterministic + LLM-based)
│   ├── conflict_policy.yaml         ← How orchestrator resolves agent disagreements
│   └── rollback_triggers.yaml       ← Deployment rollback thresholds
│
├── prompts/                         ← Versioned prompt templates (one per agent)
│   ├── requirements_agent.md
│   ├── architecture_agent.md
│   ├── gc_agent.md
│   ├── orchestrator_agent.md
│   └── <your_agent>.md              ← Add one per specialist agent you build
│
├── harness/
│   ├── config.py                    ← HarnessConfig — loads harness_config.yaml
│   ├── gate.py                      ← PhaseGate — blocks phases until artifacts exist
│   │
│   ├── model/                       ← Model layer (provider-agnostic LLM interface)
│   │   ├── base_model.py            ← BaseModel abstract interface + ModelResponse
│   │   ├── anthropic_model.py       ← Anthropic Claude implementation
│   │   ├── openai_model.py          ← OpenAI GPT drop-in swap
│   │   ├── prompt_registry.py       ← Loads prompts/ with {{variable}} interpolation
│   │   └── __init__.py              ← build_model() factory
│   │
│   ├── agents/
│   │   ├── base_agent.py            ← BaseAgent — all agents inherit from this
│   │   ├── requirements_agent.py    ← Phase 1
│   │   ├── architecture_agent.py    ← Phase 2
│   │   ├── dev_agent.py             ← Phase 3: DevAgent base + OrchestratorAgent
│   │   ├── qa_agent.py              ← Phase 4: QAAgent, ScenarioAgent, AdversarialAgent
│   │   ├── release_agent.py         ← Phase 5: ReleaseAgent + RollbackAgent
│   │   └── gc_agent.py              ← Phase 6: nightly garbage collection
│   │
│   ├── constraints/
│   │   └── validators.py            ← SchemaValidator, PolicyLinter, StructuralLinter
│   │
│   └── logs/
│       ├── decision_log.py          ← Append-only log of all agent outputs
│       └── conflict_log.py          ← ConflictLog + OverrideLog
│
├── tests/
│   ├── test_constraints.py          ← 24 tests: gates, schema, policy, structural linter
│   ├── test_model_layer.py          ← 27 tests: model factory, prompt registry, retry/fallback
│   └── scenarios/
│       └── test_scenarios.yaml      ← Test cases (grown automatically by ScenarioAgent)
│
└── .harness/
    ├── logs/                        ← decision_log.jsonl, conflict_log.jsonl, override_log.jsonl
    └── proposed_prs/                ← PRs proposed by GCAgent, awaiting human review
```

---

## The model layer

Every agent uses a provider-agnostic model interface. No agent imports
Anthropic or OpenAI directly — all LLM calls go through `BaseModel`.

### Switching providers

To switch all agents to OpenAI, edit two lines in `model_config.yaml`:

```yaml
default:
  provider: openai
  model_id: gpt-4o
  max_tokens: 2048
```

No agent code changes required.

### Per-agent model routing

Different agents can use different models. Assign cheaper/faster models
to routine tasks, reserve capable models for complex reasoning:

```yaml
# model_config.yaml
agents:
  qa_agent:
    provider: anthropic
    model_id: claude-haiku-4-5-20251001   # fast, cheap — pattern matching
    max_tokens: 2048

  gc_agent:
    provider: anthropic
    model_id: claude-sonnet-4-20250514    # full model — synthesises many logs
    max_tokens: 4000
    fallback:
      provider: anthropic
      model_id: claude-haiku-4-5-20251001 # automatic fallback on failure
      max_tokens: 4000
```

### Adding a new provider

Create `harness/model/my_provider_model.py`, subclass `BaseModel`,
implement `call()` and `stream()`, then register it in `harness/model/__init__.py`:

```python
if provider == "myprovider":
    from harness.model.my_provider_model import MyProviderModel
    return MyProviderModel(model_id=model_id, ...)
```

### Versioned prompts

Prompts live in `prompts/` as markdown files versioned in the repo.
This means the GC agent can detect when a prompt change correlates
with a confidence drop by comparing versions in the decision log.

```
prompts/
├── my_agent.md          ← main prompt (supports {{variable}} interpolation)
└── my_agent.system.md   ← optional system prompt
```

In your agent:

```python
prompt = self._render_prompt({"domain": "lending", "project": "FinCap"})
system = self._render_system()
response = self._call_llm(prompt, system=system)
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
**Gate opens when:** `uncertain_terms.md` has zero open `[ ]` items AND policy files exist
**Produces:** `architecture.md`, `policy.yaml`, `conflict_policy.yaml`

```bash
# First: resolve all items in docs/uncertain_terms.md
# Change every "- [ ]" to "- [x]", commit, then:
python cli.py run design
```

### Phase 3 — Development
**Agents:** Your specialist agents (extend `DevAgent`) + `OrchestratorAgent`
**Gate opens when:** All policy files exist + `architecture.md` committed
**Structural linter:** Layer 1 agents cannot import each other — enforced in CI

### Phase 4 — Testing
**Agents:** `QAAgent`, `ScenarioAgent`, `AdversarialAgent`
**Gate opens when:** Requirements + architecture docs exist
**Deployment blocked until:** `edge_cases.md` has zero open `[ ]` items

```bash
python cli.py run testing
```

### Phase 5 — Deployment
**Agents:** `ReleaseAgent` (staged rollout) + `RollbackAgent` (threshold monitor)
**Gate opens when:** `edge_cases.md` zero open items + `rollback_triggers.yaml` exists
**Rollback:** Automatic when any metric in `rollback_triggers.yaml` is breached

### Phase 6 — Monitoring
**Agent:** `GCAgent` (runs nightly)
**No gate** — continuous
**Produces:** PRs in `.harness/proposed_prs/` for human review

```bash
python cli.py gc
```

---

## Adapting to your domain

### 1. Add domain rules to `policies/policy.yaml`

```yaml
rules:
  - rule_id: DOMAIN_001
    description: "Credit score must be at least 700"
    condition: "credit_score < 700"
    action: block
    deterministic: true   # enforced by PolicyLinter before any LLM call
```

### 2. Define your input schema in `policies/agent_schema.json`

```json
{
  "required": ["applicant_id", "credit_score", "income"],
  "properties": {
    "credit_score": {"type": "number", "minimum": 300, "maximum": 900},
    "income":       {"type": "number", "minimum": 0}
  }
}
```

### 3. Build specialist agents by extending `DevAgent`

```python
from harness.agents.dev_agent import DevAgent
from harness.agents.base_agent import AgentResult

class BureauAgent(DevAgent):
    phase = "development"

    def _run_domain_logic(self, input_data: dict, context: str) -> AgentResult:
        # Schema + policy linter already passed by the time this is called
        prompt = self._render_prompt({"applicant_id": input_data["applicant_id"]})
        response = self._call_llm(context + "\n\n" + prompt)
        return AgentResult(
            agent_name=self.name,
            phase=self.phase,
            status="pass",
            output={"bureau_score": 750, "risk": "low"},
            confidence=0.92,
        )
```

### 4. Add the agent's model config and prompt template

```yaml
# model_config.yaml
agents:
  bureau_agent:
    provider: anthropic
    model_id: claude-haiku-4-5-20251001
    max_tokens: 1024
```

```markdown
<!-- prompts/bureau_agent.md -->
You are the BureauAgent. Evaluate the credit bureau data for applicant {{applicant_id}}.
...
Return ONLY valid JSON.
```

### 5. Record conflicts and overrides for GC agent learning

```python
from harness.logs.conflict_log import ConflictLog, OverrideLog

# When two agents disagree
ConflictLog(config.logs_dir).record(
    input_id="app_123",
    agent_a="BureauAgent", output_a={"risk": "low"},
    agent_b="FraudAgent",  output_b={"risk": "high"},
    resolution="conservative — took high risk",
)

# When a human overrides an agent decision
OverrideLog(config.logs_dir).record(
    input_id="app_123",
    agent_name="PolicyAgent",
    agent_decision={"approved": True},
    human_decision={"approved": False},
    reason="Income source unverifiable despite clean bureau score",
)
```

The GC agent reads these nightly and proposes policy rules that would
have produced the correct decision automatically next time.

---

## Configuration reference

### `harness_config.yaml`

```yaml
llm_model: "claude-sonnet-4-20250514"  # default model (overridden by model_config.yaml)
llm_max_tokens: 2048
confidence_threshold: 0.75             # below this → needs_human
canary_quality_threshold: 0.85         # below this → rollback
phase_gates_strict: true               # false = warnings only, phases still run
gc_agent_schedule: "0 2 * * *"         # cron for nightly GC agent
```

### `model_config.yaml`

```yaml
default:                               # used when no agent-specific entry exists
  provider: anthropic                  # anthropic | openai
  model_id: claude-sonnet-4-20250514
  max_tokens: 2048
  temperature: 0.0
  fallback:                            # used automatically if primary fails
    provider: anthropic
    model_id: claude-haiku-4-5-20251001
    max_tokens: 2048

agents:
  my_agent:                            # overrides default for this agent
    provider: anthropic
    model_id: claude-haiku-4-5-20251001
    max_tokens: 1024
    temperature: 0.2
```

---

## The harness rule

> **When an agent struggles, fix the harness — not the prompt.**

Every agent failure is a signal. Identify what is missing — a schema
constraint, a policy rule, a documentation gap, a prompt template —
and commit a fix. The agent will not make that mistake again.

---

## Running tests

```bash
pytest tests/ -v
# 51 tests: 24 constraint/gate tests + 27 model layer tests
```

---

## Environment variables

```bash
export ANTHROPIC_API_KEY=your_key_here   # required for Anthropic models
export OPENAI_API_KEY=your_key_here      # required if using OpenAI models
```
