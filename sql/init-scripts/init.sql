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

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='table_info' AND xtype='U')
BEGIN
    CREATE TABLE table_info (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        name          VARCHAR(100) NOT NULL,
        user_id       INT NULL,
        knowledgebase NVARCHAR(MAX) NULL,
        metadata      NVARCHAR(MAX) NULL,
        createdAt     DATETIME     NULL,
        is_deleted    BIT NOT NULL DEFAULT 0,
        deleted_at    DATETIME2 NULL,
        CONSTRAINT fk_tableinfo_user FOREIGN KEY (user_id)
            REFERENCES auth(id) ON DELETE CASCADE
    );
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='queries' AND xtype='U')
BEGIN
    CREATE TABLE queries (
        id              INT IDENTITY(1,1) PRIMARY KEY,
        prompt          NVARCHAR(MAX) NOT NULL,
        sql_query       NVARCHAR(MAX) NOT NULL,
        summary         NVARCHAR(MAX) NOT NULL,
        updated_columns NVARCHAR(MAX) NOT NULL,
        step_type       VARCHAR(20) NOT NULL
            CONSTRAINT df_queries_step_type DEFAULT 'transform'
            CONSTRAINT chk_queries_step_type CHECK (step_type IN ('transform', 'join')),
        is_deleted      BIT NOT NULL DEFAULT 0,
        deleted_at      DATETIME2 NULL,
        created_at      DATETIME2 NOT NULL DEFAULT SYSDATETIME()
    );
END
GO

-- If queries already existed without step_type (pre-migration deployments),
-- add it on now. No-op on a fresh install since the CREATE TABLE above
-- already includes it.
IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.queries') AND name = 'step_type'
)
BEGIN
    ALTER TABLE queries
        ADD step_type VARCHAR(20) NOT NULL
            CONSTRAINT df_queries_step_type DEFAULT 'transform'
            CONSTRAINT chk_queries_step_type CHECK (step_type IN ('transform', 'join'));
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='query_tables' AND xtype='U')
BEGIN
    CREATE TABLE query_tables (
        query_id INT NOT NULL,
        table_id INT NOT NULL,
        CONSTRAINT pk_query_tables PRIMARY KEY (query_id, table_id),
        CONSTRAINT fk_qt_query FOREIGN KEY (query_id)
            REFERENCES queries(id)     ON DELETE CASCADE,
        CONSTRAINT fk_qt_table FOREIGN KEY (table_id)
            REFERENCES table_info(id)  ON DELETE NO ACTION
    );
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='sessions' AND xtype='U')
BEGIN
    CREATE TABLE sessions (
        session_id   INT IDENTITY(1,1) PRIMARY KEY,
        user_id      INT NULL,
        type         VARCHAR(20) NOT NULL
            CONSTRAINT chk_session_type CHECK (type IN ('Dashboard', 'Transformation')),
        date_created DATETIME2 DEFAULT SYSDATETIME(),
        is_deleted   BIT NOT NULL DEFAULT 0,
        deleted_at   DATETIME2 NULL,
        CONSTRAINT fk_sessions_user FOREIGN KEY (user_id)
            REFERENCES auth(id) ON DELETE CASCADE
    );
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='session_queries' AND xtype='U')
BEGIN
    CREATE TABLE session_queries (
        session_id    INT      NOT NULL,
        query_id      INT      NOT NULL,
        date_created  DATETIME2 DEFAULT SYSDATETIME(),
        date_modified DATETIME2 DEFAULT SYSDATETIME(),
        CONSTRAINT pk_session_queries PRIMARY KEY (session_id, query_id),
        CONSTRAINT fk_sq_session FOREIGN KEY (session_id)
            REFERENCES sessions(session_id) ON DELETE CASCADE,
        CONSTRAINT fk_sq_query  FOREIGN KEY (query_id)
            REFERENCES queries(id)          ON DELETE NO ACTION
    );
END
GO

