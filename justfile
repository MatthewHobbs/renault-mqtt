# renault-mqtt governance recipes (the cross-repo `just` convention).
#
# `just ci` mirrors .github/workflows/ci.yaml so failures surface before push.
# `just e2e` is separate and never part of `ci`: it needs Docker and takes minutes.

# Local CI gate - the same commands remote CI runs.
ci: lint test

lint:
    ruff check renault_mqtt tests

test:
    python3 -m pytest tests -q --cov=renault_mqtt --cov-report=term-missing --cov-fail-under=100

# End-to-end Home Assistant harness (Docker). See tests/e2e/README.md.
#   just e2e 2026.9.3
#   just e2e 2026.8.1 ../other-core-worktree --catalog /path/to/alpine_a290/app:catalog --out /tmp/r.json
e2e ha_version core=justfile_directory() *args:
    python3 tests/e2e/harness.py --ha-version {{ha_version}} --core {{core}} {{args}}
