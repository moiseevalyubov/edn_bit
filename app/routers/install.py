import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Portal
from app.services.bitrix import activate_connector, bind_events, get_open_lines, register_connector

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/install")
@router.get("/install")
async def install(request: Request, db: Session = Depends(get_db)):
    # Bitrix24 sends tokens via form POST or GET params
    form = await request.form()
    params = dict(form) if form else {}
    if not params:
        params = dict(request.query_params)

    member_id = params.get("member_id") or params.get("MEMBER_ID")
    access_token = params.get("AUTH_ID")
    refresh_token_val = params.get("REFRESH_ID")
    auth_expires = int(params.get("AUTH_EXPIRES", 3600))
    client_endpoint = params.get("client_endpoint", "")
    app_token = params.get("APPLICATION_TOKEN")

    if not member_id or not access_token:
        logger.error("Install called without credentials: %s", params.keys())
        return HTMLResponse(
            "<h2>Ошибка установки: отсутствуют данные авторизации</h2>", status_code=400
        )

    portal = db.query(Portal).filter_by(member_id=member_id).first()

    if portal:
        portal.access_token = access_token
        portal.refresh_token = refresh_token_val or portal.refresh_token
        portal.token_expires_at = datetime.utcnow() + timedelta(seconds=auth_expires)
        portal.client_endpoint = client_endpoint or portal.client_endpoint
        portal.app_token = app_token or portal.app_token
        portal.uninstalled_at = None
    else:
        portal = Portal(
            member_id=member_id,
            client_endpoint=client_endpoint,
            access_token=access_token,
            refresh_token=refresh_token_val or "",
            token_expires_at=datetime.utcnow() + timedelta(seconds=auth_expires),
            app_token=app_token,
        )
        db.add(portal)

    db.commit()
    db.refresh(portal)

    try:
        register_connector(portal, db)
        bind_events(portal, db)
        lines = get_open_lines(portal, db)
        if lines:
            first_line = str(lines[0].get("ID", ""))
            if first_line:
                activate_connector(portal, db, first_line)
                portal.open_line_id = first_line
                db.commit()
    except Exception as e:
        logger.warning("Post-install setup error (non-critical): %s", e)

    return HTMLResponse("""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>MAX Bot установлен</title></head>
<body style="font-family:sans-serif;padding:40px;text-align:center;background:#f5f5f5">
  <div style="background:white;padding:40px;border-radius:12px;max-width:500px;margin:0 auto;box-shadow:0 2px 8px rgba(0,0,0,.1)">
    <h2 style="color:#005FF9">✓ MAX Bot успешно установлен</h2>
    <p>Теперь перейдите в настройки коннектора, чтобы подключить каналы MAX Bot.</p>
  </div>
  <script src="//api.bitrix24.com/api/v1/"></script>
  <script>BX24.init(function(){ BX24.installFinish(); });</script>
</body>
</html>
""")