-- ---------------------------------------------------------------------
-- query_merges — one row per join/merge step. Points at the `queries`
-- row that IS the join step (lives in the target/current session),
-- and records exactly which session/query/table it merged in, frozen
-- at merge time. The source session is untouched and stays fully
-- independent/usable afterward.
-- ---------------------------------------------------------------------
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='query_merges' AND xtype='U')
BEGIN
    CREATE TABLE query_merges (
        id                  INT IDENTITY(1,1) PRIMARY KEY,
        query_id            INT NOT NULL,        -- the join step itself (queries.id, target session)
        source_session_id   INT NOT NULL,        -- session merged FROM
        source_query_id     INT NULL,            -- last step of source session at merge time (NULL if source session had no steps yet, i.e. raw table)
        source_table_id     INT NOT NULL,        -- source session's root table_info id (for display/naming)
        join_type           VARCHAR(10) NOT NULL
            CONSTRAINT chk_query_merges_join_type CHECK (join_type IN ('INNER', 'LEFT', 'RIGHT', 'FULL')),
        join_summary        NVARCHAR(MAX) NULL, -- human-readable e.g. "Joined with Region.csv on region_id"
        created_at          DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT fk_qm_query FOREIGN KEY (query_id)
            REFERENCES queries(id) ON DELETE CASCADE,
        CONSTRAINT fk_qm_source_session FOREIGN KEY (source_session_id)
            REFERENCES sessions(session_id) ON DELETE NO ACTION,
        CONSTRAINT fk_qm_source_query FOREIGN KEY (source_query_id)
            REFERENCES queries(id) ON DELETE NO ACTION,
        CONSTRAINT fk_qm_source_table FOREIGN KEY (source_table_id)
            REFERENCES table_info(id) ON DELETE NO ACTION
    );
    CREATE UNIQUE INDEX uq_query_merges_query_id ON query_merges(query_id);
    CREATE INDEX ix_query_merges_source_session ON query_merges(source_session_id);
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='charts' AND xtype='U')
BEGIN
    CREATE TABLE charts (
        chart_id   INT IDENTITY(1,1) PRIMARY KEY,
        chart_type VARCHAR(50)   NOT NULL,
        prompt     NVARCHAR(MAX) NOT NULL,
        sql_query  NVARCHAR(MAX) NOT NULL,
        metadata   NVARCHAR(MAX) NULL,
        is_deleted BIT NOT NULL DEFAULT 0,
        deleted_at DATETIME2 NULL,
        created_at DATETIME2 NOT NULL DEFAULT SYSDATETIME()
    );
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='dashboards' AND xtype='U')
BEGIN
    CREATE TABLE dashboards (
        dashboard_id     INT IDENTITY(1,1) PRIMARY KEY,
        user_id          INT NULL,
        table_id         INT NOT NULL,
        dashboard_intent NVARCHAR(MAX) NULL,
        user_response    NVARCHAR(MAX) NULL,
        status           VARCHAR(10) NOT NULL DEFAULT 'Inactive'
            CONSTRAINT chk_dashboard_status CHECK (status IN ('Live', 'Inactive')),
        breath_time      INT         NOT NULL DEFAULT 5,
        is_deleted       BIT NOT NULL DEFAULT 0,
        deleted_at       DATETIME2 NULL,
        created_at       DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT fk_dashboards_user FOREIGN KEY (user_id)
            REFERENCES auth(id) ON DELETE CASCADE,
        CONSTRAINT fk_dashboards_table FOREIGN KEY (table_id)
            REFERENCES table_info(id)  ON DELETE NO ACTION
    );
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
        CONSTRAINT fk_dc_chart     FOREIGN KEY (chart_id)
            REFERENCES charts(chart_id)         ON DELETE CASCADE
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
        id            INT IDENTITY(1,1) PRIMARY KEY,
        email_id      VARCHAR(255) NOT NULL,
        otp_no        VARCHAR(10) NOT NULL,
        usageCode     VARCHAR(30) NOT NULL,
        insertedTime  DATETIME2 NOT NULL DEFAULT SYSDATETIME()
    );
    CREATE INDEX ix_otp_email_usage ON OTP(email_id, usageCode);
    CREATE INDEX ix_otp_insertedTime ON OTP(insertedTime);
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='plans' AND xtype='U')
BEGIN
    CREATE TABLE plans (
        id                     INT IDENTITY(1,1) PRIMARY KEY,
        name                   VARCHAR(100) NOT NULL,
        description            NVARCHAR(500) NULL,
        price                  DECIMAL(10,2) NOT NULL,
        currency               VARCHAR(10) NOT NULL DEFAULT 'INR',
        billing_interval       VARCHAR(20) NOT NULL
            CONSTRAINT chk_plan_interval CHECK (billing_interval IN ('monthly', 'yearly', 'one_time')),
        max_tables             INT NULL,
        max_queries_per_month  INT NULL,
        razorpay_plan_id       VARCHAR(100) NULL,
        is_active              BIT NOT NULL DEFAULT 1,
        created_at             DATETIME2 NOT NULL DEFAULT SYSDATETIME()
    );
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='subscriptions' AND xtype='U')
BEGIN
    CREATE TABLE subscriptions (
        id                        INT IDENTITY(1,1) PRIMARY KEY,
        user_id                   INT NOT NULL,
        plan_id                   INT NOT NULL,
        status                    VARCHAR(20) NOT NULL DEFAULT 'created'
            CONSTRAINT chk_sub_status CHECK (status IN
                ('created', 'active', 'trialing', 'past_due', 'cancelled', 'expired', 'halted')),
        razorpay_subscription_id  VARCHAR(100) NULL,
        razorpay_customer_id      VARCHAR(100) NULL,
        current_period_start      DATETIME2 NULL,
        current_period_end        DATETIME2 NULL,
        cancel_at_period_end      BIT NOT NULL DEFAULT 0,
        cancelled_at              DATETIME2 NULL,
        created_at                DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
        is_deleted                BIT NOT NULL DEFAULT 0,
        deleted_at                DATETIME2 NULL,
        CONSTRAINT fk_subscriptions_user FOREIGN KEY (user_id)
            REFERENCES auth(id) ON DELETE CASCADE,
        CONSTRAINT fk_subscriptions_plan FOREIGN KEY (plan_id)
            REFERENCES plans(id) ON DELETE NO ACTION
    );
    CREATE INDEX ix_subscriptions_user ON subscriptions(user_id);
    CREATE INDEX ix_subscriptions_status ON subscriptions(status);
    CREATE UNIQUE INDEX uq_subscriptions_razorpay_id ON subscriptions(razorpay_subscription_id)
        WHERE razorpay_subscription_id IS NOT NULL;
