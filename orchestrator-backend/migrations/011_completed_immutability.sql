-- Migration 011: enforce COMPLETED-step immutability at the DB layer (BE-C5).
--
-- Previously immutability was checked only in the API (circuit_breakers), so a
-- direct db.update_step_status could transition a COMPLETED step elsewhere. This
-- trigger makes the invariant unbypassable: a COMPLETED step can never move to a
-- different status. Non-status columns (summary, agent_output, failure_reason)
-- may still be written, since some flows legitimately annotate a completed step.

CREATE TRIGGER IF NOT EXISTS trg_steps_completed_immutable
BEFORE UPDATE OF status ON Steps
WHEN OLD.status = 'COMPLETED' AND NEW.status <> 'COMPLETED'
BEGIN
    SELECT RAISE(ABORT, 'COMPLETED step is immutable: cannot change status');
END;
