"""Queue operation names shared by enqueue and worker code."""

SEND_TO_BITRIX = "send_to_bitrix"
SEND_TO_EDNA = "send_to_edna"
SEND_CRM_TO_EDNA = "send_crm_to_edna"
UPDATE_CRM_STATUS = "update_crm_status"
SYNC_CRM_PROVIDER = "sync_crm_provider"

CRM_TASK_TYPES = frozenset({SEND_CRM_TO_EDNA, UPDATE_CRM_STATUS, SYNC_CRM_PROVIDER})
