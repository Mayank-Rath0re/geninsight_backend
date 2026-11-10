-- init.sql

IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = 'genbi_db')
BEGIN
    CREATE DATABASE genbi_db;
END
GO

USE genbi_db;
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='auth' AND xtype='U')
BEGIN
    CREATE TABLE auth (
        id                        INT IDENTITY(1,1) PRIMARY KEY,
        email                     VARCHAR(255) NOT NULL,
        name                      VARCHAR(100) NOT NULL,
        password_hash             VARCHAR(255) NOT NULL,
        is_verified               BIT NOT NULL DEFAULT 0,
        refresh_token             VARCHAR(512) NULL,
        refresh_token_expires_at  DATETIME2 NULL,
        is_deleted                BIT NOT NULL DEFAULT 0,
        deleted_at                DATETIME2 NULL,
        created_at                DATETIME2 NOT NULL DEFAULT SYSDATETIME()
    );
    CREATE UNIQUE INDEX uq_auth_email ON auth(email);
END
GO

-- Custom (client-owned) database connections.
-- API field -> column mapping (the public payload keeps the shorter names):
--   userId      -> user_id            (owner)
--   dbname      -> database_name
--   dburl       -> host
--   dbport      -> port
--   dbuser      -> username
--   dbpassword  -> secret_encrypted   (Fernet ciphertext, never stored in plain text)
--   dbtype      -> engine             (mysql | mssql | postgres)
-- Tables of a custom DB are registered in table_info with data_source_id set; no
-- physical table is ever created in genbi_db for them.
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='data_sources' AND xtype='U')
BEGIN
    CREATE TABLE data_sources (
        id                INT IDENTITY(1,1) PRIMARY KEY,
        user_id           INT NOT NULL,
        name              VARCHAR(150) NOT NULL,
        engine            VARCHAR(30) NOT NULL
            CONSTRAINT chk_ds_engine CHECK (engine IN ('postgres', 'mysql', 'mssql', 'sqlite', 'oracle')),
        host              VARCHAR(255) NULL,
        port              INT NULL,
        database_name     VARCHAR(255) NULL,
        username          VARCHAR(255) NULL,
        secret_encrypted  NVARCHAR(MAX) NOT NULL,
        ssl_enabled       BIT NOT NULL
            CONSTRAINT df_ds_ssl DEFAULT 0,
        status            VARCHAR(20) NOT NULL DEFAULT 'active'
            CONSTRAINT chk_ds_status CHECK (status IN ('active', 'unreachable', 'revoked')),
        last_connected_at DATETIME2 NULL,
        last_error        NVARCHAR(4000) NULL,
        is_deleted        BIT NOT NULL DEFAULT 0,
        deleted_at        DATETIME2 NULL,
        created_at        DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
        updated_at        DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT fk_ds_user FOREIGN KEY (user_id)
            REFERENCES auth(id) ON DELETE CASCADE
    );
    CREATE INDEX ix_data_sources_user ON data_sources(user_id);
END
GO

-- Migrations for databases created before the columns above existed.
IF COL_LENGTH('data_sources', 'ssl_enabled') IS NULL
    ALTER TABLE data_sources ADD ssl_enabled BIT NOT NULL CONSTRAINT df_ds_ssl DEFAULT 0;
GO
IF COL_LENGTH('data_sources', 'last_error') IS NULL
    ALTER TABLE data_sources ADD last_error NVARCHAR(4000) NULL;
GO
IF COL_LENGTH('data_sources', 'updated_at') IS NULL
    ALTER TABLE data_sources ADD updated_at DATETIME2 NOT NULL CONSTRAINT df_ds_updated_at DEFAULT SYSDATETIME();
