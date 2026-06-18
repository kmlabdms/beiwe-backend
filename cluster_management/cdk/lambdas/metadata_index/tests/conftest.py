"""Put the Lambda package dir on sys.path so tests import modules flatly
(`import parser`, `import handler`) exactly as they resolve in the Lambda runtime,
where every module is a sibling of handler.py.
"""
import pathlib
import sys

_PKG_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))
