CREATE TABLE external_workflow_approval_preferences (
    user_id VARCHAR(255) NOT NULL,
    workflow_id VARCHAR(64) NOT NULL,
    step_id VARCHAR(64) NOT NULL,
    approval_required BOOLEAN NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (user_id, workflow_id, step_id)
);
