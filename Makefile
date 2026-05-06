.PHONY: doc tests

clean:
	@rm -rf build dist .ruff_cache
	@rm -rf src/*.egg-info
	@find src \( -name '*.py[co]' -o -name '__pycache__' \) -delete
	@rm -rf doc/_build/*
	@rm -f src/nagare/static/livereload.js*

upgrade-precommit:
	python -m pre_commit autoupdate

install: clean
	npm i
	python -m pip install -e '.[dev']
	git init
	python -m pre_commit install
	$(MAKE) upgrade-precommit

webassets:
	python src/nagare/custom_build/build_assets.py

tests:
	python -m pytest

qa:
	python -m ruff check src
	python -m ruff format --check src

qa-fix:
	python -m ruff check --fix src
	python -m ruff format src

doc:
	python -m sphinx.cmd.build -b html doc doc/_build

wheel:
	$(MAKE) webassets
	python -m pip wheel -w dist --no-deps .
