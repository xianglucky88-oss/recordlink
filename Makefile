PY=$(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
run:
	PYTHONPATH=src $(PY) -m recordlink.cli
test:
	PYTHONPATH=src $(PY) -m pytest -q
clean:
	rm -rf .pytest_cache **/__pycache__ *.db
