import logging

# Keep the script's log lines out of test output.
logging.getLogger("rnd").addHandler(logging.NullHandler())