GO
IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'ix_data_sources_user_active')
    CREATE INDEX ix_data_sources_user_active ON data_sources(user_id, is_deleted);
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='table_info' AND xtype='U')
BEGIN
    CREATE TABLE table_info (
        id             INT IDENTITY(1,1) PRIMARY KEY,
        name           VARCHAR(255) NOT NULL,
        user_id        INT NULL,
        data_source_id INT NULL,
        source_table   VARCHAR(255) NULL,
        source_schema  VARCHAR(128) NULL,  -- default schema of a custom DB table (NULL for internal tables)
        knowledgebase  NVARCHAR(MAX) NULL,
        metadata       NVARCHAR(MAX) NULL,
        createdAt      DATETIME NULL,
        is_deleted     BIT NOT NULL DEFAULT 0,
        deleted_at     DATETIME2 NULL,
        purged_at      DATETIME2 NULL,   -- set by services/retention.py once the physical table is dropped
        CONSTRAINT fk_tableinfo_user FOREIGN KEY (user_id)
            REFERENCES auth(id) ON DELETE CASCADE,
        CONSTRAINT fk_tableinfo_datasource FOREIGN KEY (data_source_id)
            REFERENCES data_sources(id) ON DELETE NO ACTION
    );
    CREATE INDEX ix_table_info_datasource ON table_info(data_source_id);
    CREATE INDEX ix_table_info_user_created ON table_info(user_id, is_deleted, createdAt);
END
GO

IF COL_LENGTH('table_info', 'source_table') IS NULL
    ALTER TABLE table_info ADD source_table VARCHAR(255) NULL;
GO
IF COL_LENGTH('table_info', 'source_schema') IS NULL
    ALTER TABLE table_info ADD source_schema VARCHAR(128) NULL;
