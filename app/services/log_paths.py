"""Keep request diagnostics while hiding webhook and signed-file credentials."""
import logging
import re


def public_path(value):
    return re.sub(r"/(incoming|status|file)/[^\s\"?]+(?:\?[^\s\"]*)?", r"/\1/[hidden]", value)


class PrivatePathFilter(logging.Filter):
    def filter(self, record):
        record.msg = public_path(record.getMessage())
        record.args = ()
        return True
