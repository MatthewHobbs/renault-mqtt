# renault-mqtt governance recipes (the cross-repo `just` convention).
#
# `just ci` mirrors .github/workflows/ci.yaml so failures surface before push.
# Both gates are covered here - this repo has no container or e2e stage.

# Local CI gate - the same commands remote CI runs.
ci: lint test

lint:
    ruff check renault_mqtt tests

test:
    python3 -m pytest tests -q --cov=renault_mqtt --cov-report=term-missing --cov-fail-under=100
