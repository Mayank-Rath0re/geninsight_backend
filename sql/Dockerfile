FROM mcr.microsoft.com/mssql/server:2022-latest

ENV ACCEPT_EULA=Y
ENV MSSQL_SA_PASSWORD=YourStrong@Passw0rd
ENV MSSQL_DB=genbi_db

USER root

# Copy init scripts and entrypoint
COPY init-scripts/init.sql /init-scripts/init.sql
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER mssql

EXPOSE 1433

ENTRYPOINT ["/entrypoint.sh"]