END
GO

IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='payments' AND xtype='U')
BEGIN
    CREATE TABLE payments (
        id                   INT IDENTITY(1,1) PRIMARY KEY,
        user_id              INT NOT NULL,
        subscription_id      INT NULL,
        razorpay_order_id    VARCHAR(100) NULL,
        razorpay_payment_id  VARCHAR(100) NULL,
        razorpay_signature   VARCHAR(255) NULL,
        amount               DECIMAL(10,2) NOT NULL,
        currency             VARCHAR(10) NOT NULL DEFAULT 'INR',
        status               VARCHAR(20) NOT NULL DEFAULT 'created'
            CONSTRAINT chk_payment_status CHECK (status IN
                ('created', 'authorized', 'captured', 'failed', 'refunded')),
        method               VARCHAR(30) NULL,
        created_at           DATETIME2 NOT NULL DEFAULT SYSDATETIME(),
        CONSTRAINT fk_payments_user FOREIGN KEY (user_id)
            REFERENCES auth(id) ON DELETE CASCADE,
        CONSTRAINT fk_payments_subscription FOREIGN KEY (subscription_id)
            REFERENCES subscriptions(id) ON DELETE NO ACTION
    );
    CREATE INDEX ix_payments_user ON payments(user_id);
    CREATE INDEX ix_payments_subscription ON payments(subscription_id);
    CREATE UNIQUE INDEX uq_payments_razorpay_payment_id ON payments(razorpay_payment_id)
        WHERE razorpay_payment_id IS NOT NULL;
END
GO

IF NOT EXISTS (SELECT 1 FROM plans WHERE name = 'Free')
BEGIN
    INSERT INTO plans (name, description, price, currency, billing_interval, max_tables, max_queries_per_month)
    VALUES ('Free', 'Default free tier for new signups', 0.00, 'INR', 'monthly', 3, 50);
END
GO