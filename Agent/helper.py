import sys
import threading
import typing


def exit_with_error(message: str) -> typing.Never:
    sys.stderr.write(f"{message}\n")
    exit(1)


def start_thread(func: typing.Callable, args: tuple = None):
    if args: threading.Thread(target=func, args=args, daemon=True).start()
    else: threading.Thread(target=func, daemon=True).start()
