.PHONY: help test lint gates status gc monitor dashboard metrics pipeline clean

help:
	@echo "SDLC Harness — available commands"
	@echo ""
	@echo "  make test          Run full test suite (182 tests)"
	@echo "  make lint          Run structural linter + policy validator"
	@echo "  make gates         Check all phase gates"
	@echo "  make status        Show harness health status"
	@echo "  make dashboard     Open observability dashboard"
	@echo "  make metrics       Print metrics summary table"
	@echo "  make gc            Run GC agent (nightly harness improvement)"
	@echo "  make monitor       Run log monitor (one-shot triggered analysis)"
	@echo "  make monitor-poll  Run log monitor in continuous polling mode"
	@echo "  make pipeline      Run full pipeline (all phases, with resume)"
	@echo "  make clean         Clear checkpoints and proposed PRs"
	@echo ""

test:
	pytest tests/ -v --tb=short

lint:
	@python - <<'EOF'
	from harness.config import HarnessConfig
	from harness.constraints.validators import StructuralLinter, PolicyLinter
	from pathlib import Path
	import sys

	config = HarnessConfig.from_repo(".")

	print("Running structural linter...")
	linter = StructuralLinter(config.repo_root / "harness" / "agents")
	result = linter.lint()
	print(result.report())

	policy_path = Path("policies/policy.yaml")
	if policy_path.exists():
	    print("\nValidating policy.yaml...")
	    # Just check it loads — full validation in CI
	    import yaml
	    policy = yaml.safe_load(policy_path.read_text()) or {}
	    print(f"  ✓ {len(policy.get('rules', []))} rules loaded")

	if not result.passed:
	    sys.exit(1)
	EOF

gates:
	python cli.py gate --all

status:
	python cli.py status

dashboard:
	python cli.py dashboard

metrics:
	python cli.py metrics

gc:
	python cli.py gc

monitor:
	python cli.py monitor

monitor-poll:
	python cli.py monitor --poll

pipeline:
	python cli.py run requirements && \
	python cli.py run design && \
	python cli.py run testing && \
	python cli.py run monitoring

clean:
	rm -f .harness/checkpoints/*.json
	rm -f .harness/proposed_prs/MON_*.md
	@echo "Checkpoints and monitoring PRs cleared."
