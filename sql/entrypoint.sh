#!/bin/bash
# Start SQL Server in the background
/opt/mssql/bin/sqlservr &
SQL_PID=$!

echo "Waiting for SQL Server to start..."
until /opt/mssql-tools18/bin/sqlcmd \
    -S localhost -U SA -P "$MSSQL_SA_PASSWORD" \
    -C -Q "SELECT 1" &>/dev/null; do
    sleep 2
done

echo "SQL Server is up — running init script..."
/opt/mssql-tools18/bin/sqlcmd \
    -S localhost -U SA -P "$MSSQL_SA_PASSWORD" \
    -C -i /init-scripts/init.sql

echo "Init script complete."

# Hand off to the SQL Server process
wait $SQL_PID