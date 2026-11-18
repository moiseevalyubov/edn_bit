"""Keep request diagnostics while hiding webhook and signed-file credentials."""
import logging
import re


def public_path(value):
    return re.sub(r"/(incoming|status|file)/[^\s\"?]+(?:\?[^\s\"]*)?", r"/\1/[hidden]", value)


class PrivatePathFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn's AccessFormatter unpacks the original five arguments.
        # Mask strings without flattening the record into a formatted message.
        if isinstance(record.msg, str):
            record.msg = public_path(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(public_path(value) if isinstance(value, str) else value
                                for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: public_path(value) if isinstance(value, str) else value
                           for key, value in record.args.items()}
        return True
