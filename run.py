import sys, traceback

def excepthook(exctype, value, tb):
    msg = "".join(traceback.format_exception(exctype, value, tb))
    try:
        with open("crash_log.txt", "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass
    print("UNHANDLED EXCEPTION\n" + msg)
    sys.exit(1)

sys.excepthook = excepthook

from ui import main

if __name__ == "__main__":
    main()