GO
-- Custom-DB table names can be up to 128 chars (SQL Server) / 64 (MySQL, Postgres).
IF EXISTS (SELECT 1 FROM sys.columns
           WHERE object_id = OBJECT_ID('table_info') AND name = 'name' AND max_length < 255)
    ALTER TABLE table_info ALTER COLUMN name VARCHAR(255) NOT NULL;
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='sessions' AND xtype='U')
BEGIN
    CREATE TABLE sessions (
        session_id    INT IDENTITY(1,1) PRIMARY KEY,
        session_title NVARCHAR(255),
        user_id       INT NULL,
        table_id      INT NOT NULL,
        date_created  DATETIME2 DEFAULT SYSDATETIME(),
        date_modified DATETIME2 DEFAULT SYSDATETIME(),
        is_deleted    BIT NOT NULL DEFAULT 0,
        deleted_at    DATETIME2 NULL,
        CONSTRAINT fk_sessions_user FOREIGN KEY (user_id)
            REFERENCES auth(id) ON DELETE CASCADE,
        CONSTRAINT fk_sessions_table FOREIGN KEY (table_id)
            REFERENCES table_info(id) ON DELETE NO ACTION
    );
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='queries' AND xtype='U')
BEGIN
    CREATE TABLE queries (
        id                 INT IDENTITY(1,1) PRIMARY KEY,
        session_id         INT NOT NULL,
        prompt             NVARCHAR(MAX) NOT NULL,
        sql_query          NVARCHAR(MAX) NOT NULL,
        summary            NVARCHAR(MAX) NOT NULL,
        updated_columns    NVARCHAR(MAX) NOT NULL,
        step_type          VARCHAR(20) NOT NULL
            CONSTRAINT df_queries_step_type DEFAULT 'transform'
            CONSTRAINT chk_queries_step_type CHECK (step_type IN ('transform', 'join')),
        source_session_id  INT NULL,
        join_type          VARCHAR(10) NULL
            CONSTRAINT chk_queries_join_type CHECK (join_type IN ('INNER', 'LEFT', 'RIGHT', 'FULL')),
        join_summary       NVARCHAR(MAX) NULL,
        is_deleted         BIT NOT NULL DEFAULT 0,
        deleted_at         DATETIME2 NULL,
        created_at         DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT fk_queries_session FOREIGN KEY (session_id)
            REFERENCES sessions(session_id) ON DELETE CASCADE,
        CONSTRAINT fk_queries_source_session FOREIGN KEY (source_session_id)
            REFERENCES sessions(session_id) ON DELETE NO ACTION,
        CONSTRAINT chk_queries_join_fields CHECK (
            (step_type = 'join' AND source_session_id IS NOT NULL AND join_type IS NOT NULL)
            OR
            (step_type = 'transform' AND source_session_id IS NULL AND join_type IS NULL)
        )
    );
    CREATE INDEX ix_queries_session ON queries(session_id);
    CREATE INDEX ix_queries_source_session ON queries(source_session_id);
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='charts' AND xtype='U')
BEGIN
    CREATE TABLE charts (
        chart_id   INT IDENTITY(1,1) PRIMARY KEY,
        session_id INT NOT NULL,
        chart_type VARCHAR(50)   NOT NULL,
        prompt     NVARCHAR(MAX) NOT NULL,
        sql_query  NVARCHAR(MAX) NOT NULL,
        metadata   NVARCHAR(MAX) NULL,
        is_deleted BIT NOT NULL DEFAULT 0,
        deleted_at DATETIME2 NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT fk_charts_session FOREIGN KEY (session_id)
            REFERENCES sessions(session_id) ON DELETE CASCADE
    );
    CREATE INDEX ix_charts_session ON charts(session_id);
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='dashboards' AND xtype='U')
BEGIN
    CREATE TABLE dashboards (
        dashboard_id     INT IDENTITY(1,1) PRIMARY KEY,
        user_id          INT NULL,
        table_id         INT NOT NULL,
        prompt           NVARCHAR(MAX) NULL,
        dashboard_intent NVARCHAR(MAX) NULL,
        user_response    NVARCHAR(MAX) NULL,
        status           VARCHAR(10) NOT NULL DEFAULT 'Inactive'
            CONSTRAINT chk_dashboard_status CHECK (status IN ('Live', 'Inactive')),
        breath_time      INT         NOT NULL DEFAULT 5,
        is_shared        BIT NOT NULL DEFAULT 0,
        share_token      VARCHAR(64) NULL,
        shared_snapshot  NVARCHAR(MAX) NULL,
        shared_at        DATETIME2 NULL,
        is_deleted       BIT NOT NULL DEFAULT 0,
        deleted_at       DATETIME2 NULL,
        created_at       DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
        updated_at       DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT fk_dashboards_user FOREIGN KEY (user_id)
            REFERENCES auth(id) ON DELETE CASCADE,
        CONSTRAINT fk_dashboards_table FOREIGN KEY (table_id)
            REFERENCES table_info(id) ON DELETE NO ACTION
    );
    CREATE INDEX ix_dashboards_user_table ON dashboards(user_id, table_id);
    CREATE UNIQUE INDEX uq_dashboards_share_token ON dashboards(share_token)
        WHERE share_token IS NOT NULL;
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='dashboard_charts' AND xtype='U')
BEGIN
    CREATE TABLE dashboard_charts (
        dashboard_id INT NOT NULL,
        chart_id     INT NOT NULL,
        CONSTRAINT pk_dashboard_charts PRIMARY KEY (dashboard_id, chart_id),
        CONSTRAINT fk_dc_dashboard FOREIGN KEY (dashboard_id)
            REFERENCES dashboards(dashboard_id) ON DELETE CASCADE,
        CONSTRAINT fk_dc_chart FOREIGN KEY (chart_id)
            REFERENCES charts(chart_id) ON DELETE CASCADE
    );
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='password_reset_tokens' AND xtype='U')
BEGIN
    CREATE TABLE password_reset_tokens (
        id         INT IDENTITY(1,1) PRIMARY KEY,
        user_id    INT NOT NULL,
        token      VARCHAR(255) NOT NULL,
        expires_at DATETIME2 NOT NULL,
        used       BIT NOT NULL DEFAULT 0,
        created_at DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT fk_prt_user FOREIGN KEY (user_id)
            REFERENCES auth(id) ON DELETE CASCADE
    );
    CREATE UNIQUE INDEX uq_password_reset_tokens_token ON password_reset_tokens(token);
    CREATE INDEX ix_password_reset_tokens_user ON password_reset_tokens(user_id);
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='OTP' AND xtype='U')
BEGIN
    CREATE TABLE OTP (
        id           INT IDENTITY(1,1) PRIMARY KEY,
        email_id     VARCHAR(255) NOT NULL,
        otp_no       VARCHAR(10) NOT NULL,
        usageCode    VARCHAR(30) NOT NULL,
        insertedTime DATETIME2 NOT NULL DEFAULT SYSDATETIME()
    );
    CREATE INDEX ix_otp_email_usage ON OTP(email_id, usageCode);
    CREATE INDEX ix_otp_insertedTime ON OTP(insertedTime);
END
GO

-- ═══════════════════════════ Billing, usage & organizations ═══════════════════════════


IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='plans' AND xtype='U')
BEGIN
    CREATE TABLE plans (
        id                    INT IDENTITY(1,1) PRIMARY KEY,
        code                  VARCHAR(30)   NOT NULL,           -- free | individual | maxx | enterprise
        name                  VARCHAR(100)  NOT NULL,
        price                 DECIMAL(10,2) NOT NULL,           -- DISPLAY price only; Razorpay plan holds the real amount
        currency              VARCHAR(10)   NOT NULL DEFAULT 'USD',
        per_seat              BIT           NOT NULL DEFAULT 0,
        min_seats             INT           NOT NULL DEFAULT 1,
        max_files             INT           NOT NULL,
        daily_token_limit     BIGINT        NULL,               -- NULL = unlimited
        session_token_limit   BIGINT        NULL,               -- NULL = unlimited
        monthly_query_limit   INT           NULL,               -- Free tier only
        retention_days        INT           NULL,               -- NULL = forever
        max_data_sources      INT           NULL,               -- 0 = none, NULL = unlimited
        features              NVARCHAR(MAX) NULL,               -- JSON feature flags
        is_active             BIT           NOT NULL DEFAULT 1,
        created_at            DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME()
    );
    CREATE UNIQUE INDEX uq_plans_code ON plans(code);
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='organizations' AND xtype='U')
BEGIN
    CREATE TABLE organizations (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        name          NVARCHAR(150) NOT NULL,
        owner_user_id INT NOT NULL,
        created_at    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT fk_org_owner FOREIGN KEY (owner_user_id) REFERENCES auth(id) ON DELETE NO ACTION
    );
    CREATE INDEX ix_org_owner ON organizations(owner_user_id);
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='org_members' AND xtype='U')
BEGIN
    CREATE TABLE org_members (
        org_id        INT NOT NULL,
        user_id       INT NOT NULL,
        role          VARCHAR(10) NOT NULL DEFAULT 'member'
            CONSTRAINT chk_orgm_role CHECK (role IN ('owner', 'member')),
        status        VARCHAR(10) NOT NULL DEFAULT 'active'
            CONSTRAINT chk_orgm_status CHECK (status IN ('invited', 'active', 'removed')),
        invited_email VARCHAR(255) NULL,
        created_at    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT pk_org_members PRIMARY KEY (org_id, user_id),
        CONSTRAINT fk_orgm_org  FOREIGN KEY (org_id)  REFERENCES organizations(id) ON DELETE CASCADE,
        CONSTRAINT fk_orgm_user FOREIGN KEY (user_id) REFERENCES auth(id) ON DELETE CASCADE
    );
    CREATE INDEX ix_orgm_user ON org_members(user_id, status);
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='subscriptions' AND xtype='U')
BEGIN
    CREATE TABLE subscriptions (
        id                       INT IDENTITY(1,1) PRIMARY KEY,
        owner_user_id            INT NOT NULL,                  -- the payer
        org_id                   INT NULL,                      -- set for Enterprise (members share the plan)
        plan_id                  INT NOT NULL,
        seats                    INT NOT NULL DEFAULT 1,
        status                   VARCHAR(20) NOT NULL DEFAULT 'created'
            CONSTRAINT chk_sub_status CHECK (status IN
                ('created','authenticated','active','pending','halted',
                 'cancelled','completed','expired','paused')),
        razorpay_subscription_id VARCHAR(100) NOT NULL,
        razorpay_customer_id     VARCHAR(100) NULL,
        current_period_start     DATETIME2 NULL,
        current_period_end       DATETIME2 NULL,
        cancel_at_period_end     BIT NOT NULL DEFAULT 0,
        cancelled_at             DATETIME2 NULL,
        is_test_grant            BIT NOT NULL DEFAULT 0,        -- 1 = free Maxx granted during testing phase
        created_at               DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at               DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT fk_sub_owner FOREIGN KEY (owner_user_id) REFERENCES auth(id) ON DELETE NO ACTION,
        CONSTRAINT fk_sub_org   FOREIGN KEY (org_id)  REFERENCES organizations(id) ON DELETE NO ACTION,
        CONSTRAINT fk_sub_plan  FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE NO ACTION
    );
    CREATE INDEX ix_sub_owner  ON subscriptions(owner_user_id, status);
    CREATE INDEX ix_sub_org    ON subscriptions(org_id, status);
    CREATE UNIQUE INDEX uq_sub_razorpay ON subscriptions(razorpay_subscription_id);
