BEGIN;

CREATE TABLE tenants (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- References identify recoverable secrets in a future secure secret store.
-- Do not put raw webhook secrets or Slack webhook URLs in these columns.
CREATE TABLE sources (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id bigint NOT NULL REFERENCES tenants (id),
    name text NOT NULL,
    webhook_secret_ref text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id)
);

CREATE TABLE destinations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id bigint NOT NULL REFERENCES tenants (id),
    name text NOT NULL,
    webhook_url_ref text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id)
);

CREATE TABLE rules (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id bigint NOT NULL REFERENCES tenants (id),
    source_id bigint NOT NULL,
    destination_id bigint NOT NULL,
    event_type text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, source_id) REFERENCES sources (tenant_id, id),
    FOREIGN KEY (tenant_id, destination_id) REFERENCES destinations (tenant_id, id),
    UNIQUE (source_id, destination_id, event_type),
    UNIQUE (tenant_id, source_id, destination_id, id)
);

CREATE TABLE events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id bigint NOT NULL REFERENCES tenants (id),
    source_id bigint NOT NULL,
    github_delivery_id uuid NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, source_id) REFERENCES sources (tenant_id, id),
    UNIQUE (source_id, github_delivery_id),
    UNIQUE (tenant_id, source_id, id)
);

CREATE TABLE deliveries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id bigint NOT NULL REFERENCES tenants (id),
    source_id bigint NOT NULL,
    event_id bigint NOT NULL,
    destination_id bigint NOT NULL,
    rule_id bigint NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'succeeded', 'failed')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    lease_expires_at timestamptz,
    completed_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, source_id, event_id)
        REFERENCES events (tenant_id, source_id, id),
    FOREIGN KEY (tenant_id, destination_id)
        REFERENCES destinations (tenant_id, id),
    FOREIGN KEY (tenant_id, source_id, destination_id, rule_id)
        REFERENCES rules (tenant_id, source_id, destination_id, id),
    UNIQUE (event_id, destination_id),
    UNIQUE (tenant_id, event_id, id)
);

-- One queue publication job per delivery; the queue message contains delivery_id.
CREATE TABLE outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id bigint NOT NULL REFERENCES tenants (id),
    event_id bigint NOT NULL,
    delivery_id bigint NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'published')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- This also enforces the event relationship through the delivery's FK.
    FOREIGN KEY (tenant_id, event_id, delivery_id)
        REFERENCES deliveries (tenant_id, event_id, id)
);

-- Unique constraints already index source/event deduplication and delivery lookup.
CREATE INDEX events_tenant_created_at_idx ON events (tenant_id, created_at, id);
CREATE INDEX rules_tenant_source_idx ON rules (tenant_id, source_id);
CREATE INDEX outbox_pending_idx ON outbox (next_attempt_at, id)
    WHERE status = 'pending';
CREATE INDEX deliveries_expired_lease_idx ON deliveries (lease_expires_at, id)
    WHERE status = 'processing';

COMMIT;
