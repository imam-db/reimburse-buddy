-- Employee Q&A (version B, EMPLOYEE_QA flag): qa_request, qa_answer, qa_refusal. Run as the OWNER role.
ALTER TABLE audit_events DROP CONSTRAINT IF EXISTS audit_events_event_type_check;
ALTER TABLE audit_events ADD CONSTRAINT audit_events_event_type_check CHECK (event_type IN ('request', 'plan',
    'tool_call', 'staged_action', 'approval', 'rejection', 'execution', 'clarification', 'limit', 'refusal',
    'access_denied', 'injection_notice', 'injection_detected', 'notice_shown', 'expired', 'error', 'superseded',
    'qa_request', 'qa_answer', 'qa_refusal'));
