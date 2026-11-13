from .bootstrap import configure_tcl, configure_logging
configure_tcl()
configure_logging()
from .gui import main
main()
