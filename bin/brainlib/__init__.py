# bin/brainlib/__init__.py
"""Per-concern modules for the brain toolbelt.

bin/brain stays the single entry point and the place behavior is documented.
This package exists for the parts that MUST vary by machine — OS backends,
the interactive picker, the setup phases — because keeping them inline is what
made the toolbelt macOS-only and untestable without a real terminal.

Importable because Python puts the running script's directory (bin/) at
sys.path[0], so `import brainlib.osbackend` resolves for `python3 bin/brain`,
for `bin/brain-mcp`, and for the git hooks, all without a package install.
"""
