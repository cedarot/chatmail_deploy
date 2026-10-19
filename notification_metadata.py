#!/usr/bin/env python3
"""Run the installed Chatmail metadata service without replacing its modules."""
import logging
import os
import re
import sys


class PrivateFormatter(logging.Formatter):
    """Older upstream releases include opaque tokens in some error messages."""

    def format(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return f"{record.levelname}:metadata:unformattable event"
        for prefix in (
            "Notification request failed",
            "notification exceeded deadline",
            "lookup ignored",
            "unknown dictproxy request",
            "dictproxy-set failed",
            "removing spurious queue item",
            "failed to get TURN credentials",
        ):
            if message.startswith(prefix):
                return f"{record.levelname}:metadata:{prefix}"
        # Do not serialize arbitrary request data, exception strings or tracebacks.
        function = re.sub(r"[^A-Za-z0-9_]", "", record.funcName)
        return f"{record.levelname}:metadata:event in {function}"


def main():
    from chatmaild import metadata, notifier

    url = os.environ["CHATMAIL_NOTIFY_URL"]
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+/notify", url):
        raise RuntimeError("metadata adapter requires a loopback notification URL")
    if not callable(getattr(metadata, "main", None)) or not hasattr(notifier.Notifier, "URL"):
        raise RuntimeError("unsupported installed metadata interface")
    logging.basicConfig(level=logging.WARNING, force=True)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(PrivateFormatter())
    notifier.Notifier.URL = url
    return metadata.main()


if __name__ == "__main__":
    sys.exit(main())
