-- init.sql
-- ============================================================
-- Create the Database
-- ============================================================
IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = 'genbi_db')
BEGIN
    CREATE DATABASE genbi_db;
END
GO

USE genbi_db;
GO

-- ============================================================
-- 1. Auth Table (Users)
-- ============================================================
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='auth' AND xtype='U')
BEGIN
    CREATE TABLE auth (
        id       INT IDENTITY(1,1) PRIMARY KEY,
        email    VARCHAR(255) NOT NULL,
        name     VARCHAR(100) NOT NULL,
        password VARCHAR(255) NOT NULL  -- Sized to fit secure hashes (bcrypt/Argon2)
    );
END
GO

-- For Testing purposes -> To be removed post development
IF NOT EXISTS (SELECT 1 FROM auth WHERE email = 'mayank.rathore@isourse.com')
    INSERT INTO auth VALUES ('mayank.rathore@isourse.com', 'Mayank Rathore', 'Isourse@123');

IF NOT EXISTS (SELECT 1 FROM auth WHERE email = 'saransh.sharma@isourse.com')
    INSERT INTO auth VALUES ('saransh.sharma@isourse.com', 'Saransh Sharma',  'Isourse@123');
GO

-- ============================================================
-- 2. Table Info Table
-- ============================================================
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='table_info' AND xtype='U')
BEGIN
    CREATE TABLE table_info (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        name          VARCHAR(100) NOT NULL,
        user_id        INT NULL,
        knowledgebase NVARCHAR(MAX) NULL,
        metadata      NVARCHAR(MAX) NULL,   -- JSON stored as NVARCHAR(MAX); use ISJSON() to validate
        createdAt     DATETIME     NULL,
        CONSTRAINT fk_tableinfo_user FOREIGN KEY (user_id)
            REFERENCES auth(id) ON DELETE CASCADE
    );
END
GO

-- ============================================================
-- 3. Queries Table
-- ============================================================
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='queries' AND xtype='U')
BEGIN
    CREATE TABLE queries (
        id        INT IDENTITY(1,1) PRIMARY KEY,
        prompt    NVARCHAR(MAX) NOT NULL,   -- TEXT equivalent for long AI prompts
        sql_query NVARCHAR(MAX) NOT NULL,    -- TEXT equivalent for complex SQL statements
        summary   NVARCHAR(MAX) NOT NULL,   -- TEXT equivalent for query changes
        updated_columns NVARCHAR(MAX) NOT NULL -- Updated Columns in Table after Query
    );
END
GO

-- ============================================================
-- 4. Query-Table Junction Table (Many-to-Many)
-- ============================================================
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='query_tables' AND xtype='U') 
BEGIN 
    CREATE TABLE query_tables (
        query_id INT NOT NULL,
        table_id INT NOT NULL,
        CONSTRAINT pk_query_tables PRIMARY KEY (query_id, table_id),
        CONSTRAINT fk_qt_query FOREIGN KEY (query_id)
            REFERENCES queries(id)     ON DELETE CASCADE,
        CONSTRAINT fk_qt_table FOREIGN KEY (table_id)
            REFERENCES table_info(id)  ON DELETE NO ACTION  -- Avoids multiple cascade paths
    );
END 
GO 

-- ============================================================
-- 5. Sessions Table
-- ============================================================
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='sessions' AND xtype='U')
BEGIN
    CREATE TABLE sessions (
        session_id   INT IDENTITY(1,1) PRIMARY KEY,
        user_id      INT NULL,
        type         VARCHAR(20) NOT NULL               -- 'Dashboard' | 'Transformation'
            CONSTRAINT chk_session_type CHECK (type IN ('Dashboard', 'Transformation')),
        date_created DATETIME2 DEFAULT SYSDATETIME(),   -- TIMESTAMP equivalent
        CONSTRAINT fk_sessions_user FOREIGN KEY (user_id)
            REFERENCES auth(id) ON DELETE CASCADE
    );
END
GO

-- ============================================================
-- 6. Session-Queries Junction Table (Many-to-Many)
-- ============================================================
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='session_queries' AND xtype='U')
BEGIN
    CREATE TABLE session_queries (
        session_id    INT      NOT NULL,
        query_id      INT      NOT NULL,
        date_created  DATETIME2 DEFAULT SYSDATETIME(),
        date_modified DATETIME2 DEFAULT SYSDATETIME(),  -- Update via application layer or trigger
        CONSTRAINT pk_session_queries PRIMARY KEY (session_id, query_id),
        CONSTRAINT fk_sq_session FOREIGN KEY (session_id)
            REFERENCES sessions(session_id) ON DELETE CASCADE,
        CONSTRAINT fk_sq_query  FOREIGN KEY (query_id)
            REFERENCES queries(id)          ON DELETE NO ACTION  -- Avoids multiple cascade paths
    );
END
GO

-- ============================================================
-- 7. Charts Table
-- ============================================================
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='charts' AND xtype='U')
BEGIN
    CREATE TABLE charts (
        chart_id   INT IDENTITY(1,1) PRIMARY KEY,
        chart_type VARCHAR(50)   NOT NULL,
        prompt     NVARCHAR(MAX) NOT NULL,
        sql_query  NVARCHAR(MAX) NOT NULL,
        metadata   NVARCHAR(MAX) NULL    -- JSON stored as NVARCHAR(MAX)
    );
END
GO

-- ============================================================
-- 8. Dashboards Table
-- ============================================================
IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='dashboards' AND xtype='U')
BEGIN
    CREATE TABLE dashboards (
        dashboard_id INT IDENTITY(1,1) PRIMARY KEY,
        user_id  INT NULL, 
        table_id INT NOT NULL, 
        dashboard_intent NVARCHAR(MAX) NULL, 
        user_response NVARCHAR(MAX) NULL, 
        status       VARCHAR(10) NOT NULL DEFAULT 'Inactive'
            CONSTRAINT chk_dashboard_status CHECK (status IN ('Live', 'Inactive')),
        breath_time  INT         NOT NULL DEFAULT 5,    -- Time in minutes
        CONSTRAINT fk_dashboards_user FOREIGN KEY (user_id)
            REFERENCES auth(id) ON DELETE CASCADE, 
        CONSTRAINT fk_dashboards_table FOREIGN KEY (table_id)
            REFERENCES table_info(id)  ON DELETE NO ACTION  
    ); 
END 
GO 
-- ============================================================
-- 9. Dashboard-Charts Junction Table (Many-to-Many)
-- ============================================================
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