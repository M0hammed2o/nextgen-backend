"""Run run.py with webbrowser.open disabled, so nothing opens in the default browser."""
import runpy, sys, webbrowser
webbrowser.open = lambda *a, **k: True
webbrowser.open_new = webbrowser.open_new_tab = webbrowser.open
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
