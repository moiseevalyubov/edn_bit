# Convention: logging style

Use `%s` formatting in all logger calls — never f-strings:

✅ logger.info("Portal %s deactivated, %d channels", portal.member_id, count)
❌ logger.info(f"Portal {portal.member_id} deactivated")

Exception messages (RuntimeError etc.) may use f-strings — they are not logger calls:
✅ raise RuntimeError(f"Portal {portal.member_id} has been uninstalled")

Log levels:
- logger.info — normal flow events
- logger.warning — unexpected but recoverable (unknown portal, token mismatch)
- logger.error — failures that affect functionality
- logger.exception — inside except blocks (includes traceback automatically)
