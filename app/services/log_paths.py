"""Request diagnostics: webhook paths visible at the owner's explicit request."""
import json
import logging
import re


def public_path(value):
    # Restore incoming/status URLs as requested for live debugging. Signed file
    # links still contain independent download credentials and remain masked.
    return re.sub(r"/(file)/[^\s\"?]+(?:\?[^\s\"]*)?", r"/\1/[hidden]", value)


_CREDENTIAL_KEYS = frozenset({"auth", "authorization", "apikey", "xapikey", "accesstoken", "refreshtoken",
                             "clientsecret", "applicationtoken", "apptoken", "secretkey", "webhooktoken",
                             "password", "secret", "token"})


def payload_for_log(data):
    """Keep payload structure/content; omit credentials and avoid multiline logs."""
    def clean(value, depth=0):
        if depth > 20:
            return "[truncated]"
        if isinstance(value, dict):
            return {key: "[redacted]" if re.sub(r"[-_]", "", key).lower() in _CREDENTIAL_KEYS
                    else clean(item, depth + 1) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item, depth + 1) for item in value]
        if isinstance(value, str):
            value = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[redacted]@", value)
            return re.sub(r"(?i)([?&](?:auth|authorization|token|access_token|refresh_token|api_key|apikey|x-api-key|client_secret|application_token|webhook_token|secret_key|password|key|secret)=)[^&#\s]+",
                          r"\1[redacted]", value)
        return value
    serialized = json.dumps(clean(data), ensure_ascii=False, separators=(",", ":"))
    return serialized if len(serialized) <= 65536 else serialized[:65536] + " [truncated]"


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