END
GO

-- For databases created before is_test_grant existed (the CREATE TABLE above is skipped there).
IF COL_LENGTH('subscriptions', 'is_test_grant') IS NULL
    ALTER TABLE subscriptions ADD is_test_grant BIT NOT NULL DEFAULT 0;
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='payments' AND xtype='U')
BEGIN
    CREATE TABLE payments (
        id                   INT IDENTITY(1,1) PRIMARY KEY,
        user_id              INT NOT NULL,
        subscription_id      INT NULL,
        razorpay_payment_id  VARCHAR(100) NOT NULL,
        razorpay_order_id    VARCHAR(100) NULL,
        amount               DECIMAL(12,2) NOT NULL,
        currency             VARCHAR(10) NOT NULL,
        status               VARCHAR(20) NOT NULL
            CONSTRAINT chk_payment_status CHECK (status IN ('authorized','captured','failed','refunded')),
        method               VARCHAR(30) NULL,
        created_at           DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT fk_pay_user FOREIGN KEY (user_id) REFERENCES auth(id) ON DELETE NO ACTION,
        CONSTRAINT fk_pay_sub  FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE NO ACTION
    );
    CREATE UNIQUE INDEX uq_pay_rzp ON payments(razorpay_payment_id);
    CREATE INDEX ix_pay_user ON payments(user_id);
END
GO

-- Idempotency log for Razorpay webhooks (Razorpay retries deliveries).
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='webhook_events' AND xtype='U')
BEGIN
    CREATE TABLE webhook_events (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        event_id    VARCHAR(100) NOT NULL,
        event_type  VARCHAR(60)  NOT NULL,
        payload     NVARCHAR(MAX) NULL,
        received_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
    CREATE UNIQUE INDEX uq_webhook_event_id ON webhook_events(event_id);
END
GO

-- One row per user: the three live limit windows.
--   session: 5h window, opened by the first metered call after the previous one expired
--   day:     24h window, same rule
--   month:   calendar month (query count, used by Free)
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='usage_counters' AND xtype='U')
BEGIN
    CREATE TABLE usage_counters (
        user_id            INT NOT NULL PRIMARY KEY,
        session_started_at DATETIME2 NULL,
        session_tokens     BIGINT NOT NULL DEFAULT 0,
        day_started_at     DATETIME2 NULL,
        day_tokens         BIGINT NOT NULL DEFAULT 0,
        month_start        DATE NULL,
        month_queries      INT NOT NULL DEFAULT 0,
        updated_at         DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT fk_uc_user FOREIGN KEY (user_id) REFERENCES auth(id) ON DELETE CASCADE
    );
END
GO

-- Append-only ledger: audit trail and future analytics/billing disputes.
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='usage_events' AND xtype='U')
BEGIN
    CREATE TABLE usage_events (
        id            BIGINT IDENTITY(1,1) PRIMARY KEY,
        user_id       INT NOT NULL,
        kind          VARCHAR(30) NOT NULL,         -- transform | merge | dashboard | chart | knowledgebase ...
        input_tokens  BIGINT NOT NULL DEFAULT 0,
        output_tokens BIGINT NOT NULL DEFAULT 0,
        total_tokens  BIGINT NOT NULL DEFAULT 0,
        llm_calls     INT NOT NULL DEFAULT 0,
        counted_query BIT NOT NULL DEFAULT 0,
        trace_id      VARCHAR(64) NULL,
        created_at    DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT fk_ue_user FOREIGN KEY (user_id) REFERENCES auth(id) ON DELETE CASCADE
    );
    CREATE INDEX ix_ue_user_time ON usage_events(user_id, created_at);
END
GO

-- Seed catalog. ASSUMPTIONS: Maxx session cap 750k; Individual "additional features" TBD.
-- max_data_sources = how many client-owned DB connections a plan may add (NULL = unlimited):
--   Free 2, Individual 10, Maxx 25, Enterprise unlimited.
IF NOT EXISTS (SELECT 1 FROM plans WHERE code='free')
    INSERT INTO plans (code,name,price,currency,per_seat,min_seats,max_files,daily_token_limit,session_token_limit,monthly_query_limit,retention_days,max_data_sources,features)
    VALUES ('free','Free',0,'USD',0,1,5,NULL,NULL,50,90,2,'{}');
