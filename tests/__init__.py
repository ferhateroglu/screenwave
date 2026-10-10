import logging

# expected failures are logged by the app; keep test output readable
logging.getLogger("screenwave").addHandler(logging.NullHandler())
