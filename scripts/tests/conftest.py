"""Shared pytest config for healthmonitor tests."""
import sys
from pathlib import Path

# Add docker/scripts/ to sys.path so tests can `import healthmonitor`
sys.path.insert(0, str(Path(__file__).parent.parent))
