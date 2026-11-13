from manufacturer_finder.bootstrap import configure_tcl, configure_logging

configure_tcl()
configure_logging()

if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        from manufacturer_finder.selftest import run
        run()
    else:
        from manufacturer_finder.gui import main
        main()
