# Gold standard: SQLAlchemy model update pattern
# File: app/routers/handler.py, app/routers/install.py

from datetime import datetime
from sqlalchemy.orm import Session
from app.models import Portal, Channel

# Simple field update + commit:
portal.uninstalled_at = datetime.utcnow()
db.commit()

# Loop update + counter:
deactivated = 0
for channel in portal.channels:
    if channel.is_active:
        channel.is_active = False
        channel.disconnected_at = datetime.utcnow()
        deactivated += 1
db.commit()

# Rules:
# - One db.commit() after all changes in the same operation
# - Use datetime.utcnow() for all timestamps
# - Session via Depends(get_db) — never create manually in handlers
# - No Alembic — schema auto-created via Base.metadata.create_all at startup
