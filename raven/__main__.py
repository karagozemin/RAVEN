"""Allow `python -m raven` as an entry-point."""
from raven.main import main
import sys

sys.exit(main())
