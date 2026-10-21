PYTHON := python3

.PHONY: all data profile analyze test report reproduce clean

data:
	$(PYTHON) -m analysis.ingest

profile:
	$(PYTHON) -m analysis.health

analyze:
	$(PYTHON) -m analysis.metrics
	$(PYTHON) -m analysis.manifest

test:
	pytest tests/ -v

report:
	$(PYTHON) -m agent.weekly_report

reproduce: data profile analyze test

clean:
	rm -rf data/raw/*.zip data/raw/*.xlsx data/processed/*