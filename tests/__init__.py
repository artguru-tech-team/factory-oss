import os

# The suite never reads this machine's real agent data: every source points at a path that does not
# exist unless a test sets its own fixture (mock.patch.dict(os.environ, ...)). Without this, a test
# that runs the full build scanned ~/.claude/projects, the T3 DB and 1 GB of Antigravity stores.
_NOWHERE = "/nonexistent/factory-test"
_VARS = ("FACTORY_T3_DB", "FACTORY_USAGE_CACHE", "FACTORY_OPENCODE_DB", "FACTORY_ANTIGRAVITY_DIR",
         "FACTORY_CLAUDE_PROJECTS", "FACTORY_CLAUDE_SKILLS",
         "FACTORY_AGENTS_SKILLS", "FACTORY_PLUGINS_FILE")
for _var in _VARS:
    os.environ.setdefault(_var, f"{_NOWHERE}/{_var.lower()}")


def datasource_env(base_dir, **overrides):
    """Return dictionary of 8 data-source environment variables pointing inside base_dir."""
    env = {
        "FACTORY_T3_DB": os.path.join(base_dir, "statev2.sqlite"),
        "FACTORY_USAGE_CACHE": os.path.join(base_dir, "usage-scan-cache.json"),
        "FACTORY_OPENCODE_DB": os.path.join(base_dir, "opencode.db"),
        "FACTORY_ANTIGRAVITY_DIR": os.path.join(base_dir, "antigravity"),
        "FACTORY_CLAUDE_PROJECTS": os.path.join(base_dir, "projects"),
        "FACTORY_CLAUDE_SKILLS": os.path.join(base_dir, "skills"),
        "FACTORY_AGENTS_SKILLS": os.path.join(base_dir, "agents-skills"),
        "FACTORY_PLUGINS_FILE": os.path.join(base_dir, "plugins.json"),
    }
    env.update(overrides)
    return env