IF NOT EXISTS (SELECT 1 FROM plans WHERE code='individual')
    INSERT INTO plans (code,name,price,currency,per_seat,min_seats,max_files,daily_token_limit,session_token_limit,monthly_query_limit,retention_days,max_data_sources,features)
    VALUES ('individual','Individual',25.00,'USD',1,1,30,1000000,250000,NULL,365,10,'{}');
IF NOT EXISTS (SELECT 1 FROM plans WHERE code='maxx')
    INSERT INTO plans (code,name,price,currency,per_seat,min_seats,max_files,daily_token_limit,session_token_limit,monthly_query_limit,retention_days,max_data_sources,features)
    VALUES ('maxx','Maxx',49.99,'USD',1,1,60,3000000,750000,NULL,NULL,25,'{"analysis":true,"storytelling":true}');
IF NOT EXISTS (SELECT 1 FROM plans WHERE code='enterprise')
    INSERT INTO plans (code,name,price,currency,per_seat,min_seats,max_files,daily_token_limit,session_token_limit,monthly_query_limit,retention_days,max_data_sources,features)
    VALUES ('enterprise','Enterprise',40.00,'USD',1,5,75,NULL,NULL,NULL,NULL,NULL,
            '{"analysis":true,"storytelling":true,"self_db":true,"custom_llm":true,"business_context":true,"priority_support":true}');
GO

-- Backfill the data-source limits on databases seeded before this column had tiers.
UPDATE plans SET max_data_sources = 2    WHERE code = 'free';
UPDATE plans SET max_data_sources = 10   WHERE code = 'individual';
UPDATE plans SET max_data_sources = 25   WHERE code = 'maxx';
UPDATE plans SET max_data_sources = NULL WHERE code = 'enterprise';
GO

-- ═══════════════════ TESTING PHASE: every new account gets Maxx ═══════════════════
-- App is in testing only, so no pricing yet. Every new row in `auth` automatically
-- receives a free Maxx subscription flagged is_test_grant = 1.
--
-- WHEN LAUNCHING PRICING (run manually):
--   1. DROP TRIGGER trg_auth_assign_maxx_testing;
--   2. UPDATE subscriptions
--         SET status = 'expired', current_period_end = SYSUTCDATETIME(), updated_at = SYSUTCDATETIME()
--       WHERE is_test_grant = 1 AND status = 'active';
--   Users then fall back to Free. Make sure any Razorpay sync job skips is_test_grant = 1.

IF OBJECT_ID('trg_auth_assign_maxx_testing', 'TR') IS NOT NULL
    DROP TRIGGER trg_auth_assign_maxx_testing;
GO

CREATE TRIGGER trg_auth_assign_maxx_testing
ON auth
AFTER INSERT
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @maxx_plan_id INT = (SELECT id FROM plans WHERE code = 'maxx');
    IF @maxx_plan_id IS NULL RETURN;

    INSERT INTO subscriptions (
        owner_user_id, org_id, plan_id, seats, status,
        razorpay_subscription_id, razorpay_customer_id,
        current_period_start, current_period_end,
        cancel_at_period_end, is_test_grant
    )
    SELECT
        i.id, NULL, @maxx_plan_id, 1, 'active',
        CONCAT('TEST-', i.id), NULL,
        SYSUTCDATETIME(), DATEADD(YEAR, 10, SYSUTCDATETIME()),  -- effectively non-expiring during testing
        0, 1
    FROM inserted i
    WHERE NOT EXISTS (
        SELECT 1 FROM subscriptions s WHERE s.owner_user_id = i.id
    );
END
GO

-- Backfill: give Maxx to existing users who have no subscription yet
INSERT INTO subscriptions (
    owner_user_id, org_id, plan_id, seats, status,
    razorpay_subscription_id, current_period_start, current_period_end,
    cancel_at_period_end, is_test_grant
)
SELECT
    a.id, NULL, p.id, 1, 'active',
    CONCAT('TEST-', a.id), SYSUTCDATETIME(), DATEADD(YEAR, 10, SYSUTCDATETIME()),
    0, 1
FROM auth a
CROSS JOIN (SELECT id FROM plans WHERE code = 'maxx') p
WHERE a.is_deleted = 0
  AND NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.owner_user_id = a.id);
GO